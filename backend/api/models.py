from django.db import models
from django.contrib.gis.db import models as gis_models


class Layer(models.Model):
    """
    Catalogue d'une couche SIG (importée, calculée ou issue de Météo-France).

    Choix de modélisation (cf. to_do « MODÈLE DE DONNÉES ») : une table générique
    `features` (geometry 4326 + properties JSONB) plutôt qu'une table par couche.
    Justification : les imports ont des schémas d'attributs hétérogènes et
    imprévisibles ; le JSONB évite une migration par upload et garde l'ORM simple.
    Les couches « publiables QGIS » (Lot 4) seront, elles, MATÉRIALISÉES en tables
    dédiées typées avec index GIST dans le schéma carto_public.
    """

    VECTOR = 'vector'
    RASTER = 'raster'
    TYPE_CHOICES = [(VECTOR, 'Vecteur'), (RASTER, 'Raster')]

    ORIGIN_UPLOAD = 'upload'
    ORIGIN_METEO = 'meteofrance'
    ORIGIN_CALCUL = 'calcul'
    ORIGIN_CHOICES = [
        (ORIGIN_UPLOAD, 'Import'),
        (ORIGIN_METEO, 'Météo-France'),
        (ORIGIN_CALCUL, 'Calcul'),
    ]

    name = models.CharField(max_length=200)
    layer_type = models.CharField(max_length=10, choices=TYPE_CHOICES, default=VECTOR)
    origin = models.CharField(max_length=20, choices=ORIGIN_CHOICES, default=ORIGIN_UPLOAD)

    # CRS d'origine du fichier importé (trace conservée) ; les géométries sont
    # stockées reprojetées en EPSG:4326.
    srid_source = models.IntegerField(null=True, blank=True)
    geom_type = models.CharField(max_length=32, blank=True)  # Point, LineString, Polygon…
    feature_count = models.IntegerField(default=0)
    # Emprise [minx, miny, maxx, maxy] en EPSG:4326.
    bbox = models.JSONField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    # Raster (GeoTIFF) : conservé comme fichier + métadonnées, pas d'entités.
    raster_file = models.FileField(upload_to='rasters/', null=True, blank=True)

    published_qgis = models.BooleanField(default=False)
    owner_email = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'layers'
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f'{self.name} ({self.layer_type})'


class Feature(gis_models.Model):
    """Entité vectorielle générique : géométrie 4326 + attributs JSONB."""

    layer = models.ForeignKey(Layer, on_delete=models.CASCADE, related_name='features')
    geom = gis_models.GeometryField(srid=4326, spatial_index=True)
    properties = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = 'features'


class Department(models.Model):
    """Département ou équipe de l'organisation."""

    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)

    class Meta:
        db_table = 'departments'
        ordering = ['name']

    def __str__(self) -> str:
        return self.name


class UserRecord(models.Model):
    """Enregistrement d'un utilisateur Keycloak, créé automatiquement à la première connexion."""

    email = models.EmailField(primary_key=True, max_length=255)
    display_name = models.CharField(max_length=200, blank=True)
    department = models.ForeignKey(
        Department,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='members',
    )
    registered_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'user_records'
        ordering = ['email']

    def __str__(self) -> str:
        return self.display_name or self.email
