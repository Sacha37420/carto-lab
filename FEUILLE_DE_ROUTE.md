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

## Lot 4 — Gabarit front et carte en thème sombre   ·   statut : en cours
**Objectif :** même comportement que le gabarit corrigé (T3), carte lisible en sombre.
**Tâches :**
- [x] `redirectUri` sur la page courante (`core/redirect-uri.ts`, copie de lab-admin : même origine et sous `/carto-lab/`, sinon `appUrl`)
- [x] Polices en chemin relatif (`styles.scss`) ; `tokenBases()` (`core/env.ts` : la seule API de l'app ; `core/token-scope.ts`, copie de lab-admin) ; `pkceMethod: 'S256'` (front) — `--pkce` sur le client reste à poser par l'architecte après déploiement
- [x] Ports publiés liés à `${LAB_BIND_ADDR:-127.0.0.1}` (chantier « failles », I1)
- [ ] Palette OpenLayers (`map.component.ts:39,188,190`) lue depuis les tokens CSS (`getComputedStyle`)
- [ ] Retirer `Department` du gabarit (`models.py:139`) s'il est mort
**Tests à passer avant de clore le lot :**
- [ ] Un lien direct vers `/carto-lab/meteo` y revient après la connexion ; WOFF2 en 200 ; carte vérifiée en sombre
**Vérifié le :** 2026-11-01 — non déployé. Chantier « failles » (secteur E) : image candidate `carto-lab-frontend:candidat` construite (depuis une copie propre du dossier, sans `node_modules`) ; le bundle porte `pkceMethod:"S256"`, les WOFF2 sont référencés en `./media/…` (0 `/assets/fonts`) ; `docker compose config` rend `host_ip: 127.0.0.1` pour 8091 et 4209. Les tests du lot demandent le déploiement. Restent la palette OpenLayers et `Department`.
Les tests du lot demandent le déploiement. Restent la palette OpenLayers et `Department` (hors chantier).

## Lot 5 — Recettes et documentation   ·   statut : à faire
**Objectif :** des recettes fiables et une app documentée.
**Tâches :**
- [ ] Vérifier, à l'enregistrement d'une recette, que les couches citées existent (400 sinon). Les recettes restent non modifiables (405, voulu).
- [ ] `README.md` : couches, rasters, calculs, recettes, accès QGIS, chaîne Météo-France
**Tests à passer avant de clore le lot :**
- [ ] Une recette citant une couche inexistante → 400
**Vérifié le :**

## Lot 6 — Clé Météo-France dans un coffre serveur   ·   statut : livré
**Objectif :** la clé Météo-France ne vit plus dans le navigateur (`localStorage['mf_key']`, envoyée à
chaque lancement dans l'en-tête `X-Meteo-Key`) mais, chiffrée, dans un coffre serveur (chantier
« failles », F6 → I3). Coffre **par utilisateur** (clé = email du jeton) : l'identifiant applicatif
Météo-France est propre à un compte du portail et porte son quota — ce n'est pas un secret de groupe.
**Tâches :**
- [x] `api/fields.py` (`EncryptedTextField` sous `VAULT_ENCRYPTION_KEY`, copie de restauration) ; `VAULT_ENCRYPTION_KEY` lue sans valeur par défaut
- [x] Modèle `MeteoCredential` (`owner_email` unique, `api_key` chiffrée) et migration `0008_meteocredential`
- [x] `GET /api/meteo/key/` → `{"meteo_key_configured": true|false}` ; `PUT /api/meteo/key/` `{"meteo_key": "…"}` → 204, chaîne vide ⇒ ligne supprimée
- [x] `POST /api/meteo/jobs/` lit la clé dans le coffre de l'appelant (400 si absente ou indéchiffrable) ; l'en-tête `X-Meteo-Key` n'est plus lu ; worker inchangé (jeton Redis éphémère)
- [x] Page Météo : état « clé enregistrée / aucune clé », champ mot de passe, boutons Enregistrer / Effacer ; plus aucune clé en `localStorage` (l'ancienne `mf_key` est effacée à l'ouverture, sans être envoyée)
**Tests à passer avant de clore le lot :**
- [x] `GET` ne rend jamais la valeur ; `PUT` puis `GET` → configurée ; `PUT ""` → non configurée, ligne supprimée
- [x] Valeur en base chiffrée (`gAAAAA…`, sans la clé) ; valeur indéchiffrable traitée comme absente (lancement 400)
- [x] Un autre utilisateur ne voit pas la clé et ne lance pas avec ; lancement sans clé → 400 ; l'en-tête seul ne suffit plus ; la clé déposée pour le worker est celle du coffre
**Vérifié le :** 2026-11-01 — non déployé. `ESSAI_CMD='python manage.py test --settings=config.settings_test' bash chantiers/tests/essai.sh carto-lab` : 121 tests OK (dont le coffre et le lancement dans `api/tests/test_meteo.py` ; inventaire des routes de `test_serrure.py` passé à 17, PUT ajouté aux méthodes refusées à un anonyme) ; `makemigrations --check` : aucun changement ; bundle front sans `X-Meteo-Key`.


## Mise en service — chantier « failles » (2026-11-01)
Les mentions « non déployé » ci-dessus datent d'avant cette mise en service. Image construite pendant
que l'ancienne tournait, `scripts/app-test.sh` : 121 tests OK, puis `up -d --no-build` ; ports
publiés sur `127.0.0.1` ; client Keycloak passé en PKCE S256 exigé, URI de retour réduites à celles de
l'app (`create-app-client.sh --pkce --prune-uris`) ; cloisonnement `lab-runner` 3/3 avant et après le durcissement du client, puis de nouveau 3/3 sur Keycloak 26.8.1.
Restent les vérifications en navigateur réel notées dans les lots (images, coffres de clés, envois).
