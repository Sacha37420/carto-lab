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


@indicator('mean', 'Moyenne annuelle')
def _mean(cols, main, params):
    a = _series(cols, main, None)
    return float(np.mean(a)) if a.size else None


@indicator('min', 'Minimum')
def _min(cols, main, params):
    a = _series(cols, main, None)
    return float(np.min(a)) if a.size else None


@indicator('max', 'Maximum')
def _max(cols, main, params):
    a = _series(cols, main, None)
    return float(np.max(a)) if a.size else None


@indicator('sum', 'Cumul annuel')
def _sum(cols, main, params):
    a = _series(cols, main, None)
    return float(np.sum(a)) if a.size else None


@indicator('std', 'Écart-type')
def _std(cols, main, params):
    a = _series(cols, main, None)
    return float(np.std(a)) if a.size else None


@indicator('amplitude', 'Amplitude (max − min)')
def _amp(cols, main, params):
    a = _series(cols, main, None)
    return float(np.max(a) - np.min(a)) if a.size else None


@indicator('count_above', 'Nb jours au-dessus d’un seuil',
           params=[{'name': 'threshold', 'type': 'number', 'label': 'Seuil', 'default': 30}])
def _above(cols, main, params):
    a = _series(cols, main, None)
    thr = float(params.get('threshold', 30))
    return float(np.count_nonzero(a > thr)) if a.size else None


@indicator('count_below', 'Nb jours en-dessous d’un seuil',
           params=[{'name': 'threshold', 'type': 'number', 'label': 'Seuil', 'default': 0}])
def _below(cols, main, params):
    a = _series(cols, main, None)
    thr = float(params.get('threshold', 0))
    return float(np.count_nonzero(a < thr)) if a.size else None


@indicator('frost_days', 'Jours de gel (Tmin < 0 °C)', column='TN')
def _frost(cols, main, params):
    # Utilise la température minimale si disponible.
    a = _series(cols, main, 'TN')
    return float(np.count_nonzero(a < 0)) if a.size else None


def catalog() -> list[dict]:
    return [{k: v for k, v in ind.items() if k != 'func'} for ind in INDICATORS.values()]


def compute(name: str, cols: dict, main: str, params: dict):
    ind = INDICATORS.get(name)
    if ind is None:
        raise ValueError(f"Indicateur inconnu : {name}")
    return ind['func'](cols, main, params or {})
