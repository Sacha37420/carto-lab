from django.conf import settings
from rest_framework import serializers
from .models import Department, Job, Layer, Recipe, UserRecord


class RecipeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Recipe
        fields = ['id', 'name', 'steps', 'owner_email', 'result_layer', 'created_at']
        read_only_fields = ['id', 'owner_email', 'result_layer', 'created_at']


class JobSerializer(serializers.ModelSerializer):
    class Meta:
        model = Job
        fields = ['id', 'kind', 'status', 'progress', 'message', 'params',
                  'result_layer', 'created_at', 'updated_at']
        read_only_fields = fields


class LayerSerializer(serializers.ModelSerializer):
    """Métadonnées d'une couche pour la liste/galerie (Feature 6)."""

    raster_url = serializers.SerializerMethodField()

    class Meta:
        model = Layer
        fields = [
            'id', 'name', 'layer_type', 'origin', 'srid_source', 'geom_type',
            'feature_count', 'bbox', 'metadata', 'published_qgis',
            'raster_url', 'created_at',
        ]
        read_only_fields = fields

    def get_raster_url(self, obj) -> str | None:
        """URL storage directe (pas de proxy carto-lab pour la LECTURE) : le
        frontend l'appelle avec son propre token (`Authorization: Bearer`, cf.
        CLAUDE.md « Verrou 2 généralisé »). Sûr sans filtrage supplémentaire ici
        — n'importe quel membre du groupe `developers` a déjà un accès direct
        au namespace storage `carto-lab` (`Share.required_groups='developers'`),
        et `azp='carto-lab'` figure dans `KEYCLOAK_TRUSTED_CLIENTS` côté
        storage. Même origine (Caddy route `/storage-api/*` vers
        storage-backend sur le même DOMAIN que carto-lab) : pas de CORS à
        gérer. L'écriture (upload/delete) reste proxiée par ce backend, cf.
        `LayersView.post` / `LayerDetailView.delete` (`api/storage_client.py`)
        — elle doit enregistrer/retirer la ligne `Layer` en même temps."""
        if not obj.raster_path:
            return None
        request = self.context.get('request')
        path = f'/storage-api/api/files/{settings.STORAGE_NAMESPACE}/content/{obj.raster_path}'
        return request.build_absolute_uri(path) if request else path


class DepartmentSerializer(serializers.ModelSerializer):
    member_count = serializers.IntegerField(source='members.count', read_only=True)

    class Meta:
        model = Department
        fields = ['id', 'name', 'description', 'member_count']


class UserRecordSerializer(serializers.ModelSerializer):
    department = DepartmentSerializer(read_only=True)

    class Meta:
        model = UserRecord
        fields = ['email', 'display_name', 'department', 'registered_at']
