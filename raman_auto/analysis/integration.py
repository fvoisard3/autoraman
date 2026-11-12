"""Fenêtres d'intégration Raman et mise en grille des valeurs pour les cartes."""
import json
import math
from dataclasses import asdict, dataclass

import numpy as np

MEASURES = {"aire": "Aire", "hauteur": "Hauteur", "position": "Position du max"}
EDGE_PIXELS = 3  # pixels moyennés à chaque bord de la fenêtre pour le fond linéaire


@dataclass(eq=False)
class Window:
    name: str
    start: float          # dans l'unité de l'axe spectral (cm⁻¹ ou nm, voir unit)
    end: float
    unit: str = "cm-1"    # "cm-1" ou "nm"
    measure: str = "aire"
    baseline: bool = True  # retirer un fond linéaire tiré entre les deux bords

    @property
    def lo(self):
        return min(self.start, self.end)

    @property
    def hi(self):
        return max(self.start, self.end)

    def label(self):
        u = "cm⁻¹" if self.unit == "cm-1" else "nm"
        return f"{self.name} — {MEASURES[self.measure].lower()} {self.lo:g}–{self.hi:g} {u}"


def evaluate(x, spectra, window):
    """Valeur de la fenêtre pour chaque spectre (lignes de `spectra`) ; x = axe dans l'unité de la fenêtre.

    Retourne aussi le fond (pente, ordonnée) de chaque spectre, pour l'affichage.
    """
    spectra = np.atleast_2d(spectra)
    order = np.argsort(x)
    x, spectra = x[order], spectra[:, order]
    sel = np.flatnonzero((x >= window.lo) & (x <= window.hi))
    n = spectra.shape[0]
    if sel.size < 2:
        return np.full(n, np.nan), np.zeros((n, 2))
    xs, ys = x[sel], spectra[:, sel]
    if window.baseline:
        k = max(1, min(EDGE_PIXELS, sel.size // 3))
        xa, xb = xs[:k].mean(), xs[-k:].mean()
        ya, yb = ys[:, :k].mean(axis=1), ys[:, -k:].mean(axis=1)
        slope = (yb - ya) / (xb - xa)
        intercept = ya - slope * xa
    else:
        slope, intercept = np.zeros(n), np.zeros(n)
    corrected = ys - (slope[:, None] * xs + intercept[:, None])
    if window.measure == "aire":
        values = np.trapezoid(corrected, xs, axis=1)
    elif window.measure == "hauteur":
        values = corrected.max(axis=1)
    else:  # position du maximum, affinée par une parabole sur 3 pixels
        i = corrected.argmax(axis=1)
        values = xs[i].astype(float)
        inner = (i > 0) & (i < xs.size - 1)
        r = np.arange(n)[inner]
        ii = i[inner]
        y0, y1, y2 = corrected[r, ii - 1], corrected[r, ii], corrected[r, ii + 1]
        denom = y0 - 2 * y1 + y2
        with np.errstate(divide="ignore", invalid="ignore"):
            frac = np.where(denom != 0, 0.5 * (y0 - y2) / denom, 0.0)
        dx = (xs[ii + 1] - xs[ii - 1]) / 2
        values[inner] = xs[ii] + np.clip(frac, -1, 1) * dx
    return values, np.column_stack((slope, intercept))


def grid_rect(zone, targets, values):
    """Image (ny, nx) d'une zone rectangulaire à partir des points mesurés ; NaN pour les points absents.

    Retourne (image, (x_min, y_min, largeur, hauteur)) avec des pixels centrés sur les points.
    """
    xs0, ys0 = min(zone.x0, zone.x1), min(zone.y0, zone.y1)
    nx = math.floor((abs(zone.x1 - zone.x0)) / zone.step_x + 1e-9) + 1
    ny = math.floor((abs(zone.y1 - zone.y0)) / zone.step_y + 1e-9) + 1
    img = np.full((ny, nx), np.nan)
    ix = np.rint((targets[:, 0] - xs0) / zone.step_x).astype(int)
    iy = np.rint((targets[:, 1] - ys0) / zone.step_y).astype(int)
    ok = (ix >= 0) & (ix < nx) & (iy >= 0) & (iy < ny)
    img[iy[ok], ix[ok]] = values[ok]
    rect = (xs0 - zone.step_x / 2, ys0 - zone.step_y / 2, nx * zone.step_x, ny * zone.step_y)
    return img, rect


def save_windows(windows, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(w) for w in windows], f, indent=2, ensure_ascii=False)


def load_windows(path):
    with open(path, encoding="utf-8") as f:
        return [Window(**d) for d in json.load(f)]
