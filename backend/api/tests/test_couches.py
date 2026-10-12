"""Couches : import (formats, limites, CRS), GeoJSON reprojeté, rasters dans
``storage``, et ce que l'API expose — jamais un jeton.

Autorisation propre à l'app : « carte commune ». Tout membre du groupe lit,
calcule et supprime toutes les couches ; il n'y a pas de cloisonnement par
propriétaire, et c'est voulu (CLAUDE.md, « Partage lié à un groupe Keycloak »).
"""
import io
import zipfile
from unittest import mock

from api import geo
from api.models import Department, Feature, Layer, UserRecord

from .helpers import ALICE, BOB, LabTestCase, couche, envoi, geojson, geotiff

POINT_PARIS = {'type': 'Point', 'coordinates': [2.3522, 48.8566]}
PARIS_L93 = (652469.0, 6862035.0)      # même point en Lambert-93, à quelques mètres près


class ImportVecteurTests(LabTestCase):

    def importer(self, nom, contenu, client=None, **champs):
        return (client or self.alice).post('/api/layers/', {'file': envoi(nom, contenu), **champs},
                                           format='multipart')

    def test_geojson(self):
        contenu = geojson([(POINT_PARIS, {'ville': 'Paris', 'pop': 2100000}),
                           ({'type': 'Point', 'coordinates': [0.69, 47.39]}, {'ville': 'Tours'})])
        resp = self.importer('villes.geojson', contenu)
        self.assertEqual(resp.status_code, 201, resp.content)
        corps = resp.json()
        self.assertEqual((corps['name'], corps['layer_type'], corps['origin']),
                         ('villes.geojson', 'vector', 'upload'))
        self.assertEqual((corps['feature_count'], corps['geom_type'], corps['srid_source']),
                         (2, 'Point', 4326))
        self.assertEqual(corps['bbox'], [0.69, 47.39, 2.3522, 48.8566])
        self.assertEqual(corps['metadata'], {'source_format': 'geojson'})
        layer = Layer.objects.get(pk=corps['id'])
        self.assertEqual(layer.owner_email, ALICE)                       # vient du jeton
        self.assertEqual(sorted(f.properties['ville'] for f in layer.features.all()),
                         ['Paris', 'Tours'])
        self.assertEqual(self.storage.appels, [])                        # un vecteur reste en base

    def test_nom_choisi_et_proprietaire_non_falsifiable(self):
        resp = self.importer('a.geojson', geojson([(POINT_PARIS, {})]), name='Ma couche',
                             owner_email='bob@ssolab.local')
        self.assertEqual(resp.json()['name'], 'Ma couche')
        self.assertEqual(Layer.objects.get(pk=resp.json()['id']).owner_email, ALICE)

    def test_geojson_en_lambert_93_reprojete(self):
        contenu = geojson([({'type': 'Point', 'coordinates': list(PARIS_L93)}, {})], crs=2154)
        resp = self.importer('l93.geojson', contenu)
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()['srid_source'], 2154)               # trace gardée
        geom = Feature.objects.get(layer_id=resp.json()['id']).geom      # stocké en 4326
        self.assertAlmostEqual(geom.x, 2.3522, places=3)
        self.assertAlmostEqual(geom.y, 48.8566, places=3)

    def test_csv_ponctuel(self):
        contenu = ('Nom;Latitude;LONGITUDE;alt\n'
                   'Paris;48,8566;2,3522;35\n'
                   'Sans coordonnées;;;0\n'
                   'Tours;47.39;0.69;50\n').encode('utf-8-sig')
        resp = self.importer('stations.csv', contenu)
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()['feature_count'], 2)                # la ligne vide est ignorée
        paris = Feature.objects.get(layer_id=resp.json()['id'], properties__Nom='Paris')
        self.assertEqual((paris.geom.x, paris.geom.y), (2.3522, 48.8566))  # virgule décimale
        self.assertEqual(paris.properties, {'Nom': 'Paris', 'alt': '35'})

    def test_csv_dans_un_autre_crs(self):
        contenu = f'x,y\n{PARIS_L93[0]},{PARIS_L93[1]}\n'.encode()
        resp = self.importer('l93.csv', contenu, source_srid=2154)
        self.assertEqual(resp.json()['srid_source'], 2154)
        geom = Feature.objects.get(layer_id=resp.json()['id']).geom
        self.assertAlmostEqual(geom.x, 2.3522, places=3)

    def test_refus(self):
        kmz = io.BytesIO()
        with zipfile.ZipFile(kmz, 'w') as zf:
            zf.writestr('lisezmoi.txt', 'rien')
        cas = {
            'extension interdite': ('script.py', b'print(1)', {}),
            'shapefile nu': ('a.shp', b'x', {}),
            'sans extension': ('couche', geojson([(POINT_PARIS, {})]), {}),
            'double extension': ('couche.geojson.exe', geojson([(POINT_PARIS, {})]), {}),
            'CSV sans colonnes de coordonnées': ('a.csv', b'nom,valeur\nParis,3\n', {}),
            'CSV sans ligne valide': ('a.csv', b'lat,lon\nx,y\n', {}),
            'CRS forcé inconnu': ('a.csv', b'lat,lon\n1,2\n', {'source_srid': 999999}),
            'CRS forcé illisible': ('a.csv', b'lat,lon\n1,2\n', {'source_srid': 'abc'}),
            'KMZ sans KML': ('a.kmz', kmz.getvalue(), {}),
            'GeoJSON sans entité': ('vide.geojson', geojson([]), {}),
        }
        for nom, (fichier, contenu, champs) in cas.items():
            with self.subTest(cas=nom):
                resp = self.importer(fichier, contenu, **champs)
                self.assertEqual(resp.status_code, 400, resp.content)
                self.assertTrue(resp.json()['detail'])
        self.assertEqual(self.alice.post('/api/layers/', {}, format='multipart').status_code, 400)
        self.assertEqual(Layer.objects.count(), 0)
        self.assertEqual(Feature.objects.count(), 0)

    def test_taille_maximale(self):
        contenu = geojson([(POINT_PARIS, {})])
        with mock.patch.object(geo, 'MAX_UPLOAD_BYTES', len(contenu) - 1):
            self.assertEqual(self.importer('a.geojson', contenu).status_code, 400)
        with mock.patch.object(geo, 'MAX_UPLOAD_BYTES', len(contenu)):
            self.assertEqual(self.importer('a.geojson', contenu).status_code, 201)
        self.assertEqual(Layer.objects.count(), 1)

    def test_fichier_illisible_refuse_proprement(self):
        # Corrigé (C1) : l'exception de GDAL n'était pas attrapée (500).
        cas = {
            'GeoJSON tronqué': ('casse.geojson', b'{"type": "FeatureCollection", "features": ['),
            'GeoJSON qui n’en est pas': ('casse.json', b'ceci n est pas du JSON'),
            'GeoPackage binaire quelconque': ('casse.gpkg', b'\x00\x01\x02'),
            'KML vide': ('casse.kml', b''),
            'archive qui n’en est pas une': ('casse.zip', b'pas une archive'),
            'KMZ qui n’est pas une archive': ('casse.kmz', b'pas une archive'),
            'CSV binaire': ('casse.csv', b'lat,lon\n\xff\xfe\x00,1\n'),
            'GeoTIFF qui n’en est pas un': ('casse.tif', b'pas un raster'),
        }
        for nom, (fichier, contenu) in cas.items():
            with self.subTest(cas=nom):
                resp = self.importer(fichier, contenu)
                self.assertEqual(resp.status_code, 400, resp.content)
                self.assertTrue(resp.json()['detail'])
        self.assertEqual(Layer.objects.count(), 0)
        self.assertEqual(self.storage.appels, [])


class CatalogueTests(LabTestCase):

    def setUp(self):
        super().setUp()
        self.layer = couche('Points', ['POINT(2.3522 48.8566)', 'POINT(0.69 47.39)'],
                            props=[{'ville': 'Paris'}, {'ville': 'Tours'}], owner_email=ALICE)

    def test_carte_commune_tout_membre_lit_et_supprime(self):
        # Voulu : pas de cloisonnement par propriétaire dans cette app.
        self.assertEqual([l['id'] for l in self.bob.get('/api/layers/').json()], [self.layer.pk])
        self.assertEqual(self.bob.get(f'/api/layers/{self.layer.pk}/').status_code, 200)
        self.assertEqual(self.bob.delete(f'/api/layers/{self.layer.pk}/').status_code, 204)
        self.assertEqual(Feature.objects.count(), 0)                      # entités en cascade
        self.assertEqual(self.storage.appels, [])
        self.assertEqual(self.bob.get(f'/api/layers/{self.layer.pk}/').status_code, 404)
        self.assertEqual(self.bob.delete(f'/api/layers/{self.layer.pk}/').status_code, 404)

    def test_le_catalogue_ne_montre_ni_proprietaire_ni_chemin_interne(self):
        Layer.objects.create(name='R', layer_type='raster', raster_path='rasters/x.tif')
        for ligne in self.bob.get('/api/layers/').json():
            self.assertNotIn('owner_email', ligne)
            self.assertNotIn('raster_path', ligne)

    def test_la_couche_n_est_pas_modifiable_par_l_api(self):
        url = f'/api/layers/{self.layer.pk}/'
        for methode in ('put', 'patch', 'post'):
            with self.subTest(methode=methode):
                resp = getattr(self.alice, methode)(url, {'name': 'x', 'metadata': {'a': 1}},
                                                    format='json')
                self.assertEqual(resp.status_code, 405)

    def test_geojson_en_4326_par_defaut(self):
        resp = self.alice.get(f'/api/layers/{self.layer.pk}/geojson/')
        self.assertEqual(resp.status_code, 200)
        corps = resp.json()
        self.assertEqual(corps['crs']['properties']['name'], 'EPSG:4326')
        paris = next(f for f in corps['features'] if f['properties']['ville'] == 'Paris')
        self.assertEqual(paris['geometry'], {'type': 'Point', 'coordinates': [2.3522, 48.8566]})

    def test_geojson_reprojete_a_la_demande(self):
        corps = self.alice.get(f'/api/layers/{self.layer.pk}/geojson/', {'srid': 2154}).json()
        self.assertEqual(corps['crs']['properties']['name'], 'EPSG:2154')
        paris = next(f for f in corps['features'] if f['properties']['ville'] == 'Paris')
        x, y = paris['geometry']['coordinates']
        self.assertAlmostEqual(x, PARIS_L93[0], delta=5)
        self.assertAlmostEqual(y, PARIS_L93[1], delta=5)
        # La reprojection est faite sur une copie : la base reste en 4326.
        self.assertEqual(Feature.objects.get(properties__ville='Paris').geom.x, 2.3522)

    def test_geojson_refus(self):
        raster = Layer.objects.create(name='R', layer_type='raster', raster_path='rasters/x.tif')
        self.assertEqual(self.alice.get(f'/api/layers/{raster.pk}/geojson/').status_code, 400)
        for srid in ('abc', '999999', '0'):
            with self.subTest(srid=srid):
                resp = self.alice.get(f'/api/layers/{self.layer.pk}/geojson/', {'srid': srid})
                self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.alice.get('/api/layers/999999/geojson/').status_code, 404)

    def test_identite_et_annuaire(self):
        moi = self.alice.get('/api/me/', {'email': BOB}).json()
        self.assertEqual((moi['email'], moi['is_new'], moi['department']), (ALICE, True, None))
        self.assertFalse(self.alice.get('/api/me/').json()['is_new'])
        self.assertEqual(UserRecord.objects.filter(email=ALICE).count(), 1)
        # L'annuaire hérité du gabarit (adresses de tous les comptes) n'existe plus.
        for chemin in ('/api/users/', '/api/departments/'):
            self.assertEqual(self.bob.get(chemin).status_code, 404)


class CrsTests(LabTestCase):

    def test_liste_et_description(self):
        srids = [c['srid'] for c in self.alice.get('/api/crs/').json()]
        self.assertIn(2154, srids)
        self.assertEqual([s for s in range(3942, 3951) if s in srids], list(range(3942, 3951)))
        l93 = self.alice.get('/api/crs/', {'srid': 2154}).json()
        self.assertEqual((l93['srid'], l93['projected'], l93['geographic']), (2154, True, False))
        # Hors de la liste proposée, tout EPSG valide est accepté.
        self.assertTrue(self.alice.get('/api/crs/', {'srid': 32630}).json()['projected'])
        for srid in ('999999', 'abc', '-1'):
            with self.subTest(srid=srid):
                self.assertEqual(self.alice.get('/api/crs/', {'srid': srid}).status_code, 400)

    def transformer(self, **corps):
        return self.alice.post('/api/crs/transform-point/', corps, format='json')

    def test_conversion_d_un_point(self):
        aller = self.transformer(x=2.3522, y=48.8566, from_srid=4326, to_srid=2154).json()
        self.assertEqual(aller['srid'], 2154)
        self.assertAlmostEqual(aller['x'], PARIS_L93[0], delta=5)
        self.assertAlmostEqual(aller['y'], PARIS_L93[1], delta=5)
        retour = self.transformer(x=aller['x'], y=aller['y'], from_srid=2154, to_srid=4326).json()
        self.assertAlmostEqual(retour['x'], 2.3522, places=6)             # x = longitude, toujours
        self.assertAlmostEqual(retour['y'], 48.8566, places=6)

    def test_conversion_refus(self):
        cas = [
            {'x': 1, 'y': 2, 'from_srid': 4326},
            {'x': 'a', 'y': 2, 'from_srid': 4326, 'to_srid': 2154},
            {'x': 1, 'y': None, 'from_srid': 4326, 'to_srid': 2154},
            {'x': 1, 'y': 2, 'from_srid': 999999, 'to_srid': 2154},
        ]
        for corps in cas:
            with self.subTest(corps=corps):
                self.assertEqual(self.transformer(**corps).status_code, 400)


class RasterTests(LabTestCase):
    """Les rasters vont dans le partage storage ``carto-lab``, avec le jeton de
    l'utilisateur lui-même (pas de compte de service) : storage applique donc
    ses propres règles à CET utilisateur."""

    def setUp(self):
        super().setUp()
        self.tif = geotiff([[1, 2], [3, 4]])

    def importer(self, client=None, nom='mnt.tif'):
        return (client or self.alice).post(
            '/api/layers/', {'file': envoi(nom, self.tif, 'image/tiff')}, format='multipart')

    def test_import_d_un_geotiff(self):
        resp = self.importer()
        self.assertEqual(resp.status_code, 201, resp.content)
        corps = resp.json()
        layer = Layer.objects.get(pk=corps['id'])
        self.assertRegex(layer.raster_path, r'^rasters/[0-9a-f]{32}-mnt\.tif$')
        self.assertEqual(self.storage.files[layer.raster_path], self.tif)   # octets intacts
        self.assertEqual((corps['layer_type'], corps['srid_source'], corps['feature_count']),
                         ('raster', 4326, 0))
        self.assertEqual(corps['bbox'], [2.0, 47.0, 3.0, 48.0])
        self.assertEqual(corps['metadata'], {'source_format': 'geotiff', 'width': 2, 'height': 2,
                                             'bands': 1, 'dtypes': ['uint8']})
        # Lecture : adresse storage directe, même origine que l'app.
        self.assertEqual(corps['raster_url'],
                         f'http://testserver/storage-api/api/files/carto-lab/content/{layer.raster_path}')
        self.assertEqual(Feature.objects.count(), 0)

    def test_storage_recoit_le_jeton_de_l_appelant(self):
        self.importer(self.bob)
        layer = Layer.objects.get()
        self.assertEqual(self.storage.appels,
                         [('POST', layer.raster_path, f'Bearer {self.bob.token}')])
        # Alice supprime la couche de Bob : c'est SON jeton qui part vers storage.
        self.assertEqual(self.alice.delete(f'/api/layers/{layer.pk}/').status_code, 204)
        self.assertEqual(self.storage.appels[-1],
                         ('DELETE', layer.raster_path, f'Bearer {self.alice.token}'))
        self.assertEqual(self.storage.files, {})
        self.assertEqual(Layer.objects.count(), 0)

    def test_le_nom_du_fichier_ne_sort_pas_du_dossier(self):
        self.importer(nom='../../etc/passwd.tif')
        chemin = Layer.objects.get().raster_path
        self.assertTrue(chemin.startswith('rasters/'))
        self.assertEqual(chemin.count('/'), 1)
        self.assertNotIn('..', chemin)

    def test_raster_deja_absent_de_storage_la_couche_part_quand_meme(self):
        layer = Layer.objects.create(name='R', layer_type='raster', raster_path='rasters/perdu.tif')
        self.assertEqual(self.alice.delete(f'/api/layers/{layer.pk}/').status_code, 204)

    def test_storage_en_panne_erreur_propre_et_etat_coherent(self):
        # Corrigé (C2) : StorageClientError n'était pas attrapée (500 brut).
        gardee = Layer.objects.create(name='R', layer_type='raster', raster_path='rasters/a.tif')
        self.storage.files['rasters/a.tif'] = b'x'
        self.storage.down = True
        resp = self.importer()
        self.assertEqual(resp.status_code, 502)
        self.assertIn('Stockage indisponible', resp.json()['detail'])
        self.assertEqual(Layer.objects.count(), 1)             # pas de fiche sans fichier
        resp = self.alice.delete(f'/api/layers/{gardee.pk}/')
        self.assertEqual(resp.status_code, 502)
        self.assertTrue(Layer.objects.filter(pk=gardee.pk).exists())   # pas de fichier sans fiche
        # Le jeton de l'appelant n'est pas recopié dans le message d'erreur.
        self.assertNotIn(self.alice.token, resp.content.decode())
        # Un vecteur ne dépend pas de storage : il se supprime toujours.
        vecteur = Layer.objects.create(name='V')
        self.assertEqual(self.alice.delete(f'/api/layers/{vecteur.pk}/').status_code, 204)

    def test_storage_refuse_l_ecriture(self):
        # storage répond mais refuse (jeton sans droit sur le partage) : même 502.
        from .helpers import Reponse
        self.storage.post = lambda url, **kw: Reponse(403, text='refusé')
        self.assertEqual(self.importer().status_code, 502)
        self.assertEqual(Layer.objects.count(), 0)

    def test_aucun_jeton_dans_ce_que_l_api_expose(self):
        """Le jeton de l'appelant traverse l'import, la suppression et la
        vectorisation ; il ne doit rester ni en base ni dans une réponse."""
        self.importer()
        raster = Layer.objects.get()
        produit = self.alice.post('/api/processings/run/', {
            'operation': 'polygonize', 'inputs': [raster.pk], 'params': {'band': 1}},
            format='json')
        self.assertEqual(produit.status_code, 201, produit.content)
        recette = self.alice.post('/api/recipes/', {'name': 'R', 'steps': [
            {'op': 'polygonize', 'inputs': [{'layer': raster.pk}], 'params': {}}]}, format='json')
        self.alice.post(f"/api/recipes/{recette.json()['id']}/run/")
        jeton = self.alice.token
        for chemin in ('/api/layers/', '/api/recipes/', '/api/jobs/',
                       f"/api/layers/{produit.json()['id']}/"):
            with self.subTest(chemin=chemin):
                texte = self.alice.get(chemin).content.decode()
                self.assertNotIn(jeton, texte)
                self.assertNotIn('Bearer', texte)
        for layer in Layer.objects.all():
            self.assertNotIn(jeton, str(layer.metadata))
            self.assertNotIn('Bearer', str(layer.metadata))
        self.assertGreaterEqual(Layer.objects.count(), 3)
