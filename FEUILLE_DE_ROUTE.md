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

Tests à passer : `python manage.py test --settings=config.settings_test` — tous verts, aucun
`expectedFailure` (les cinq défauts que la suite avait montrés sont corrigés).

## Notes

- Sans `--settings=config.settings_test`, la suite ne démarre pas : la base de test n'aurait pas
  PostGIS (extension réservée à un superutilisateur) et la migration `0006_reader_role` toucherait
  au rôle `carto_reader` de l'instance. Le détail est en tête de `backend/config/settings_test.py`.
- La base de test est une copie du modèle `template_postgis` de l'image `postgis/postgis`.

---

Lots ajoutés par l'analyse du 2026-10-31 (vue d'ensemble : `to_do_global.md` à la racine du dépôt
du lab). [C] constaté, [D] déduit.

## Lot 2 — Porte OGC conforme au chemin à portes   ·   statut : à faire
**Objectif :** l'app passe `validate-app.sh` sans contournement (chantier T1).
**Constat [C] :** `validate-app.sh` refuse le chemin Caddy `/carto-ogc` (hors `/<app>` et
`/<app>-api`) et les noms de service `oauth2-proxy-ogc`, `pgfeatureserv` et `worker`. L'image
`pramsey/pg_featureserv:latest` n'est pas épinglée (`docker-compose.yml:105`).
**Tâches :**
- [ ] Ⓟ Décision d'architecte : passer à `/carto-lab-api/ogc`, ou ajouter une exception déclarée au validateur
- [ ] Renommer les services en `carto-lab-*` ; épingler `pg_featureserv` sur une version
- [ ] Mettre à jour `setup-ogc-auth.sh` et la configuration QGIS documentée
**Tests à passer avant de clore le lot :**
- [ ] `validate-app.sh carto-lab` rend 0
- [ ] QGIS ouvre une couche publiée ; un compte hors `developers` est refusé par oauth2-proxy
**Vérifié le :**

## Lot 3 — Tables orphelines de `carto_public` (contrôle C5)   ·   statut : à faire
**Objectif :** aucune donnée « supprimée » n'est encore servie à QGIS.
**Constat [C] :** contrôle demandé par `chantiers/tests/rapports/carto-arbre.md` (C5), jamais fait.
Des tables laissées avant `9cd3d00` sont peut-être encore publiées par `pg_featureserv` [D].
**Tâches :**
- [ ] Requête qui compare `carto_public` aux `Layer` publiées (lecture seule)
- [ ] Ⓟ Supprimer les orphelines
**Tests à passer avant de clore le lot :**
- [ ] 0 table orpheline ; la collection `pg_featureserv` correspondante répond 404
**Vérifié le :**

## Lot 4 — Gabarit front et carte en thème sombre   ·   statut : à faire
**Objectif :** même comportement que le gabarit corrigé (T3), carte lisible en sombre.
**Tâches :**
- [ ] `redirectUri` sur la page courante (`keycloak.service.ts:32` utilise `env.appUrl`)
- [ ] Polices en chemin relatif ; `tokenBases()` ; PKCE
- [ ] Palette OpenLayers (`map.component.ts:39,188,190`) lue depuis les tokens CSS (`getComputedStyle`)
- [ ] Retirer `Department` du gabarit (`models.py:139`) s'il est mort
**Tests à passer avant de clore le lot :**
- [ ] Un lien direct vers `/carto-lab/meteo` y revient après la connexion ; WOFF2 en 200 ; carte vérifiée en sombre
**Vérifié le :**

## Lot 5 — Recettes et documentation   ·   statut : à faire
**Objectif :** des recettes fiables et une app documentée.
**Tâches :**
- [ ] Vérifier, à l'enregistrement d'une recette, que les couches citées existent (400 sinon). Les recettes restent non modifiables (405, voulu).
- [ ] `README.md` : couches, rasters, calculs, recettes, accès QGIS, chaîne Météo-France
**Tests à passer avant de clore le lot :**
- [ ] Une recette citant une couche inexistante → 400
**Vérifié le :**
