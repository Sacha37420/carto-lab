import json

from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import generics, status
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response

from django.contrib.gis.geos import GEOSGeometry
from drf_spectacular.utils import extend_schema, OpenApiParameter

from .models import Department, Feature, Job, Layer, Recipe, UserRecord
from .serializers import (
    DepartmentSerializer, JobSerializer, LayerSerializer, RecipeSerializer,
    UserRecordSerializer,
)
from . import crs as crs_mod
from .geo import LayerImportError, import_layer
from . import processing
from . import indicators as ind_mod
from . import choropleth as choro_mod
from . import secret_store
from .tasks import build_meteo_choropleth


class MeView(APIView):
    """
    permission_classes = [IsAuthenticated]
    GET /api/me/
    Retourne l'identité de l'utilisateur authentifié (depuis le JWT + DB).
    Crée un UserRecord à la première visite.
    """

    def get(self, request):
        email    = request.user.email
        username = request.user.username
        groups   = request.user.claims.get('groups', [])

        record, created = UserRecord.objects.get_or_create(
            email=email,
            defaults={'display_name': username},
        )

        return Response({
            'email':        email,
            'username':     username,
            'groups':       groups,
            'display_name': record.display_name,
            'department':   DepartmentSerializer(record.department).data
                            if record.department else None,
            'registered_at': record.registered_at,
            'is_new':        created,
        })


class DepartmentListView(generics.ListAPIView):
    """GET /api/departments/ — liste tous les départements."""

    queryset         = Department.objects.all()
    serializer_class = DepartmentSerializer


class UserListView(generics.ListAPIView):
    """GET /api/users/ — liste tous les utilisateurs enregistrés."""

    queryset         = UserRecord.objects.select_related('department')
    serializer_class = UserRecordSerializer


# ──────────────────────────────────────────────────────────────────────────────
# COUCHES — import, catalogue, GeoJSON reprojeté (Features 1, 2, 6)
# ──────────────────────────────────────────────────────────────────────────────
class LayersView(APIView):
    """
    GET  /api/layers/  — catalogue de toutes les couches (Feature 6).
    POST /api/layers/  — import d'un fichier SIG (Feature 1, multipart/form-data).
        champs : file (obligatoire), name (optionnel), source_srid (optionnel,
        force le CRS d'origine si absent des métadonnées).
    """

    parser_classes = [MultiPartParser, FormParser]

    def get(self, request):
        layers = Layer.objects.all()
        return Response(LayerSerializer(layers, many=True, context={'request': request}).data)

    @extend_schema(request={'multipart/form-data': {
        'type': 'object',
        'properties': {
            'file': {'type': 'string', 'format': 'binary'},
            'name': {'type': 'string'},
            'source_srid': {'type': 'integer'},
        },
        'required': ['file'],
    }})
    def post(self, request):
        uploaded = request.FILES.get('file')
        if uploaded is None:
            return Response({'detail': "Champ 'file' manquant."},
                            status=status.HTTP_400_BAD_REQUEST)
        name = request.data.get('name') or uploaded.name
        force_srid = request.data.get('source_srid') or None
        if force_srid is not None:
            try:
                force_srid = crs_mod.validate_srid(force_srid)
            except ValueError as exc:
                return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        try:
            layer = import_layer(
                uploaded, name=name, force_srid=force_srid,
                owner_email=getattr(request.user, 'email', ''),
            )
        except LayerImportError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            LayerSerializer(layer, context={'request': request}).data,
            status=status.HTTP_201_CREATED,
        )


class LayerDetailView(APIView):
    """
    GET    /api/layers/<id>/ — détail d'une couche.
    DELETE /api/layers/<id>/ — suppression (entités en cascade).
    """

    def _get(self, pk):
        try:
            return Layer.objects.get(pk=pk)
        except Layer.DoesNotExist:
            return None

    def get(self, request, pk):
        layer = self._get(pk)
        if layer is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(LayerSerializer(layer, context={'request': request}).data)

    def delete(self, request, pk):
        layer = self._get(pk)
        if layer is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        layer.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class LayerGeoJSONView(APIView):
    """
    GET /api/layers/<id>/geojson/?srid=<epsg>
    Entités d'une couche vectorielle en GeoJSON. Reprojection à la volée vers le
    CRS demandé (défaut 4326) — cœur de la Feature 2 côté serveur.
    """

    @extend_schema(parameters=[
        OpenApiParameter('srid', int, description='EPSG de sortie (défaut 4326)'),
    ])
    def get(self, request, pk):
        try:
            layer = Layer.objects.get(pk=pk)
        except Layer.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)
        if layer.layer_type != Layer.VECTOR:
            return Response({'detail': "Couche raster : pas de GeoJSON vectoriel."},
                            status=status.HTTP_400_BAD_REQUEST)

        target = request.query_params.get('srid', 4326)
        try:
            target = crs_mod.validate_srid(target)
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        out = []
        for feat in layer.features.all().iterator():
            geom: GEOSGeometry = feat.geom
            if target != 4326:
                geom = geom.clone()
                geom.transform(target)
            out.append({
                'type': 'Feature',
                'geometry': json.loads(geom.geojson),
                'properties': feat.properties,
                'id': feat.pk,
            })
        return Response({
            'type': 'FeatureCollection',
            'crs': {'type': 'name', 'properties': {'name': f'EPSG:{target}'}},
            'features': out,
        })


# ──────────────────────────────────────────────────────────────────────────────
# SYSTÈMES DE COORDONNÉES (Feature 2)
# ──────────────────────────────────────────────────────────────────────────────
class CRSListView(APIView):
    """
    GET /api/crs/            — liste curatée des CRS français prioritaires.
    GET /api/crs/?srid=<n>   — valide/décrit n'importe quel EPSG.
    """

    @extend_schema(parameters=[OpenApiParameter('srid', int, required=False)])
    def get(self, request):
        srid = request.query_params.get('srid')
        if srid is not None:
            try:
                return Response(crs_mod.describe_srid(crs_mod.validate_srid(srid)))
            except ValueError as exc:
                return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(crs_mod.COMMON_CRS)


class TransformPointView(APIView):
    """
    POST /api/crs/transform-point/
        { "x": .., "y": .., "from_srid": .., "to_srid": .. }
    Convertit un point ponctuel entre deux CRS (Feature 2).
    """

    def post(self, request):
        d = request.data
        try:
            x = float(d['x']); y = float(d['y'])
            from_srid = crs_mod.validate_srid(d['from_srid'])
            to_srid = crs_mod.validate_srid(d['to_srid'])
        except (KeyError, TypeError, ValueError) as exc:
            return Response({'detail': f"Paramètres invalides : {exc}"},
                            status=status.HTTP_400_BAD_REQUEST)
        tx, ty = crs_mod.transform_point(x, y, from_srid, to_srid)
        return Response({'x': tx, 'y': ty, 'srid': to_srid})


# ──────────────────────────────────────────────────────────────────────────────
# MOTEUR DE CALCULS (Feature 3) + CONSTRUCTEUR / RECETTES (Feature 6)
# ──────────────────────────────────────────────────────────────────────────────
def _resolve_layers(ids):
    """Charge des couches par id en préservant l'ordre ; lève si l'une manque."""
    layers = []
    for lid in ids:
        try:
            layers.append(Layer.objects.get(pk=lid))
        except Layer.DoesNotExist:
            raise processing.ProcessingError(f"Couche {lid} introuvable.")
    return layers


class ProcessingCatalogView(APIView):
    """GET /api/processings/ — catalogue des traitements disponibles (+ schéma des params)."""

    def get(self, request):
        return Response(processing.catalog())


class ProcessingRunView(APIView):
    """
    POST /api/processings/run/
        { "operation": "buffer", "inputs": [<layer_id>, ...], "params": {...}, "name": "opt" }
    Exécute un traitement unique → nouvelle couche (origine=calcul).
    """

    def post(self, request):
        d = request.data
        op = d.get('operation')
        inputs = d.get('inputs') or []
        params = d.get('params') or {}
        try:
            layers = _resolve_layers(inputs)
            layer = processing.run_operation(
                op, layers, params,
                out_name=d.get('name', ''),
                owner_email=getattr(request.user, 'email', ''),
            )
        except processing.ProcessingError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            LayerSerializer(layer, context={'request': request}).data,
            status=status.HTTP_201_CREATED,
        )


def _run_recipe(recipe: Recipe, owner_email: str) -> Layer:
    """Exécute les étapes d'une recette dans l'ordre ; renvoie la couche finale."""
    outputs: list[Layer] = []          # sortie de chaque étape (par index)
    for i, step in enumerate(recipe.steps):
        op = step.get('op')
        params = step.get('params') or {}
        refs = step.get('inputs') or []
        inputs = []
        for ref in refs:
            if 'layer' in ref:
                inputs.append(_resolve_layers([ref['layer']])[0])
            elif 'step' in ref:
                idx = ref['step']
                if idx < 0 or idx >= len(outputs):
                    raise processing.ProcessingError(
                        f"Étape {i}: référence d'étape {idx} invalide."
                    )
                inputs.append(outputs[idx])
            else:
                raise processing.ProcessingError(f"Étape {i}: entrée mal formée {ref}.")
        out = processing.run_operation(
            op, inputs, params,
            out_name=step.get('name', f"{recipe.name} — étape {i + 1}"),
            owner_email=owner_email,
        )
        out.metadata = {**out.metadata, 'recipe': recipe.name, 'recipe_step': i}
        out.save(update_fields=['metadata'])
        outputs.append(out)
    if not outputs:
        raise processing.ProcessingError("Recette vide : aucune étape.")
    return outputs[-1]


class RecipesView(APIView):
    """
    GET  /api/recipes/  — liste des recettes.
    POST /api/recipes/  — crée une recette ({ name, steps }).
    """

    def get(self, request):
        return Response(RecipeSerializer(Recipe.objects.all(), many=True).data)

    def post(self, request):
        ser = RecipeSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        recipe = ser.save(owner_email=getattr(request.user, 'email', ''))
        return Response(RecipeSerializer(recipe).data, status=status.HTTP_201_CREATED)


class RecipeDetailView(APIView):
    """GET / DELETE /api/recipes/<id>/."""

    def get(self, request, pk):
        try:
            return Response(RecipeSerializer(Recipe.objects.get(pk=pk)).data)
        except Recipe.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)

    def delete(self, request, pk):
        try:
            Recipe.objects.get(pk=pk).delete()
        except Recipe.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(status=status.HTTP_204_NO_CONTENT)


class RecipeRunView(APIView):
    """POST /api/recipes/<id>/run/ — rejoue la recette → couche résultat."""

    def post(self, request, pk):
        try:
            recipe = Recipe.objects.get(pk=pk)
        except Recipe.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)
        try:
            layer = _run_recipe(recipe, getattr(request.user, 'email', ''))
        except processing.ProcessingError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        recipe.result_layer = layer
        recipe.save(update_fields=['result_layer'])
        return Response(
            LayerSerializer(layer, context={'request': request}).data,
            status=status.HTTP_201_CREATED,
        )


# ──────────────────────────────────────────────────────────────────────────────
# MÉTÉO-FRANCE — grandeurs, lancement de job async, suivi (Feature 4)
# ──────────────────────────────────────────────────────────────────────────────
class MeteoOptionsView(APIView):
    """GET /api/meteo/options/ — grandeurs, indicateurs, classifications, rampes (pour l'UI)."""

    def get(self, request):
        return Response({
            'grandeurs': [
                {'key': k, 'label': v['label'], 'unit': v['unit']}
                for k, v in ind_mod.GRANDEURS.items()
            ],
            'indicators': ind_mod.catalog(),
            'classifications': choro_mod.CLASSIFICATIONS,
            'ramps': list(choro_mod.RAMPS.keys()),
        })


class MeteoJobLaunchView(APIView):
    """
    POST /api/meteo/jobs/ — lance la construction async d'une carte Météo-France.
    La clé API est fournie dans le header `X-Meteo-Key` (jamais dans le corps, jamais
    persistée) et déposée en Redis éphémère ; seul un jeton transite vers le worker.
    Corps : { grandeur, year, indicator, indicator_params?, classification?, n_classes?,
              ramp?, max_stations? }
    """

    def post(self, request):
        api_key = request.headers.get('X-Meteo-Key', '').strip()
        if not api_key:
            return Response({'detail': "Clé API Météo-France manquante (header X-Meteo-Key)."},
                            status=status.HTTP_400_BAD_REQUEST)
        d = request.data
        if d.get('grandeur') not in ind_mod.GRANDEURS:
            return Response({'detail': "Grandeur invalide."}, status=status.HTTP_400_BAD_REQUEST)
        if d.get('indicator') not in ind_mod.INDICATORS:
            return Response({'detail': "Indicateur invalide."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            year = int(d.get('year'))
        except (TypeError, ValueError):
            return Response({'detail': "Année invalide."}, status=status.HTTP_400_BAD_REQUEST)

        params = {
            'grandeur': d['grandeur'],
            'year': year,
            'indicator': d['indicator'],
            'indicator_params': d.get('indicator_params', {}),
            'classification': d.get('classification', 'quantiles'),
            'n_classes': int(d.get('n_classes', 5)),
            'ramp': d.get('ramp', 'YlOrRd'),
            'max_stations': d.get('max_stations'),
        }
        job = Job.objects.create(
            kind='meteofrance', status=Job.PENDING, params=params,
            owner_email=getattr(request.user, 'email', ''),
        )
        token = secret_store.put(api_key)             # clé éphémère en Redis
        build_meteo_choropleth.delay(job.id, token, params)
        return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


class JobsView(APIView):
    """GET /api/jobs/ — liste des jobs (récents)."""

    def get(self, request):
        return Response(JobSerializer(Job.objects.all()[:50], many=True).data)


class JobDetailView(APIView):
    """GET /api/jobs/<id>/ — état d'un job (polling frontend)."""

    def get(self, request, pk):
        try:
            return Response(JobSerializer(Job.objects.get(pk=pk)).data)
        except Job.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)
