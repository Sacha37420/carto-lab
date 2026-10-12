from django.conf import settings
from rest_framework import serializers
from .models import Department, Job, Layer, Recipe, UserRecord


class RecipeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Recipe
        fields = ['id', 'name', 'steps', 'owner_email', 'result_layer', 'created_at']
        read_only_fields = ['id', 'owner_email', 'result_layer', 'created_at']

    def validate_steps(self, steps):
        """`steps` est un JSON libre : sa forme se vérifie à l'enregistrement
        (création comme modification), pas seulement à l'exécution. Une recette
        vide reste permise (brouillon) ; l'existence des couches citées n'est
        pas vérifiée ici — une couche peut disparaître après coup, l'exécution
        le dit."""
        from . import processing  # import tardif : processing importe les modèles

        def refuse(message):
            raise serializers.ValidationError(message)

        def entier(value):
            return isinstance(value, int) and not isinstance(value, bool)

        if not isinstance(steps, list):
            refuse("« steps » doit être une liste d'étapes.")
        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                refuse(f"Étape {i} : objet attendu.")
            if not isinstance(step.get('op'), str) or step['op'] not in processing.OPERATIONS:
                refuse(f"Étape {i} : opération inconnue.")
            if not isinstance(step.get('params') or {}, dict):
                refuse(f"Étape {i} : « params » doit être un objet.")
            if not isinstance(step.get('name', ''), str):
                refuse(f"Étape {i} : « name » doit être un texte.")
            refs = step.get('inputs') or []
            if not isinstance(refs, list):
                refuse(f"Étape {i} : « inputs » doit être une liste.")
            for ref in refs:
                if not isinstance(ref, dict):
                    refuse(f"Étape {i} : entrée mal formée.")
                if 'layer' in ref:
                    layer = ref['layer']
                    if not (entier(layer) or (isinstance(layer, str) and layer.isdecimal())):
                        refuse(f"Étape {i} : identifiant de couche invalide.")
                elif 'step' in ref:
                    if not entier(ref['step']) or not 0 <= ref['step'] < i:
                        refuse(f"Étape {i} : référence d'étape invalide "
                               "(une étape ne reprend que la sortie d'une étape précédente).")
                else:
                    refuse(f"Étape {i} : une entrée désigne une couche (« layer ») "
                           "ou une étape (« step »).")
        return steps


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
