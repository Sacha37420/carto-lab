"""
Rôle Postgres read-only strictement scopé au schéma carto_public (Lot 4 / point 5).

pg_featureserv se connecte avec ce rôle : il ne voit donc QUE les couches
matérialisées « publiables », jamais les tables applicatives de carto_lab.
Verrous : CONNECTION LIMIT, statement_timeout, USAGE/SELECT limités à carto_public
(+ public en lecture pour geometry_columns/PostGIS), REVOKE sur carto_lab.
"""
import re

from django.conf import settings
from django.db import migrations


def setup_reader(apps, schema_editor):
    user = settings.CARTO_READER_USER
    pwd = settings.CARTO_READER_PASSWORD
    if not pwd:
        # Pas de mot de passe configuré (dev sans OGC) : on ne crée pas le rôle.
        return
    if not re.match(r'^[a-z_][a-z0-9_]*$', user):
        raise ValueError(f"CARTO_READER_USER invalide : {user!r}")
    p = pwd.replace("'", "''")
    dbname = settings.DATABASES['default']['NAME']

    stmts = [
        f"""DO $$ BEGIN
              IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='{user}') THEN
                CREATE ROLE {user} LOGIN;
              END IF;
            END $$;""",
        f"ALTER ROLE {user} WITH LOGIN PASSWORD '{p}' CONNECTION LIMIT 5",
        f"ALTER ROLE {user} SET statement_timeout = '30s'",
        # Connexion à la base uniquement.
        f"GRANT CONNECT ON DATABASE {dbname} TO {user}",
        # Lecture du schéma public (geometry_columns, spatial_ref_sys, fonctions PostGIS).
        f"GRANT USAGE ON SCHEMA public TO {user}",
        # Schéma publiable en lecture seule.
        f"GRANT USAGE ON SCHEMA carto_public TO {user}",
        f"GRANT SELECT ON ALL TABLES IN SCHEMA carto_public TO {user}",
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA carto_public GRANT SELECT ON TABLES TO {user}",
        # Interdiction stricte des tables applicatives.
        f"REVOKE ALL ON SCHEMA carto_lab FROM {user}",
        f"REVOKE ALL ON ALL TABLES IN SCHEMA carto_lab FROM {user}",
    ]
    with schema_editor.connection.cursor() as cur:
        for s in stmts:
            cur.execute(s)


def noop(apps, schema_editor):
    # On ne supprime pas le rôle en reverse (il peut posséder des grants/objets).
    pass


class Migration(migrations.Migration):

    dependencies = [('api', '0005_job')]
    operations = [migrations.RunPython(setup_reader, noop)]
