"""Outillage commun : jetons signés localement, faux ``storage``, faux Redis, données SIG."""
from __future__ import annotations

import io
import json
import time
from unittest import mock

import jwt
import requests
from cryptography.hazmat.primitives.asymmetric import rsa
from django.contrib.gis.geos import GEOSGeometry
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from api import secret_store, storage_client
from api.authentication import KeycloakJWTAuthentication
from api.models import Feature, Layer

CLIENT_ID = 'carto-lab'
GROUPE = 'developers'
ALICE = 'alice@ssolab.local'
BOB = 'bob@ssolab.local'

RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
FOREIGN_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

_ABSENT = object()


class LocalJwks:
    """Tient lieu de ``PyJWKClient`` : la clé publique locale, sans réseau."""

    class _Key:
        key = RSA_KEY.public_key()

    def get_signing_key_from_jwt(self, token):
        return self._Key()


def make_token(email=_ABSENT, *, azp=CLIENT_ID, groups=_ABSENT, exp_in=300, key=RSA_KEY) -> str:
    if email is _ABSENT:
        email = ALICE
    claims = {'azp': azp, 'exp': int(time.time()) + exp_in}
    if email is not None:
        claims['email'] = email
        claims['preferred_username'] = email.split('@')[0]
    if groups is _ABSENT:
        groups = [GROUPE]
    if groups is not None:
        claims['groups'] = groups
    return jwt.encode(claims, key, algorithm='RS256')


def client_for(email: str = ALICE, **kwargs) -> APIClient:
    client = APIClient()
    client.token = make_token(email, **kwargs)
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {client.token}')
    return client


class Reponse:
    def __init__(self, status_code=200, payload=None, content=b'', text='', headers=None):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.text = text or ('' if payload is None else json.dumps(payload))
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError('pas de JSON')
        return self._payload

    def iter_content(self, chunk_size=65536):
        yield self.content


class FakeStorage:
    """L'API fichiers de ``storage`` vue par ``storage_client``, en mémoire.
    ``files[chemin]`` = octets ; ``appels`` = (méthode, chemin, en-tête Authorization)."""

    RequestException = requests.RequestException

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.appels: list[tuple[str, str, str]] = []
        self.down = False

    def _route(self, method, url, **kwargs):
        prefix = 'http://storage.test/api/files/carto-lab/'
        assert url.startswith(prefix), f'URL storage inattendue : {url}'
        rest = url[len(prefix):]
        auth = (kwargs.get('headers') or {}).get('Authorization', '')
        if self.down:
            raise requests.ConnectionError('storage arrêté (simulé)')
        if method == 'POST':
            path = kwargs['data']['relative_path']
            self.appels.append((method, path, auth))
            self.files[path] = kwargs['files']['file'][1].read()
            return Reponse(201, {'relative_path': path})
        assert rest.startswith('content/'), rest
        path = rest[len('content/'):]
        self.appels.append((method, path, auth))
        if method == 'GET':
            return Reponse(200, content=self.files[path]) if path in self.files else Reponse(404)
        return Reponse(204) if self.files.pop(path, None) is not None else Reponse(404)

    def post(self, url, **kw):
        return self._route('POST', url, **kw)

    def get(self, url, **kw):
        return self._route('GET', url, **kw)

    def delete(self, url, **kw):
        return self._route('DELETE', url, **kw)


class FakeRedis:
    """Les trois commandes dont ``secret_store`` se sert."""

    def __init__(self):
        self.data: dict[str, bytes] = {}
        self.ttl: dict[str, int] = {}

    def setex(self, key, ttl, value):
        self.data[key] = value.encode() if isinstance(value, str) else value
        self.ttl[key] = ttl

    def get(self, key):
        return self.data.get(key)

    def delete(self, key):
        self.data.pop(key, None)


class LabTestCase(TestCase):
    """JWKS local, faux storage, faux Redis, réseau fermé, aucune attente."""

    def setUp(self):
        super().setUp()
        self._patch(mock.patch.object(KeycloakJWTAuthentication, '_get_jwks_client',
                                      classmethod(lambda cls: LocalJwks())))
        self.storage = FakeStorage()
        self._patch(mock.patch.object(storage_client, 'requests', self.storage))
        self.redis = FakeRedis()
        self._patch(mock.patch.object(secret_store, '_client', self.redis))
        self.network = self._patch(mock.patch(
            'requests.sessions.Session.request',
            side_effect=requests.ConnectionError('réseau fermé pendant les tests')))
        self.sleep = self._patch(mock.patch('time.sleep'))
        self.alice = client_for(ALICE)
        self.bob = client_for(BOB)

    def _patch(self, patcher):
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started


# ── Données SIG ───────────────────────────────────────────────────────────────

def couche(name='Couche', geoms=(), props=None, **extra) -> Layer:
    """Couche vectorielle créée directement en base. `geoms` : WKT en 4326."""
    layer = Layer.objects.create(name=name, layer_type=Layer.VECTOR, srid_source=4326,
                                 feature_count=len(geoms), **extra)
    for i, wkt in enumerate(geoms):
        p = props[i] if props else {'n': i}
        Feature.objects.create(layer=layer, geom=GEOSGeometry(wkt, srid=4326), properties=p)
    return layer


def carre(x, y, c=1.0) -> str:
    return f'POLYGON(({x} {y}, {x + c} {y}, {x + c} {y + c}, {x} {y + c}, {x} {y}))'


def geojson(features, crs=None) -> bytes:
    doc = {'type': 'FeatureCollection', 'features': [
        {'type': 'Feature', 'geometry': g, 'properties': p} for g, p in features]}
    if crs:
        doc['crs'] = {'type': 'name', 'properties': {'name': f'urn:ogc:def:crs:EPSG::{crs}'}}
    return json.dumps(doc).encode()


def envoi(nom: str, contenu: bytes, type_='application/octet-stream') -> SimpleUploadedFile:
    return SimpleUploadedFile(nom, contenu, content_type=type_)


def geotiff(valeurs, *, epsg=4326, nodata=None, origine=(2.0, 48.0), pas=0.5) -> bytes:
    """GeoTIFF d'une bande en mémoire. `valeurs` : liste de lignes."""
    import numpy as np
    import rasterio
    from rasterio.io import MemoryFile
    from rasterio.transform import from_origin

    arr = np.asarray(valeurs, dtype='uint8')
    profil = {'driver': 'GTiff', 'height': arr.shape[0], 'width': arr.shape[1], 'count': 1,
              'dtype': 'uint8', 'transform': from_origin(origine[0], origine[1], pas, pas)}
    if epsg:
        profil['crs'] = rasterio.crs.CRS.from_epsg(epsg)
    if nodata is not None:
        profil['nodata'] = nodata
    with MemoryFile() as mem:
        with mem.open(**profil) as dst:
            dst.write(arr, 1)
        return mem.read()
