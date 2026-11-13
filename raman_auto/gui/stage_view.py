"""Vue 2D de la platine : position de la sonde, dessin et édition des zones d'analyse, carte mesurée."""
import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QPen
from PySide6.QtWidgets import QGraphicsRectItem

from ..core.zones import rotate
from .map_layer import MapLayer, auto_levels

ZONE_COLOR = "#2563EB"
PROBE_COLOR = "#C2410C"
DONE_COLOR = "#111827"
INK = "#374151"
PLATE_FILL = "#F3F4F6"
PLATE_EDGE = "#9CA3AF"
MIN_DRAWN_MM = 0.05  # en dessous, un tracé est considéré comme un clic accidentel


class _DrawingViewBox(pg.ViewBox):
    """ViewBox qui, avec un outil de dessin actif, trace une forme au lieu de déplacer la vue."""

    drawn = Signal(str, float, float, float, float)
    dragging = Signal(str, float, float, float, float)
    drag_finished = Signal()
    double_clicked = Signal(float, float)

    def __init__(self):
        super().__init__(lockAspect=True, enableMenu=False)
        self.tool = "select"

    def mouseDragEvent(self, ev, axis=None):
        if self.tool not in ("rect", "line") or ev.button() != Qt.MouseButton.LeftButton:
            super().mouseDragEvent(ev, axis)
            return
        ev.accept()
        a, b = self.mapSceneToView(ev.buttonDownScenePos()), self.mapSceneToView(ev.scenePos())
        coords = (self.tool, a.x(), a.y(), b.x(), b.y())
        if ev.isFinish():
            self.drag_finished.emit()
            self.drawn.emit(*coords)
        else:
            self.dragging.emit(*coords)

    def mouseClickEvent(self, ev):
        if ev.button() != Qt.MouseButton.LeftButton:
            super().mouseClickEvent(ev)
            return
        ev.accept()
        p = self.mapSceneToView(ev.scenePos())
        if ev.double():
            self.double_clicked.emit(p.x(), p.y())
        elif self.tool == "point":
            self.drawn.emit("point", p.x(), p.y(), p.x(), p.y())


class StageView(pg.PlotWidget):
    zone_drawn = Signal(str, float, float, float, float)
    zone_changed = Signal(object)
    zone_clicked = Signal(object)
    position_picked = Signal(float, float)

    def __init__(self, limits, invert_x=False, invert_y=False):
        viewbox = _DrawingViewBox()
        super().__init__(viewBox=viewbox, background="w")
        self._vb = viewbox
        self.limits = limits
        self._items = {}  # id(zone) → (zone, élément graphique, étiquette)
        self._selected = None

        (xlo, xhi), (ylo, yhi) = limits["x"], limits["y"]
        self._bounds = QRectF(xlo, ylo, xhi - xlo, yhi - ylo)
        plate = QGraphicsRectItem(self._bounds)
        plate.setPen(pg.mkPen(PLATE_EDGE, width=1.5, cosmetic=True))
        plate.setBrush(QBrush(QColor(PLATE_FILL)))
        plate.setZValue(-10)
        self.addItem(plate)

        for side, label in (("bottom", "X (mm)"), ("left", "Y (mm)")):
            axis = self.getAxis(side)
            axis.setPen(pg.mkPen(PLATE_EDGE))
            axis.setTextPen(pg.mkPen(INK))
            axis.setLabel(label, color=INK)
        self.showGrid(x=True, y=True, alpha=0.12)
        self._vb.invertX(invert_x)
        self._vb.invertY(invert_y)
        margin = 0.04 * max(xhi - xlo, yhi - ylo)
        self._vb.setRange(xRange=(xlo - margin, xhi + margin), yRange=(ylo - margin, yhi + margin), padding=0)

        self._points = pg.ScatterPlotItem(size=3, pen=None, brush=pg.mkBrush(ZONE_COLOR), pxMode=True)
        self._points.setZValue(5)
        self.addItem(self._points)
        self._done = pg.ScatterPlotItem(size=5, pen=None, brush=pg.mkBrush(DONE_COLOR), pxMode=True)
        self._done.setZValue(6)
        self.addItem(self._done)
        self._preview = pg.PlotDataItem(pen=pg.mkPen(ZONE_COLOR, width=2, style=Qt.PenStyle.DashLine))
        self._preview.setZValue(20)
        self.addItem(self._preview)
        self._probe = pg.ScatterPlotItem(size=16, symbol="o", pen=pg.mkPen(PROBE_COLOR, width=2),
                                         brush=pg.mkBrush(255, 255, 255, 0), pxMode=True)
        self._probe_dot = pg.ScatterPlotItem(size=4, symbol="o", pen=None, brush=pg.mkBrush(PROBE_COLOR), pxMode=True)
        for item in (self._probe, self._probe_dot):
            item.setZValue(30)
            self.addItem(item)

        # Carte mesurée, sous les zones et au-dessus du plateau
        self.map = MapLayer(self.getPlotItem(), z=-5)
        self.colorbar = pg.ColorBarItem(values=(0, 1), colorMap=self.map.cmap, interactive=True, width=14)
        self.colorbar.sigLevelsChanged.connect(lambda bar: self.map.set_levels(*bar.levels()))
        layout = self.getPlotItem().layout
        layout.addItem(self.colorbar, 2, 5)
        layout.setColumnFixedWidth(4, 5)
        self.colorbar.setVisible(False)

        self._vb.drawn.connect(self.zone_drawn)
        self._vb.double_clicked.connect(self.position_picked)
        self._vb.dragging.connect(self._show_preview)
        self._vb.drag_finished.connect(lambda: self._preview.setData([], []))

    # ------------------------------------------------------------------ outils et sonde
    def set_tool(self, tool):
        self._vb.tool = tool

    def set_probe(self, x, y):
        self._probe.setData([x], [y])
        self._probe_dot.setData([x], [y])

    def set_points(self, xy):
        if xy is None or len(xy) == 0:
            self._points.setData([], [])
        else:
            self._points.setData(xy[:, 0], xy[:, 1])

    def set_done_points(self, xy):
        """Points déjà mesurés pendant l'exécution des zones."""
        if xy is None or len(xy) == 0:
            self._done.setData([], [])
        else:
            self._done.setData(xy[:, 0], xy[:, 1])

    def set_map(self, zones, targets, zone_index, values, label="", keep_levels=False):
        """Affiche une carte mesurée (valeurs par point) ; values=None l'efface."""
        if values is None or len(values) == 0:
            self.map.clear()
            self.colorbar.setVisible(False)
            return
        self.map.set_data(zones, targets, zone_index, values)
        if not keep_levels:
            lo, hi = auto_levels(values)
            self.colorbar.setLevels((lo, hi))
        self.map.set_levels(*self.colorbar.levels())
        self.colorbar.axis.setLabel(label, color=INK)
        self.colorbar.setVisible(True)

    def _show_preview(self, kind, x0, y0, x1, y1):
        if kind == "rect":
            self._preview.setData([x0, x1, x1, x0, x0], [y0, y0, y1, y1, y0])
        else:
            self._preview.setData([x0, x1], [y0, y1])

    # ------------------------------------------------------------------ zones
    def _pen(self, zone):
        return pg.mkPen(ZONE_COLOR, width=3 if zone is self._selected else 2)

    def _make_item(self, zone):
        pen = self._pen(zone)
        if zone.kind == "rect":
            w, h = abs(zone.x1 - zone.x0), abs(zone.y1 - zone.y0)
            # Origine de l'élément = coin (xmin, ymin) du rectangle tourné autour de son centre
            origin = rotate([[min(zone.x0, zone.x1), min(zone.y0, zone.y1)]], zone.angle, zone.center)[0]
            item = pg.RectROI(list(origin), [w, h], angle=zone.angle, pen=pen, removable=False, sideScalers=True)
            item.addRotateHandle([1, 0.5], [0.5, 0.5])  # poignée de rotation autour du centre
        elif zone.kind == "line":
            item = pg.LineSegmentROI([[zone.x0, zone.y0], [zone.x1, zone.y1]], pen=pen, removable=False)
        else:
            item = pg.TargetItem(pos=(zone.x0, zone.y0), size=14, symbol="crosshair", pen=pen,
                                 hoverPen=pen, brush=None, movable=True)
            item.sigPositionChangeFinished.connect(lambda _: self._read_item(zone))
            return item
        item.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        item.sigClicked.connect(lambda *_: self.zone_clicked.emit(zone))
        item.sigRegionChangeFinished.connect(lambda _: self._read_item(zone))
        return item

    @staticmethod
    def _label_pos(zone):
        c = zone.corners() if zone.kind == "rect" else np.array([[zone.x0, zone.y0], [zone.x1, zone.y1]])
        return float(c[:, 0].min()), float(c[:, 1].max())

    def _make_label(self, zone):
        label = pg.TextItem(zone.name, color=INK, anchor=(0, 1))
        label.setPos(*self._label_pos(zone))
        label.setZValue(15)
        return label

    def _read_item(self, zone):
        """Répercute dans le modèle un déplacement de la zone à la souris."""
        item = self._items[id(zone)][1]
        before = (zone.x0, zone.y0, zone.x1, zone.y1, zone.angle)
        if zone.kind == "rect":
            p, s, angle = item.pos(), item.size(), item.angle()
            w, h = abs(s.x()), abs(s.y())
            # Le centre est l'origine plus la demi-diagonale tournée
            cx, cy = rotate([[p.x() + w / 2, p.y() + h / 2]], angle, (p.x(), p.y()))[0]
            zone.x0, zone.y0, zone.x1, zone.y1 = cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
            zone.angle = round(((angle + 180.0) % 360.0) - 180.0, 3)
        elif zone.kind == "line":
            a, b = (item.mapToParent(h["item"].pos()) for h in item.handles[:2])
            zone.x0, zone.y0, zone.x1, zone.y1 = a.x(), a.y(), b.x(), b.y()
        else:
            p = item.pos()
            zone.x0, zone.y0, zone.x1, zone.y1 = p.x(), p.y(), p.x(), p.y()
        moved = (zone.x0, zone.y0, zone.x1, zone.y1)
        zone.clamp(self.limits)
        if not np.allclose((zone.x0, zone.y0, zone.x1, zone.y1), moved):
            # Sortie des limites : on redessine, mais pas pendant le signal de l'élément lui-même
            QTimer.singleShot(0, lambda: self.update_zone(zone))
        else:
            self._items[id(zone)][2].setPos(*self._label_pos(zone))
        if (zone.x0, zone.y0, zone.x1, zone.y1, zone.angle) != before:
            self.zone_changed.emit(zone)

    def add_zone(self, zone):
        item, label = self._make_item(zone), self._make_label(zone)
        item.setZValue(10)
        self.addItem(item)
        self.addItem(label)
        self._items[id(zone)] = (zone, item, label)

    def remove_zone(self, zone):
        _, item, label = self._items.pop(id(zone))
        self.removeItem(item)
        self.removeItem(label)
        if self._selected is zone:
            self._selected = None

    def update_zone(self, zone):
        """Reconstruit l'élément graphique après une modification numérique."""
        if id(zone) in self._items:
            self.remove_zone_keep_selection(zone)
            self.add_zone(zone)

    def remove_zone_keep_selection(self, zone):
        selected = self._selected
        self.remove_zone(zone)
        self._selected = selected

    def highlight(self, zone):
        self._selected = zone
        for z, item, _ in self._items.values():
            item.setPen(self._pen(z))
