"""
Grandeurs Météo-France et moteur d'indicateurs (Feature 4.2).

- GRANDEURS : mappe une grandeur (température, précipitations, vent…) au paramètre
  `liste-stations` de l'API DPClim et aux colonnes du CSV quotidien.
- INDICATORS : registre extensible. Chaque indicateur reçoit les séries de colonnes
  (dict colonne -> np.ndarray), la colonne principale de la grandeur, et ses
  paramètres ; il renvoie un float (ou None si non calculable).
"""
import numpy as np

# ── Grandeurs (CSV quotidien DPClim : NUM_POSTE;...;RR;TN;TX;TM;FFM;...) ────────
# `parametre` = valeur réellement acceptée par liste-stations/quotidienne, vérifiée
# en direct sur l'API (public-api.meteofrance.fr/public/DPClim/v1) : temperature,
# precipitation, vent, pression, humidite, rayonnement et insolation renvoient 200 ;
# neige, visibilite, etat_sol, evapotranspiration, nebulosite et point_de_rosee
# renvoient 404 « Le paramètre n'existe pas » et n'existent donc pas côté DPClim.
# `columns` = colonnes correspondantes, confirmées présentes dans l'en-tête réel du
# CSV quotidien retourné par commande/fichier.
GRANDEURS = {
    'temperature': {
        'label': 'Température',
        'parametre': 'temperature',
        'columns': ['TN', 'TX', 'TM'],
        'main': 'TM',
        'unit': '°C',
    },
    'precipitation': {
        'label': 'Précipitations',
        'parametre': 'precipitation',
        'columns': ['RR'],
        'main': 'RR',
        'unit': 'mm',
    },
    'vent': {
        'label': 'Vent',
        'parametre': 'vent',
        'columns': ['FFM'],
        'main': 'FFM',
        'unit': 'm/s',
    },
    'pression': {
        'label': 'Pression (niveau mer)',
        'parametre': 'pression',
        'columns': ['PMERM'],
        'main': 'PMERM',
        'unit': 'hPa',
    },
    'humidite': {
        'label': 'Humidité',
        'parametre': 'humidite',
        'columns': ['UN', 'UX', 'UM'],
        'main': 'UM',
        'unit': '%',
    },
    'rayonnement': {
        'label': 'Rayonnement global',
        'parametre': 'rayonnement',
        'columns': ['GLOT'],
        'main': 'GLOT',
        'unit': 'J/cm²',
    },
    'insolation': {
        'label': 'Insolation',
        'parametre': 'insolation',
        'columns': ['INST'],
        'main': 'INST',
        'unit': 'min',
    },
}


def _clean(a: np.ndarray) -> np.ndarray:
    a = a[np.isfinite(a)]
    return a


# ── Indicateurs ────────────────────────────────────────────────────────────────
INDICATORS: dict[str, dict] = {}


def indicator(name, label, params=None, column=None):
    def wrap(func):
        INDICATORS[name] = {
            'name': name, 'label': label, 'params': params or [],
            'column': column, 'func': func,
        }
        return func
    return wrap


def _series(cols, main, override):
    col = override or main
    a = cols.get(col)
    if a is None:
        a = cols.get(main)
    return _clean(a) if a is not None else np.array([])


# Chaque fonction reçoit désormais aussi `dated` : [(jour_de_l'année, valeur), ...]
# triés pour la colonne principale de la grandeur (cf. meteo_client.parse_daily_csv).
# Les indicateurs statistiques (moyenne, min…) l'ignorent ; seuls les indicateurs
# qui intègrent la grandeur dans le temps (intégrale à seuil) en ont besoin, car
# `cols`/`_series` ne conservent qu'un tableau à plat, sans les jours associés —
# indispensable pour connaître l'écart entre deux relevés consécutifs.
@indicator('mean', 'Moyenne annuelle')
def _mean(cols, main, params, dated):
    a = _series(cols, main, None)
    return float(np.mean(a)) if a.size else None


@indicator('min', 'Minimum')
def _min(cols, main, params, dated):
    a = _series(cols, main, None)
    return float(np.min(a)) if a.size else None


@indicator('max', 'Maximum')
def _max(cols, main, params, dated):
    a = _series(cols, main, None)
    return float(np.max(a)) if a.size else None


@indicator('sum', 'Cumul annuel')
def _sum(cols, main, params, dated):
    a = _series(cols, main, None)
    return float(np.sum(a)) if a.size else None


@indicator('std', 'Écart-type')
def _std(cols, main, params, dated):
    a = _series(cols, main, None)
    return float(np.std(a)) if a.size else None


@indicator('amplitude', 'Amplitude (max − min)')
def _amp(cols, main, params, dated):
    a = _series(cols, main, None)
    return float(np.max(a) - np.min(a)) if a.size else None


@indicator('count_above', 'Nb jours au-dessus d’un seuil',
           params=[{'name': 'threshold', 'type': 'number', 'label': 'Seuil', 'default': 30}])
def _above(cols, main, params, dated):
    a = _series(cols, main, None)
    thr = float(params.get('threshold', 30))
    return float(np.count_nonzero(a > thr)) if a.size else None


@indicator('count_below', 'Nb jours en-dessous d’un seuil',
           params=[{'name': 'threshold', 'type': 'number', 'label': 'Seuil', 'default': 0}])
def _below(cols, main, params, dated):
    a = _series(cols, main, None)
    thr = float(params.get('threshold', 0))
    return float(np.count_nonzero(a < thr)) if a.size else None


@indicator('frost_days', 'Jours de gel (Tmin < 0 °C)', column='TN')
def _frost(cols, main, params, dated):
    # Utilise la température minimale si disponible.
    a = _series(cols, main, 'TN')
    return float(np.count_nonzero(a < 0)) if a.size else None


@indicator('percentile', 'Percentile',
           params=[{'name': 'p', 'type': 'number', 'label': 'Percentile (0-100)', 'default': 90}])
def _percentile(cols, main, params, dated):
    a = _series(cols, main, None)
    if not a.size:
        return None
    try:
        p = float(params.get('p', 90))
    except (TypeError, ValueError):
        p = 90.0
    p = min(100.0, max(0.0, p))
    return float(np.percentile(a, p))


def _threshold_integral_parts(dated: list[tuple[int, float]], seuil: float) -> tuple[float, float]:
    """
    Intègre, entre chaque paire de points (jour, valeur) consécutifs — la grandeur
    étant supposée varier de manière AFFINE entre deux relevés, y compris à travers
    un trou de plusieurs jours —, la partie positive et la partie négative de
    (grandeur - seuil). Renvoie (intégrale positive, intégrale négative), en
    unité×jour. Solution analytique exacte (le trapèze est exact pour une fonction
    affine ; un segment qui change de signe est coupé au point de croisement).
    """
    pos_total = 0.0
    neg_total = 0.0
    for (d0, v0), (d1, v1) in zip(dated, dated[1:]):
        dx = d1 - d0
        if dx <= 0:
            continue
        f0, f1 = v0 - seuil, v1 - seuil
        if f0 * f1 < 0:
            frac = f0 / (f0 - f1)  # position (0..1) du passage à zéro sur le segment
            if f0 > 0:
                pos_total += 0.5 * dx * frac * f0
                neg_total += 0.5 * dx * (1 - frac) * (-f1)
            else:
                neg_total += 0.5 * dx * frac * (-f0)
                pos_total += 0.5 * dx * (1 - frac) * f1
        else:
            avg = (f0 + f1) / 2
            if avg >= 0:
                pos_total += dx * avg
            else:
                neg_total += dx * (-avg)
    return pos_total, neg_total


_SEUIL_PARAM = [{'name': 'seuil', 'type': 'number', 'label': 'Seuil', 'default': 0}]


@indicator('integral_positive', 'Intégrale à seuil (positive)', params=_SEUIL_PARAM)
def _integral_positive(cols, main, params, dated):
    if len(dated) < 2:
        return None
    pos, _neg = _threshold_integral_parts(dated, float(params.get('seuil', 0)))
    return pos


@indicator('integral_negative', 'Intégrale à seuil (négative)', params=_SEUIL_PARAM)
def _integral_negative(cols, main, params, dated):
    if len(dated) < 2:
        return None
    _pos, neg = _threshold_integral_parts(dated, float(params.get('seuil', 0)))
    return neg


@indicator('integral_absolue', 'Intégrale à seuil (absolue)', params=_SEUIL_PARAM)
def _integral_absolue(cols, main, params, dated):
    if len(dated) < 2:
        return None
    pos, neg = _threshold_integral_parts(dated, float(params.get('seuil', 0)))
    return pos - neg


def catalog() -> list[dict]:
    return [{k: v for k, v in ind.items() if k != 'func'} for ind in INDICATORS.values()]


def compute(name: str, cols: dict, main: str, params: dict, dated: list[tuple[int, float]] | None = None):
    ind = INDICATORS.get(name)
    if ind is None:
        raise ValueError(f"Indicateur inconnu : {name}")
    return ind['func'](cols, main, params or {}, dated or [])
