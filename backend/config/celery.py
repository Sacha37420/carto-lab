"""Application Celery pour les tâches asynchrones (imports Météo-France, calculs longs)."""
import os

from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

app = Celery('carto_lab')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()
