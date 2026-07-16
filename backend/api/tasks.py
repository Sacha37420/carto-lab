"""
Tâches Celery — orchestration Météo-France 4.1 → 4.4 (Feature 4).

La clé API est récupérée depuis le stockage éphémère (jeton), utilisée en mémoire,
jamais journalisée ni persistée.
"""
from celery import shared_task

from . import indicators, meteo_pipeline, secret_store
from .meteo_client import (
    METRO_DEPARTEMENTS, MeteoClient, MeteoError, parse_daily_csv, station_lonlat,
)
from .models import Job


def _collect_stations(client: MeteoClient, parametre: str, departements, job: Job):
    """4.1 — parcourt les départements et agrège les stations (dédupliquées)."""
    seen: dict[str, dict] = {}
    total = len(departements)
    for i, dep in enumerate(departements):
        try:
            for st in client.list_stations(dep, parametre):
                sid = str(st.get('id') or st.get('id_station') or st.get('NUM_POSTE') or '')
                ll = station_lonlat(st)
                if not sid or ll is None:
                    continue
                seen.setdefault(sid, {
                    'id_station': sid,
                    'nom': st.get('nom') or st.get('NOM_USUEL') or sid,
                    'lon': ll[0], 'lat': ll[1],
                })
        except MeteoError:
            continue  # un département en échec ne bloque pas le reste
        job.set_state(progress=5 + int(20 * (i + 1) / total),
                      message=f"Stations : {len(seen)} trouvées ({i + 1}/{total} dép.)")
    return list(seen.values())


def _compute_indicator(client, stations, grandeur_cfg, indicator_name, indicator_params,
                       year, job):
    """4.2 — commande + télécharge les données de chaque station et calcule l'indicateur."""
    date_deb = f"{year}-01-01T00:00:00Z"
    date_fin = f"{year}-12-31T23:00:00Z"
    cols = grandeur_cfg['columns']
    total = len(stations)
    ind = indicators.INDICATORS[indicator_name]
    for i, s in enumerate(stations):
        try:
            cmde = client.order_daily(s['id_station'], date_deb, date_fin)
            csv_text = client.fetch_file(cmde)
            series = parse_daily_csv(csv_text, cols)
            import numpy as np
            colmap = {c: np.asarray(series.get(c, []), dtype=float) for c in cols}
            s['valeur'] = indicators.compute(indicator_name, colmap, grandeur_cfg['main'],
                                             indicator_params)
        except (MeteoError, Exception):
            s['valeur'] = None
        if i % 5 == 0 or i == total - 1:
            job.set_state(progress=25 + int(60 * (i + 1) / total),
                          message=f"Indicateur « {ind['label']} » : {i + 1}/{total} stations")
    return stations


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

        cfg = indicators.GRANDEURS.get(grandeur)
        if cfg is None:
            raise MeteoError(f"Grandeur inconnue : {grandeur}.")
        ind = indicators.INDICATORS.get(indicator_name)
        if ind is None:
            raise MeteoError(f"Indicateur inconnu : {indicator_name}.")

        job.set_state(status=Job.RUNNING, progress=5, message="Recherche des stations…")
        client = MeteoClient(api_key)

        stations = _collect_stations(client, cfg['parametre'], departements, job)
        if max_stations:
            stations = stations[: int(max_stations)]
        if not stations:
            raise MeteoError("Aucune station trouvée pour cette grandeur.")

        stations = _compute_indicator(client, stations, cfg, indicator_name,
                                      indicator_params, year, job)
        usable = [s for s in stations if s.get('valeur') is not None]
        if not usable:
            raise MeteoError("Aucune donnée exploitable pour construire la carte.")

        job.set_state(progress=88, message="Construction des stations et du Voronoï…")
        stations_layer = meteo_pipeline.build_stations_layer(
            usable, grandeur, ind['label'], year, job.owner_email)

        job.set_state(progress=94, message="Découpage France + classification choroplèthe…")
        title = f"{cfg['label']} {year} — {ind['label']}"
        result = meteo_pipeline.build_choropleth(
            stations_layer, title, classification, n_classes, ramp,
            owner_email=job.owner_email)

        job.result_layer = result
        job.save(update_fields=['result_layer'])
        job.set_state(status=Job.DONE, progress=100,
                      message=f"Carte « {title} » créée ({result.feature_count} polygones, "
                              f"{len(usable)} stations).")
    except Exception as exc:
        job.set_state(status=Job.ERROR, message=str(exc))
