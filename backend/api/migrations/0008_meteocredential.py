import api.fields
from django.db import migrations, models


class Migration(migrations.Migration):
    """Coffre par utilisateur de la clé Météo-France (chantier « failles », I3).

    Aucune migration de données : la clé n'était jusqu'ici jamais côté serveur
    (elle vivait dans le `localStorage` du navigateur) — il n'y a rien à reprendre.
    """

    dependencies = [('api', '0007_layer_raster_path')]

    operations = [
        migrations.CreateModel(
            name='MeteoCredential',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False,
                                           verbose_name='ID')),
                ('owner_email', models.CharField(max_length=255, unique=True)),
                ('api_key', api.fields.EncryptedTextField(blank=True, default='')),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={'db_table': 'meteo_credentials'},
        ),
    ]
