from rest_framework import serializers
from .models import Department, Layer, Recipe, UserRecord


class RecipeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Recipe
        fields = ['id', 'name', 'steps', 'owner_email', 'result_layer', 'created_at']
        read_only_fields = ['id', 'owner_email', 'result_layer', 'created_at']


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
        if obj.raster_file:
            request = self.context.get('request')
            url = obj.raster_file.url
            return request.build_absolute_uri(url) if request else url
        return None


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
