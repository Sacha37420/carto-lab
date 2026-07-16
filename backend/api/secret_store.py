"""
Stockage ÉPHÉMÈRE de la clé API Météo-France (Feature 4 / SÉCURITÉ).

La clé n'est jamais en base ni dans les arguments (loggables) d'une tâche Celery :
la vue la dépose dans Redis sous un jeton opaque à courte durée de vie ; seule ce
jeton transite comme argument de tâche. Le worker consomme (GET+DEL) la clé.
"""
import secrets

import redis
from django.conf import settings

_client = redis.Redis.from_url(settings.REDIS_SECRETS_URL)
_PREFIX = 'mfkey:'


def put(value: str, ttl: int = 3600) -> str:
    token = secrets.token_urlsafe(24)
    _client.setex(_PREFIX + token, ttl, value)
    return token


def take(token: str) -> str | None:
    key = _PREFIX + token
    val = _client.get(key)
    _client.delete(key)
    return val.decode() if val else None
