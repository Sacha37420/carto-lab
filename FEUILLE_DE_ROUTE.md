# Feuille de route — carto-lab

App livrée avant l'obligation de feuille de route : ses lots d'origine ne sont pas repris ici.
Ce fichier existe d'abord pour déclarer la commande de test au lanceur du lab
(`scripts/app-test.sh`), qui joue les tests avant toute mise en service.

## Commande de test
python manage.py test --settings=config.settings_test

## Lot 1 — Tests backend   ·   statut : livré

Suite `backend/api/tests/` (chantier « tests », 2026-10-11) : serrure de l'API, import et
catalogue des couches, rasters dans `storage`, moteur de calculs PostGIS, recettes, publication
QGIS, chaîne Météo-France sur un faux DPClim. Vérifié le 2026-10-11 par
`bash chantiers/tests/essai.sh carto-lab` (commande ci-dessus).

Tests à passer : `python manage.py test --settings=config.settings_test` — tous verts ; les tests
marqués `expectedFailure` décrivent des défauts connus du code, à corriger un par un (retirer le
décorateur avec la correction).

## Notes

- Sans `--settings=config.settings_test`, la suite ne démarre pas : la base de test n'aurait pas
  PostGIS (extension réservée à un superutilisateur) et la migration `0006_reader_role` toucherait
  au rôle `carto_reader` de l'instance. Le détail est en tête de `backend/config/settings_test.py`.
- La base de test est une copie du modèle `template_postgis` de l'image `postgis/postgis`.
