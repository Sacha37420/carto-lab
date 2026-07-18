"""
Tâches Celery — orchestration Météo-France 4.1 → 4.4 (Feature 4).

La clé API est récupérée depuis le stockage éphémère (jeton), utilisée en mémoire,
jamais journalisée ni persistée.
"""
import time

from celery import shared_task

from . import indicators, meteo_pipeline, secret_store
from .meteo_client import (
    METRO_DEPARTEMENTS, MeteoClient, MeteoError, MeteoQuotaError,
    parse_daily_csv, station_lonlat,
)
from .models import Job

# Au-delà de cette attente unique, mieux vaut abandonner que bloquer le job : un
# 429 avec un Retry-After aussi long ressemble à un quota journalier/horaire, pas
# à la fenêtre courte (~1 min, vérifiée en direct) qu'on peut se permettre
# d'attendre dans une tâche asynchrone.
QUOTA_MAX_SINGLE_WAIT = 150
QUOTA_MAX_RETRIES = 3


def _with_quota_retry(fn, job, label):
    """
    Exécute `fn()` ; si Météo-France répond 429, attend le délai indiqué par
    Retry-After (dans une limite raisonnable) puis réessaie, au lieu d'abandonner
    immédiatement — vérifié en direct : le quota de commande-station se
    reconstitue en moins d'une minute. Relève l'exception si l'attente demandée
    est trop longue, ou après QUOTA_MAX_RETRIES tentatives.
    """
    for attempt in range(QUOTA_MAX_RETRIES):
        try:
            return fn()
        except MeteoQuotaError as exc:
            wait = exc.retry_after_seconds
            if wait is None or wait > QUOTA_MAX_SINGLE_WAIT or attempt == QUOTA_MAX_RETRIES - 1:
                raise
            job.set_state(message=f"{label} : quota Météo-France atteint, reprise dans {wait}s…")
            time.sleep(wait + 1)
    raise MeteoQuotaError("Quota Météo-France toujours dépassé après plusieurs tentatives.")


def _cast_threshold(value, caster):
    """None/'' → pas de seuil ; sinon caste, ou None si invalide (seuil ignoré plutôt que job en erreur)."""
    if value is None or value == '':
        return None
    try:
        return caster(value)
    except (TypeError, ValueError):
        return None


def _passes_quality(station: dict, thresholds: dict) -> bool:
    q = station.get('qualite') or {}
    if thresholds['min_completeness'] is not None and q.get('taux_completude', 0.0) < thresholds['min_completeness']:
        return False
    if thresholds['max_gap_hours'] is not None and q.get('trou_max_heures', float('inf')) > thresholds['max_gap_hours']:
        return False
    if thresholds['max_same_datetime'] is not None and q.get('max_releves_meme_datetime', 0) > thresholds['max_same_datetime']:
        return False
    if thresholds['max_duplicates'] is not None and q.get('duplicatas_datetime', 0) > thresholds['max_duplicates']:
        return False
    return True


def _collect_stations(client: MeteoClient, parametre: str, departements, job: Job):
    """
    4.1 — parcourt les départements et agrège les stations (dédupliquées).

    Ne garde que les stations `posteOuvert: true` : vérifié en direct sur DPClim,
    une station fermée renvoie systématiquement 404 « production en échec » sur
    commande/fichier, quelle que soit la période demandée — les inclure ne peut
    que gaspiller des commandes et, si elles dominent la liste (fréquent : Paris
    intra-muros par ex.), faire échouer tout le job une fois tronqué par
    `max_stations` alors que des stations ouvertes existaient plus loin.
    """
    seen: dict[str, dict] = {}
    total = len(departements)
    quota_hit = False
    for i, dep in enumerate(departements):
        try:
            stations = _with_quota_retry(lambda: client.list_stations(dep, parametre),
                                         job, f"Département {dep}")
            for st in stations:
                if not st.get('posteOuvert'):
                    continue
                sid = str(st.get('id') or st.get('id_station') or st.get('NUM_POSTE') or '')
                ll = station_lonlat(st)
                if not sid or ll is None:
                    continue
                seen.setdefault(sid, {
                    'id_station': sid,
                    'nom': st.get('nom') or st.get('NOM_USUEL') or sid,
                    'lon': ll[0], 'lat': ll[1],
                })
        except MeteoQuotaError:
            # inutile d'interroger les départements restants : ils échoueront
            # tous pareil et chaque essai ronge encore le quota qui doit se
            # reconstituer. On garde ce qui a déjà été trouvé.
            quota_hit = True
            break
        except MeteoError:
            continue  # un département en échec ne bloque pas le reste
        job.set_state(progress=5 + int(20 * (i + 1) / total),
                      message=f"Stations : {len(seen)} trouvées ({i + 1}/{total} dép.)")
    if quota_hit:
        job.set_state(message=f"Quota Météo-France atteint — {len(seen)} stations trouvées "
                              f"avant l'arrêt ({i + 1}/{total} dép. interrogés).")
    return list(seen.values())


def _compute_indicator(client, stations, grandeur_cfg, indicator_name, indicator_params,
                       year, job):
    """4.2 — commande + télécharge les données de chaque station et calcule l'indicateur."""
    date_deb = f"{year}-01-01T00:00:00Z"
    date_fin = f"{year}-12-31T23:00:00Z"
    cols = grandeur_cfg['columns']
    total = len(stations)
    ind = indicators.INDICATORS[indicator_name]
    last_error = None
    for i, s in enumerate(stations):
        s.setdefault('qualite', None)
        try:
            label = f"Station {s['id_station']}"
            cmde = _with_quota_retry(
                lambda: client.order_daily(s['id_station'], date_deb, date_fin), job, label)
            csv_text = _with_quota_retry(lambda: client.fetch_file(cmde), job, label)
            parsed = parse_daily_csv(csv_text, cols, grandeur_cfg['main'], year)
            import numpy as np
            colmap = {c: np.asarray(parsed['series'].get(c, []), dtype=float) for c in cols}
            s['valeur'] = indicators.compute(indicator_name, colmap, grandeur_cfg['main'],
                                             indicator_params, parsed['dated_main'])
            s['qualite'] = parsed['quality']
        except MeteoQuotaError as exc:
            # comme dans _collect_stations : inutile de mitrailler les stations
            # restantes, elles échoueront toutes pareil tant que le quota ne
            # s'est pas reconstitué. On garde les valeurs déjà calculées.
            last_error = str(exc)
            job.set_state(message=f"Quota Météo-France atteint — {i}/{total} stations "
                                  f"traitées avant l'arrêt.")
            break
        except Exception as exc:
            s['valeur'] = None
            last_error = str(exc)
        if i % 5 == 0 or i == total - 1:
            job.set_state(progress=25 + int(60 * (i + 1) / total),
                          message=f"Indicateur « {ind['label']} » : {i + 1}/{total} stations")
    return stations, last_error


@shared_task(bind=True)
def build_meteo_choropleth(self, job_id: int, key_token: str, params: dict):
    job = Job.objects.get(pk=job_id)
    job.celery_task_id = self.request.id
    job.save(update_fields=['celery_task_id'])

    api_key = secret_store.take(key_token)
    try:
        if not api_key:
            raise MeteoError("Clé API absente ou expirée (relancez la commande).")

        grandeur = params['grandeur']
        year = int(params['year'])
        indicator_name = params['indicator']
        indicator_params = params.get('indicator_params', {})
        classification = params.get('classification', 'quantiles')
        n_classes = int(params.get('n_classes', 5))
        ramp = params.get('ramp', 'YlOrRd')
        max_stations = params.get('max_stations')          # cap optionnel
        departements = params.get('departements') or METRO_DEPARTEMENTS
        raw_qt = params.get('quality_thresholds') or {}
        quality_thresholds = {
            'min_completeness': _cast_threshold(raw_qt.get('min_completeness'), float),
            'max_gap_hours': _cast_threshold(raw_qt.get('max_gap_hours'), float),
            'max_same_datetime': _cast_threshold(raw_qt.get('max_same_datetime'), int),
            'max_duplicates': _cast_threshold(raw_qt.get('max_duplicates'), int),
        }

        cfg = indicators.GRANDEURS.get(grandeur)
        if cfg is None:
            raise MeteoError(f"Grandeur inconnue : {grandeur}.")
        ind = indicators.INDICATORS.get(indicator_name)
        if ind is None:
            raise MeteoError(f"Indicateur inconnu : {indicator_name}.")

        job.set_state(status=Job.RUNNING, progress=2, message="Authentification Météo-France…")
        client = MeteoClient(api_key)
        client.authenticate()  # échec rapide si l'identifiant est refusé, avant tout appel DPClim

        job.set_state(progress=5, message="Recherche des stations…")

        stations = _collect_stations(client, cfg['parametre'], departements, job)
        if max_stations:
            stations = stations[: int(max_stations)]
        if not stations:
            raise MeteoError("Aucune station trouvée pour cette grandeur.")

        stations, last_error = _compute_indicator(client, stations, cfg, indicator_name,
                                                  indicator_params, year, job)
        usable = [s for s in stations if s.get('valeur') is not None]
        if not usable:
            detail = f" Dernière erreur rencontrée : {last_error}" if last_error else ""
            raise MeteoError(f"Aucune donnée exploitable pour construire la carte.{detail}")

        job.set_state(progress=88, message="Construction des stations et du Voronoï…")
        # Couche ponctuelle : TOUTES les stations interrogées (y compris celles sans
        # valeur exploitable), avec leurs métriques de qualité en propriétés — la
        # station qui n'a rien produit reste visible, avec de quoi expliquer pourquoi.
        stations_layer = meteo_pipeline.build_stations_layer(
            stations, grandeur, ind['label'], year, job.owner_email)

        filtered = [s for s in usable if _passes_quality(s, quality_thresholds)]
        if not filtered:
            raise MeteoError(
                "Aucune station ne respecte les seuils de qualité imposés — la couche "
                "ponctuelle a été créée, mais pas de choroplèthe possible avec ces seuils."
            )

        job.set_state(progress=94, message="Découpage France + classification choroplèthe…")
        title = f"{cfg['label']} {year} — {ind['label']}"
        included_ids = {s['id_station'] for s in filtered}
        result = meteo_pipeline.build_choropleth(
            stations_layer, title, classification, n_classes, ramp,
            owner_email=job.owner_email, included_station_ids=included_ids)

        job.result_layer = result
        job.save(update_fields=['result_layer'])
        job.set_state(status=Job.DONE, progress=100,
                      message=f"Carte « {title} » créée ({result.feature_count} polygones, "
                              f"{len(usable)} stations).")
    except Exception as exc:
        job.set_state(status=Job.ERROR, message=str(exc))
