"""Tests backend de carto-lab.

Lancement : ``python manage.py test --settings=config.settings_test`` (commande
déclarée dans FEUILLE_DE_ROUTE.md ; le pourquoi est en tête de
``config/settings_test.py``). Base PostGIS réelle — les traitements sont du SQL
``ST_*`` — et aucun réseau : Keycloak (JWKS), ``storage``, Redis, Celery et
Météo-France sont simulés (``helpers.py``).

    test_serrure.py       Verrou 2 : jetons RS256 réels, groupe ``developers``.
    test_couches.py       Import (formats, limites, CRS), GeoJSON reprojeté,
                          rasters dans ``storage`` avec le jeton de l'appelant,
                          aucun jeton dans ce que l'API expose.
    test_traitements.py   Moteur de calculs PostGIS et recettes.
    test_publication.py   Matérialisation dans ``carto_public`` pour QGIS.
    test_meteo.py         Clé Météo-France (jamais stockée), tâche Celery de bout
                          en bout sur un faux DPClim, CSV, qualité, indicateurs,
                          classification.

Les défauts que cette suite a montrés (C1 à C5 du rapport) sont corrigés ; un
test ``expectedFailure`` ajouté plus tard montrerait un défaut connu, à corriger
en retirant le décorateur.
"""
