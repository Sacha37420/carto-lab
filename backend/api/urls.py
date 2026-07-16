from django.urls import path
from .views import (
    MeView, DepartmentListView, UserListView,
    LayersView, LayerDetailView, LayerGeoJSONView,
    CRSListView, TransformPointView,
)

urlpatterns = [
    path('me/',          MeView.as_view()),
    path('departments/', DepartmentListView.as_view()),
    path('users/',       UserListView.as_view()),

    # ── Couches (Features 1, 6) ──────────────────────────────────────────────
    path('layers/',                 LayersView.as_view()),
    path('layers/<int:pk>/',        LayerDetailView.as_view()),
    path('layers/<int:pk>/geojson/', LayerGeoJSONView.as_view()),

    # ── Systèmes de coordonnées (Feature 2) ──────────────────────────────────
    path('crs/',                    CRSListView.as_view()),
    path('crs/transform-point/',    TransformPointView.as_view()),
]
