"""Publication QGIS : la couche est copiée dans une vraie table du schéma
``carto_public``, seul schéma que voit le rôle en lecture seule de
pg_featureserv. Les noms de table et de colonne viennent de données
utilisateur (nom de couche, clés d'attributs) : ils doivent être neutralisés."""
import unittest

from django.conf import settings
from django.db import connection
from django.test.utils import CaptureQueriesContext

from api.models import Layer
from api.publish import _ident, table_name

from .helpers import LabTestCase, carre, couche


def table_existe(nom: str) -> bool:
    with connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema='carto_public' AND table_name=%s", [nom])
        return cur.fetchone()[0] == 1


def colonnes(nom: str) -> dict:
    with connection.cursor() as cur:
        cur.execute("SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema='carto_public' AND table_name=%s", [nom])
        return dict(cur.fetchall())


def tables_publiques() -> list:
    with connection.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='carto_public' ORDER BY 1")
        return [r[0] for r in cur.fetchall()]


class PublicationTests(LabTestCase):

    def setUp(self):
        super().setUp()
        self.layer = couche('Mes Zones 2024', [carre(0, 0), carre(2, 2)], props=[
            {'nom': 'A', 'surface': 12.5, 'code': '037', 'vide': '', 'mixte': 3},
            {'nom': 'B', 'surface': '7', 'code': '75', 'vide': None, 'mixte': 'trois'},
        ])
        self.url = f'/api/layers/{self.layer.pk}/publish/'
        self.table = f'pub_{self.layer.pk}_mes_zones_2024'

    def test_publier(self):
        self.assertEqual(self.alice.get(self.url).json(), {'published': False})
        with CaptureQueriesContext(connection) as requetes:
            resp = self.alice.post(self.url)
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(table_name(self.layer), self.table)
        self.assertTrue(table_existe(self.table))
        # Colonnes à plat, typées d'après les valeurs : numérique si TOUTES le sont.
        self.assertEqual(colonnes(self.table), {
            'fid': 'bigint', 'geom': 'USER-DEFINED', 'nom': 'text', 'surface': 'double precision',
            'code': 'double precision', 'vide': 'double precision', 'mixte': 'text'})
        with connection.cursor() as cur:
            cur.execute(f'SELECT nom, surface, mixte, ST_SRID(geom) FROM carto_public."{self.table}" '
                        'ORDER BY nom')
            self.assertEqual(cur.fetchall(), [('A', 12.5, '3', 4326), ('B', 7.0, 'trois', 4326)])
        # Le rôle de pg_featureserv reçoit la LECTURE de cette table, rien d'autre.
        droits = [q['sql'] for q in requetes if 'GRANT' in q['sql'].upper()]
        self.assertEqual(droits, [f'GRANT SELECT ON carto_public."{self.table}" '
                                  f'TO {settings.CARTO_READER_USER}'])
        info = resp.json()
        self.assertTrue(info['published'])
        self.assertEqual(info['collection'], f'carto_public.{self.table}')
        self.assertEqual(info['ogc_url'], 'https://lab.example.org/carto-ogc')
        self.assertEqual(info['items_url'],
                         f'https://lab.example.org/carto-ogc/collections/carto_public.{self.table}/items')
        self.assertEqual(self.bob.get(self.url).json()['collection'], info['collection'])
        self.layer.refresh_from_db()
        self.assertTrue(self.layer.published_qgis)
        self.assertTrue(self.alice.get(f'/api/layers/{self.layer.pk}/').json()['published_qgis'])

    def test_republier_remplace_la_table(self):
        self.alice.post(self.url)
        self.layer.features.first().delete()
        self.assertEqual(self.alice.post(self.url).status_code, 201)
        with connection.cursor() as cur:
            cur.execute(f'SELECT count(*) FROM carto_public."{self.table}"')
            self.assertEqual(cur.fetchone()[0], 1)
        self.assertEqual(tables_publiques(), [self.table])

    def test_depublier(self):
        self.alice.post(self.url)
        self.assertEqual(self.bob.delete(self.url).status_code, 204)
        self.assertFalse(table_existe(self.table))
        self.layer.refresh_from_db()
        self.assertFalse(self.layer.published_qgis)
        self.assertNotIn('published', self.layer.metadata)
        self.assertEqual(self.alice.get(self.url).json(), {'published': False})
        self.assertEqual(self.bob.delete(self.url).status_code, 204)        # sans effet, sans erreur

    def test_refus(self):
        raster = Layer.objects.create(name='R', layer_type='raster', raster_path='rasters/x.tif')
        vide = Layer.objects.create(name='Vide')
        for layer in (raster, vide):
            with self.subTest(couche=layer.name):
                resp = self.alice.post(f'/api/layers/{layer.pk}/publish/')
                self.assertEqual(resp.status_code, 400)
                self.assertFalse(Layer.objects.get(pk=layer.pk).published_qgis)
        for methode in ('get', 'post', 'delete'):
            self.assertEqual(getattr(self.alice, methode)('/api/layers/999999/publish/').status_code,
                             404)
        self.assertEqual(tables_publiques(), [])

    def test_noms_hostiles_neutralises(self):
        hostile = couche('x"; DROP TABLE carto_lab.layers; --', [carre(0, 0)], props=[{
            'a"b': 1, "c'd": 'apostrophe', 'geom': 'réservé', 'fid': 5, 'FID': 6,
            '1er': 'chiffre en tête', 'é à': 'accents', '"; DROP TABLE carto_lab.features; --': 'x',
        }])
        resp = self.alice.post(f'/api/layers/{hostile.pk}/publish/')
        self.assertEqual(resp.status_code, 201, resp.content)
        table = resp.json()['collection'].split('.', 1)[1]
        self.assertRegex(table, r'^pub_\d+_[a-z0-9_]+$')
        cols = colonnes(table)
        for col in cols:
            self.assertRegex(col, r'^[a-z_][a-z0-9_]*$')
        # Les clés réservées sont renommées, pas écrasées ; aucune colonne perdue.
        self.assertEqual(len(cols), 2 + 8)
        self.assertEqual(cols['fid'], 'bigint')
        self.assertIn('geom_1', cols)
        with connection.cursor() as cur:
            cur.execute(f'SELECT c_d FROM carto_public."{table}"')
            self.assertEqual(cur.fetchone()[0], 'apostrophe')
        self.assertEqual(Layer.objects.count(), 2)                          # rien n'a été détruit

    def test_identifiants(self):
        for brut, attendu in (('Nom Propre', 'nom_propre'), ('1er', '_1er'), ('', 'col'),
                              ('___', 'col'), ('a' * 80, 'a' * 60), ('É', 'col'), ('a-b.c', 'a_b_c')):
            with self.subTest(brut=brut):
                self.assertEqual(_ident(brut), attendu)

    # DÉFAUT (rapport, C4) — une clé d'attribut contenant « % » est recopiée dans
    # le SQL exécuté avec des paramètres liés : psycopg y voit un marqueur de
    # paramètre et la publication lève une exception non attrapée (500). Pas
    # d'injection possible, mais une couche importée d'un fichier dont un champ
    # s'appelle « taux_% » est impubliable.
    @unittest.expectedFailure
    def test_cle_d_attribut_avec_un_pourcent(self):
        self.alice.raise_request_exception = False
        layer = couche('Taux', [carre(0, 0)], props=[{'taux_%': 12, 'part %s': 'x'}])
        self.assertEqual(self.alice.post(f'/api/layers/{layer.pk}/publish/').status_code, 201)

    def test_supprimer_une_couche_publiee_retire_sa_table(self):
        # Corrigé (C5) : la table de carto_public restait, toujours servie par
        # pg_featureserv, sans plus aucune route pour la retirer.
        autre = couche('Autre', [carre(5, 5)])
        self.alice.post(f'/api/layers/{autre.pk}/publish/')
        self.alice.post(self.url)
        self.assertEqual(self.alice.delete(f'/api/layers/{self.layer.pk}/').status_code, 204)
        self.assertFalse(table_existe(self.table))
        self.assertFalse(Layer.objects.filter(pk=self.layer.pk).exists())
        # … et seulement la sienne.
        self.assertEqual(tables_publiques(), [table_name(autre)])

    def test_supprimer_une_couche_non_publiee_ne_touche_pas_carto_public(self):
        self.alice.post(self.url)
        autre = couche('Autre', [carre(5, 5)])
        with CaptureQueriesContext(connection) as requetes:
            self.assertEqual(self.alice.delete(f'/api/layers/{autre.pk}/').status_code, 204)
        self.assertFalse([q for q in requetes if 'DROP TABLE' in q['sql'].upper()])
        self.assertEqual(tables_publiques(), [self.table])
