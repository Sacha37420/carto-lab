from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [('api', '0006_reader_role')]

    operations = [
        migrations.AddField(
            model_name='layer',
            name='raster_path',
            field=models.CharField(max_length=1024, null=True, blank=True),
        ),
        migrations.RemoveField(
            model_name='layer',
            name='raster_file',
        ),
    ]
