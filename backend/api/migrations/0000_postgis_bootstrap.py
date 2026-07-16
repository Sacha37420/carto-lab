"""
Bootstrap PostGIS : extension + schémas applicatifs.

DOIT s'exécuter AVANT 0001_initial (run_before) : le search_path est
`carto_lab,public`, donc si le schéma carto_lab n'existe pas encore au moment de
créer les premières tables, Django les créerait dans public (bug documenté dans
CLAUDE.md). On garantit ici l'ordre.

L'image postgis/postgis crée déjà l'extension dans la base ; CreateExtension est
idempotent (IF NOT EXISTS) et sert de filet dans le cas d'un volume pré-existant.
"""
from django.contrib.postgres.operations import CreateExtension
from django.db import migrations


class Migration(migrations.Migration):

    initial = True
    dependencies = []
    run_before = [('api', '0001_initial')]

    operations = [
        CreateExtension('postgis'),
        migrations.RunSQL(
            sql=(
                "CREATE SCHEMA IF NOT EXISTS carto_lab;"
                "CREATE SCHEMA IF NOT EXISTS carto_public;"
            ),
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
