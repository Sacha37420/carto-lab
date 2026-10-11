"""Météo-France : la clé de l'utilisateur, la tâche asynchrone, le client HTTP,
le CSV quotidien, la qualité, les indicateurs et la classification.

La clé Météo-France appartient à l'utilisateur. Elle arrive dans l'en-tête
``X-Meteo-Key``, attend dans Redis sous un jeton opaque à durée limitée, est
consommée une fois par le worker — et n'est jamais en base, ni dans les
arguments de la tâche, ni dans un message.
"""
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import numpy as np
import requests
from django.test import SimpleTestCase

from api import choropleth, indicators, meteo_client, secret_store, tasks
from api.meteo_client import (
    MeteoClient, MeteoError, MeteoQuotaError, _parse_retry_after, combine_period,
    parse_daily_csv, station_lonlat,
)
from api.models import Job, Layer

from .helpers import ALICE, LabTestCase, Reponse

CLE = 'Y2xpZW50OnNlY3JldC1tZXRlbw=='          # « identifiant applicatif » de l'utilisateur
CORPS = {'grandeur': 'temperature', 'year_start': 2023, 'indicator': 'mean'}


class LancementTests(LabTestCase):

    def setUp(self):
        super().setUp()
        self.delay = self._patch(mock.patch('api.views.build_meteo_choropleth.delay'))

    def lancer(self, corps=None, cle=CLE, client=None):
        entetes = {'HTTP_X_METEO_KEY': cle} if cle is not None else {}
        return (client or self.alice).post('/api/meteo/jobs/', CORPS if corps is None else corps,
                                           format='json', **entetes)

    def test_sans_cle_rien_n_est_lance(self):
        for cle in (None, '', '   '):
            with self.subTest(cle=cle):
                self.assertEqual(self.lancer(cle=cle).status_code, 400)
        # Une clé glissée dans le corps n'est pas acceptée à la place de l'en-tête.
        self.assertEqual(self.lancer({**CORPS, 'api_key': CLE, 'X-Meteo-Key': CLE}, cle=None)
                         .status_code, 400)
        self.assertEqual(Job.objects.count(), 0)
        self.delay.assert_not_called()
        self.assertEqual(self.redis.data, {})

    def test_demande_invalide(self):
        cas = [
            {**CORPS, 'grandeur': 'neige'},
            {**CORPS, 'indicator': 'mediane'},
            {**CORPS, 'year_start': 'deux mille'},
            {'grandeur': 'temperature', 'indicator': 'mean'},
            {**CORPS, 'year_end': 2022},                      # fin avant début
            {**CORPS, 'year_end': 'x'},
        ]
        for corps in cas:
            with self.subTest(corps=corps):
                self.assertEqual(self.lancer(corps).status_code, 400)
        self.assertEqual(Job.objects.count(), 0)
        self.delay.assert_not_called()
        self.assertEqual(self.redis.data, {})                 # la clé n'a même pas été déposée

    def test_la_cle_ne_va_ni_en_base_ni_dans_la_tache_ni_dans_la_reponse(self):
        resp = self.lancer({**CORPS, 'year_end': 2024, 'max_stations': 3,
                            'quality_thresholds': {'min_completeness': 0.8}})
        self.assertEqual(resp.status_code, 202, resp.content)
        job = Job.objects.get()
        self.assertEqual((job.status, job.kind, job.owner_email), ('PENDING', 'meteofrance', ALICE))
        self.assertEqual(job.params, {
            'grandeur': 'temperature', 'year_start': 2023, 'year_end': 2024, 'indicator': 'mean',
            'indicator_params': {}, 'classification': 'quantiles', 'n_classes': 5,
            'ramp': 'YlOrRd', 'max_stations': 3,
            'quality_thresholds': {'min_completeness': 0.8}})
        # La tâche reçoit un JETON opaque, jamais la clé.
        (job_id, jeton, params), kwargs = self.delay.call_args
        self.assertEqual((job_id, params, kwargs), (job.pk, job.params, {}))
        self.assertNotEqual(jeton, CLE)
        self.assertGreaterEqual(len(jeton), 24)
        # La clé attend dans Redis, sous ce jeton, pour une durée bornée.
        self.assertEqual(self.redis.data, {f'mfkey:{jeton}': CLE.encode()})
        self.assertEqual(self.redis.ttl[f'mfkey:{jeton}'], 3600)
        for texte in (resp.content.decode(), str(job.params), job.message,
                      self.alice.get('/api/jobs/').content.decode(),
                      self.alice.get(f'/api/jobs/{job.pk}/').content.decode()):
            self.assertNotIn(CLE, texte)
            self.assertNotIn(jeton, texte)

    def test_annee_unique_par_defaut(self):
        self.lancer()
        self.assertEqual(Job.objects.get().params['year_end'], 2023)

    def test_la_cle_se_consomme_une_seule_fois(self):
        jeton = secret_store.put('ma-cle', ttl=60)
        self.assertEqual(secret_store.take(jeton), 'ma-cle')
        self.assertIsNone(secret_store.take(jeton))
        self.assertIsNone(secret_store.take('jeton-inconnu'))
        self.assertEqual(self.redis.data, {})
        self.assertNotEqual(secret_store.put('a'), secret_store.put('a'))

    def test_suivi_des_taches(self):
        self.lancer()
        job = Job.objects.get()
        # Carte commune : un autre membre suit la tâche (sans la clé, cf. plus haut).
        self.assertEqual(self.bob.get(f'/api/jobs/{job.pk}/').json()['status'], 'PENDING')
        self.assertEqual(self.bob.get('/api/jobs/999999/').status_code, 404)
        self.assertEqual(self.bob.post('/api/jobs/', {}, format='json').status_code, 405)
        self.assertEqual(self.bob.delete(f'/api/jobs/{job.pk}/').status_code, 405)

    def test_options(self):
        options = self.alice.get('/api/meteo/options/').json()
        self.assertEqual({g['key'] for g in options['grandeurs']}, set(indicators.GRANDEURS))
        self.assertEqual({i['name'] for i in options['indicators']}, set(indicators.INDICATORS))
        self.assertEqual(options['classifications'], ['quantiles', 'equal', 'jenks'])
        self.assertNotIn('func', options['indicators'][0])


# ── Faux DPClim ───────────────────────────────────────────────────────────────

def csv_annee(annee: int, tm, jours: int = 365, debut: int = 1) -> str:
    """CSV quotidien d'une station : `jours` relevés consécutifs à partir du jour `debut`."""
    lignes = ['POSTE;DATE;RR;TN;TX;TM']
    premier = date(annee, 1, 1) + timedelta(days=debut - 1)
    for i in range(jours):
        d = premier + timedelta(days=i)
        if d.year != annee:
            break
        valeur = str(tm).replace('.', ',')
        lignes.append(f'1;{d:%Y%m%d};0,0;{valeur};{valeur};{valeur}')
    return '\n'.join(lignes) + '\n'


STATIONS = {
    '37': [
        {'id': '37001', 'nom': 'TOURS', 'lat': 47.39, 'lon': 0.69, 'posteOuvert': True},
        {'id': '37002', 'nom': 'FERMEE', 'lat': 47.0, 'lon': 0.5, 'posteOuvert': False},
        {'id': '37003', 'nom': 'SANS COORDONNEES', 'posteOuvert': True},
    ],
    '75': [
        {'id': '75001', 'nom': 'PARIS', 'lat': 48.85, 'lon': 2.35, 'posteOuvert': True},
        {'id': '37001', 'nom': 'TOURS (doublon)', 'lat': 47.39, 'lon': 0.69, 'posteOuvert': True},
    ],
    '69': [{'id': '69001', 'nom': 'LYON', 'lat': 45.76, 'lon': 4.83, 'posteOuvert': True}],
    '33': [{'id': '33001', 'nom': 'BORDEAUX', 'lat': 44.84, 'lon': -0.58, 'posteOuvert': True}],
}
TEMPERATURES = {'37001': 10.0, '75001': 12.0, '69001': 14.0, '33001': 16.0}


class FauxDPClim:
    """Tient lieu de MeteoClient : mêmes méthodes, réponses écrites ici."""

    instances = []

    def __init__(self, cle):
        self.cle = cle
        self.commandes: list[tuple] = []
        self.authentifie = False
        self.incomplete: set[str] = set()       # stations à 10 jours de relevés
        self.en_panne: set[str] = set()         # stations dont la commande échoue
        self.quota_a_partir_de: int | None = None
        FauxDPClim.instances.append(self)

    def authenticate(self):
        self.authentifie = True

    def list_stations(self, departement, parametre):
        self.parametre = parametre
        if departement not in STATIONS:
            raise MeteoError(f'liste-stations {departement} → HTTP 404.')
        return STATIONS[departement]

    def order_daily(self, id_station, date_deb, date_fin):
        if self.quota_a_partir_de is not None and len(self.commandes) >= self.quota_a_partir_de:
            raise MeteoQuotaError('Quota Météo-France dépassé (HTTP 429).')
        self.commandes.append((id_station, date_deb, date_fin))
        if id_station in self.en_panne:
            raise MeteoError(f'commande-station {id_station} → HTTP 404.')
        return f'{id_station}:{date_deb[:4]}'

    def fetch_file(self, id_cmde):
        sid, annee = id_cmde.split(':')
        jours = 10 if sid in self.incomplete else 366
        return csv_annee(int(annee), TEMPERATURES[sid], jours=jours)


class TacheTests(LabTestCase):
    """build_meteo_choropleth de bout en bout : stations → indicateur → Voronoï
    → découpage France → classes, sur une vraie base PostGIS."""

    def setUp(self):
        super().setUp()
        FauxDPClim.instances = []
        self._patch(mock.patch.object(tasks, 'MeteoClient', FauxDPClim))

    def executer(self, prepare=None, cle=CLE, **params):
        params = {'grandeur': 'temperature', 'year_start': 2023, 'year_end': 2023,
                  'indicator': 'mean', 'departements': ['37', '75', '69', '33', '99'], **params}
        job = Job.objects.create(kind='meteofrance', params=params, owner_email=ALICE)
        jeton = secret_store.put(cle) if cle is not None else 'jeton-expire'
        if prepare:
            original = FauxDPClim.__init__

            def init(client, cle_recue):
                original(client, cle_recue)
                prepare(client)
            with mock.patch.object(FauxDPClim, '__init__', init):
                tasks.build_meteo_choropleth.apply(args=[job.pk, jeton, params])
        else:
            tasks.build_meteo_choropleth.apply(args=[job.pk, jeton, params])
        job.refresh_from_db()
        return job

    def test_carte_complete(self):
        job = self.executer()
        self.assertEqual((job.status, job.progress), ('DONE', 100), job.message)
        client = FauxDPClim.instances[0]
        self.assertEqual((client.cle, client.authentifie, client.parametre),
                         (CLE, True, 'temperature'))
        self.assertEqual(self.redis.data, {})                         # clé consommée
        # Stations : ouvertes, localisées, sans doublon ; une commande par station et par an.
        self.assertEqual(sorted(c[0] for c in client.commandes),
                         ['33001', '37001', '69001', '75001'])
        self.assertEqual(client.commandes[0][1:], ('2023-01-01T00:00:00Z', '2023-12-31T23:00:00Z'))

        carte = job.result_layer
        self.assertEqual(carte.name, 'Température 2023 — Moyenne')
        self.assertEqual((carte.origin, carte.owner_email, carte.feature_count),
                         ('calcul', ALICE, 4))
        valeurs = sorted(f.properties['valeur'] for f in carte.features.all())
        self.assertEqual(valeurs, [10.0, 12.0, 14.0, 16.0])
        # Découpée sur la France : aucune cellule ne déborde sur l'océan ou l'étranger.
        self.assertTrue(-5.5 < carte.bbox[0] and carte.bbox[2] < 10 and 41 < carte.bbox[1]
                        and carte.bbox[3] < 51.5, carte.bbox)
        choro = carte.metadata['choropleth']
        self.assertEqual(len(choro['breaks']) - 1, len(choro['colors']))
        self.assertEqual((choro['breaks'][0], choro['breaks'][-1]), (10.0, 16.0))
        classes = {f.properties['valeur']: f.properties['__class'] for f in carte.features.all()}
        self.assertLess(classes[10.0], classes[16.0])                  # plus chaud, classe plus haute
        for f in carte.features.all():
            self.assertEqual(f.properties['__color'], choro['colors'][f.properties['__class']])
        self.assertEqual((carte.metadata['grandeur'], carte.metadata['annee_debut']),
                         ('temperature', 2023))

        stations = Layer.objects.get(origin='meteofrance')
        self.assertEqual((stations.name, stations.feature_count, stations.geom_type),
                         ('Stations temperature 2023 — Moyenne', 4, 'Point'))
        tours = stations.features.get(properties__id_station='37001').properties
        self.assertEqual((tours['nom'], tours['valeur'], tours['qualite_taux_completude']),
                         ('TOURS', 10.0, 1.0))
        # Les couches intermédiaires (stations filtrées, Voronoï brut) sont supprimées.
        self.assertEqual(sorted(Layer.objects.values_list('name', flat=True)), sorted([
            'France métropolitaine (réf.)', stations.name, carte.name]))
        # La clé n'a laissé de trace nulle part.
        for texte in [job.message, str(job.params)] + [str(l.metadata) for l in Layer.objects.all()]:
            self.assertNotIn(CLE, texte)

    def test_periode_pluriannuelle(self):
        job = self.executer(year_start=2022, year_end=2023, departements=['37', '75', '69'])
        self.assertEqual(job.status, 'DONE', job.message)
        commandes = FauxDPClim.instances[0].commandes
        self.assertEqual(len(commandes), 3 * 2)                        # une requête par station ET par an
        self.assertEqual([c[1][:4] for c in commandes if c[0] == '37001'], ['2022', '2023'])
        self.assertEqual(job.result_layer.name, 'Température 2022–2023 — Moyenne')
        self.assertEqual((job.result_layer.metadata['annee_debut'],
                          job.result_layer.metadata['annee_fin']), (2022, 2023))
        tours = Layer.objects.get(origin='meteofrance').features.get(properties__id_station='37001')
        self.assertEqual(tours.properties['qualite_heures_annee'], 730 * 24)
        self.assertEqual(tours.properties['qualite_taux_completude'], 1.0)

    def test_station_en_echec_reste_sur_la_carte_des_stations(self):
        job = self.executer(lambda c: c.en_panne.add('69001'))
        self.assertEqual(job.status, 'DONE', job.message)
        self.assertEqual(job.result_layer.feature_count, 3)
        lyon = Layer.objects.get(origin='meteofrance').features.get(properties__id_station='69001')
        self.assertIsNone(lyon.properties['valeur'])                   # visible, sans valeur

    def test_seuil_de_qualite_ecarte_de_la_carte_pas_des_stations(self):
        job = self.executer(lambda c: c.incomplete.add('33001'),
                            quality_thresholds={'min_completeness': '0.9', 'max_gap_hours': ''})
        self.assertEqual(job.status, 'DONE', job.message)
        self.assertEqual(sorted(f.properties['valeur'] for f in job.result_layer.features.all()),
                         [10.0, 12.0, 14.0])
        stations = Layer.objects.get(origin='meteofrance')
        self.assertEqual(stations.feature_count, 4)
        bordeaux = stations.features.get(properties__id_station='33001').properties
        self.assertEqual(bordeaux['valeur'], 16.0)
        self.assertAlmostEqual(bordeaux['qualite_taux_completude'], 10 / 365, places=3)
        self.assertEqual(bordeaux['qualite_trou_max_heures'], 355 * 24)

    def test_seuil_illisible_ignore(self):
        job = self.executer(quality_thresholds={'min_completeness': 'beaucoup',
                                                'max_duplicates': 'x'})
        self.assertEqual((job.status, job.result_layer.feature_count), ('DONE', 4), job.message)

    def test_aucune_station_ne_passe_les_seuils(self):
        job = self.executer(quality_thresholds={'min_completeness': 2})
        self.assertEqual(job.status, 'ERROR')
        self.assertIn('seuils de qualité', job.message)
        self.assertIsNone(job.result_layer)
        self.assertEqual(Layer.objects.filter(origin='meteofrance').count(), 1)   # stations gardées

    def test_nombre_de_stations_plafonne(self):
        job = self.executer(max_stations=2)
        self.assertEqual(job.status, 'DONE', job.message)
        self.assertEqual(len(FauxDPClim.instances[0].commandes), 2)

    def test_quota_atteint_on_garde_ce_qui_est_calcule(self):
        job = self.executer(lambda c: setattr(c, 'quota_a_partir_de', 3))
        self.assertEqual(job.status, 'DONE', job.message)
        self.assertEqual(len(FauxDPClim.instances[0].commandes), 3)    # pas de mitraillage ensuite
        self.assertEqual(job.result_layer.feature_count, 3)

    def test_echecs(self):
        def refuse(client):
            client.authenticate = mock.Mock(side_effect=MeteoError('Identifiant refusé (HTTP 401).'))
        cas = {
            'clé expirée ou déjà consommée': (dict(cle=None), 'Clé API absente'),
            'identifiant refusé': (dict(prepare=refuse), 'Identifiant refusé'),
            'aucune station': (dict(departements=['99']), 'Aucune station'),
            'aucune donnée': (dict(prepare=lambda c: c.en_panne.update(TEMPERATURES)),
                              'Aucune donnée exploitable'),
            'grandeur inconnue': (dict(grandeur='neige'), 'Grandeur inconnue'),
            'indicateur inconnu': (dict(indicator='mediane'), 'Indicateur inconnu'),
        }
        for nom, (kwargs, message) in cas.items():
            with self.subTest(cas=nom):
                job = self.executer(**kwargs)
                self.assertEqual(job.status, 'ERROR')
                self.assertIn(message, job.message)
                self.assertNotIn(CLE, job.message)
                self.assertIsNone(job.result_layer)
                self.assertEqual(Layer.objects.count(), 0)
                self.assertEqual(self.redis.data, {})                  # clé toujours retirée

    def test_attente_sur_quota(self):
        job = mock.Mock()
        appels = mock.Mock(side_effect=[MeteoQuotaError('429', retry_after_seconds=40), 'ok'])
        self.assertEqual(tasks._with_quota_retry(appels, job, 'Station X'), 'ok')
        self.sleep.assert_called_once_with(41)
        self.assertIn('reprise dans 40s', job.set_state.call_args.kwargs['message'])
        # Attente trop longue ou inconnue : on abandonne tout de suite, sans dormir.
        self.sleep.reset_mock()
        for attente in (151, None):
            with self.subTest(attente=attente):
                with self.assertRaises(MeteoQuotaError):
                    tasks._with_quota_retry(
                        mock.Mock(side_effect=MeteoQuotaError('429', retry_after_seconds=attente)),
                        job, 'x')
        self.sleep.assert_not_called()
        # Jamais plus de trois essais.
        toujours = mock.Mock(side_effect=MeteoQuotaError('429', retry_after_seconds=1))
        with self.assertRaises(MeteoQuotaError):
            tasks._with_quota_retry(toujours, job, 'x')
        self.assertEqual(toujours.call_count, 3)


class ClientHttpTests(LabTestCase):
    """Le vrai MeteoClient, la couche HTTP seule remplacée."""

    def setUp(self):
        super().setUp()
        self.post = self._patch(mock.patch.object(
            meteo_client.requests, 'post',
            side_effect=lambda *a, **k: Reponse(200, {'access_token': f'jeton-{self.post.call_count}'})))
        self.client = MeteoClient(CLE)
        self.get = self._patch(mock.patch.object(self.client._session, 'get'))

    def test_l_identifiant_ne_sert_qu_a_obtenir_un_jeton(self):
        self.get.return_value = Reponse(200, [{'id': '1'}])
        self.assertEqual(self.client.list_stations('37', 'temperature'), [{'id': '1'}])
        (url,), kwargs = self.post.call_args
        self.assertEqual(url, 'http://meteo.test/token')
        self.assertEqual(kwargs['headers'], {'Authorization': f'Basic {CLE}'})
        (url,), kwargs = self.get.call_args
        self.assertEqual(url, 'http://meteo.test/DPClim/v1/liste-stations/quotidienne')
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer jeton-1')
        self.assertEqual(kwargs['params'], {'id-departement': '37', 'parametre': 'temperature'})
        self.assertNotIn(CLE, str(kwargs))                             # jamais vers DPClim
        self.client.list_stations('75', 'temperature')
        self.assertEqual(self.post.call_count, 1)                      # jeton réutilisé

    def test_jeton_expire_renouvele_une_fois(self):
        self.get.side_effect = [Reponse(401), Reponse(200, {'stations': [{'id': '2'}]})]
        self.assertEqual(self.client.list_stations('37', 'vent'), [{'id': '2'}])
        self.assertEqual(self.post.call_count, 2)
        self.assertEqual(self.get.call_args.kwargs['headers']['Authorization'], 'Bearer jeton-2')
        self.get.side_effect = [Reponse(401), Reponse(401)]
        with self.assertRaises(MeteoError) as refus:
            self.client.list_stations('37', 'vent')
        self.assertNotIsInstance(refus.exception, MeteoQuotaError)

    def test_quota(self):
        dans_90s = datetime.now(timezone.utc) + timedelta(seconds=90)
        mois = ['janv.', 'févr.', 'mars', 'avr.', 'mai', 'juin', 'juil.', 'août', 'sept.', 'oct.',
                'nov.', 'déc.'][dans_90s.month - 1]
        entete = f'sam., {dans_90s.day} {mois} {dans_90s.year} {dans_90s:%H:%M:%S} GMT'
        self.get.return_value = Reponse(429, headers={'Retry-After': entete})
        with self.assertRaises(MeteoQuotaError) as quota:
            self.client.order_daily('37001', 'a', 'b')
        self.assertAlmostEqual(quota.exception.retry_after_seconds, 90, delta=3)

    def test_commande_et_fichier(self):
        self.get.return_value = Reponse(202, {'elaboreProduitAvecDemandeResponse': {'return': 771}})
        self.assertEqual(self.client.order_daily('37001', '2023-01-01T00:00:00Z',
                                                 '2023-12-31T23:00:00Z'), '771')
        self.assertEqual(self.get.call_args.kwargs['params']['id-station'], '37001')
        for reponse in (Reponse(404), Reponse(200, {}), Reponse(200, text='pas du JSON')):
            self.get.return_value = reponse
            with self.assertRaises(MeteoError):
                self.client.order_daily('37001', 'a', 'b')
        # Le fichier n'est pas prêt tout de suite : on attend, puis on le lit.
        self.get.return_value = None
        self.get.side_effect = [Reponse(204), Reponse(204), Reponse(200, text='POSTE;DATE\n')]
        self.assertEqual(self.client.fetch_file('771', poll_interval=5), 'POSTE;DATE\n')
        self.assertEqual(self.sleep.call_args_list, [mock.call(5), mock.call(5)])
        # … mais pas indéfiniment.
        self.get.side_effect = None
        self.get.return_value = Reponse(204)
        with self.assertRaisesRegex(MeteoError, 'Délai dépassé'):
            self.client.fetch_file('771', poll_interval=30, max_wait=120)
        self.get.return_value = Reponse(500)
        with self.assertRaisesRegex(MeteoError, '500'):
            self.client.fetch_file('771')

    def test_erreurs_sans_fuite_de_l_identifiant(self):
        self.get.side_effect = requests.ConnectionError('injoignable')
        with self.assertRaises(MeteoError) as reseau:
            self.client.list_stations('37', 'vent')
        self.assertNotIn(CLE, str(reseau.exception))
        self.get.side_effect = None
        self.get.return_value = Reponse(503)
        with self.assertRaises(MeteoError):
            self.client.list_stations('37', 'vent')
        self.post.side_effect = lambda *a, **k: Reponse(401, text=f'Basic {CLE} refusé')
        with self.assertRaises(MeteoError) as refus:
            MeteoClient(CLE).authenticate()
        self.assertNotIn(CLE, str(refus.exception))
        self.post.side_effect = requests.ConnectionError('injoignable')
        with self.assertRaises(MeteoError):
            MeteoClient(CLE).authenticate()
        self.post.side_effect = lambda *a, **k: Reponse(200, {})
        with self.assertRaises(MeteoError):
            MeteoClient(CLE).authenticate()
        for vide in ('', None):
            with self.assertRaises(MeteoError):
                MeteoClient(vide)


class CsvEtQualiteTests(SimpleTestCase):

    CSV = ('POSTE;DATE;RR;TN;TX;TM\n'
           '1;20230101;0,0;-2,5;4,0;1,0\n'
           '1;20230102;1,2;;5,0;\n'              # TM absent ce jour-là
           '1;20230103;;0,0;6,0;3,0\n'
           '1;20230103;;0,0;6,0;9,0\n'           # même date une seconde fois
           '1;20230110;x;1,0;7,0;5,0\n')

    def test_lecture_du_csv(self):
        lu = parse_daily_csv(self.CSV, ['TN', 'TX', 'TM'], 'TM')
        self.assertEqual(lu['series']['TN'], [-2.5, 0.0, 0.0, 1.0])
        self.assertEqual(lu['series']['TM'], [1.0, 3.0, 9.0, 5.0])
        # Série datée : une valeur par jour, la première rencontrée.
        self.assertEqual(lu['dated_main'], [('20230101', 1.0), ('20230103', 3.0), ('20230110', 5.0)])
        self.assertEqual(lu['date_counts'], {'20230101': 1, '20230102': 1, '20230103': 2,
                                             '20230110': 1})
        self.assertEqual(parse_daily_csv('', ['TM'], 'TM'),
                         {'series': {'TM': []}, 'dated_main': [], 'date_counts': {}})

    def test_qualite_sur_la_periode(self):
        lu = parse_daily_csv(self.CSV, ['TM'], 'TM')
        periode = combine_period([lu], date(2023, 1, 1), date(2023, 1, 20))
        self.assertEqual(periode['dated_main'], [(1, 1.0), (3, 3.0), (10, 5.0)])
        self.assertEqual(periode['quality'], {
            'heures_couvertes': 3 * 24, 'heures_annee': 20 * 24, 'taux_completude': 0.15,
            'trou_max_heures': 10 * 24,              # du 11 au 20 : dix jours sans relevé
            'max_releves_meme_datetime': 2, 'duplicatas_datetime': 1})

    def test_trou_au_debut_et_periode_vide(self):
        un = parse_daily_csv('DATE;TM\n20230120;1\n', ['TM'], 'TM')
        qualite = combine_period([un], date(2023, 1, 1), date(2023, 1, 20))['quality']
        self.assertEqual(qualite['trou_max_heures'], 19 * 24)
        vide = combine_period([], date(2023, 1, 1), date(2023, 12, 31))['quality']
        self.assertEqual((vide['taux_completude'], vide['trou_max_heures'], vide['heures_annee']),
                         (0.0, 365 * 24, 365 * 24))

    def test_plusieurs_annees_jours_continus(self):
        a = parse_daily_csv('DATE;TM\n20221231;1\n', ['TM'], 'TM')
        b = parse_daily_csv('DATE;TM\n20230101;2\n20240101;3\n', ['TM'], 'TM')
        periode = combine_period([b, a], date(2022, 1, 1), date(2023, 12, 31))
        # Jours comptés depuis le début de la période ; hors période ignoré ; triés.
        self.assertEqual(periode['dated_main'], [(365, 1.0), (366, 2.0)])
        self.assertEqual(periode['series']['TM'], [2.0, 3.0, 1.0])
        self.assertEqual(periode['quality']['heures_annee'], 730 * 24)

    def test_coordonnees_d_une_station(self):
        self.assertEqual(station_lonlat({'lat': '47.39', 'lon': 0.69}), (0.69, 47.39))
        self.assertEqual(station_lonlat({'latitude': 1, 'longitude': 2}), (2.0, 1.0))
        self.assertIsNone(station_lonlat({'lat': 47.39}))
        self.assertIsNone(station_lonlat({'lat': 'x', 'lon': 'y'}))

    def test_retry_after(self):
        self.assertEqual(_parse_retry_after('45'), 45)
        self.assertEqual(_parse_retry_after('sam., 18 juil. 2020 16:52:14 GMT'), 0)   # passé
        for illisible in (None, '', 'bientôt', 'sam., 18 brumaire 2026 16:52:14 GMT',
                          'sam., 31 févr. 2099 16:52:14 GMT'):
            with self.subTest(valeur=illisible):
                self.assertIsNone(_parse_retry_after(illisible))


class IndicateursTests(SimpleTestCase):

    def calcul(self, nom, tm, params=None, dated=None, **autres):
        cols = {'TM': np.asarray(tm, dtype=float), **{k: np.asarray(v, dtype=float)
                                                      for k, v in autres.items()}}
        return indicators.compute(nom, cols, 'TM', params or {}, dated)

    def test_statistiques(self):
        tm = [10, 20, float('nan'), 30]                  # les trous sont ignorés
        self.assertEqual(self.calcul('mean', tm), 20.0)
        self.assertEqual((self.calcul('min', tm), self.calcul('max', tm)), (10.0, 30.0))
        self.assertEqual((self.calcul('sum', tm), self.calcul('amplitude', tm)), (60.0, 20.0))
        self.assertEqual(self.calcul('count_above', tm, {'threshold': 15}), 2.0)
        self.assertEqual(self.calcul('count_above', tm, {'threshold': 30}), 0.0)   # strict
        self.assertEqual(self.calcul('count_below', tm, {'threshold': 20}), 1.0)
        self.assertEqual(self.calcul('percentile', tm, {'p': 50}), 20.0)
        self.assertEqual(self.calcul('percentile', tm, {'p': 400}), 30.0)          # borné à 100
        self.assertEqual(self.calcul('percentile', tm, {'p': 'x'}),
                         float(np.percentile([10, 20, 30], 90)))

    def test_jours_de_gel_sur_la_temperature_minimale(self):
        self.assertEqual(self.calcul('frost_days', [5, 5, 5], TN=[-1, 0, -3]), 2.0)
        # Sans colonne TN (autre grandeur) : repli sur la colonne principale.
        self.assertEqual(self.calcul('frost_days', [-5, 5, 5]), 1.0)

    def test_sans_donnee_pas_de_valeur(self):
        for nom in indicators.INDICATORS:
            with self.subTest(indicateur=nom):
                self.assertIsNone(self.calcul(nom, [], dated=[]))
                self.assertIsNone(self.calcul(nom, [float('nan')], dated=[(1, 2.0)]))
        with self.assertRaises(ValueError):
            self.calcul('mediane', [1])

    def test_integrale_a_seuil(self):
        # Constante à +2 au-dessus du seuil pendant 4 jours : 8 unité×jour.
        plat = [(1, 12.0), (5, 12.0)]
        self.assertEqual(self.calcul('integral_positive', [1], {'seuil': 10}, plat), 8.0)
        self.assertEqual(self.calcul('integral_negative', [1], {'seuil': 10}, plat), 0.0)
        # Passage par zéro au milieu du segment : deux triangles égaux.
        croise = [(1, -1.0), (3, 1.0)]
        self.assertEqual(self.calcul('integral_positive', [1], None, croise), 0.5)
        self.assertEqual(self.calcul('integral_negative', [1], None, croise), 0.5)
        self.assertEqual(self.calcul('integral_absolue', [1], None, croise), 0.0)
        # Trou de dix jours : variation supposée affine d'un relevé à l'autre.
        trou = [(1, 0.0), (11, 10.0)]
        self.assertEqual(self.calcul('integral_positive', [1], None, trou), 50.0)
        # Degrés-jours de chauffage : sous le seuil de 18.
        froid = [(1, 8.0), (2, 8.0), (3, 18.0)]
        self.assertEqual(self.calcul('integral_negative', [1], {'seuil': 18}, froid), 10.0 + 5.0)
        # Deux relevés du même jour ne comptent pas (largeur nulle).
        self.assertEqual(self.calcul('integral_positive', [1], None, [(1, 5.0), (1, 9.0)]), 0.0)


class ClassificationTests(SimpleTestCase):

    def test_intervalles_egaux(self):
        self.assertEqual(choropleth.classify([0, 10, 3, None, float('nan')], 'equal', 5),
                         [0.0, 2.0, 4.0, 6.0, 8.0, 10.0])

    def test_quantiles_toujours_croissants(self):
        self.assertEqual(choropleth.classify(range(1, 101), 'quantiles', 4),
                         [1.0, 25.75, 50.5, 75.25, 100.0])
        concentre = choropleth.classify([5] * 50 + [9], 'quantiles', 5)
        self.assertEqual(len(concentre), 6)
        self.assertTrue(all(a < b for a, b in zip(concentre, concentre[1:])))

    def test_cas_degeneres(self):
        self.assertEqual(choropleth.classify([], 'equal', 5), [])
        self.assertEqual(choropleth.classify([None, float('inf')], 'equal', 5), [])
        self.assertEqual(choropleth.classify([7, 7, 7], 'jenks', 5), [7.0, 7.0])
        self.assertEqual(len(choropleth.classify(range(100), 'equal', 50)), 8 + 1)    # 8 classes au plus
        self.assertEqual(len(choropleth.classify(range(100), 'equal', 0)), 1 + 1)
        self.assertEqual(choropleth.classify([1, 2, 3], 'inconnue', 2),
                         choropleth.classify([1, 2, 3], 'quantiles', 2))

    def test_jenks_separe_les_groupes_naturels(self):
        valeurs = [1, 2, 3, 20, 21, 22, 50, 51, 52]
        bornes = choropleth.classify(valeurs, 'jenks', 3)
        self.assertEqual(bornes, [1.0, 3.0, 22.0, 52.0])
        self.assertEqual([choropleth.class_index(v, bornes) for v in valeurs],
                         [0, 0, 0, 1, 1, 1, 2, 2, 2])

    def test_classe_d_une_valeur(self):
        bornes = [0.0, 10.0, 20.0, 30.0]
        cas = {0: 0, 5: 0, 10: 0, 10.01: 1, 20: 1, 30: 2, 99: 2, -5: 0, None: 0}
        for valeur, classe in cas.items():
            with self.subTest(valeur=valeur):
                self.assertEqual(choropleth.class_index(valeur, bornes), classe)
        self.assertEqual(choropleth.class_index(5, []), 0)

    def test_couleurs_et_legende(self):
        ancres = choropleth.RAMPS['Blues']
        self.assertEqual(choropleth.ramp_colors('Blues', 6), ancres)
        trois = choropleth.ramp_colors('Blues', 3)
        self.assertEqual((trois[0], trois[-1]), (ancres[0], ancres[-1]))
        self.assertEqual(choropleth.ramp_colors('Blues', 1), [ancres[-1]])
        self.assertEqual(choropleth.ramp_colors('inconnue', 2), choropleth.ramp_colors('YlOrRd', 2))
        self.assertEqual(choropleth.build_legend([0, 1.23456, 2], ['#a', '#b']), [
            {'min': 0.0, 'max': 1.235, 'color': '#a'}, {'min': 1.235, 'max': 2.0, 'color': '#b'}])
