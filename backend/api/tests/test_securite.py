"""Relecture de sécurité (2026-11-20).

- GDAL choisit son pilote d'après le contenu : un fichier déposé sous une extension permise mais qui
  est un VRT désigne d'autres sources (fichier du conteneur, adresse du réseau interne). Il est refusé
  à l'import (vecteur et raster) et à la vectorisation d'un raster lu dans storage.
- Pas d'annuaire : la liste des recettes ne rend plus l'adresse de leurs auteurs aux autres membres.
- Le texte d'une exception `requests` (qui porte l'URL demandée) n'est plus rendu au client.
"""
import shutil
import tempfile
from unittest import mock

import requests

from api import meteo_client
from api.models import Layer, Recipe

from .helpers import ALICE, LabTestCase, envoi, geotiff


class VrtDeguiseTests(LabTestCase):

    def setUp(self):
        super().setUp()
        self.dossier = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dossier, ignore_errors=True)
        self.csv = f'{self.dossier}/source.csv'
        with open(self.csv, 'w') as f:
            f.write('x,y,nom\n2.35,48.85,interne\n')
        self.tif = f'{self.dossier}/source.tif'
        with open(self.tif, 'wb') as f:
            f.write(geotiff([[1, 2], [3, 4]]))

    def vrt_vecteur(self) -> bytes:
        return (f'<OGRVRTDataSource><OGRVRTLayer name="source">'
                f'<SrcDataSource>{self.csv}</SrcDataSource><GeometryType>wkbPoint</GeometryType>'
                f'<LayerSRS>EPSG:4326</LayerSRS><GeometryField encoding="PointFromColumns" x="x" y="y"/>'
                f'</OGRVRTLayer></OGRVRTDataSource>').encode()

    def vrt_raster(self) -> bytes:
        return (f'<VRTDataset rasterXSize="2" rasterYSize="2"><VRTRasterBand dataType="Byte" band="1">'
                f'<SimpleSource><SourceFilename relativeToVRT="0">{self.tif}</SourceFilename>'
                f'<SourceBand>1</SourceBand></SimpleSource></VRTRasterBand></VRTDataset>').encode()

    def test_vrt_sous_une_extension_vectorielle_refuse(self):
        for nom in ('piege.geojson', 'piege.json', 'piege.gml'):
            with self.subTest(nom=nom):
                resp = self.alice.post('/api/layers/', {'file': envoi(nom, self.vrt_vecteur())},
                                       format='multipart')
                self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(Layer.objects.count(), 0)

    def test_vrt_sous_une_extension_raster_refuse(self):
        resp = self.alice.post('/api/layers/', {'file': envoi('piege.tif', self.vrt_raster(), 'image/tiff')},
                               format='multipart')
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(Layer.objects.count(), 0)
        self.assertEqual(self.storage.appels, [])          # rien n'est parti vers storage

    def test_vectorisation_d_un_vrt_lu_dans_storage_refusee(self):
        self.storage.files['rasters/piege.tif'] = self.vrt_raster()
        source = Layer.objects.create(name='R', layer_type='raster', raster_path='rasters/piege.tif')
        resp = self.alice.post('/api/processings/run/', {'operation': 'polygonize', 'inputs': [source.pk],
                                                         'params': {}}, format='json')
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(Layer.objects.count(), 1)


class RecettesSansAnnuaireTests(LabTestCase):

    def test_l_adresse_de_l_auteur_n_est_rendue_qu_a_lui(self):
        resp = self.alice.post('/api/recipes/', {'name': 'R', 'steps': []}, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()['owner_email'], ALICE)
        rid = resp.json()['id']
        self.assertEqual(self.alice.get(f'/api/recipes/{rid}/').json()['owner_email'], ALICE)
        self.assertEqual(self.bob.get(f'/api/recipes/{rid}/').json()['owner_email'], '')
        self.assertEqual([r['owner_email'] for r in self.bob.get('/api/recipes/').json()], [''])
        self.assertEqual(Recipe.objects.get(pk=rid).owner_email, ALICE)


class ErreursReseauSansUrlTests(LabTestCase):

    def test_storage_injoignable_sans_l_adresse_interne(self):
        self.storage.post = mock.Mock(side_effect=requests.ConnectionError(
            'HTTPConnectionPool(host=storage.test): Max retries exceeded with url: /api/files/carto-lab/'))
        resp = self.alice.post('/api/layers/', {'file': envoi('mnt.tif', geotiff([[1]]), 'image/tiff')},
                               format='multipart')
        self.assertEqual(resp.status_code, 502)
        self.assertIn('Stockage indisponible', resp.json()['detail'])
        self.assertNotIn('storage.test', resp.json()['detail'])

    def test_portail_meteo_injoignable_sans_l_adresse(self):
        with mock.patch.object(meteo_client.requests, 'post', side_effect=requests.ConnectionError(
                'Max retries exceeded with url: https://portail.test/token?client=x')):
            with self.assertRaises(meteo_client.MeteoError) as erreur:
                meteo_client.fetch_access_token('abc', token_url='https://portail.test/token')
        self.assertNotIn('portail.test', str(erreur.exception))
