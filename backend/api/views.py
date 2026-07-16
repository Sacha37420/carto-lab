import json

from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import generics, status
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response

from django.contrib.gis.geos import GEOSGeometry
from drf_spectacular.utils import extend_schema, OpenApiParameter

from .models import Department, Feature, Layer, UserRecord
from .serializers import DepartmentSerializer, LayerSerializer, UserRecordSerializer
from . import crs as crs_mod
from .geo import LayerImportError, import_layer


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
