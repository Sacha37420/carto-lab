"""
Import de couches SIG (Feature 1).

Parsing vectoriel via GeoDjango GDAL (django.contrib.gis.gdal.DataSource) qui
s'appuie sur la libgdal SYSTÈME (gdal-bin) — donc l'ensemble complet des pilotes
(GeoJSON, Shapefile, GeoPackage, KML/KMZ, GML). Le CSV lat/lon est géré à part
(pandas/csv), et le raster GeoTIFF via rasterio.

Sécurité (cf. to_do « SÉCURITÉ ») : allow-list d'extensions, taille maximale,
noms de fichiers neutralisés (pas de path traversal — on écrit dans un répertoire
temporaire avec un nom généré), aucune exécution du contenu importé.
"""
import csv as csv_module
import os
import tempfile
import zipfile

from django.conf import settings
from django.contrib.gis.gdal import DataSource
from django.contrib.gis.geos import GEOSGeometry, Point

from .crs import validate_srid

MAX_UPLOAD_BYTES = getattr(settings, 'MAX_UPLOAD_BYTES', 100 * 1024 * 1024)  # 100 Mo

VECTOR_EXTS = {'.geojson', '.json', '.gpkg', '.kml', '.kmz', '.gml', '.zip', '.csv'}
RASTER_EXTS = {'.tif', '.tiff'}
ALLOWED_EXTS = VECTOR_EXTS | RASTER_EXTS

# Colonnes candidates pour un CSV ponctuel (insensible à la casse).
_LAT_KEYS = {'lat', 'latitude', 'y'}
_LON_KEYS = {'lon', 'lng', 'long', 'longitude', 'x'}


class LayerImportError(Exception):
    """Erreur d'import fonctionnelle (message destiné à l'utilisateur)."""


# ──────────────────────────────────────────────────────────────────────────────
# Aides
# ──────────────────────────────────────────────────────────────────────────────
def _bbox_and_type(features):
    """Emprise [minx,miny,maxx,maxy] et type géométrique dominant (en 4326)."""
    minx = miny = float('inf')
    maxx = maxy = float('-inf')
    gtype = ''
    for geom, _props in features:
        x0, y0, x1, y1 = geom.extent
        minx, miny = min(minx, x0), min(miny, y0)
        maxx, maxy = max(maxx, x1), max(maxy, y1)
        gtype = gtype or geom.geom_type
    if minx == float('inf'):
        return None, gtype
    return [minx, miny, maxx, maxy], gtype


def _extract_kml_from_kmz(kmz_path: str, workdir: str) -> str:
    with zipfile.ZipFile(kmz_path) as zf:
        kml_names = [n for n in zf.namelist() if n.lower().endswith('.kml')]
        if not kml_names:
            raise LayerImportError("KMZ sans fichier .kml interne.")
        # doc.kml en priorité, sinon le premier .kml
        target = next((n for n in kml_names if n.lower().endswith('doc.kml')), kml_names[0])
        zf.extract(target, workdir)
        return os.path.join(workdir, target)


# ──────────────────────────────────────────────────────────────────────────────
# Parsing vectoriel
# ──────────────────────────────────────────────────────────────────────────────
def _parse_ogr(source: str, force_srid=None):
    """
    Lit une source OGR, reprojette en 4326, renvoie (features, source_srid).
    `features` = liste de (GEOSGeometry 4326, dict properties).
    """
    ds = DataSource(source)
    if len(ds) == 0:
        raise LayerImportError("Aucune couche lisible dans le fichier.")
    layer = ds[0]

    srs = layer.srs
    source_srid = None
    if srs is not None and srs.srid:
        source_srid = int(srs.srid)
    elif force_srid is not None:
        source_srid = validate_srid(force_srid)
    else:
        raise LayerImportError(
            "CRS d'origine introuvable (pas de .prj / métadonnées). "
            "Fournissez 'source_srid' pour le forcer."
        )

    features = []
    fields = layer.fields
    for feat in layer:
        ogr_geom = feat.geom
        if ogr_geom is None or not ogr_geom.wkt:
            continue
        if source_srid != 4326:
            ogr_geom.transform(4326)
        geos = GEOSGeometry(ogr_geom.wkt, srid=4326)
        props = {}
        for fld in fields:
            try:
                props[fld] = feat.get(fld)
            except Exception:
                props[fld] = None
        features.append((geos, props))

    if not features:
        raise LayerImportError("Aucune entité géométrique valide trouvée.")
    return features, source_srid


def _parse_csv(path: str, force_srid=None):
    """CSV ponctuel : détecte lat/lon, construit des points, reprojette en 4326."""
    source_srid = validate_srid(force_srid) if force_srid is not None else 4326

    with open(path, newline='', encoding='utf-8-sig') as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv_module.Sniffer().sniff(sample, delimiters=',;\t')
        except csv_module.Error:
            dialect = csv_module.excel
        reader = csv_module.DictReader(f, dialect=dialect)
        headers = reader.fieldnames or []
        lower = {h.lower().strip(): h for h in headers}
        lat_col = next((lower[k] for k in _LAT_KEYS if k in lower), None)
        lon_col = next((lower[k] for k in _LON_KEYS if k in lower), None)
        if not lat_col or not lon_col:
            raise LayerImportError(
                "CSV : colonnes latitude/longitude introuvables "
                "(attendu lat/latitude/y et lon/lng/longitude/x)."
            )
        features = []
        for row in reader:
            try:
                x = float(str(row[lon_col]).replace(',', '.'))
                y = float(str(row[lat_col]).replace(',', '.'))
            except (TypeError, ValueError):
                continue
            pt = Point(x, y, srid=source_srid)
            if source_srid != 4326:
                pt.transform(4326)
            props = {k: v for k, v in row.items() if k not in (lat_col, lon_col)}
            features.append((pt, props))

    if not features:
        raise LayerImportError("CSV : aucune ligne avec des coordonnées valides.")
    return features, source_srid


# ──────────────────────────────────────────────────────────────────────────────
# Orchestration
# ──────────────────────────────────────────────────────────────────────────────
def import_layer(uploaded_file, name: str, force_srid=None, owner_email: str = ''):
    """
    Importe un fichier uploadé et crée la couche + entités correspondantes.
    Retourne l'instance Layer. Lève LayerImportError en cas d'entrée invalide.
    """
    from .models import Feature, Layer  # import tardif : évite un cycle au chargement

    ext = os.path.splitext(uploaded_file.name)[1].lower()
    if ext not in ALLOWED_EXTS:
        raise LayerImportError(
            f"Extension '{ext}' non supportée. Formats acceptés : "
            f"{', '.join(sorted(ALLOWED_EXTS))}."
        )
    size = getattr(uploaded_file, 'size', None)
    if size is not None and size > MAX_UPLOAD_BYTES:
        raise LayerImportError(
            f"Fichier trop volumineux ({size} octets, max {MAX_UPLOAD_BYTES})."
        )

    # ── Raster : persistance du fichier + métadonnées, pas d'entités ──────────
    if ext in RASTER_EXTS:
        return _import_raster(uploaded_file, name, owner_email)

    # ── Vecteur : écriture dans un tmp neutre, parsing OGR/CSV ────────────────
    with tempfile.TemporaryDirectory() as workdir:
        safe_name = 'upload' + ext
        tmp_path = os.path.join(workdir, safe_name)
        with open(tmp_path, 'wb') as out:
            for chunk in uploaded_file.chunks():
                out.write(chunk)

        if ext == '.csv':
            features, source_srid = _parse_csv(tmp_path, force_srid)
        elif ext == '.zip':
            # Shapefile compressé : lecture via le système de fichiers virtuel GDAL.
            features, source_srid = _parse_ogr(f'/vsizip/{tmp_path}', force_srid)
        elif ext == '.kmz':
            kml_path = _extract_kml_from_kmz(tmp_path, workdir)
            features, source_srid = _parse_ogr(kml_path, force_srid)
        else:
            features, source_srid = _parse_ogr(tmp_path, force_srid)

        bbox, geom_type = _bbox_and_type(features)
        layer = Layer.objects.create(
            name=name,
            layer_type=Layer.VECTOR,
            origin=Layer.ORIGIN_UPLOAD,
            srid_source=source_srid,
            geom_type=geom_type,
            feature_count=len(features),
            bbox=bbox,
            owner_email=owner_email,
            metadata={'source_format': ext.lstrip('.')},
        )
        Feature.objects.bulk_create(
            [Feature(layer=layer, geom=geom, properties=props) for geom, props in features],
            batch_size=1000,
        )
        return layer


def _import_raster(uploaded_file, name: str, owner_email: str = ''):
    from .models import Layer  # import tardif

    layer = Layer(
        name=name,
        layer_type=Layer.RASTER,
        origin=Layer.ORIGIN_UPLOAD,
        owner_email=owner_email,
    )
    # Persistance via FileField (media/rasters/…). Django gère un nom sûr.
    layer.raster_file.save(os.path.basename(uploaded_file.name), uploaded_file, save=False)

    # Métadonnées raster via rasterio (GDAL bundlé).
    import rasterio

    with rasterio.open(layer.raster_file.path) as src:
        source_srid = None
        if src.crs is not None:
            try:
                source_srid = src.crs.to_epsg()
            except Exception:
                source_srid = None
        b = src.bounds
        # Emprise reprojetée en 4326 pour l'affichage.
        bbox = None
        if src.crs is not None:
            from rasterio.warp import transform_bounds
            try:
                bbox = list(transform_bounds(src.crs, 'EPSG:4326',
                                             b.left, b.bottom, b.right, b.top))
            except Exception:
                bbox = [b.left, b.bottom, b.right, b.top]
        layer.srid_source = source_srid
        layer.bbox = bbox
        layer.metadata = {
            'source_format': 'geotiff',
            'width': src.width,
            'height': src.height,
            'bands': src.count,
            'dtypes': [str(d) for d in src.dtypes],
        }
    layer.save()
    return layer
