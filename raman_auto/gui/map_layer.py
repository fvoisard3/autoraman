"""Couche de carte superposable à un graphique pyqtgraph : rectangles en image (éventuellement tournés),
lignes et points en pastilles, couleurs partagées."""
import numpy as np
import pyqtgraph as pg
from PySide6.QtGui import QTransform

from ..analysis.integration import grid_rect

BLUES = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]


def blues():
    return pg.ColorMap(np.linspace(0, 1, len(BLUES)), BLUES)


def place_image(item, rect, angle=0.0, center=(0.0, 0.0)):
    """Place une ImageItem (row-major, ny × nx) sur rect = (x, y, largeur, hauteur) dans le repère du
    rectangle, puis la tourne de angle degrés autour de center."""
    image = item.image
    ny, nx = image.shape[:2]
    x, y, w, h = rect
    t = QTransform()
    t.translate(x, y)
    t.scale(w / nx, h / ny)
    if angle:
        r = QTransform()
        r.translate(center[0], center[1])
        r.rotate(angle)
        r.translate(-center[0], -center[1])
        t = t * r  # d'abord placement dans le repère du rectangle, puis rotation
    item.setTransform(t)


def auto_levels(values):
    v = np.asarray(values, float)
    v = v[np.isfinite(v)]
    if v.size == 0:
        return 0.0, 1.0
    lo, hi = np.percentile(v, [1, 99])
    if hi <= lo:
        lo, hi = lo - abs(lo) * 0.01 - 1e-12, hi + abs(hi) * 0.01 + 1e-12
    return float(lo), float(hi)


class MapLayer:
    """Images et pastilles d'une carte dans un PlotItem ; les couleurs suivent levels et colormap."""

    def __init__(self, plot, z=-1, spot_size=9):
        self.plot = plot
        self.z = z
        self.images = []
        self.scatter = pg.ScatterPlotItem(pxMode=True, size=spot_size, pen=pg.mkPen("#fcfcfb", width=1))
        self.scatter.setZValue(z + 0.5)
        self.plot.addItem(self.scatter)
        self.cmap = blues()
        self.levels = (0.0, 1.0)
        self._scatter_values = np.empty(0)
        self.scatter_index = np.empty(0, dtype=int)

    def clear(self):
        for img in self.images:
            self.plot.removeItem(img)
        self.images = []
        self.scatter.setData([], [])
        self._scatter_values = np.empty(0)
        self.scatter_index = np.empty(0, dtype=int)

    def set_data(self, zones, targets, zone_index, values, visible_zones=None):
        """zones : liste de Zone ; targets (N, 2) ; zone_index (N,) ; values (N,) (NaN = pas de valeur)."""
        self.clear()
        targets = np.asarray(targets, float).reshape(-1, 2)
        zone_index = np.asarray(zone_index, int)
        values = np.asarray(values, float)
        scatter = np.ones(len(values), dtype=bool)
        if visible_zones is not None:
            scatter &= np.isin(zone_index, list(visible_zones))
        for k, zone in enumerate(zones):
            if zone.kind != "rect" or (visible_zones is not None and k not in visible_zones):
                continue
            sel = zone_index == k
            if not sel.any():
                continue
            local = zone.to_local(targets[sel])
            grid, rect = grid_rect(zone, local, values[sel])
            # niveaux fixés par apply_colors : pas d'auto-niveaux (grille encore vide pendant l'exécution)
            img = pg.ImageItem(grid, axisOrder="row-major", autoLevels=False, levels=self.levels or (0.0, 1.0))
            place_image(img, rect, zone.angle, zone.center)
            img.setZValue(self.z)
            self.plot.addItem(img)
            self.images.append(img)
            scatter &= ~sel
        self.scatter_index = np.flatnonzero(scatter)
        self._scatter_values = values[self.scatter_index]
        self.scatter.setData(x=targets[self.scatter_index, 0], y=targets[self.scatter_index, 1],
                             data=self.scatter_index)
        self.apply_colors()

    def set_levels(self, lo, hi):
        self.levels = (float(lo), float(hi))
        self.apply_colors()

    def set_colormap(self, cmap):
        self.cmap = cmap
        self.apply_colors()

    def apply_colors(self):
        lut = self.cmap.getLookupTable(nPts=256, alpha=True)
        for img in self.images:
            img.setLookupTable(lut)
            img.setLevels(self.levels)
        if self._scatter_values.size:
            lo, hi = self.levels
            t = np.clip((self._scatter_values - lo) / (hi - lo if hi > lo else 1.0), 0, 1)
            brushes = [pg.mkBrush(self.cmap.map(float(x), mode="qcolor")) if np.isfinite(x)
                       else pg.mkBrush(0, 0, 0, 0) for x in t]
            self.scatter.setBrush(brushes)
