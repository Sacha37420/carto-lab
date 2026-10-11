"""Moteur de calculs (SQL PostGIS réel) et recettes.

Chaque traitement produit TOUJOURS une nouvelle couche ; un traitement qui
échoue n'en laisse aucune. Les géométries de test sont des carrés en degrés
près de l'équateur, où 1° ≈ 111 km dans les deux directions.
"""
import math
import unittest

from api import processing
from api.models import Feature, Layer, Recipe

from .helpers import ALICE, BOB, LabTestCase, carre, couche, geotiff


class Base(LabTestCase):

    def run_op(self, operation, inputs, params=None, client=None, **extra):
        return (client or self.alice).post('/api/processings/run/', {
            'operation': operation, 'inputs': [l.pk if isinstance(l, Layer) else l for l in inputs],
            'params': params or {}, **extra}, format='json')

    def ok(self, *args, **kwargs) -> Layer:
        resp = self.run_op(*args, **kwargs)
        self.assertEqual(resp.status_code, 201, resp.content)
        return Layer.objects.get(pk=resp.json()['id'])

    def geoms(self, layer):
        return [f.geom for f in layer.features.order_by('id')]


class OperationsTests(Base):

    def test_catalogue(self):
        ops = {o['name']: o for o in self.alice.get('/api/processings/').json()}
        self.assertEqual(set(ops), set(processing.OPERATIONS))
        self.assertEqual(len(ops), 14)
        self.assertEqual((ops['clip']['inputs'], ops['buffer']['inputs']), (2, 1))
        self.assertEqual(ops['buffer']['params'][0]['name'], 'distance_m')
        for op in ops.values():
            self.assertNotIn('func', op)

    def test_resultat_est_une_nouvelle_couche_tracee(self):
        source = couche('Source', ['POINT(0 0)'], owner_email=BOB)
        out = self.ok('centroid', [source])
        self.assertNotEqual(out.pk, source.pk)
        self.assertEqual((out.origin, out.layer_type, out.owner_email), ('calcul', 'vector', ALICE))
        self.assertEqual(out.name, 'Centroïdes — Source')
        self.assertEqual(out.metadata, {'operation': 'centroid', 'inputs': [source.pk], 'params': {}})
        self.assertEqual(source.features.count(), 1)                    # la source n'est pas touchée
        nomme = self.ok('centroid', [source], name='Mon résultat')
        self.assertEqual(nomme.name, 'Mon résultat')

    def test_tampon_en_metres_et_mesures(self):
        tampon = self.ok('buffer', [couche('P', ['POINT(0 0)'], props=[{'nom': 'origine'}])],
                         {'distance_m': 1000})
        self.assertEqual((tampon.feature_count, tampon.geom_type), (1, 'POLYGON'))
        mesure = self.ok('measure', [tampon])
        props = mesure.features.get().properties
        self.assertEqual(props['nom'], 'origine')                       # attributs repris
        self.assertAlmostEqual(props['aire_m2'], math.pi * 1000 ** 2, delta=0.02 * math.pi * 1e6)
        self.assertAlmostEqual(props['perimetre_m'], 2 * math.pi * 1000, delta=0.02 * 6283)
        # Emprise ≈ ±1000 m autour de l'origine, soit ±0,009°.
        self.assertAlmostEqual(tampon.bbox[2], 0.009, delta=0.0005)

    def test_centroide_enveloppe_et_enveloppe_convexe(self):
        source = couche('S', [carre(0, 0, 2), 'LINESTRING(10 10, 12 14)'])
        centres = self.geoms(self.ok('centroid', [source]))
        self.assertEqual([(c.x, c.y) for c in centres], [(1.0, 1.0), (11.0, 12.0)])
        emprises = self.geoms(self.ok('envelope', [source]))
        self.assertEqual(emprises[1].extent, (10.0, 10.0, 12.0, 14.0))
        coque = self.ok('convex_hull', [couche('P', ['POINT(0 0)', 'POINT(4 0)', 'POINT(0 3)',
                                                     'POINT(1 1)'])])
        self.assertEqual(coque.feature_count, 1)
        self.assertAlmostEqual(self.geoms(coque)[0].area, 6.0)          # le point intérieur n'ajoute rien

    def test_decoupage(self):
        out = self.ok('clip', [couche('A', [carre(0, 0, 2), carre(10, 10, 1)],
                                      props=[{'nom': 'grand'}, {'nom': 'loin'}]),
                               couche('Masque', [carre(1, 1, 2)])])
        self.assertEqual(out.feature_count, 1)                          # l'entité lointaine disparaît
        self.assertEqual(out.bbox, [1.0, 1.0, 2.0, 2.0])
        self.assertEqual(out.features.get().properties, {'nom': 'grand'})

    def test_difference(self):
        out = self.ok('difference', [couche('A', [carre(0, 0, 2), carre(10, 10, 1)]),
                                     couche('B', [carre(1, 0, 2)])])
        aires = sorted(round(g.area, 6) for g in self.geoms(out))
        self.assertEqual(aires, [1.0, 2.0])                             # 4 − 2 ; l'autre intacte

    def test_union_et_simplification(self):
        fusion = self.ok('dissolve', [couche('A', [carre(0, 0, 1), carre(1, 0, 1), carre(5, 5, 1)])])
        self.assertEqual(fusion.feature_count, 1)
        self.assertAlmostEqual(self.geoms(fusion)[0].area, 3.0)
        ligne = couche('L', ['LINESTRING(0 0, 1 0.0001, 2 0, 3 0.0001, 4 0)'])
        simple = self.ok('simplify', [ligne], {'tolerance': 0.01})
        self.assertEqual(self.geoms(simple)[0].num_coords, 2)

    def test_jointure_spatiale(self):
        points = couche('Pts', ['POINT(0.5 0.5)', 'POINT(9 9)'], props=[{'id': 'dedans'},
                                                                       {'id': 'dehors'}])
        zones = couche('Zones', [carre(0, 0, 1)], props=[{'commune': 'Tours'}])
        props = {f.properties['id']: f.properties
                 for f in self.ok('spatial_join', [points, zones]).features.all()}
        self.assertEqual(props['dedans'], {'id': 'dedans', 'commune': 'Tours'})
        self.assertEqual(props['dehors'], {'id': 'dehors'})             # gardé, sans attribut

    def test_voronoi_reprend_les_attributs_du_point(self):
        points = couche('Stations', ['POINT(0 0)', 'POINT(2 0)', 'POINT(1 2)', carre(5, 5)],
                        props=[{'valeur': 1}, {'valeur': 2}, {'valeur': 3}, {'valeur': 99}])
        out = self.ok('voronoi', [points])
        self.assertEqual(sorted(f.properties['valeur'] for f in out.features.all()), [1, 2, 3])
        for cellule in out.features.all():                              # chaque cellule contient SON point
            point = points.features.get(properties__valeur=cellule.properties['valeur']).geom
            self.assertTrue(cellule.geom.contains(point))

    def test_grille(self):
        out = self.ok('grid', [couche('E', [carre(0, 0, 1)])], {'cell_deg': 0.5})
        self.assertGreaterEqual(out.feature_count, 4)
        self.assertAlmostEqual(self.geoms(out)[0].area, 0.25)

    def test_tableau_croise_de_surfaces(self):
        parcelles = couche('Parcelles', [carre(0, 0, 0.02), carre(1, 1, 0.02)],
                           props=[{'id': 'p1'}, {'id': 'p2'}])
        occupation = couche('Occupation', [
            'POLYGON((0 0, 0.01 0, 0.01 0.02, 0 0.02, 0 0))',           # moitié ouest de p1
            'POLYGON((0.01 0, 0.02 0, 0.02 0.01, 0.01 0.01, 0.01 0))',  # quart sud-est de p1
            'POLYGON((0.005 0.015, 0.006 0.015, 0.006 0.016, 0.005 0.016, 0.005 0.015))',
            carre(50, 50, 0.01), carre(60, 60, 0.01),
        ], props=[{'type': 'forêt'}, {'type': 'eau'}, {'type': 'forêt'}, {'type': 'ville'},
                  {'autre': 'sans catégorie'}])
        out = self.ok('tabulate_intersection', [parcelles, occupation],
                      {'field': 'type', 'prefix': 'surf_'})
        props = {f.properties['id']: f.properties for f in out.features.all()}
        aire_p1 = self.ok('measure', [parcelles]).features.get(properties__id='p1') \
                      .properties['aire_m2']
        p1 = props['p1']
        self.assertEqual(set(p1), {'id', 'surf_forêt', 'surf_eau', 'surf_ville'})
        # Deux entités « forêt » qui se recouvrent sont fusionnées : pas de double compte.
        self.assertAlmostEqual(p1['surf_forêt'], aire_p1 / 2, delta=aire_p1 * 0.001)
        self.assertAlmostEqual(p1['surf_eau'], aire_p1 / 4, delta=aire_p1 * 0.001)
        self.assertEqual(p1['surf_ville'], 0)                           # présent, à zéro
        self.assertEqual({k: v for k, v in props['p2'].items() if k != 'id'},
                         {'surf_forêt': 0, 'surf_eau': 0, 'surf_ville': 0})

    def test_tableau_croise_refus(self):
        a = couche('A', [carre(0, 0)])
        fin = couche('Fin', [carre(i, 0, 0.5) for i in range(51)],
                     props=[{'code': f'c{i}'} for i in range(51)])
        for params in ({}, {'field': '  '}, {'field': 'inexistant'}, {'field': 'code'}):
            with self.subTest(params=params):
                self.assertEqual(self.run_op('tabulate_intersection', [a, fin], params).status_code,
                                 400)


class RefusTests(Base):
    """Un traitement refusé ou en échec ne laisse aucune couche derrière lui."""

    def setUp(self):
        super().setUp()
        self.a = couche('A', [carre(0, 0)])
        self.b = couche('B', [carre(5, 5)])
        self.avant = Layer.objects.count()

    def tearDown(self):
        self.assertEqual(Layer.objects.count(), self.avant)
        self.assertEqual(Feature.objects.count(), 2)
        super().tearDown()

    def test_refus(self):
        cas = {
            'opération inconnue': ('drop_table', [self.a], {}),
            'opération absente': (None, [self.a], {}),
            'fonction SQL non enregistrée': ('ST_Buffer', [self.a], {}),
            'une couche au lieu de deux': ('clip', [self.a], {}),
            'deux couches au lieu d’une': ('buffer', [self.a, self.b], {}),
            'aucune couche': ('buffer', [], {}),
            'couche introuvable': ('buffer', [999999], {}),
            'paramètre non numérique': ('buffer', [self.a], {'distance_m': 'cent'}),
            'injection dans un nombre': ('buffer', [self.a], {'distance_m': '1); DROP TABLE layers; --'}),
            'injection dans un nom de champ': ('tabulate_intersection', [self.a, self.b],
                                               {'field': "x'; DROP TABLE features; --"}),
            'résultat vide': ('clip', [self.a, self.b], {}),
            'différence totale': ('difference', [self.a, self.a], {}),
            'vectoriser un vecteur': ('polygonize', [self.a], {}),
            'paramètres mal formés': ('buffer', [self.a], 'pas un objet'),
        }
        for nom, (op, inputs, params) in cas.items():
            with self.subTest(cas=nom):
                resp = self.alice.post('/api/processings/run/', {
                    'operation': op, 'inputs': [getattr(l, 'pk', l) for l in inputs],
                    'params': params}, format='json')
                self.assertEqual(resp.status_code, 400, resp.content)
                self.assertTrue(resp.json()['detail'])
                self.assertEqual(Layer.objects.count(), self.avant)

    # DÉFAUT (rapport, C3) — `inputs` n'est pas validé : une valeur qui n'est pas
    # une liste d'identifiants fait lever ValueError/TypeError dans
    # _resolve_layers (seul Layer.DoesNotExist est attrapé) : 500 au lieu de 400.
    @unittest.expectedFailure
    def test_entrees_mal_formees_refusees_proprement(self):
        self.alice.raise_request_exception = False
        for inputs in ('abc', [{'layer': 1}], ['un']):
            resp = self.alice.post('/api/processings/run/', {
                'operation': 'buffer', 'inputs': inputs, 'params': {}}, format='json')
            self.assertEqual(resp.status_code, 400)


class VectorisationTests(Base):

    def raster(self, contenu, chemin='rasters/r.tif'):
        self.storage.files[chemin] = contenu
        return Layer.objects.create(name='Raster', layer_type='raster', raster_path=chemin)

    def test_un_polygone_par_zone_de_meme_valeur(self):
        source = self.raster(geotiff([[1, 1], [2, 0]], nodata=0))
        out = self.ok('polygonize', [source], client=self.bob)
        valeurs = sorted(f.properties['value'] for f in out.features.all())
        self.assertEqual(valeurs, [1.0, 2.0])                           # le nodata est ignoré
        zone1 = out.features.get(properties__value=1.0).geom
        self.assertEqual(zone1.extent, (2.0, 47.5, 3.0, 48.0))          # deux pixels de 0,5°
        self.assertEqual(out.srid_source, 4326)
        # Le raster est lu dans storage avec le jeton de celui qui lance le calcul.
        self.assertEqual(self.storage.appels, [('GET', 'rasters/r.tif', f'Bearer {self.bob.token}')])
        self.assertEqual(out.metadata['params'], {})

    def test_raster_projete_ramene_en_4326(self):
        source = self.raster(geotiff([[7]], epsg=2154, origine=(652000.0, 6863000.0), pas=1000.0))
        out = self.ok('polygonize', [source])
        self.assertEqual(out.srid_source, 2154)                         # provenance gardée
        x0, y0, x1, y1 = out.features.get().geom.extent
        self.assertAlmostEqual(x0, 2.345, delta=0.02)                   # Paris, en degrés
        self.assertAlmostEqual(y1, 48.865, delta=0.02)

    def test_refus(self):
        source = self.raster(geotiff([[0, 0]], nodata=0))
        avant = Layer.objects.count()
        self.assertEqual(self.run_op('polygonize', [source]).status_code, 400)       # tout nodata
        self.assertEqual(self.run_op('polygonize', [source], {'band': 2}).status_code, 400)
        self.storage.files.clear()
        self.assertEqual(self.run_op('polygonize', [source]).status_code, 400)       # fichier absent
        self.assertEqual(Layer.objects.count(), avant)


class RecettesTests(Base):

    def setUp(self):
        super().setUp()
        self.points = couche('Pts', ['POINT(0 0)', 'POINT(0.001 0)'])
        self.masque = couche('Masque', [carre(-1, -1, 2)])

    def creer(self, steps, client=None, **extra):
        return (client or self.alice).post('/api/recipes/', {'name': 'Chaîne', 'steps': steps,
                                                             **extra}, format='json')

    def test_creation(self):
        resp = self.creer([], owner_email=BOB, result_layer=self.points.pk)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual((resp.json()['owner_email'], resp.json()['result_layer']), (ALICE, None))
        self.assertEqual(self.creer(None).status_code, 400)
        # Carte commune : les recettes aussi sont à tout le groupe.
        self.assertEqual(len(self.bob.get('/api/recipes/').json()), 1)
        self.assertEqual(self.bob.delete(f"/api/recipes/{resp.json()['id']}/").status_code, 204)
        self.assertEqual(self.bob.get(f"/api/recipes/{resp.json()['id']}/").status_code, 404)
        self.assertEqual(self.bob.delete('/api/recipes/999999/').status_code, 404)
        self.assertEqual(self.bob.post('/api/recipes/999999/run/').status_code, 404)

    def test_une_etape_reprend_la_sortie_d_une_autre(self):
        recette = self.creer([
            {'op': 'buffer', 'inputs': [{'layer': self.points.pk}], 'params': {'distance_m': 500}},
            {'op': 'dissolve', 'inputs': [{'step': 0}]},
            {'op': 'clip', 'inputs': [{'step': 1}, {'layer': self.masque.pk}], 'name': 'Final'},
        ]).json()
        resp = self.bob.post(f"/api/recipes/{recette['id']}/run/")
        self.assertEqual(resp.status_code, 201, resp.content)
        final = Layer.objects.get(pk=resp.json()['id'])
        self.assertEqual((final.name, final.feature_count, final.owner_email), ('Final', 1, BOB))
        self.assertEqual((final.metadata['recipe'], final.metadata['recipe_step']), ('Chaîne', 2))
        self.assertEqual(final.metadata['operation'], 'clip')
        self.assertEqual(Recipe.objects.get(pk=recette['id']).result_layer, final)
        etapes = Layer.objects.filter(metadata__recipe='Chaîne').order_by('metadata__recipe_step')
        self.assertEqual([l.name for l in etapes], ['Chaîne — étape 1', 'Chaîne — étape 2', 'Final'])
        # Rejouable : une seconde exécution produit de nouvelles couches.
        self.assertEqual(self.bob.post(f"/api/recipes/{recette['id']}/run/").status_code, 201)
        self.assertEqual(Layer.objects.filter(metadata__recipe='Chaîne').count(), 6)

    def test_recettes_refusees(self):
        cas = {
            'vide': [],
            'référence à une étape future': [{'op': 'dissolve', 'inputs': [{'step': 0}]}],
            'référence négative': [{'op': 'centroid', 'inputs': [{'layer': self.points.pk}]},
                                   {'op': 'dissolve', 'inputs': [{'step': -1}]}],
            'entrée mal formée': [{'op': 'dissolve', 'inputs': [{'couche': 1}]}],
            'couche disparue': [{'op': 'dissolve', 'inputs': [{'layer': 999999}]}],
            'opération inconnue': [{'op': 'rm', 'inputs': [{'layer': self.points.pk}]}],
        }
        for nom, steps in cas.items():
            with self.subTest(cas=nom):
                recette = self.creer(steps).json()
                resp = self.alice.post(f"/api/recipes/{recette['id']}/run/")
                self.assertEqual(resp.status_code, 400, resp.content)
                self.assertIsNone(Recipe.objects.get(pk=recette['id']).result_layer)

    # DÉFAUT (rapport, C3) — `steps` est un JSON libre, accepté tel quel à la
    # création : une recette dont les étapes ne sont pas des objets fait lever
    # AttributeError dans _run_recipe à l'exécution : 500 au lieu de 400.
    @unittest.expectedFailure
    def test_etapes_mal_formees_refusees_proprement(self):
        self.alice.raise_request_exception = False
        recette = self.creer(['buffer']).json()
        self.assertEqual(self.alice.post(f"/api/recipes/{recette['id']}/run/").status_code, 400)
