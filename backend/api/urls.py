from django.urls import path
from .views import (
    MeView, DepartmentListView, UserListView,
    LayersView, LayerDetailView, LayerGeoJSONView,
    CRSListView, TransformPointView,
    ProcessingCatalogView, ProcessingRunView,
    RecipesView, RecipeDetailView, RecipeRunView,
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

    # ── Moteur de calculs (Feature 3) ────────────────────────────────────────
    path('processings/',            ProcessingCatalogView.as_view()),
    path('processings/run/',        ProcessingRunView.as_view()),

    # ── Constructeur / recettes (Feature 6) ──────────────────────────────────
    path('recipes/',                RecipesView.as_view()),
    path('recipes/<int:pk>/',       RecipeDetailView.as_view()),
    path('recipes/<int:pk>/run/',   RecipeRunView.as_view()),
]
