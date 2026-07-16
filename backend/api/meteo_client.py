"""
Client de l'API « Données Climatologiques » (DPClim) de Météo-France (Feature 4.1).

Flux ASYNCHRONE documenté :
  1. GET liste-stations/quotidienne?id-departement=&parametre=   → stations
  2. GET commande-station/quotidienne?id-station=&date-deb-periode=&date-fin-periode=
                                                                  → 202 + id-cmde
  3. GET commande/fichier?id-cmde=   → 201 (CSV prêt) | 204 (attente) | erreur

Sécurité (cf. to_do point 4 / SÉCURITÉ) : la clé API est fournie par l'utilisateur,
passée en header `apikey`, et n'est **ni persistée ni journalisée**. On ne logge
jamais la valeur de la clé ; les erreurs réseau n'incluent pas les headers.
"""
import csv
import io
import time

import requests
from django.conf import settings

# Départements métropolitains (Corse en 2A/2B). Pas de DOM (France métropolitaine).
METRO_DEPARTEMENTS = [f"{i:02d}" for i in range(1, 96) if i != 20] + ['2A', '2B']


class MeteoError(Exception):
    """Erreur fonctionnelle côté Météo-France (message sûr, sans la clé)."""


class MeteoClient:
    def __init__(self, api_key: str, base_url: str | None = None, timeout: int = 30):
        if not api_key:
            raise MeteoError("Clé API Météo-France manquante.")
        self._key = api_key
        self.base = (base_url or settings.METEOFRANCE_BASE_URL).rstrip('/')
        self.timeout = timeout
        self._session = requests.Session()

    def _headers(self, accept='application/json'):
        return {'apikey': self._key, 'Accept': accept}

    def _get(self, path, params=None, accept='application/json'):
        url = f"{self.base}/{path.lstrip('/')}"
        try:
            return self._session.get(url, headers=self._headers(accept),
                                     params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            # exc peut contenir l'URL mais jamais les headers → pas de fuite de clé.
            raise MeteoError(f"Erreur réseau vers Météo-France : {exc}") from exc

    # ── 1. Stations d'un département fournissant un paramètre ─────────────────
    def list_stations(self, departement: str, parametre: str) -> list[dict]:
        r = self._get('liste-stations/quotidienne',
                      {'id-departement': departement, 'parametre': parametre})
        if r.status_code == 401:
            raise MeteoError("Clé API refusée (401). Vérifiez votre abonnement DPClim.")
        if r.status_code != 200:
            raise MeteoError(f"liste-stations {departement} → HTTP {r.status_code}.")
        try:
            data = r.json()
        except ValueError:
            return []
        return data if isinstance(data, list) else data.get('stations', [])

    # ── 2. Commande de données quotidiennes d'une station ─────────────────────
    def order_daily(self, id_station: str, date_deb: str, date_fin: str) -> str:
        r = self._get('commande-station/quotidienne', {
            'id-station': id_station,
            'date-deb-periode': date_deb,
            'date-fin-periode': date_fin,
        })
        if r.status_code not in (200, 201, 202):
            raise MeteoError(f"commande-station {id_station} → HTTP {r.status_code}.")
        try:
            payload = r.json()
        except ValueError:
            raise MeteoError("Réponse de commande illisible.")
        # {'elaboreProduitAvecDemandeResponse': {'return': '<id-cmde>'}}
        cmde = (payload.get('elaboreProduitAvecDemandeResponse', {}) or {}).get('return')
        if not cmde:
            raise MeteoError("Identifiant de commande absent de la réponse.")
        return str(cmde)

    # ── 3. Récupération du fichier CSV (polling) ──────────────────────────────
    def fetch_file(self, id_cmde: str, poll_interval: float = 5.0, max_wait: float = 120.0) -> str:
        waited = 0.0
        while True:
            r = self._get('commande/fichier', {'id-cmde': id_cmde}, accept='*/*')
            if r.status_code in (200, 201):
                return r.text
            if r.status_code == 204:
                if waited >= max_wait:
                    raise MeteoError("Délai dépassé en attendant le fichier Météo-France.")
                time.sleep(poll_interval)
                waited += poll_interval
                continue
            if r.status_code == 500:
                raise MeteoError("Production du fichier en erreur côté Météo-France (500).")
            raise MeteoError(f"commande/fichier → HTTP {r.status_code}.")


# ── Parsing du CSV quotidien ──────────────────────────────────────────────────
def parse_daily_csv(text: str, columns: list[str]) -> dict[str, list[float]]:
    """
    Parse un CSV quotidien DPClim (séparateur ';') et renvoie {colonne: [valeurs]}
    pour les colonnes demandées. Les cellules vides / non numériques sont ignorées.
    """
    out: dict[str, list[float]] = {c: [] for c in columns}
    reader = csv.DictReader(io.StringIO(text), delimiter=';')
    for row in reader:
        for c in columns:
            raw = (row.get(c) or '').strip().replace(',', '.')
            if raw in ('', 'nan'):
                continue
            try:
                out[c].append(float(raw))
            except ValueError:
                continue
    return out


def station_lonlat(st: dict) -> tuple[float, float] | None:
    """Extrait (lon, lat) d'une station DPClim quelles que soient les clés."""
    lat = st.get('lat') or st.get('latitude') or st.get('LAT')
    lon = st.get('lon') or st.get('longitude') or st.get('LON')
    try:
        return float(lon), float(lat)
    except (TypeError, ValueError):
        return None
