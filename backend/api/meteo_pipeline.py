"""
Pipeline cartographique Météo-France (Features 4.3 & 4.4).

À partir d'une liste de stations {lon, lat, valeur d'indicateur, métadonnées} :
  1. couche ponctuelle des stations (4.1) ;
  2. polygones de Voronoï/Thiessen portant la valeur (4.3) ;
  3. découpage sur la France métropolitaine (frontière fournie ou couche bundlée) ;
  4. classification choroplèthe + rampe + légende, persistée comme couche (4.4).

Ce module est INDÉPENDANT de l'API Météo-France : il prend des stations déjà
résolues, donc il est testable avec des données synthétiques.
"""
import json
import os

from django.contrib.gis.geos import GEOSGeometry

from . import choropleth
from . import processing
from .models import Feature, Layer

FRANCE_GEOJSON = os.path.join(os.path.dirname(__file__), 'data', 'france_metropole.geojson')
FRANCE_LAYER_NAME = 'France métropolitaine (réf.)'


def ensure_france_layer() -> Layer:
    """Couche de référence de la frontière France métropolitaine (idempotente)."""
    existing = Layer.objects.filter(name=FRANCE_LAYER_NAME, origin=Layer.ORIGIN_CALCUL).first()
    if existing and existing.features.exists():
        return existing
    with open(FRANCE_GEOJSON) as f:
        gj = json.load(f)
    geom = GEOSGeometry(json.dumps(gj['geometry']), srid=4326)
    layer = existing or Layer.objects.create(
        name=FRANCE_LAYER_NAME, layer_type=Layer.VECTOR, origin=Layer.ORIGIN_CALCUL,
        srid_source=4326, geom_type=geom.geom_type, feature_count=1,
        bbox=list(geom.extent), metadata={'reference': True},
    )
    Feature.objects.create(layer=layer, geom=geom, properties={'nom': 'France métropolitaine'})
    return layer


def build_stations_layer(stations: list[dict], grandeur: str, indicateur_label: str,
                         year: int, owner_email: str = '') -> Layer:
    """
    Crée la couche ponctuelle des stations (4.1). Chaque station :
        {'lon', 'lat', 'valeur', 'id_station', 'nom', 'qualite'}
    'valeur' = indicateur déjà calculé (peut être None — la station reste quand
    même affichée, avec ses métriques de qualité en propriétés : c'est souvent
    justement ce qui explique l'absence de valeur, ex. 0% de complétude).
    Seules les stations sans coordonnées (jamais censé arriver, cf. filtrage à la
    collecte) sont réellement exclues, faute de pouvoir les placer sur la carte.
    """
    layer = Layer.objects.create(
        name=f"Stations {grandeur} {year} — {indicateur_label}",
        layer_type=Layer.VECTOR, origin=Layer.ORIGIN_METEO, srid_source=4326,
        geom_type='Point', owner_email=owner_email,
        metadata={'grandeur': grandeur, 'annee': year, 'indicateur': indicateur_label},
    )
    feats = []
    xs, ys = [], []
    for s in stations:
        if s.get('lon') is None or s.get('lat') is None:
            continue
        q = s.get('qualite') or {}
        pt = GEOSGeometry(f"POINT({s['lon']} {s['lat']})", srid=4326)
        feats.append(Feature(layer=layer, geom=pt, properties={
            'id_station': s.get('id_station'),
            'nom': s.get('nom'),
            'valeur': s.get('valeur'),
            'indicateur': indicateur_label,
            'qualite_taux_completude': q.get('taux_completude'),
            'qualite_heures_couvertes': q.get('heures_couvertes'),
            'qualite_heures_annee': q.get('heures_annee'),
            'qualite_trou_max_heures': q.get('trou_max_heures'),
            'qualite_max_releves_meme_datetime': q.get('max_releves_meme_datetime'),
            'qualite_duplicatas_datetime': q.get('duplicatas_datetime'),
        }))
        xs.append(s['lon'])
        ys.append(s['lat'])
    Feature.objects.bulk_create(feats, batch_size=1000)
    layer.feature_count = len(feats)
    if xs:
        layer.bbox = [min(xs), min(ys), max(xs), max(ys)]
    layer.save(update_fields=['feature_count', 'bbox'])
    return layer


def build_choropleth(stations_layer: Layer, title: str,
                     classification: str = 'quantiles', n_classes: int = 5,
                     ramp: str = 'YlOrRd', boundary_layer: Layer | None = None,
                     owner_email: str = '',
                     included_station_ids: set[str] | None = None) -> Layer:
    """
    Voronoï → clip France → classification. Renvoie la couche choroplèthe finale.

    `included_station_ids`, si fourni, restreint les points admis dans le Voronoï
    à ce sous-ensemble (ex. filtrage qualité) : `stations_layer` elle-même garde
    TOUTES ses stations intactes — seule l'entrée du Voronoï, donc la choroplèthe
    résultante, est restreinte. Le filtrage se fait via une couche temporaire
    (créée puis supprimée, comme le Voronoï intermédiaire) : les opérations de
    `processing.py` travaillent toujours sur une couche entière en base, pas sur
    un sous-ensemble de features passé en mémoire.
    """
    boundary = boundary_layer or ensure_france_layer()

    voronoi_input = stations_layer
    filtered_layer = None
    if included_station_ids is not None:
        feats = list(stations_layer.features.all())
        kept = [f for f in feats if f.properties.get('id_station') in included_station_ids]
        filtered_layer = Layer.objects.create(
            name=f"{title} — stations filtrées (qualité)", layer_type=Layer.VECTOR,
            origin=Layer.ORIGIN_CALCUL, srid_source=4326, geom_type='Point',
            owner_email=owner_email,
        )
        Feature.objects.bulk_create(
            [Feature(layer=filtered_layer, geom=f.geom, properties=f.properties) for f in kept],
            batch_size=1000,
        )
        filtered_layer.feature_count = len(kept)
        filtered_layer.save(update_fields=['feature_count'])
        voronoi_input = filtered_layer

    # 4.3 — Voronoï des stations (reprend la valeur de chaque station).
    try:
        vor = processing.run_operation('voronoi', [voronoi_input], {},
                                       out_name=f"{title} — Voronoï", owner_email=owner_email)
    finally:
        if filtered_layer is not None:
            filtered_layer.delete()
    try:
        # Clip sur la France métropolitaine.
        clipped = processing.run_operation('clip', [vor, boundary], {},
                                           out_name=title, owner_email=owner_email)
    finally:
        vor.delete()  # intermédiaire

    # 4.4 — Classification + couleurs + légende.
    feats = list(clipped.features.all())
    values = [f.properties.get('valeur') for f in feats]
    breaks = choropleth.classify(values, classification, n_classes)
    colors = choropleth.ramp_colors(ramp, max(1, len(breaks) - 1))
    for f in feats:
        idx = choropleth.class_index(f.properties.get('valeur'), breaks)
        f.properties['__class'] = idx
        f.properties['__color'] = colors[idx] if idx < len(colors) else colors[-1]
    Feature.objects.bulk_update(feats, ['properties'], batch_size=1000)

    clipped.origin = Layer.ORIGIN_CALCUL
    clipped.metadata = {
        **clipped.metadata,
        'choropleth': {
            'title': title,
            'classification': classification,
            'n_classes': n_classes,
            'ramp': ramp,
            'breaks': [round(b, 4) for b in breaks],
            'colors': colors,
            'legend': choropleth.build_legend(breaks, colors),
            'value_field': 'valeur',
        },
        'grandeur': stations_layer.metadata.get('grandeur'),
        'annee': stations_layer.metadata.get('annee'),
    }
    clipped.save(update_fields=['origin', 'metadata'])
    return clipped
