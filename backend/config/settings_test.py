"""Réglages de test de carto-lab.

    python manage.py test --settings=config.settings_test

Pourquoi des réglages à part (commande déclarée dans FEUILLE_DE_ROUTE.md,
section « Commande de test ») :

* **La base de test doit avoir PostGIS.** Le lanceur de Django crée
  `test_<base>` à partir de `template1`, donc SANS l'extension, puis le moteur
  GeoDjango exécute `CREATE EXTENSION IF NOT EXISTS postgis` — refusé à un rôle
  non superutilisateur (« Must be superuser to create this extension »), et le
  rôle des tests ne l'est pas, à dessein. L'image `postgis/postgis` fournit un
  modèle `template_postgis` (extension déjà installée, marqué « modèle », donc
  clonable par tout rôle CREATEDB) : la base de test en est une copie.
* **Aucun rôle PostgreSQL n'est touché par les tests.** La migration
  `0006_reader_role` crée et modifie le rôle `carto_reader` quand
  `CARTO_READER_PASSWORD` est renseigné — ce qu'il est dans le `.env` de service.
  Un rôle est un objet de l'INSTANCE, pas de la base de test : la migration
  modifierait le vrai rôle du lab (ou échouerait, le rôle de test n'ayant pas
  CREATEROLE). Mot de passe vide ⇒ la migration ne fait rien.
* Valeurs figées pour ce que les tests affirment (client Keycloak, groupe,
  domaine), indépendamment du `.env` de service.
"""
from .settings import *  # noqa: F401,F403
from .settings import DATABASES

DATABASES['default']['TEST'] = {'TEMPLATE': 'template_postgis'}

CARTO_READER_PASSWORD = ''
# Le GRANT de lecture posé à la publication vise un rôle qui doit exister : en
# test, le rôle courant lui-même (cf. api/tests/test_publication.py).
CARTO_READER_USER = DATABASES['default']['USER']

KEYCLOAK_CLIENT_ID = 'carto-lab'
KEYCLOAK_REQUIRED_GROUPS = 'developers'
KEYCLOAK_ISSUER_URI = 'http://keycloak.test/realms/ssolab'
STORAGE_INTERNAL_URL = 'http://storage.test'
STORAGE_NAMESPACE = 'carto-lab'
DOMAIN = 'lab.example.org'
OGC_SERVICE_PATH = 'carto-ogc'
METEOFRANCE_BASE_URL = 'http://meteo.test/DPClim/v1'
METEOFRANCE_TOKEN_URL = 'http://meteo.test/token'
METEOFRANCE_MIN_INTERVAL = 0
DEBUG = False
