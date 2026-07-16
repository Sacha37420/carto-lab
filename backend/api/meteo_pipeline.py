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
        {'lon', 'lat', 'valeur', 'id_station', 'nom'}
    'valeur' = indicateur déjà calculé (peut être None → station ignorée).
    """
    layer = Layer.objects.create(
        name=f"Stations {grandeur} {year} — {indicateur_label}",
        layer_type=Layer.VECTOR, origin=Layer.ORIGIN_METEO, srid_source=4326,
        geom_type='Point', owner_email=owner_email,
        metadata={'grandeur': grandeur, 'annee': year, 'indicateur': indicateur_label},
    )
    feats = []
    for s in stations:
        if s.get('valeur') is None or s.get('lon') is None or s.get('lat') is None:
            continue
        pt = GEOSGeometry(f"POINT({s['lon']} {s['lat']})", srid=4326)
        feats.append(Feature(layer=layer, geom=pt, properties={
            'id_station': s.get('id_station'),
            'nom': s.get('nom'),
            'valeur': s['valeur'],
            'indicateur': indicateur_label,
        }))
    Feature.objects.bulk_create(feats, batch_size=1000)
    layer.feature_count = len(feats)
    if feats:
        xs = [s['lon'] for s in stations if s.get('valeur') is not None and s.get('lon') is not None]
        ys = [s['lat'] for s in stations if s.get('valeur') is not None and s.get('lat') is not None]
        layer.bbox = [min(xs), min(ys), max(xs), max(ys)]
    layer.save(update_fields=['feature_count', 'bbox'])
    return layer


def build_choropleth(stations_layer: Layer, title: str,
                     classification: str = 'quantiles', n_classes: int = 5,
                     ramp: str = 'YlOrRd', boundary_layer: Layer | None = None,
                     owner_email: str = '') -> Layer:
    """Voronoï → clip France → classification. Renvoie la couche choroplèthe finale."""
    boundary = boundary_layer or ensure_france_layer()

    # 4.3 — Voronoï des stations (reprend la valeur de chaque station).
    vor = processing.run_operation('voronoi', [stations_layer], {},
                                   out_name=f"{title} — Voronoï", owner_email=owner_email)
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
