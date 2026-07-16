"""
Classification choroplèthe (Feature 4.4) : bornes (quantiles / intervalles égaux /
Jenks), rampes de couleurs, légende. Sans dépendance externe (numpy uniquement).
"""
import numpy as np

# Rampes séquentielles (couleurs d'ancrage, interpolées vers n classes).
RAMPS = {
    'YlOrRd': ['#ffffb2', '#fed976', '#feb24c', '#fd8d3c', '#f03b20', '#bd0026'],
    'Blues':  ['#eff3ff', '#c6dbef', '#9ecae1', '#6baed6', '#3182bd', '#08519c'],
    'Viridis': ['#440154', '#3b528b', '#21918c', '#5ec962', '#fde725'],
    'RdBu':   ['#b2182b', '#ef8a62', '#fddbc7', '#d1e5f0', '#67a9cf', '#2166ac'],
}

CLASSIFICATIONS = ['quantiles', 'equal', 'jenks']


def _hex_to_rgb(h):
    h = h.lstrip('#')
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _rgb_to_hex(rgb):
    return '#%02x%02x%02x' % tuple(int(round(c)) for c in rgb)


def ramp_colors(name: str, n: int) -> list[str]:
    anchors = RAMPS.get(name, RAMPS['YlOrRd'])
    if n <= 1:
        return [anchors[-1]]
    rgb = [_hex_to_rgb(c) for c in anchors]
    out = []
    for i in range(n):
        t = i / (n - 1)                      # 0..1
        pos = t * (len(rgb) - 1)
        lo = int(np.floor(pos)); hi = min(lo + 1, len(rgb) - 1)
        f = pos - lo
        col = [rgb[lo][k] + (rgb[hi][k] - rgb[lo][k]) * f for k in range(3)]
        out.append(_rgb_to_hex(col))
    return out


def _jenks_breaks(data: list[float], n_classes: int) -> list[float]:
    data = sorted(data)
    n = len(data)
    if n <= n_classes:
        return [data[0]] + data  # dégénéré
    mat1 = [[0] * (n_classes + 1) for _ in range(n + 1)]
    mat2 = [[0.0] * (n_classes + 1) for _ in range(n + 1)]
    for i in range(1, n_classes + 1):
        mat1[1][i] = 1
        mat2[1][i] = 0.0
        for j in range(2, n + 1):
            mat2[j][i] = float('inf')
    for l in range(2, n + 1):
        s1 = s2 = w = 0.0
        for m in range(1, l + 1):
            i3 = l - m + 1
            val = data[i3 - 1]
            s2 += val * val
            s1 += val
            w += 1
            var = s2 - (s1 * s1) / w
            i4 = i3 - 1
            if i4 != 0:
                for j in range(2, n_classes + 1):
                    if mat2[l][j] >= (var + mat2[i4][j - 1]):
                        mat1[l][j] = i3
                        mat2[l][j] = var + mat2[i4][j - 1]
        mat1[l][1] = 1
        mat2[l][1] = var
    k = n
    breaks = [0.0] * (n_classes + 1)
    breaks[n_classes] = data[-1]
    breaks[0] = data[0]
    for j in range(n_classes, 1, -1):
        idx = int(mat1[k][j]) - 2
        breaks[j - 1] = data[idx]
        k = int(mat1[k][j]) - 1
    return breaks


def classify(values, method: str = 'quantiles', n_classes: int = 5) -> list[float]:
    """Renvoie n_classes+1 bornes croissantes."""
    v = np.asarray([x for x in values if x is not None and np.isfinite(x)], dtype=float)
    if v.size == 0:
        return []
    n_classes = max(1, min(n_classes, 8))
    vmin, vmax = float(v.min()), float(v.max())
    if vmin == vmax:
        return [vmin, vmax]
    if method == 'equal':
        return [float(x) for x in np.linspace(vmin, vmax, n_classes + 1)]
    if method == 'jenks':
        try:
            return [float(x) for x in _jenks_breaks(v.tolist(), n_classes)]
        except Exception:
            method = 'quantiles'
    # quantiles (défaut)
    qs = np.linspace(0, 100, n_classes + 1)
    breaks = [float(x) for x in np.percentile(v, qs)]
    # dédoublonne les bornes égales (distribution très concentrée)
    out = [breaks[0]]
    for b in breaks[1:]:
        out.append(b if b > out[-1] else out[-1] + 1e-9)
    return out


def class_index(value, breaks: list[float]) -> int:
    """Index de classe [0..n-1] pour une valeur, selon les bornes."""
    if value is None or not breaks:
        return 0
    for i in range(1, len(breaks)):
        if value <= breaks[i]:
            return i - 1
    return len(breaks) - 2


def build_legend(breaks: list[float], colors: list[str]) -> list[dict]:
    return [
        {'min': float(round(breaks[i], 3)), 'max': float(round(breaks[i + 1], 3)), 'color': colors[i]}
        for i in range(len(colors))
    ]
