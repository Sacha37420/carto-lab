"""
Matérialisation d'une couche « publiable QGIS » (Lot 4 / points 5 & 6).

Une couche calculée (table générique geometry+JSONB) est copiée en une VRAIE table
PostGIS typée dans le schéma dédié carto_public : géométrie geometry(Geometry,4326)
indexée GIST + colonnes d'attributs à plat (issues du JSONB). pg_featureserv, connecté
avec le rôle read-only scopé, expose alors cette table en OGC API – Features.
"""
import re

from django.conf import settings
from django.db import connection, transaction

from .models import Layer

SCHEMA = 'carto_public'
RESERVED = {'fid', 'geom'}
MAX_COLS = 100


class PublishError(Exception):
    pass


def _ident(s: str, fallback: str = 'col') -> str:
    out = re.sub(r'[^a-z0-9_]', '_', (s or '').lower()).strip('_')
    if not out or out[0].isdigit():
        out = f'_{out}' if out else fallback
    return out[:60]


def table_name(layer: Layer) -> str:
    return f"pub_{layer.id}_{_ident(layer.name, 'layer')}"[:60]


def _infer_columns(layer: Layer) -> dict[str, bool]:
    """Renvoie {clé_propriété: is_numeric} en balayant les entités."""
    keys: dict[str, bool] = {}
    for props in layer.features.values_list('properties', flat=True).iterator():
        if not isinstance(props, dict):
            continue
        for k, v in props.items():
            if v is None or v == '':
                keys.setdefault(k, True)
                continue
            is_num = isinstance(v, (int, float)) and not isinstance(v, bool)
            if isinstance(v, str):
                try:
                    float(v)
                    is_num = True
                except ValueError:
                    is_num = False
            keys[k] = keys.get(k, True) and is_num
        if len(keys) > MAX_COLS:
            break
    return dict(list(keys.items())[:MAX_COLS])


@transaction.atomic
def publish_layer(layer: Layer) -> dict:
    if layer.layer_type != Layer.VECTOR:
        raise PublishError("Seules les couches vectorielles sont publiables.")
    if not layer.features.exists():
        raise PublishError("Couche vide : rien à publier.")

    table = table_name(layer)
    keys = _infer_columns(layer)

    # Mappe chaque clé JSONB vers un nom de colonne unique et sûr.
    colmap: list[tuple[str, str, bool]] = []  # (clé, colonne, is_numeric)
    used = set(RESERVED)
    for key, is_num in keys.items():
        col = _ident(key, 'attr')
        base = col
        i = 1
        while col in used:
            col = f"{base}_{i}"; i += 1
        used.add(col)
        colmap.append((key, col, is_num))

    col_defs = ", ".join(
        f'"{col}" {"double precision" if is_num else "text"}' for _, col, is_num in colmap
    )
    select_cols = ", ".join(
        (f"NULLIF(properties->>'{key.replace(chr(39), chr(39) * 2)}','')::double precision"
         if is_num else
         f"properties->>'{key.replace(chr(39), chr(39) * 2)}'")
        for key, _, is_num in colmap
    )
    insert_cols = ", ".join(f'"{col}"' for _, col, _ in colmap)

    reader = settings.CARTO_READER_USER
    with connection.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS {SCHEMA}."{table}" CASCADE')
        cur.execute(
            f'CREATE TABLE {SCHEMA}."{table}" ('
            f'  fid bigserial PRIMARY KEY,'
            f'  geom geometry(Geometry,4326)'
            f'{", " + col_defs if col_defs else ""}'
            f')'
        )
        insert_sql = (
            f'INSERT INTO {SCHEMA}."{table}" (geom{", " + insert_cols if insert_cols else ""}) '
            f'SELECT geom{", " + select_cols if select_cols else ""} '
            f'FROM carto_lab.features WHERE layer_id = %s'
        )
        cur.execute(insert_sql, [layer.id])
        cur.execute(f'CREATE INDEX "{table}_geom_gist" ON {SCHEMA}."{table}" USING gist (geom)')
        # Lecture seule explicite pour pg_featureserv.
        if re.match(r'^[a-z_][a-z0-9_]*$', reader):
            cur.execute(f'GRANT SELECT ON {SCHEMA}."{table}" TO {reader}')

    collection = f"{SCHEMA}.{table}"
    layer.published_qgis = True
    layer.metadata = {
        **layer.metadata,
        'published': {'schema': SCHEMA, 'table': table, 'collection': collection},
    }
    layer.save(update_fields=['published_qgis', 'metadata'])
    return connection_info(layer)


@transaction.atomic
def unpublish_layer(layer: Layer) -> None:
    pub = (layer.metadata or {}).get('published') or {}
    table = pub.get('table') or table_name(layer)
    with connection.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS {SCHEMA}."{table}" CASCADE')
    layer.published_qgis = False
    meta = dict(layer.metadata or {})
    meta.pop('published', None)
    layer.metadata = meta
    layer.save(update_fields=['published_qgis', 'metadata'])


def connection_info(layer: Layer) -> dict:
    domain = settings.DOMAIN
    path = settings.OGC_SERVICE_PATH
    base = (f"https://{domain}/{path}" if domain and domain != 'CHANGE_ME' else f"/{path}")
    pub = (layer.metadata or {}).get('published') or {}
    collection = pub.get('collection')
    items = f"{base}/collections/{collection}/items" if collection else None
    return {
        'published': layer.published_qgis,
        'service_type': 'OGC API - Features',
        'ogc_url': base,
        'collections_url': f"{base}/collections",
        'collection': collection,
        'items_url': items,
        'qgis_steps': [
            "QGIS → Explorateur → clic droit sur « OGC API - Features » → Nouvelle connexion.",
            f"URL du service : {base}",
            "Authentification : onglet « Authentification » → Ajouter → type OAuth2, "
            "grant « Authorization Code », Request URL/Token URL du realm ssolab, "
            "client public — la session cookie oauth2-proxy protège le service.",
            f"Développer la connexion et charger la collection « {collection} ».",
        ],
    }
