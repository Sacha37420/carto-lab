"""
Client de l'API « Données Climatologiques » (DPClim) de Météo-France (Feature 4.1).

Flux ASYNCHRONE documenté :
  1. GET liste-stations/quotidienne?id-departement=&parametre=   → stations
  2. GET commande-station/quotidienne?id-station=&date-deb-periode=&date-fin-periode=
                                                                  → 202 + id-cmde
  3. GET commande/fichier?id-cmde=   → 201 (CSV prêt) | 204 (attente) | erreur

Sécurité (cf. to_do point 4 / SÉCURITÉ) : la clé API est fournie par l'utilisateur,
n'est **ni persistée ni journalisée**. On ne logge jamais la valeur de la clé ; les
erreurs réseau n'incluent pas les headers.

Deux identifiants bien distincts, à ne pas confondre (vérifié en direct sur l'API) :
  - l'IDENTIFIANT APPLICATIF fourni par le portail Météo-France (chaîne Basic
    `client_id:client_secret`, telle qu'affichée dans l'exemple curl du portail) —
    longue durée de vie, c'est **cette valeur que l'utilisateur saisit et qui est
    mémorisée** (cf. frontend, localStorage) ;
  - le JETON D'ACCÈS OAuth2 (Bearer), obtenu en échangeant l'identifiant ci-dessus
    via `fetch_access_token()` (POST /token, grant_type=client_credentials) — courte
    durée de vie (~1h, non stocké), c'est lui qui s'utilise en `Authorization: Bearer`
    sur DPClim.
DPClim n'accepte **jamais** l'identifiant applicatif directement, ni en header
`apikey` (401 « Invalid Credentials » quel que soit le jeton fourni ainsi) : il faut
systématiquement passer par l'échange OAuth2 puis par `Authorization: Bearer`.
"""
import csv
import io
import re
import time
from datetime import date, datetime, timezone

import requests
from django.conf import settings

# Départements métropolitains (Corse en 2A/2B). Pas de DOM (France métropolitaine).
METRO_DEPARTEMENTS = [f"{i:02d}" for i in range(1, 96) if i != 20] + ['2A', '2B']


class MeteoError(Exception):
    """Erreur fonctionnelle côté Météo-France (message sûr, sans la clé)."""


class MeteoQuotaError(MeteoError):
    """
    Quota Météo-France dépassé (HTTP 429). Contrairement aux autres MeteoError
    (ex. station sans donnée), inutile de continuer sur les stations/départements
    suivants : ils échoueront tous pareil tant que le quota n'est pas repartagé,
    et chaque essai supplémentaire consomme encore un peu du quota qui doit se
    reconstituer. Les appelants doivent arrêter la boucle en cours dès qu'ils la
    rencontrent, pas la traiter comme un simple échec ponctuel — ou, mieux,
    attendre `retry_after_seconds` puis réessayer (cf. `retry_after_seconds`).
    """
    def __init__(self, message: str, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


# Le portail Météo-France (WSO2) renvoie un header Retry-After au format HTTP-date
# (RFC 7231) mais LOCALISÉ EN FRANÇAIS (ex. « sam., 18 juil. 2026 16:52:14 GMT »),
# donc ni un entier de secondes standard, ni parsable par un parseur HTTP-date
# classique (qui attend des noms de mois anglais) — vérifié en direct sur un vrai
# 429. Le corps JSON porte la même info sous `nextAccessTime`, tout aussi
# francisée ; on ne s'appuie que sur le header, qui suffit.
_FR_MONTHS = {
    'jan': 1, 'janv': 1, 'fev': 2, 'fevr': 2, 'févr': 2, 'mar': 3, 'mars': 3,
    'avr': 4, 'mai': 5, 'juin': 6, 'juil': 7, 'aou': 8, 'août': 8, 'aout': 8,
    'sep': 9, 'sept': 9, 'oct': 10, 'nov': 11, 'dec': 12, 'déc': 12,
}


def _parse_retry_after(value: str | None) -> int | None:
    """Retourne un délai d'attente en secondes (>= 0), ou None si non déterminable."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():  # forme standard RFC 7231 (rare ici, mais gérée par sécurité)
        return int(value)
    m = re.search(r'(\d{1,2})\s+([A-Za-zéÉ]+)\.?\s+(\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})', value)
    if not m:
        return None
    day, month_fr, year, h, mi, s = m.groups()
    month = _FR_MONTHS.get(month_fr.lower().rstrip('.'))
    if not month:
        return None
    try:
        target = datetime(int(year), month, int(day), int(h), int(mi), int(s), tzinfo=timezone.utc)
    except ValueError:
        return None
    return max(0, int((target - datetime.now(timezone.utc)).total_seconds()))


def fetch_access_token(app_credentials: str, token_url: str | None = None, timeout: int = 15) -> str:
    """
    Échange l'identifiant applicatif Météo-France (chaîne Basic client_id:client_secret
    fournie par le portail) contre un jeton d'accès OAuth2 Bearer (~1h de validité).
    """
    app_credentials = (app_credentials or '').strip()
    if not app_credentials:
        raise MeteoError("Identifiant Météo-France manquant.")
    url = token_url or settings.METEOFRANCE_TOKEN_URL
    try:
        r = requests.post(
            url, data={'grant_type': 'client_credentials'},
            headers={'Authorization': f'Basic {app_credentials}'}, timeout=timeout,
        )
    except requests.RequestException as exc:
        raise MeteoError(f"Erreur réseau vers le portail Météo-France : {exc}") from exc
    if r.status_code != 200:
        raise MeteoError(
            f"Identifiant Météo-France refusé par le portail (HTTP {r.status_code}). "
            "Vérifiez la chaîne Basic copiée depuis le portail (exemple curl « /token »)."
        )
    try:
        token = r.json().get('access_token')
    except ValueError:
        token = None
    if not token:
        raise MeteoError("Réponse du portail Météo-France illisible (jeton absent).")
    return token


class MeteoClient:
    """
    Gère elle-même le cycle de vie du jeton Bearer : reçoit l'IDENTIFIANT
    APPLICATIF (longue durée), pas un jeton déjà émis. Le jeton est obtenu
    paresseusement au premier appel, et automatiquement RENOUVELÉ en cas de 401
    en cours de route (jeton expiré — ~1h de validité, un job qui traite
    beaucoup de stations peut largement dépasser ce délai) : l'appel est alors
    rejoué une fois avec le nouveau jeton, de façon transparente pour l'appelant.
    """
    def __init__(self, app_credentials: str, base_url: str | None = None, timeout: int = 30,
                min_interval: float | None = None):
        if not app_credentials:
            raise MeteoError("Identifiant Météo-France manquant.")
        self._app_credentials = app_credentials
        self._token: str | None = None
        self.base = (base_url or settings.METEOFRANCE_BASE_URL).rstrip('/')
        self.timeout = timeout
        self._session = requests.Session()
        # Espace les appels plutôt que de foncer jusqu'au 429 puis payer la
        # pénalité d'attente : vérifié en direct, moins cher au global (cf.
        # settings.METEOFRANCE_MIN_INTERVAL).
        self._min_interval = (
            settings.METEOFRANCE_MIN_INTERVAL if min_interval is None else min_interval
        )
        self._last_call_at = 0.0

    def authenticate(self) -> None:
        """Force l'obtention d'un jeton dès maintenant (échec rapide et message clair en début de job)."""
        if self._token is None:
            self._token = fetch_access_token(self._app_credentials)

    def _headers(self, accept='application/json'):
        self.authenticate()
        return {'Authorization': f'Bearer {self._token}', 'Accept': accept}

    def _throttle(self):
        wait = self._min_interval - (time.monotonic() - self._last_call_at)
        if wait > 0:
            time.sleep(wait)
        self._last_call_at = time.monotonic()

    def _request(self, url, params, accept):
        self._throttle()
        try:
            return self._session.get(url, headers=self._headers(accept),
                                     params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            # exc peut contenir l'URL mais jamais les headers → pas de fuite de clé.
            raise MeteoError(f"Erreur réseau vers Météo-France : {exc}") from exc

    def _get(self, path, params=None, accept='application/json'):
        url = f"{self.base}/{path.lstrip('/')}"
        r = self._request(url, params, accept)
        if r.status_code == 401:
            # Jeton expiré en cours de route (le plus probable, cf. docstring) —
            # on ne peut pas être face à un identifiant invalide ici : celui-ci a
            # déjà été validé avec succès pour obtenir un premier jeton (sinon
            # `authenticate()` aurait échoué avant même le premier appel DPClim).
            self._token = None
            self.authenticate()
            r = self._request(url, params, accept)
            if r.status_code == 401:
                raise MeteoError(
                    "Jeton Météo-France refusé (HTTP 401) même après renouvellement."
                )
        if r.status_code == 429:
            wait = _parse_retry_after(r.headers.get('Retry-After'))
            hint = f" Réessayez dans {wait}s." if wait is not None else " Réessayez plus tard."
            raise MeteoQuotaError(f"Quota Météo-France dépassé (HTTP 429).{hint}", retry_after_seconds=wait)
        return r

    # ── 1. Stations d'un département fournissant un paramètre ─────────────────
    def list_stations(self, departement: str, parametre: str) -> list[dict]:
        r = self._get('liste-stations/quotidienne',
                      {'id-departement': departement, 'parametre': parametre})
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
def _day_offset(date_str: str, period_start: date, period_end: date) -> int | None:
    """
    « YYYYMMDD » → indice de jour continu depuis `period_start` (1-based), ou
    None si invalide ou hors de `[period_start, period_end]`. Généralise
    l'ancien « jour de l'année » : pour une période d'un an (period_start =
    1er janvier, period_end = 31 décembre de la même année), produit
    exactement les mêmes valeurs (1..365/366).
    """
    try:
        d = datetime.strptime(date_str, '%Y%m%d').date()
    except ValueError:
        return None
    if not (period_start <= d <= period_end):
        return None
    return (d - period_start).days + 1


def _quality_metrics(valid_days: set[str], date_counts: dict[str, int],
                     period_start: date, period_end: date) -> dict:
    """
    Métriques de qualité d'une station pour une grandeur donnée, sur toute la
    période demandée (une année ou une période pluriannuelle).

    La donnée DPClim « quotidienne » n'a qu'UN point par jour (pas de résolution
    horaire) : « heures couvertes »/« trou max » sont donc calculés au jour près
    puis convertis ×24 — une simple mise à l'échelle, pas une vraie précision
    horaire (DPClim ne fournit rien de plus fin sur ce produit). Les duplicatas
    de date, eux, sont une vraie mesure directe (plusieurs lignes pour un même
    jour dans le CSV brut = défaut de la source, indépendant de la grandeur).
    """
    total_days = (period_end - period_start).days + 1
    total_hours = total_days * 24
    valid_hours = len(valid_days) * 24

    offsets = sorted(
        o for o in (_day_offset(d, period_start, period_end) for d in valid_days) if o is not None
    )
    if offsets:
        gaps = [offsets[0] - 1]                        # avant le 1er relevé valide
        gaps += [b - a - 1 for a, b in zip(offsets, offsets[1:])]  # entre relevés
        gaps.append(total_days - offsets[-1])           # après le dernier relevé valide
        max_gap_days = max(gaps)
    else:
        max_gap_days = total_days                        # aucun relevé valide sur la période

    max_same_datetime = max(date_counts.values()) if date_counts else 0
    total_duplicates = sum(c - 1 for c in date_counts.values() if c > 1)

    return {
        'heures_couvertes': valid_hours,
        'heures_annee': total_hours,
        'taux_completude': round(valid_hours / total_hours, 4) if total_hours else 0.0,
        'trou_max_heures': max_gap_days * 24,
        'max_releves_meme_datetime': max_same_datetime,
        'duplicatas_datetime': total_duplicates,
    }


def parse_daily_csv(text: str, columns: list[str], main_column: str) -> dict:
    """
    Parse UN chunk de CSV quotidien DPClim (séparateur ';') — en pratique une
    station pour une seule année, cf. `tasks._compute_indicator` qui découpe
    toute période demandée en une requête DPClim par année. Renvoie les
    ingrédients bruts, non encore rattachés à une période :
      - 'series'     : {colonne: [valeurs]} pour chaque colonne demandée (cellules
                        vides/non numériques ignorées) ;
      - 'dated_main' : [(date 'YYYYMMDD', valeur), ...] pour `main_column`
                        uniquement. En cas de date dupliquée DANS CE CHUNK, seule
                        la 1re valeur rencontrée est gardée (une même date ne
                        peut pas avoir deux valeurs distinctes pour une fonction
                        bien définie) ; le doublon lui-même est compté dans
                        'date_counts'.
      - 'date_counts': {date: nb de lignes}.
    La conversion en jour continu et le calcul de qualité sont différés à
    `combine_period`, qui a besoin de voir TOUS les chunks d'une station pour
    raisonner sur la période entière plutôt que sur un fragment.
    """
    out: dict[str, list[float]] = {c: [] for c in columns}
    dated_main: list[tuple[str, float]] = []
    date_counts: dict[str, int] = {}
    valid_main_days: set[str] = set()

    reader = csv.DictReader(io.StringIO(text), delimiter=';')
    for row in reader:
        date_str = (row.get('DATE') or '').strip()
        if date_str:
            date_counts[date_str] = date_counts.get(date_str, 0) + 1

        for c in columns:
            raw = (row.get(c) or '').strip().replace(',', '.')
            if raw in ('', 'nan'):
                continue
            try:
                val = float(raw)
            except ValueError:
                continue
            out[c].append(val)
            if c == main_column and date_str and date_str not in valid_main_days:
                dated_main.append((date_str, val))
                valid_main_days.add(date_str)

    return {'series': out, 'dated_main': dated_main, 'date_counts': date_counts}


def combine_period(chunks: list[dict], period_start: date, period_end: date) -> dict:
    """
    Fusionne les chunks (un par année interrogée, cf. `parse_daily_csv`) d'une
    même station en un seul jeu de données couvrant toute la période demandée :
    concatène les séries et additionne les compteurs de dates (des dates
    'YYYYMMDD' de chunks différents — des années différentes — sont toujours
    distinctes par construction, aucune déduplication inter-chunk n'est donc
    nécessaire), convertit chaque date valide en indice de jour continu depuis
    `period_start` (pour que les indicateurs à intégrale restent corrects même
    à travers un trou de plusieurs années), et calcule la qualité une seule
    fois sur la période entière (pas année par année).
    """
    series: dict[str, list[float]] = {}
    date_counts: dict[str, int] = {}
    valid_days: set[str] = set()
    dated_raw: list[tuple[str, float]] = []

    for chunk in chunks:
        for c, vals in chunk['series'].items():
            series.setdefault(c, []).extend(vals)
        for d, n in chunk['date_counts'].items():
            date_counts[d] = date_counts.get(d, 0) + n
        for d, v in chunk['dated_main']:
            valid_days.add(d)
            dated_raw.append((d, v))

    dated_main: list[tuple[int, float]] = []
    for d, v in dated_raw:
        off = _day_offset(d, period_start, period_end)
        if off is not None:
            dated_main.append((off, v))
    dated_main.sort(key=lambda p: p[0])

    return {
        'series': series,
        'dated_main': dated_main,
        'quality': _quality_metrics(valid_days, date_counts, period_start, period_end),
    }


def station_lonlat(st: dict) -> tuple[float, float] | None:
    """Extrait (lon, lat) d'une station DPClim quelles que soient les clés."""
    lat = st.get('lat') or st.get('latitude') or st.get('LAT')
    lon = st.get('lon') or st.get('longitude') or st.get('LON')
    try:
        return float(lon), float(lat)
    except (TypeError, ValueError):
        return None
