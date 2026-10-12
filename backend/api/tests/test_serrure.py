"""Verrou 2 — la serrure de l'API : jetons signés, client ``carto-lab``, groupe ``developers``."""
import jwt
from django.test import override_settings
from rest_framework.permissions import AllowAny
from rest_framework.test import APIClient

from api import urls as api_urls

from .helpers import ALICE, FOREIGN_KEY, LabTestCase, couche, make_token

REFUS = (401, 403)


def bearer(token: str) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
    return client


class SerrureTests(LabTestCase):

    def chemins(self):
        layer = couche(geoms=['POINT(2 48)'])
        return ['/api/' + str(p.pattern).replace('<int:pk>', str(layer.pk))
                for p in api_urls.urlpatterns]

    def test_sans_jeton_aucune_route_ne_repond(self):
        """Inventaire tiré de api/urls.py : rien n'est ouvert à un anonyme,
        quelle que soit la méthode."""
        anonyme = APIClient()
        chemins = self.chemins()
        self.assertEqual(len(chemins), 16)
        for chemin in chemins:
            for methode in ('get', 'post', 'delete'):
                with self.subTest(chemin=chemin, methode=methode):
                    resp = getattr(anonyme, methode)(chemin)
                    self.assertIn(resp.status_code, REFUS)
        self.assertEqual(self.storage.appels, [])

    def test_aucune_vue_ne_desactive_la_permission(self):
        """Pas d'endpoint AllowAny ni d'authentification particulière dans cette
        app : une vue qui en gagne un doit être écrite ici, avec ce qu'elle ouvre."""
        for p in api_urls.urlpatterns:
            vue = p.callback.view_class
            with self.subTest(vue=vue.__name__):
                self.assertNotIn(AllowAny, vue.permission_classes)
                self.assertEqual([c.__name__ for c in vue.authentication_classes],
                                 ['KeycloakJWTAuthentication'])

    def test_un_non_membre_n_atteint_aucune_route(self):
        for groups in (['famille', 'amis'], [], None):
            client = bearer(make_token(ALICE, groups=groups))
            for chemin in self.chemins():
                with self.subTest(groups=groups, chemin=chemin):
                    resp = client.get(chemin)
                    self.assertIn(resp.status_code, REFUS)
                    self.assertIn('groupe(s)', resp.json()['detail'])

    def test_un_membre_entre(self):
        resp = bearer(make_token(ALICE, groups=['amis', 'developers'])).get('/api/me/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['email'], ALICE)
        self.assertEqual(resp.json()['groups'], ['amis', 'developers'])

    def test_jeton_d_un_autre_client_refuse_meme_membre(self):
        # admin-cli : client public à mot de passe du realm. storage : une autre app du lab.
        for azp in ('admin-cli', 'account', 'storage', 'carto-lab-admin', '', None):
            with self.subTest(azp=azp):
                resp = bearer(make_token(ALICE, azp=azp)).get('/api/layers/')
                self.assertIn(resp.status_code, REFUS)
                self.assertIn('autre client', resp.json()['detail'])

    def test_jeton_expire_mal_signe_ou_non_signe(self):
        claims = {'azp': 'carto-lab', 'email': ALICE, 'groups': ['developers'],
                  'exp': 9_999_999_999}
        jetons = {
            'expiré': make_token(ALICE, exp_in=-30),
            'autre clé': make_token(ALICE, key=FOREIGN_KEY),
            'non signé': jwt.encode(claims, None, algorithm='none'),
            'HS256': jwt.encode(claims, 'x' * 32, algorithm='HS256'),
            'bruit': 'pas.un.jeton',
        }
        for nom, tok in jetons.items():
            with self.subTest(jeton=nom):
                resp = bearer(tok).get('/api/layers/')
                self.assertIn(resp.status_code, REFUS)
                self.assertIn('Token invalide', resp.json()['detail'])

    def test_jeton_sans_email_refuse(self):
        resp = bearer(make_token(None)).get('/api/layers/')
        self.assertIn(resp.status_code, REFUS)
        self.assertIn('email', resp.json()['detail'])

    @override_settings(KEYCLOAK_REQUIRED_GROUPS='developers, admins')
    def test_plusieurs_groupes_requis_un_seul_suffit(self):
        self.assertEqual(bearer(make_token(ALICE, groups=['admins'])).get('/api/me/').status_code, 200)
        self.assertIn(bearer(make_token(ALICE, groups=['dev'])).get('/api/me/').status_code, REFUS)

    def test_la_documentation_de_l_api_n_existe_pas_en_service(self):
        for chemin in ('/api/schema/', '/api/docs/', '/'):
            with self.subTest(chemin=chemin):
                self.assertEqual(self.alice.get(chemin).status_code, 404)
