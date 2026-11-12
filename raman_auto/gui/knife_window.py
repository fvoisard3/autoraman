"""Fenêtre knife-edge : paramètres, suivi en direct (carte, profil, caustique) et résultats.

Utilise la platine et le wattmètre déjà connectés dans la fenêtre principale ; pendant la mesure,
les commandes manuelles de la platine et l'exécution des zones sont bloquées.
"""
import math

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QScrollArea, QSpinBox, QSplitter, QVBoxLayout, QWidget)

from ..config import output_dir, timestamp
from ..core.zones import MIN_STEP_MM
from ..drivers.lightfield import format_grating
from ..drivers.pm100d import format_power
from ..knife.fit import caustic, edge_profile
from ..knife.routine import DEFAULT_WINDOWS, SIGNALS, KnifeEdgeRoutine, KnifeParams, KnifeRecorder
from .stage_view import INK, PLATE_EDGE

SURFACE = "#fcfcfb"
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
BLUES = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]


def _plot(bottom, left, units_left=None):
    p = pg.PlotWidget()
    p.setBackground(SURFACE)
    p.showGrid(x=True, y=True, alpha=0.15)
    for side, label, units in (("bottom", bottom, None), ("left", left, units_left)):
        axis = p.getAxis(side)
        axis.setPen(pg.mkPen(PLATE_EDGE))
        axis.setTextPen(pg.mkPen(INK))
        axis.setLabel(label, units=units, color=INK)
    return p


def _spin(lo, hi, value, decimals, suffix, special=None):
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(decimals)
    s.setValue(value)
    s.setSuffix(suffix)
    if special:
        s.setSpecialValueText(special)
    return s


def format_duration(seconds):
    m, s = divmod(int(round(seconds)), 60)
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min {s:02d} s"


class KnifeEdgeWindow(QWidget):
    def __init__(self, main):
        super().__init__(main, Qt.WindowType.Window)
        self.main = main
        self.routine = None
        self.last_edge = None
        self._spec_zoomed = False
        self.setWindowTitle("Knife-edge — taille et foyer du spot" + (" (SIMULATION)" if main.simulate else ""))
        self.resize(1400, 900)
        self._build()
        self.refresh_zones()
        self._on_signal_changed()

    # ================================================================== construction
    def _build(self):
        limits = self.main.limits
        zlo, zhi = limits["z"]
        form_widget = QWidget()
        col = QVBoxLayout(form_widget)

        box = QGroupBox("1. Carte de recherche du bord")
        form = QFormLayout(box)
        zone_row = QHBoxLayout()
        self.zone_combo = QComboBox()
        self.zone_combo.currentIndexChanged.connect(self._on_zone_changed)
        refresh = QPushButton("↻")
        refresh.setFixedWidth(30)
        refresh.setToolTip("Relire les rectangles du tableau des zones")
        refresh.clicked.connect(self.refresh_zones)
        zone_row.addWidget(self.zone_combo, 1)
        zone_row.addWidget(refresh)
        form.addRow("Rectangle", zone_row)
        self.zone_info = QLabel()
        self.zone_info.setWordWrap(True)
        form.addRow(self.zone_info)
        self.map_step = _spin(MIN_STEP_MM, 20.0, 0.5, 4, " mm")
        self.map_step.valueChanged.connect(lambda _: self._refresh())
        form.addRow("Pas de la carte", self.map_step)
        self.reuse = QCheckBox("Réutiliser le bord trouvé (pas de carte)")
        self.reuse.toggled.connect(lambda _: self._refresh())
        form.addRow(self.reuse)
        hint = QLabel("Dessiner un rectangle (outil « Rectangle ») qui contient le bord de la lame. "
                      "Pas grossier : la carte sert seulement à trouver le bord et son orientation.")
        hint.setWordWrap(True)
        form.addRow(hint)
        col.addWidget(box)

        box = QGroupBox("2. Lignes perpendiculaires au bord")
        form = QFormLayout(box)
        self.line_length = _spin(0.0, 20.0, 0.0, 3, " mm", "automatique")
        self.line_step = _spin(0.0, 1.0, 0.0, 4, " mm", "automatique")
        self.readings = QSpinBox()
        self.readings.setRange(1, 50)
        self.readings.setValue(1)
        self.readings.valueChanged.connect(lambda _: self._refresh())
        form.addRow("Longueur", self.line_length)
        form.addRow("Pas", self.line_step)
        form.addRow("Lectures par point", self.readings)
        hint = QLabel("Automatique : la ligne couvre ~10 w et le pas vaut w/5 (au moins 12,5 µm), "
                      "réajustés après chaque ligne.")
        hint.setWordWrap(True)
        form.addRow(hint)
        col.addWidget(box)

        box = QGroupBox("3. Recherche du foyer (Z)")
        form = QFormLayout(box)
        self.z_scan = QCheckBox("Balayer Z (une ligne par hauteur)")
        self.z_scan.setChecked(True)
        self.z_scan.toggled.connect(lambda _: self._refresh())
        form.addRow(self.z_scan)
        self.z_from = _spin(zlo, zhi, zlo, 3, " mm")
        self.z_to = _spin(zlo, zhi, zlo, 3, " mm")
        self.z_step = _spin(0.01, 20.0, 0.25, 3, " mm")
        for s in (self.z_from, self.z_to, self.z_step):
            s.valueChanged.connect(lambda _: self._refresh())
        form.addRow("Z de", self.z_from)
        form.addRow("Z à", self.z_to)
        form.addRow("Pas en Z", self.z_step)
        around = QPushButton("Autour du Z actuel (± 1 mm)")
        around.clicked.connect(self._z_around_current)
        form.addRow(around)
        self.refine = QCheckBox("Second passage fin autour du minimum (pas / 4)")
        self.refine.setChecked(True)
        self.goto_focus = QCheckBox("Finir au foyer trouvé")
        self.goto_focus.setChecked(True)
        form.addRow(self.refine)
        form.addRow(self.goto_focus)
        hint = QLabel(f"Z petit = plateau proche de la sonde. Limites de la platine : {zlo:g} à {zhi:g} mm.")
        hint.setWordWrap(True)
        form.addRow(hint)
        col.addWidget(box)

        box = QGroupBox("Signal")
        form = QFormLayout(box)
        self.signal = QComboBox()
        self.signal.addItem("Wattmètre — lame de rasoir, capteur dessous", "power")
        self.signal.addItem("Raman — pic du Si (bord de wafer)", "raman")
        self.signal.addItem("Raie laser résiduelle (bord de wafer)", "laser")
        self.signal.currentIndexChanged.connect(self._on_signal_changed)
        form.addRow(self.signal)
        self.spectral_box = QWidget()
        sform = QFormLayout(self.spectral_box)
        sform.setContentsMargins(0, 0, 0, 0)
        self.win_lo = _spin(-2000.0, 5000.0, 495.0, 1, " cm⁻¹")
        self.win_hi = _spin(-2000.0, 5000.0, 545.0, 1, " cm⁻¹")
        for s in (self.win_lo, self.win_hi):
            s.valueChanged.connect(self._on_window_spin)
        self.baseline = QCheckBox("Retirer un fond linéaire (entre les bords de la fenêtre)")
        self.baseline.setChecked(True)
        sform.addRow("Fenêtre de", self.win_lo)
        sform.addRow("à", self.win_hi)
        sform.addRow(self.baseline)
        self.spectro_info = QLabel()
        self.spectro_info.setWordWrap(True)
        sform.addRow(self.spectro_info)
        form.addRow(self.spectral_box)
        self.signal_hint = QLabel()
        self.signal_hint.setWordWrap(True)
        form.addRow(self.signal_hint)
        col.addWidget(box)

        self.estimate = QLabel()
        self.estimate.setWordWrap(True)
        col.addWidget(self.estimate)
        buttons = QHBoxLayout()
        self.start_button = QPushButton("Lancer le knife-edge")
        self.start_button.clicked.connect(self._start)
        self.stop_button = QPushButton("Arrêter")
        self.stop_button.clicked.connect(self._stop)
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.stop_button)
        col.addLayout(buttons)
        self.status = QLabel("Prêt.")
        self.status.setWordWrap(True)
        col.addWidget(self.status)
        col.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(form_widget)
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(400)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        # ---- graphiques
        self.map_plot = _plot("X (mm)", "Y (mm)")
        self.map_plot.setAspectLocked(True)
        gui = self.main.gui_cfg
        self.map_plot.getViewBox().invertX(bool(gui.get("invert_x", False)))
        self.map_plot.getViewBox().invertY(bool(gui.get("invert_y", False)))
        self.map_planned = pg.ScatterPlotItem(size=4, brush=pg.mkBrush(PLATE_EDGE), pen=None)
        self.map_points = pg.ScatterPlotItem(pxMode=False, symbol="s", pen=None)
        self.edge_line = pg.InfiniteLine(pen=pg.mkPen(ORANGE, width=2))
        self.edge_line.setVisible(False)
        self.scan_path = pg.PlotDataItem(pen=pg.mkPen(AQUA, width=3))
        for item in (self.map_planned, self.map_points, self.edge_line, self.scan_path):
            self.map_plot.addItem(item)
        self._map_xy, self._map_v = [], []

        self.profile_plot = _plot("Position perpendiculaire au bord (µm)", "Signal", "W")
        self.profile_points = pg.ScatterPlotItem(size=7, brush=pg.mkBrush(BLUE), pen=pg.mkPen(SURFACE, width=1))
        self.profile_fit = pg.PlotDataItem(pen=pg.mkPen(ORANGE, width=2))
        self.profile_plot.addItem(self.profile_points)
        self.profile_plot.addItem(self.profile_fit)
        self.profile_title = QLabel("Profil de la ligne en cours")
        self._line_s, self._line_v = [], []

        self.caustic_plot = _plot("Z (mm)", "Rayon w à 1/e² (µm)")
        self.caustic_points = pg.ScatterPlotItem(size=9, brush=pg.mkBrush(BLUE), pen=pg.mkPen(SURFACE, width=1))
        self.caustic_fit = pg.PlotDataItem(pen=pg.mkPen(ORANGE, width=2))
        self.focus_line = pg.InfiniteLine(angle=90, pen=pg.mkPen(INK, width=1, style=Qt.PenStyle.DashLine))
        self.focus_line.setVisible(False)
        for item in (self.caustic_points, self.caustic_fit, self.focus_line):
            self.caustic_plot.addItem(item)

        self.results = QLabel("Résultats : —")
        self.results.setWordWrap(True)
        self.results.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.results.setStyleSheet("font-size: 11pt;")

        def titled(title_widget, plot):
            w = QWidget()
            lay = QVBoxLayout(w)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.addWidget(title_widget)
            lay.addWidget(plot, 1)
            return w

        self.spec_plot = _plot("Décalage Raman (cm⁻¹)", "Counts (somme des lignes)")
        self.spec_curve = self.spec_plot.plot(pen=pg.mkPen(BLUE, width=1.5))
        self.spec_region = pg.LinearRegionItem((495, 545), brush=pg.mkBrush(235, 104, 52, 40),
                                               pen=pg.mkPen(ORANGE, width=1.5))
        self.spec_region.sigRegionChangeFinished.connect(self._on_region_moved)
        self.spec_plot.addItem(self.spec_region)
        self.spec_title = QLabel("Dernier spectre et fenêtre d'intégration")
        self.spec_panel = titled(self.spec_title, self.spec_plot)

        bottom = QSplitter()
        bottom.addWidget(titled(self.profile_title, self.profile_plot))
        bottom.addWidget(titled(QLabel("Caustique w(z)"), self.caustic_plot))
        bottom.addWidget(self.spec_panel)
        bottom.setSizes([420, 300, 380])
        right = QSplitter(Qt.Orientation.Vertical)
        right.addWidget(titled(QLabel("Carte du signal et bord ajusté"), self.map_plot))
        right.addWidget(bottom)
        right.addWidget(self.results)
        right.setSizes([430, 380, 90])

        split = QSplitter()
        split.addWidget(scroll)
        split.addWidget(right)
        split.setSizes([420, 980])
        layout = QVBoxLayout(self)
        layout.addWidget(split)

    # ================================================================== état
    def refresh_zones(self):
        current = self.zone_combo.currentData()
        self.zone_combo.blockSignals(True)
        self.zone_combo.clear()
        for z in self.main.zones:
            if z.kind == "rect":
                self.zone_combo.addItem(z.name, z)
        if current is not None:
            i = self.zone_combo.findData(current)
            if i >= 0:
                self.zone_combo.setCurrentIndex(i)
        self.zone_combo.blockSignals(False)
        self._on_zone_changed()

    def _zone(self):
        return self.zone_combo.currentData()

    def _on_zone_changed(self, *_):
        z = self._zone()
        if z is None:
            self.zone_info.setText("Aucun rectangle dans le tableau des zones.")
        else:
            self.zone_info.setText(f"X {min(z.x0, z.x1):.3f} → {max(z.x0, z.x1):.3f} mm, "
                                   f"Y {min(z.y0, z.y1):.3f} → {max(z.y0, z.y1):.3f} mm")
            self.map_step.setValue(min(z.step_x, z.step_y))
        self._refresh()

    def _on_signal_changed(self, *_):
        key = self.signal.currentData()
        spectral = key in DEFAULT_WINDOWS
        self.spectral_box.setVisible(spectral)
        self.spec_panel.setVisible(spectral)
        if spectral:
            lo, hi = DEFAULT_WINDOWS[key]
            self.win_lo.setValue(lo)
            self.win_hi.setValue(hi)
        self.signal_hint.setText({
            "power": "Lame fixée sur le plateau, capteur du wattmètre dessous. Mesure la taille du spot laser.",
            "raman": "Bord clivé d'un wafer de Si. Mesure la résolution effective de la sonde "
                     "(excitation × collection), différente du spot laser seul.",
            "laser": "Bord clivé d'un wafer de Si : raie laser réfléchie qui traverse le filtre. Signal plus fort "
                     "que le Si, mais attention à la saturation de la caméra.",
        }[key])
        axis = self.profile_plot.getAxis("left")
        if spectral:
            axis.setLabel("Aire de la fenêtre (counts·cm⁻¹)", units=None, color=INK)
        else:
            axis.setLabel("Signal", units="W", color=INK)
        self._refresh()

    def _on_window_spin(self, *_):
        self.spec_region.blockSignals(True)
        self.spec_region.setRegion(sorted((self.win_lo.value(), self.win_hi.value())))
        self.spec_region.blockSignals(False)
        self._refresh()

    def _on_region_moved(self):
        lo, hi = self.spec_region.getRegion()
        self.win_lo.setValue(round(lo, 1))
        self.win_hi.setValue(round(hi, 1))

    def _on_spectrum(self, shift, spectrum, saturated):
        self.spec_curve.setData(shift, spectrum)
        if not self._spec_zoomed:  # premier spectre : vue autour de la fenêtre (le zoom reste libre ensuite)
            lo, hi = sorted((self.win_lo.value(), self.win_hi.value()))
            margin = max(3 * (hi - lo), 150.0)
            self.spec_plot.setXRange(lo - margin, hi + margin, padding=0)
            vb = self.spec_plot.getViewBox()
            vb.setAutoVisible(y=True)          # échelle verticale ajustée à la partie visible…
            vb.enableAutoRange(axis=vb.YAxis)  # …à chaque nouveau spectre
            self._spec_zoomed = True
        self.spec_curve.setPen(pg.mkPen("#e34948" if saturated else BLUE, width=1.5))
        self.spec_title.setText("Dernier spectre" + (" — CAMÉRA SATURÉE" if saturated else "")
                                + " et fenêtre d'intégration")

    def _exposure_ms(self):
        return float(self.main.applied.get("exposure", self.main.exposure_spin.value()))

    def _z_around_current(self):
        if self.main.position is None:
            return
        z = self.main.position[2]
        lo, hi = self.main.limits["z"]
        self.z_from.setValue(max(lo, z - 1.0))
        self.z_to.setValue(min(hi, z + 1.0))

    def _z_values(self):
        if not self.z_scan.isChecked():
            return []
        a, b, step = self.z_from.value(), self.z_to.value(), self.z_step.value()
        lo, hi = min(a, b), max(a, b)
        n = int(math.floor((hi - lo) / step + 1e-9)) + 1
        return [round(lo + i * step, 4) for i in range(n)]

    def params(self):
        z = self._zone()
        p = KnifeParams(map_step=self.map_step.value(), line_length=self.line_length.value(),
                        line_step=self.line_step.value(), readings=self.readings.value(),
                        z_values=self._z_values(), refine=self.refine.isChecked(),
                        goto_focus=self.goto_focus.isChecked(), signal=self.signal.currentData(),
                        laser_nm=float(self.main.laser_combo.currentData()),
                        window_lo=min(self.win_lo.value(), self.win_hi.value()),
                        window_hi=max(self.win_lo.value(), self.win_hi.value()),
                        baseline=self.baseline.isChecked(), exposure_ms=self._exposure_ms())
        if z is not None:
            p.x0, p.y0, p.x1, p.y1 = z.x0, z.y0, z.x1, z.y1
        if self.reuse.isChecked() and self.last_edge:
            p.reuse_edge = self.last_edge
        return p

    def _refresh(self):
        running = self.routine is not None
        self.reuse.setEnabled(self.last_edge is not None and not running)
        if self.last_edge is None:
            self.reuse.setChecked(False)
        p = self.params()
        n_map = 0 if p.reuse_edge else (len(p.map_points()) if self._zone() is not None else 0)
        n_z = len(p.z_values)
        self.estimate.setText(f"Carte : {n_map} points. Lignes : {max(n_z, 1)}"
                              + (f" (+ passage fin)" if p.refine and n_z >= 4 else "")
                              + f". Durée estimée ~{format_duration(p.estimate_s())}.")
        for w in (self.zone_combo, self.map_step, self.line_length, self.line_step, self.readings, self.z_scan,
                  self.z_from, self.z_to, self.z_step, self.refine, self.goto_focus, self.signal,
                  self.win_lo, self.win_hi, self.baseline):
            w.setEnabled(not running)
        self.spec_region.setMovable(not running)
        m = self.main
        if m.spectro is None:
            self.spectro_info.setText("Spectromètre non connecté.")
        else:
            self.spectro_info.setText(
                f"Réglages du spectromètre (fenêtre principale) : réseau "
                f"{format_grating(m.applied.get('grating', '?'))}, centre {m.applied.get('center', 0):.1f} nm, "
                f"exposition {self._exposure_ms():g} ms. Chaque point = une image complète sommée sur les lignes.")
        for w in (self.z_from, self.z_to, self.z_step, self.refine, self.goto_focus):
            w.setEnabled(not running and self.z_scan.isChecked())
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)

    # ================================================================== lancement
    def _start(self):
        m = self.main
        problems = []
        if m.stage is None or not m.homed:
            problems.append("connecter la platine et faire le homing")
        spectral = self.signal.currentData() in DEFAULT_WINDOWS
        if spectral and m.spectro is None:
            problems.append("connecter le spectromètre")
        if not spectral and m.power.pm is None:
            problems.append("connecter le wattmètre")
        if spectral and m.spectro_thread.busy:
            problems.append("attendre que le spectromètre ait fini d'appliquer ses réglages")
        if m.runner is not None:
            problems.append("attendre la fin de l'exécution en cours")
        p = self.params()
        if p.reuse_edge is None and self._zone() is None:
            problems.append("dessiner un rectangle autour du bord de la lame")
        if p.reuse_edge is None and len(p.map_points()) < 9:
            problems.append("carte trop petite (au moins 3 × 3 points)")
        if self.z_scan.isChecked() and len(p.z_values) < 2:
            problems.append("au moins 2 hauteurs Z")
        if problems:
            QMessageBox.warning(self, "Knife-edge", "Avant de lancer : " + " ; ".join(problems) + ".")
            return
        z_now = m.position[2]
        lines = []
        if p.reuse_edge:
            lines.append(f"Bord repris de la mesure précédente (direction {p.reuse_edge['edge_angle_deg']:.1f}°).")
        else:
            lines.append(f"Carte : {len(p.map_points())} points au pas de {p.map_step:g} mm, au Z actuel "
                         f"({z_now:.3f} mm).")
        if p.z_values:
            lines.append(f"Hauteurs : {len(p.z_values)} de Z = {min(p.z_values):.3f} à {max(p.z_values):.3f} mm "
                         f"(pas {self.z_step.value():g} mm). Z minimum = plateau le plus proche de la sonde : "
                         f"{min(p.z_values):.3f} mm.")
            lines.append("Fin : " + ("au foyer trouvé." if p.goto_focus else f"retour à Z = {z_now:.3f} mm."))
        else:
            lines.append(f"Une ligne au Z actuel ({z_now:.3f} mm).")
        if p.spectral:
            lines.append(f"Signal : {SIGNALS[p.signal]}, fenêtre {p.window_lo:g}–{p.window_hi:g} cm⁻¹"
                         f"{' avec fond linéaire' if p.baseline else ''} ; réseau "
                         f"{format_grating(m.applied.get('grating', '?'))}, exposition {p.exposure_ms:g} ms"
                         f"{f' × {p.readings}' if p.readings > 1 else ''} par point.")
            setup = f"Wafer de Si en place, filtre de sonde {p.laser_nm:g} nm installé"
        else:
            setup = "Lame et capteur en place"
        lines += [f"Durée estimée ~{format_duration(p.estimate_s())}.", "",
                  f"{setup}, laser {p.laser_nm:g} nm allumé et obturateur ouvert ?",
                  "La sonde ne doit rien toucher dans toute la plage de Z."]
        if QMessageBox.question(self, "Lancer le knife-edge", "\n".join(lines)) != QMessageBox.StandardButton.Yes:
            return

        path = output_dir(m.cfg, "knife_edge") / f"knife_{timestamp()}.h5"
        attrs = {"simulation": m.simulate, "laser_nm": p.laser_nm, "signal": p.signal,
                 "sensor": "LightField (PIXIS)" if p.spectral else "PM100D", "z_start_mm": z_now}

        def make_recorder(params):
            return KnifeRecorder(path, params, attrs)

        self._reset_plots(p)
        self.routine = KnifeEdgeRoutine(m.stage_thread, m.power.thread, m.stage, m.power.pm, p, make_recorder,
                                        spectro_thread=m.spectro_thread, spectro=m.spectro)
        r = self.routine
        r.spectrum.connect(self._on_spectrum)
        r.status.connect(self._on_status)
        r.map_started.connect(self._on_map_started)
        r.map_point.connect(self._on_map_point)
        r.map_done.connect(self._on_map_done)
        r.line_started.connect(self._on_line_started)
        r.line_point.connect(self._on_line_point)
        r.line_done.connect(self._on_line_done)
        r.caustic_done.connect(self._on_caustic)
        r.finished.connect(self._on_finished)
        m.runner = r
        m.power.set_running(True)
        m.log(f"Knife-edge lancé → {path}")
        m._refresh_stage_ui()
        m._refresh_spectro_ui()
        self._refresh()
        r.start()

    def _stop(self):
        if self.routine is not None:
            self.routine.stop()

    # ================================================================== suivi
    def _reset_plots(self, p):
        self._map_xy, self._map_v = [], []
        self.map_points.clear()
        self.map_planned.clear()
        self.edge_line.setVisible(False)
        self.scan_path.setData([], [])
        self.profile_points.clear()
        self.profile_fit.setData([], [])
        self.caustic_points.clear()
        self.caustic_fit.setData([], [])
        self.focus_line.setVisible(False)
        self.results.setText("Résultats : —")
        self._map_step = p.map_step
        self._spec_zoomed = False
        self.spec_curve.setData([], [])

    def _on_status(self, text):
        self.status.setText(text)

    def _on_map_started(self, points):
        self.map_planned.setData(points[:, 0], points[:, 1])
        self.map_plot.getViewBox().autoRange(padding=0.1)

    def _on_map_point(self, x, y, v):
        self._map_xy.append((x, y))
        self._map_v.append(v)
        v = np.array(self._map_v)
        lo, hi = float(np.min(v)), float(np.max(v))
        cmap = pg.ColorMap(np.linspace(0, 1, len(BLUES)), BLUES)
        t = (v - lo) / (hi - lo) if hi > lo else np.zeros_like(v)
        xy = np.array(self._map_xy)
        self.map_points.setData(xy[:, 0], xy[:, 1], size=self._map_step * 0.95,
                                brush=[pg.mkBrush(cmap.map(float(k), mode="qcolor")) for k in t])

    def _on_map_done(self, edge):
        if not edge.get("ok"):
            self.status.setText(f"Bord non trouvé : {edge.get('message')}")
            return
        th = math.radians(edge["theta_deg"])
        n = np.array([math.cos(th), math.sin(th)])
        self.edge_line.setPos(pg.Point(*(edge["d"] * n)))
        self.edge_line.setAngle(edge["edge_angle_deg"])
        self.edge_line.setVisible(True)

    def _on_line_started(self, index, z, center, normal):
        self._line_s, self._line_v = [], []
        self.profile_points.clear()
        self.profile_fit.setData([], [])
        self.profile_title.setText(f"Ligne {index + 1} — Z = {z:.3f} mm")
        half = 0.5 * (self.line_length.value() or 0.0)
        self._line_center, self._line_normal = center, normal
        if half:
            a, b = center - half * normal, center + half * normal
            self.scan_path.setData([a[0], b[0]], [a[1], b[1]])

    def _on_line_point(self, s, v):
        self._line_s.append(s * 1000)
        self._line_v.append(v)
        self.profile_points.setData(self._line_s, self._line_v)
        if len(self._line_s) >= 2:  # trajet réellement parcouru
            s_arr = np.array([self._line_s[0], self._line_s[-1]]) / 1000
            pts = self._line_center + np.outer(s_arr, self._line_normal)
            self.scan_path.setData(pts[:, 0], pts[:, 1])

    def _on_line_done(self, index, z, fit, s, v):
        if not (math.isfinite(fit["w"]) and math.isfinite(fit["s0"])):
            self.profile_title.setText(f"Ligne {index + 1} — Z = {z:.3f} mm : {fit['message']}")
            return
        xs = np.linspace(s.min(), s.max(), 400)
        self.profile_fit.setData(xs * 1000, edge_profile(xs, fit["s0"], fit["w"], fit["p_low"], fit["p_high"]))
        state = "" if fit["ok"] else f" — {fit['message']}"
        spectral = self.routine is not None and self.routine.params.spectral
        level = (f"contraste {abs(fit['p_high'] - fit['p_low']):.3g} counts·cm⁻¹" if spectral
                 else f"{format_power(fit['p_high'])} transmis")
        self.profile_title.setText(
            f"Ligne {index + 1} — Z = {z:.3f} mm : w = {fit['w'] * 1000:.1f} ± {fit['w_err'] * 1000:.1f} µm "
            f"(diamètre 1/e² {2 * fit['w'] * 1000:.1f} µm), {level}{state}")
        if self.routine is not None and not self.routine.params.z_values:
            self.results.setText(f"Résultats : w = {fit['w'] * 1000:.1f} µm à Z = {z:.3f} mm")

    def _on_caustic(self, zs, ws, fit):
        good = np.isfinite(ws)
        self.caustic_points.setData(zs[good], ws[good] * 1000)
        if fit and math.isfinite(fit.get("zr", math.nan)):
            zz = np.linspace(zs.min(), zs.max(), 300)
            self.caustic_fit.setData(zz, caustic(zz, fit["z0"], fit["w0"], fit["zr"]) * 1000)
        if fit and math.isfinite(fit.get("z0", math.nan)):
            self.focus_line.setPos(fit["z0"])
            self.focus_line.setVisible(True)
            spectral = self.routine is not None and self.routine.params.spectral
            text = ("Résolution effective (Raman) — " if spectral else "") + (
                f"Foyer : Z = {fit['z0']:.3f} ± {fit['z0_err']:.3f} mm — waist w0 = {fit['w0'] * 1000:.1f} µm "
                f"(diamètre 1/e² {2 * fit['w0'] * 1000:.1f} µm, FWHM {1.1774 * fit['w0'] * 1000:.1f} µm)")
            if math.isfinite(fit.get("zr", math.nan)):
                text += f" — zR = {fit['zr']:.3f} mm"
            if math.isfinite(fit.get("m2", math.nan)):
                text += f" — M² ≈ {fit['m2']:.1f}"
            if fit.get("message"):
                text += f"\n{fit['message']}"
            self.results.setText(text)

    def _on_finished(self, message, result):
        m = self.main
        self.routine = None
        m.runner = None
        m.power.set_running(False)
        if result.get("edge", {}).get("ok") and result.get("center"):
            self.last_edge = {k: v for k, v in result["edge"].items()
                              if k in ("theta_deg", "edge_angle_deg", "d", "w", "ok")}
            self.last_edge["center_hint"] = result["center"]
            self.last_edge["map_step"] = self.map_step.value()
        self.status.setText(message)
        m.log(message)
        m._refresh_stage_ui()
        m._refresh_spectro_ui()
        self._refresh()

    def closeEvent(self, event):
        if self.routine is not None:
            if QMessageBox.question(self, "Knife-edge", "Mesure en cours. L'arrêter ?") \
                    != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.routine.stop()
        super().closeEvent(event)
