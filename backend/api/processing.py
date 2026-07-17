"""
Moteur de calculs géo/carto (Feature 3).

Bibliothèque de traitements composables. Chaque traitement est un « nœud » avec
un nombre d'entrées (couches) typé et des paramètres déclarés (le frontend rend
le formulaire à partir de OPERATIONS). L'exécution se fait EN BASE via des
fonctions PostGIS ST_* (INSERT ... SELECT), ce qui reste performant et évite de
rapatrier les géométries. Le résultat est TOUJOURS une nouvelle couche (Layer +
Features), réutilisable comme entrée d'un autre traitement (cf. recettes).

Sécurité : les identifiants de couche sont passés en paramètres liés (jamais
concaténés) ; seuls les noms d'opérations du registre sont exécutables.
"""
from django.db import connection, transaction

from .models import Layer


class ProcessingError(Exception):
    """Erreur fonctionnelle d'un traitement (message destiné à l'utilisateur)."""


# Registre rempli par le décorateur @operation.
OPERATIONS: dict[str, dict] = {}


def operation(name, label, description, inputs, params, output_geom=''):
    def wrap(func):
        OPERATIONS[name] = {
            'name': name,
            'label': label,
            'description': description,
            'inputs': inputs,          # nombre de couches d'entrée
            'params': params,          # [{name,type,label,default,options?}]
            'output_geom': output_geom,
            'func': func,
        }
        return func
    return wrap


def catalog() -> list[dict]:
    """Description JSON-sérialisable des opérations (sans les callables)."""
    return [
        {k: v for k, v in op.items() if k != 'func'}
        for op in OPERATIONS.values()
    ]


# ──────────────────────────────────────────────────────────────────────────────
# Aides
# ──────────────────────────────────────────────────────────────────────────────
def _num(params, key, default):
    try:
        return float(params.get(key, default))
    except (TypeError, ValueError):
        raise ProcessingError(f"Paramètre '{key}' invalide (nombre attendu).")


def _finalize(cur, layer: Layer):
    """Recalcule count / bbox / geom_type de la couche produite."""
    cur.execute("SELECT count(*) FROM features WHERE layer_id=%s", [layer.id])
    count = cur.fetchone()[0]
    bbox = None
    geom_type = ''
    if count:
        cur.execute(
            "SELECT ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e) "
            "FROM (SELECT ST_Extent(geom) e FROM features WHERE layer_id=%s) t",
            [layer.id],
        )
        bbox = list(cur.fetchone())
        cur.execute("SELECT GeometryType(geom) FROM features WHERE layer_id=%s LIMIT 1", [layer.id])
        geom_type = (cur.fetchone() or [''])[0] or ''
    layer.feature_count = count
    layer.bbox = bbox
    layer.geom_type = geom_type
    layer.save(update_fields=['feature_count', 'bbox', 'geom_type'])
    if count == 0:
        raise ProcessingError("Le traitement n'a produit aucune entité.")


def run_operation(name: str, input_layers: list[Layer], params: dict,
                  out_name: str = '', owner_email: str = '') -> Layer:
    """Exécute une opération et renvoie la nouvelle couche."""
    op = OPERATIONS.get(name)
    if op is None:
        raise ProcessingError(f"Opération inconnue : '{name}'.")
    if len(input_layers) != op['inputs']:
        raise ProcessingError(
            f"L'opération '{name}' attend {op['inputs']} couche(s) d'entrée, "
            f"{len(input_layers)} fournie(s)."
        )

    out = Layer.objects.create(
        name=out_name or f"{op['label']} — {input_layers[0].name if input_layers else ''}".strip(' —'),
        layer_type=Layer.VECTOR,
        origin=Layer.ORIGIN_CALCUL,
        srid_source=4326,
        owner_email=owner_email,
        metadata={'operation': name, 'inputs': [l.id for l in input_layers], 'params': params},
    )
    try:
        with transaction.atomic():
            with connection.cursor() as cur:
                op['func'](cur, out.id, input_layers, params)
                _finalize(cur, out)
    except ProcessingError:
        out.delete()
        raise
    except Exception as exc:  # SQL/PostGIS
        out.delete()
        raise ProcessingError(f"Échec du traitement : {exc}") from exc
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Opérations — 1 entrée
# ──────────────────────────────────────────────────────────────────────────────
@operation('buffer', 'Tampon (buffer)', "Zone tampon à distance fixe (mètres, géodésique).",
           1, [{'name': 'distance_m', 'type': 'number', 'label': 'Distance (m)', 'default': 100}],
           'Polygon')
def _buffer(cur, out, ins, params):
    dist = _num(params, 'distance_m', 100)
    cur.execute(
        "INSERT INTO features (layer_id, geom, properties) "
        "SELECT %s, ST_Buffer(geom::geography, %s)::geometry, properties "
        "FROM features WHERE layer_id=%s",
        [out, dist, ins[0].id],
    )


@operation('centroid', 'Centroïdes', "Point central de chaque entité.",
           1, [], 'Point')
def _centroid(cur, out, ins, params):
    cur.execute(
        "INSERT INTO features (layer_id, geom, properties) "
        "SELECT %s, ST_Centroid(geom), properties FROM features WHERE layer_id=%s",
        [out, ins[0].id],
    )


@operation('convex_hull', 'Enveloppe convexe', "Plus petit polygone convexe englobant toutes les entités.",
           1, [], 'Polygon')
def _convex_hull(cur, out, ins, params):
    cur.execute(
        "INSERT INTO features (layer_id, geom, properties) "
        "SELECT %s, ST_ConvexHull(ST_Collect(geom)), '{}'::jsonb "
        "FROM features WHERE layer_id=%s",
        [out, ins[0].id],
    )


@operation('envelope', 'Emprises (bounding box)', "Rectangle englobant de chaque entité.",
           1, [], 'Polygon')
def _envelope(cur, out, ins, params):
    cur.execute(
        "INSERT INTO features (layer_id, geom, properties) "
        "SELECT %s, ST_Envelope(geom), properties FROM features WHERE layer_id=%s",
        [out, ins[0].id],
    )


@operation('simplify', 'Simplification', "Simplifie la géométrie (Douglas-Peucker, tolérance en degrés).",
           1, [{'name': 'tolerance', 'type': 'number', 'label': 'Tolérance (°)', 'default': 0.001}])
def _simplify(cur, out, ins, params):
    tol = _num(params, 'tolerance', 0.001)
    cur.execute(
        "INSERT INTO features (layer_id, geom, properties) "
        "SELECT %s, ST_SimplifyPreserveTopology(geom, %s), properties "
        "FROM features WHERE layer_id=%s AND geom IS NOT NULL",
        [out, tol, ins[0].id],
    )


@operation('dissolve', 'Union (dissoudre)', "Fusionne toutes les entités en une seule géométrie.",
           1, [])
def _dissolve(cur, out, ins, params):
    cur.execute(
        "INSERT INTO features (layer_id, geom, properties) "
        "SELECT %s, ST_Union(geom), '{}'::jsonb FROM features WHERE layer_id=%s",
        [out, ins[0].id],
    )


@operation('measure', 'Mesures (aire/périmètre/longueur)',
           "Ajoute aire_m2 / perimetre_m / longueur_m (géodésiques) aux attributs.",
           1, [])
def _measure(cur, out, ins, params):
    cur.execute(
        "INSERT INTO features (layer_id, geom, properties) "
        "SELECT %s, geom, properties || jsonb_build_object("
        "  'aire_m2', ST_Area(geom::geography),"
        "  'perimetre_m', ST_Perimeter(geom::geography),"
        "  'longueur_m', ST_Length(geom::geography)) "
        "FROM features WHERE layer_id=%s",
        [out, ins[0].id],
    )


@operation('voronoi', 'Voronoï / Thiessen', "Polygones de Voronoï des points (attributs du point repris).",
           1, [], 'Polygon')
def _voronoi(cur, out, ins, params):
    cur.execute(
        "WITH pts AS (SELECT geom, properties FROM features "
        "             WHERE layer_id=%s AND GeometryType(geom)='POINT'), "
        "vor AS (SELECT (ST_Dump(ST_VoronoiPolygons(ST_Collect(geom)))).geom AS geom FROM pts) "
        "INSERT INTO features (layer_id, geom, properties) "
        "SELECT %s, ST_SetSRID(v.geom, 4326), COALESCE(p.properties, '{}'::jsonb) "
        "FROM vor v LEFT JOIN pts p ON ST_Contains(ST_SetSRID(v.geom,4326), p.geom)",
        [ins[0].id, out],
    )


@operation('grid', 'Grille régulière', "Grille carrée couvrant l'emprise de la couche (taille en degrés).",
           1, [{'name': 'cell_deg', 'type': 'number', 'label': 'Taille de maille (°)', 'default': 0.1}],
           'Polygon')
def _grid(cur, out, ins, params):
    cell = _num(params, 'cell_deg', 0.1)
    cur.execute(
        "WITH ext AS (SELECT ST_SetSRID(ST_Extent(geom), 4326) e FROM features WHERE layer_id=%s) "
        "INSERT INTO features (layer_id, geom, properties) "
        "SELECT %s, (ST_SquareGrid(%s, e)).geom, '{}'::jsonb FROM ext",
        [ins[0].id, out, cell],
    )


# ──────────────────────────────────────────────────────────────────────────────
# Opérations — 2 entrées
# ──────────────────────────────────────────────────────────────────────────────
@operation('clip', 'Découpage (clip)', "Intersecte la 1re couche par l'emprise de la 2e (masque).",
           2, [])
def _clip(cur, out, ins, params):
    cur.execute(
        "WITH mask AS (SELECT ST_Union(geom) g FROM features WHERE layer_id=%s) "
        "INSERT INTO features (layer_id, geom, properties) "
        "SELECT %s, gi, props FROM ("
        "  SELECT ST_Intersection(f.geom, m.g) gi, f.properties props "
        "  FROM features f, mask m WHERE f.layer_id=%s AND ST_Intersects(f.geom, m.g)"
        ") s WHERE NOT ST_IsEmpty(gi)",
        [ins[1].id, out, ins[0].id],
    )


@operation('difference', 'Différence', "Retire de la 1re couche la géométrie de la 2e.",
           2, [])
def _difference(cur, out, ins, params):
    cur.execute(
        "WITH mask AS (SELECT ST_Union(geom) g FROM features WHERE layer_id=%s) "
        "INSERT INTO features (layer_id, geom, properties) "
        "SELECT %s, gd, props FROM ("
        "  SELECT ST_Difference(f.geom, m.g) gd, f.properties props "
        "  FROM features f, mask m WHERE f.layer_id=%s"
        ") s WHERE NOT ST_IsEmpty(gd)",
        [ins[1].id, out, ins[0].id],
    )


@operation('spatial_join', 'Jointure spatiale', "Reporte sur chaque entité de la 1re couche les attributs de la 2e qui la contient.",
           2, [])
def _spatial_join(cur, out, ins, params):
    cur.execute(
        "INSERT INTO features (layer_id, geom, properties) "
        "SELECT %s, a.geom, a.properties || COALESCE(b.properties, '{}'::jsonb) "
        "FROM features a LEFT JOIN features b "
        "  ON b.layer_id=%s AND ST_Intersects(b.geom, a.geom) "
        "WHERE a.layer_id=%s",
        [out, ins[1].id, ins[0].id],
    )


# Cardinalité maximale du champ de catégorie : au-delà, un champ à forte
# cardinalité (ex. identifiant quasi-unique) générerait des centaines
# d'attributs par polygone — mieux vaut échouer clairement qu'en silence.
MAX_TABULATE_CATEGORIES = 50


@operation('tabulate_intersection', 'Tableau croisé de surfaces (par catégorie)',
           "Pour chaque polygone de la 1re couche, ajoute un attribut par valeur distincte du "
           "champ choisi de la 2e couche, valant la surface (m², géodésique) d'intersection "
           "avec les entités de cette catégorie.",
           2, [
               {'name': 'field', 'type': 'text', 'label': 'Champ de catégorie (2e couche)'},
               {'name': 'prefix', 'type': 'text', 'label': 'Préfixe des attributs', 'default': ''},
           ])
def _tabulate_intersection(cur, out, ins, params):
    field = str(params.get('field', '')).strip()
    prefix = str(params.get('prefix', ''))
    if not field:
        raise ProcessingError("Paramètre 'field' requis (champ de catégorie de la 2e couche).")

    cur.execute(
        "SELECT DISTINCT properties->>%s FROM features "
        "WHERE layer_id=%s AND properties->>%s IS NOT NULL",
        [field, ins[1].id, field],
    )
    values = [r[0] for r in cur.fetchall()]
    if not values:
        raise ProcessingError(f"Aucune valeur trouvée pour le champ « {field} » dans la 2e couche.")
    if len(values) > MAX_TABULATE_CATEGORIES:
        raise ProcessingError(
            f"Le champ « {field} » a {len(values)} valeurs distinctes "
            f"(max {MAX_TABULATE_CATEGORIES}) — choisissez un champ moins fin."
        )

    # Optimisation : sans filtrage, il faudrait appeler ST_Intersection (coûteux)
    # sur TOUTES les paires (polygone de la 1re couche × union de catégorie), y
    # compris celles dont les emprises ne se touchent même pas. `hits` ne visite
    # que les paires dont les bbox se recoupent (`&&`, exploite l'index GiST de
    # features.geom — vérifié via EXPLAIN) et confirmé par `ST_Intersects`,
    # avant de calculer l'aire exacte. `all_pairs` reste nécessaire pour garantir
    # un attribut à 0 (et non absent) sur les catégories sans recouvrement, mais
    # ne manipule que des (id, texte) — aucune géométrie, donc peu coûteux même
    # à grande échelle.
    cur.execute(
        "WITH cats AS ("
        "  SELECT DISTINCT properties->>%(field)s AS val FROM features "
        "  WHERE layer_id=%(l2)s AND properties->>%(field)s IS NOT NULL"
        "), unions AS ("
        "  SELECT c.val, ST_Union(f.geom) AS geom FROM cats c "
        "  JOIN features f ON f.layer_id=%(l2)s AND f.properties->>%(field)s = c.val "
        "  GROUP BY c.val"
        "), hits AS ("
        "  SELECT l1.id AS fid, u.val, "
        "    ST_Area(ST_CollectionExtract(ST_Intersection(l1.geom, u.geom), 3)::geography) AS area "
        "  FROM features l1 "
        "  JOIN unions u ON l1.geom && u.geom AND ST_Intersects(l1.geom, u.geom) "
        "  WHERE l1.layer_id=%(l1)s"
        "), all_pairs AS ("
        "  SELECT l1.id AS fid, c.val FROM features l1 CROSS JOIN cats c WHERE l1.layer_id=%(l1)s"
        ") "
        "INSERT INTO features (layer_id, geom, properties) "
        "SELECT %(out)s, l1.geom, "
        "  l1.properties || jsonb_object_agg(%(prefix)s || p.val, COALESCE(h.area, 0)) "
        "FROM all_pairs p "
        "JOIN features l1 ON l1.id = p.fid "
        "LEFT JOIN hits h ON h.fid = p.fid AND h.val = p.val "
        "GROUP BY l1.id, l1.geom, l1.properties",
        {'field': field, 'prefix': prefix, 'l1': ins[0].id, 'l2': ins[1].id, 'out': out},
    )
