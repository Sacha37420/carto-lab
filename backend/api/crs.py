"""
Systèmes de coordonnées (Feature 2).

Support de tout code EPSG via pyproj/PROJ. On expose une liste curatée des CRS
français prioritaires pour l'UI, mais toute la logique valide n'importe quel EPSG.
"""
from functools import lru_cache

from pyproj import CRS, Transformer
from pyproj.exceptions import CRSError

# CRS français prioritaires (cf. to_do point 2) — proposés en tête d'UI.
# La liste n'est PAS limitative : validate_srid accepte n'importe quel EPSG.
COMMON_CRS = [
    {'srid': 4326, 'name': 'WGS 84 (géographique)', 'group': 'Mondial'},
    {'srid': 3857, 'name': 'Web Mercator', 'group': 'Mondial'},
    {'srid': 2154, 'name': 'Lambert-93 (RGF93)', 'group': 'France'},
    # Coniques Conformes Zone (CC42 → CC50), EPSG:3942 à 3950.
    {'srid': 3942, 'name': 'RGF93 / CC42', 'group': 'France (CC)'},
    {'srid': 3943, 'name': 'RGF93 / CC43', 'group': 'France (CC)'},
    {'srid': 3944, 'name': 'RGF93 / CC44', 'group': 'France (CC)'},
    {'srid': 3945, 'name': 'RGF93 / CC45', 'group': 'France (CC)'},
    {'srid': 3946, 'name': 'RGF93 / CC46', 'group': 'France (CC)'},
    {'srid': 3947, 'name': 'RGF93 / CC47', 'group': 'France (CC)'},
    {'srid': 3948, 'name': 'RGF93 / CC48', 'group': 'France (CC)'},
    {'srid': 3949, 'name': 'RGF93 / CC49', 'group': 'France (CC)'},
    {'srid': 3950, 'name': 'RGF93 / CC50', 'group': 'France (CC)'},
    {'srid': 32631, 'name': 'WGS 84 / UTM 31N', 'group': 'UTM'},
    {'srid': 32632, 'name': 'WGS 84 / UTM 32N', 'group': 'UTM'},
    {'srid': 27572, 'name': 'NTF / Lambert II étendu (hist.)', 'group': 'France'},
]


@lru_cache(maxsize=512)
def get_crs(srid: int) -> CRS:
    """Retourne l'objet pyproj CRS pour un EPSG, ou lève ValueError si inconnu."""
    try:
        return CRS.from_epsg(int(srid))
    except (CRSError, ValueError) as exc:
        raise ValueError(f"EPSG:{srid} inconnu ou invalide") from exc


def describe_srid(srid: int) -> dict:
    """Métadonnées lisibles d'un EPSG (nom, unité, type projeté/géographique)."""
    crs = get_crs(srid)
    axis = crs.axis_info[0] if crs.axis_info else None
    return {
        'srid': int(srid),
        'name': crs.name,
        'projected': crs.is_projected,
        'geographic': crs.is_geographic,
        'unit': axis.unit_name if axis else None,
    }


def validate_srid(srid) -> int:
    """Valide et normalise un EPSG fourni par l'utilisateur."""
    get_crs(srid)  # lève ValueError si invalide
    return int(srid)


@lru_cache(maxsize=1024)
def _transformer(from_srid: int, to_srid: int) -> Transformer:
    # always_xy=True : ordre (lon/x, lat/y) cohérent quel que soit le CRS.
    return Transformer.from_crs(get_crs(from_srid), get_crs(to_srid), always_xy=True)


def transform_point(x: float, y: float, from_srid: int, to_srid: int) -> tuple[float, float]:
    """Transforme un point (x, y) d'un CRS vers un autre."""
    tx, ty = _transformer(int(from_srid), int(to_srid)).transform(x, y)
    return tx, ty
