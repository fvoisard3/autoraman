"""Fenêtre principale : platine 2D, zones d'analyse, spectromètre (dont choix du réseau)."""
import json
import time
from dataclasses import asdict

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSplitter,
    QTableWidget, QTableWidgetItem, QTabWidget, QToolBar, QVBoxLayout, QWidget)

from ..config import PROJECT_ROOT, output_dir, timestamp
from ..core.storage import RunWriter
from ..core.zones import MIN_STEP_MM, Zone, estimate_seconds, load_zones, save_zones
from ..drivers.lightfield import LightFieldSpectrometer, format_grating, raman_shift
from ..drivers.pm100d import format_power
from ..drivers.sim import SimSpectrometer, SimStage
from ..drivers.ultimaker import UltimakerStage
from .power_panel import PowerPanel
from .scan import ScanRunner, format_bytes, image_bytes
from .stage_view import INK, MIN_DRAWN_MM, PLATE_EDGE, ZONE_COLOR, StageView
from .workers import DeviceThread

ZONE_COLUMNS = [("Nom", "name"), ("Type", None), ("X0", "x0"), ("Y0", "y0"), ("X1", "x1"), ("Y1", "y1"),
                ("Pas X", "step_x"), ("Pas Y", "step_y"), ("Raman", "raman"), ("Wattmètre", "power"),
                ("Points", None)]
CHECK_KEYS = ("raman", "power")
KIND_LABELS = {"rect": "Rectangle", "line": "Ligne", "point": "Point"}
JOG_STEPS_MM = [0.0125, 0.05, 0.1, 1.0, 10.0]
MAX_PREVIEW_POINTS = 20000
NEAR_ZERO_MM = 1e-3


def format_duration(seconds):
    minutes, s = divmod(int(round(seconds)), 60)
    h, m = divmod(minutes, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min {s:02d} s"


class MainWindow(QMainWindow):
    def __init__(self, cfg, simulate=False):
        super().__init__()
        self.cfg = cfg
        self.simulate = simulate
        self.ucfg = cfg["ultimaker"]
        self.limits = self.ucfg["limits"]
        self.gui_cfg = cfg.get("gui", {})
        self.setWindowTitle("Contrôle Raman" + (" — SIMULATION" if simulate else ""))
        self.resize(1500, 980)

        self.zones = []
        self._zone_counter = 0
        self.stage = None
        self.spectro = None
        self.position = None
        self.homed = False
        self.last_spectrum = None
        self.applied = {}  # dernières valeurs envoyées au spectromètre
        self.runner = None

        self.stage_thread = DeviceThread(self)
        self.spectro_thread = DeviceThread(self)
        self.stage_thread.done.connect(self._on_stage_done)
        self.stage_thread.failed.connect(self._on_stage_failed)
        self.stage_thread.busy_changed.connect(lambda _: self._refresh_stage_ui())
        self.spectro_thread.done.connect(self._on_spectro_done)
        self.spectro_thread.failed.connect(self._on_spectro_failed)
        self.spectro_thread.busy_changed.connect(lambda _: self._refresh_spectro_ui())

        self._build_ui()
        self._refresh_stage_ui()
        self._refresh_spectro_ui()
        self._refresh_zone_summary()
        self.log("Mode simulation : aucun appareil réel n'est utilisé." if simulate else "Prêt.")

    # ------------------------------------------------------------------ construction
    def _build_ui(self):
        self.view = StageView(self.limits, self.gui_cfg.get("invert_x", False), self.gui_cfg.get("invert_y", False))
        self.view.zone_drawn.connect(self._on_zone_drawn)
        self.view.zone_changed.connect(self._on_zone_moved)
        self.view.zone_clicked.connect(self._select_zone)
        self.view.position_picked.connect(self._on_position_picked)
        self._build_toolbar()

        self.spectrum_plot = pg.PlotWidget(background="w")
        self.spectrum_curve = self.spectrum_plot.plot(pen=pg.mkPen(ZONE_COLOR, width=1.5))
        for side, label in (("bottom", "Décalage Raman (cm⁻¹)"), ("left", "Intensité (counts)")):
            axis = self.spectrum_plot.getAxis(side)
            axis.setPen(pg.mkPen(PLATE_EDGE))
            axis.setTextPen(pg.mkPen(INK))
            axis.setLabel(label, color=INK)
        self.spectrum_plot.showGrid(x=True, y=True, alpha=0.12)

        self.log_box = QPlainTextEdit(readOnly=True)
        self.log_box.setMaximumBlockCount(1000)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_zones_panel(), "Zones d'analyse")
        self.tabs.addTab(self.spectrum_plot, "Spectre")
        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.addWidget(self.tabs)
        bottom_layout.addLayout(self._build_run_controls())
        bottom_layout.addWidget(self.run_status)
        left = QSplitter(Qt.Orientation.Vertical)
        left.addWidget(self.view)
        left.addWidget(bottom)
        left.setSizes([600, 340])

        controls = QWidget()
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.addWidget(self._build_stage_panel())
        controls_layout.addWidget(self._build_spectro_panel())
        self.power = PowerPanel(self.cfg, self.simulate, self.laser_combo.currentData(), self)
        self.power.logged.connect(self.log)
        self.laser_combo.currentIndexChanged.connect(lambda _: self.power.set_wavelength(self.laser_combo.currentData()))
        controls_layout.addWidget(self.power)
        right = QSplitter(Qt.Orientation.Vertical)
        right.addWidget(controls)
        right.addWidget(self.log_box)
        right.setStretchFactor(1, 1)

        main = QSplitter(Qt.Orientation.Horizontal)
        main.addWidget(left)
        main.addWidget(right)
        main.setSizes([920, 560])
        self.setCentralWidget(main)

    def _build_toolbar(self):
        bar = QToolBar("Outils de dessin")
        bar.setMovable(False)
        self.addToolBar(bar)
        group = QActionGroup(self)
        self.tool_actions = {}
        for tool, text, tip in (
                ("select", "Déplacer / éditer", "Glisser pour déplacer la vue, molette pour zoomer, poignées pour éditer"),
                ("rect", "Rectangle", "Glisser pour dessiner une zone de cartographie"),
                ("line", "Ligne", "Glisser pour dessiner une ligne de points"),
                ("point", "Point", "Cliquer pour ajouter un point de mesure")):
            action = QAction(text, self, checkable=True, toolTip=tip)
            action.triggered.connect(lambda _=False, t=tool: self.view.set_tool(t))
            group.addAction(action)
            bar.addAction(action)
            self.tool_actions[tool] = action
        self.tool_actions["select"].setChecked(True)
        bar.addSeparator()
        bar.addWidget(QLabel(" Pas par défaut : "))
        self.default_step = self._spin(MIN_STEP_MM, 200.0, float(self.gui_cfg.get("default_step_mm", 1.0)), 4, " mm")
        bar.addWidget(self.default_step)
        fit = QAction("Vue complète", self)
        fit.triggered.connect(self._fit_view)
        bar.addAction(fit)
        bar.addSeparator()
        analyse = QAction("Analyse des fichiers…", self,
                          toolTip="Ouvre l'outil de cartes et graphiques (programme séparé) sur le dernier fichier")
        analyse.triggered.connect(self._open_analysis)
        bar.addAction(analyse)

    def _open_analysis(self):
        """Lance run_analyse.py dans un processus séparé : l'acquisition n'est pas affectée."""
        import subprocess
        import sys
        runs = PROJECT_ROOT / self.cfg.get("data_dir", "data") / "runs"
        files = sorted(runs.glob("*/*.h5"), key=lambda p: p.stat().st_mtime) if runs.exists() else []
        args = [sys.executable, str(PROJECT_ROOT / "run_analyse.py")] + ([str(files[-1])] if files else [])
        subprocess.Popen(args, cwd=str(PROJECT_ROOT))
        self.log("Outil d'analyse lancé" + (f" sur {files[-1].name}." if files else "."))

    @staticmethod
    def _spin(lo, hi, value, decimals, suffix=""):
        spin = QDoubleSpinBox()
        spin.setRange(lo, hi)
        spin.setDecimals(decimals)
        spin.setValue(value)
        spin.setSuffix(suffix)
        spin.setKeyboardTracking(False)
        return spin

    def _build_zones_panel(self):
        self.zone_table = QTableWidget(0, len(ZONE_COLUMNS))
        self.zone_table.setHorizontalHeaderLabels([c[0] for c in ZONE_COLUMNS])
        self.zone_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.zone_table.verticalHeader().setVisible(False)
        self.zone_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.zone_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.zone_table.cellChanged.connect(self._on_zone_cell_changed)
        self.zone_table.itemSelectionChanged.connect(self._on_zone_selection)

        self.zone_summary = QLabel()
        delete = QPushButton("Supprimer la zone")
        delete.clicked.connect(self._delete_selected_zone)
        save = QPushButton("Enregistrer…")
        save.clicked.connect(self._save_zones)
        load = QPushButton("Charger…")
        load.clicked.connect(self._load_zones)
        row = QHBoxLayout()
        row.addWidget(self.zone_summary, 1)
        for button in (delete, save, load):
            row.addWidget(button)

        box = QWidget()
        layout = QVBoxLayout(box)
        layout.addWidget(QLabel("Coordonnées et pas en mm. Double-clic sur une cellule pour la modifier."))
        layout.addWidget(self.zone_table)
        layout.addLayout(row)
        return box

    def _build_run_controls(self):
        self.run_name = QLineEdit("acquisition")
        self.run_name.setToolTip("Préfixe du fichier HDF5 (data/runs/<date>/<nom>_<heure>.h5)")
        self.run_start = QPushButton("Lancer les zones")
        self.run_start.clicked.connect(self._start_run)
        self.run_pause = QPushButton("Pause")
        self.run_pause.clicked.connect(self._toggle_pause)
        self.run_stop = QPushButton("Arrêter")
        self.run_stop.clicked.connect(self._stop_run)
        self.run_progress = QProgressBar()
        self.run_status = QLabel()
        self.run_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row = QHBoxLayout()
        row.addWidget(QLabel("Nom :"))
        row.addWidget(self.run_name)
        for widget in (self.run_start, self.run_pause, self.run_stop):
            row.addWidget(widget)
        row.addWidget(self.run_progress, 1)
        return row

    def _build_stage_panel(self):
        self.stage_connect = QPushButton("Connecter la platine")
        self.stage_connect.clicked.connect(self._toggle_stage)
        self.stage_home = QPushButton("Homing (G28)")
        self.stage_home.clicked.connect(self._home)
        self.stage_status = QLabel()
        self.position_label = QLabel("X —    Y —    Z —")
        self.position_label.setStyleSheet("font-weight: 600;")

        self.jog_step = QComboBox()
        for step in JOG_STEPS_MM:
            self.jog_step.addItem(f"{step:g} mm", step)
        self.jog_step.setCurrentIndex(3)
        self.jog_buttons = []
        jog = QGridLayout()
        for text, axis, sign, r, c in (("Y+", "y", 1, 0, 1), ("X−", "x", -1, 1, 0), ("X+", "x", 1, 1, 2),
                                       ("Y−", "y", -1, 2, 1), ("Z+", "z", 1, 0, 4), ("Z−", "z", -1, 2, 4)):
            button = QPushButton(text)
            button.clicked.connect(lambda _=False, a=axis, s=sign: self._jog(a, s))
            jog.addWidget(button, r, c)
            self.jog_buttons.append(button)
        jog.addWidget(QLabel("Pas :"), 1, 3, Qt.AlignmentFlag.AlignRight)
        jog.addWidget(self.jog_step, 1, 4)

        self.goto = {ax: self._spin(*self.limits[ax], float(self.limits[ax][0]), 4) for ax in "xyz"}
        self.goto_xy = QPushButton("Aller en X, Y")
        self.goto_xy.clicked.connect(self._goto_xy)
        self.goto_z = QPushButton("Aller en Z")
        self.goto_z.clicked.connect(self._goto_z)
        goto = QGridLayout()
        for i, ax in enumerate("xyz"):
            goto.addWidget(QLabel(ax.upper()), 0, i * 2, Qt.AlignmentFlag.AlignRight)
            goto.addWidget(self.goto[ax], 0, i * 2 + 1)
        goto.addWidget(self.goto_xy, 1, 0, 1, 4)
        goto.addWidget(self.goto_z, 1, 4, 1, 2)

        top = QHBoxLayout()
        top.addWidget(self.stage_connect)
        top.addWidget(self.stage_home)
        box = QGroupBox("Platine")
        layout = QVBoxLayout(box)
        layout.addLayout(top)
        layout.addWidget(self.stage_status)
        layout.addWidget(self.position_label)
        layout.addLayout(jog)
        layout.addLayout(goto)
        return box

    def _build_spectro_panel(self):
        self.spectro_connect = QPushButton("Connecter le spectromètre")
        self.spectro_connect.clicked.connect(self._toggle_spectro)
        self.spectro_status = QLabel()
        self.grating_combo = QComboBox()
        self.grating_combo.activated.connect(self._apply_grating)
        self.center_spin = self._spin(100.0, 2000.0, 585.0, 3, " nm")
        self.center_spin.editingFinished.connect(self._apply_center)
        self.exposure_spin = self._spin(1.0, 3.6e6, 1000.0, 1, " ms")
        self.exposure_spin.editingFinished.connect(self._apply_exposure)
        self.exposure_spin.valueChanged.connect(lambda _: self._refresh_zone_summary())
        self.laser_combo = QComboBox()
        for nm in self.cfg["laser"]["lines_nm"]:
            self.laser_combo.addItem(f"{nm:g} nm", float(nm))
        self.laser_combo.setCurrentIndex(self.laser_combo.findData(float(self.cfg["laser"]["default_nm"])))
        self.laser_combo.currentIndexChanged.connect(lambda _: self._plot_spectrum())
        self.acquire = QPushButton("Acquérir un spectre")
        self.acquire.clicked.connect(self._acquire)

        form = QFormLayout()
        form.addRow("Réseau", self.grating_combo)
        form.addRow("Centre", self.center_spin)
        form.addRow("Exposition", self.exposure_spin)
        form.addRow("Raie laser", self.laser_combo)
        box = QGroupBox("Spectromètre")
        layout = QVBoxLayout(box)
        layout.addWidget(self.spectro_connect)
        layout.addWidget(self.spectro_status)
        layout.addLayout(form)
        layout.addWidget(self.acquire)
        return box

    # ------------------------------------------------------------------ journal
    def log(self, message):
        self.log_box.appendPlainText(f"{time.strftime('%H:%M:%S')}  {message}")

    def _fit_view(self):
        (xlo, xhi), (ylo, yhi) = self.limits["x"], self.limits["y"]
        self.view.getViewBox().setRange(xRange=(xlo, xhi), yRange=(ylo, yhi))

    # ------------------------------------------------------------------ zones
    def _on_zone_drawn(self, kind, x0, y0, x1, y1):
        if kind == "rect" and min(abs(x1 - x0), abs(y1 - y0)) < MIN_DRAWN_MM:
            return
        if kind == "line" and max(abs(x1 - x0), abs(y1 - y0)) < MIN_DRAWN_MM:
            return
        self._zone_counter += 1
        step = self.default_step.value()
        self._add_zone(Zone(f"Zone {self._zone_counter}", kind, x0, y0, x1, y1, step, step))
        self._select_zone(self.zones[-1])

    def _add_zone(self, zone):
        zone.clamp(self.limits)
        self.zones.append(zone)
        self.view.add_zone(zone)
        self._rebuild_zone_table()
        self._refresh_zone_summary()
        self._refresh_run_ui()

    def _on_zone_moved(self, zone):
        self._rebuild_zone_table()
        self._refresh_zone_summary()

    def _cell_editable(self, zone, key):
        if key is None:
            return False
        if zone.kind == "point":
            return key in ("name", "x0", "y0")
        if zone.kind == "line":
            return key != "step_y"
        return True

    def _cell_text(self, zone, key, header):
        if header == "Type":
            return KIND_LABELS[zone.kind]
        if header == "Points":
            return f"{zone.count():,}".replace(",", " ")
        if not self._cell_editable(zone, key):
            return "—"
        value = getattr(zone, key)
        return value if key == "name" else f"{value:.4g}"

    def _rebuild_zone_table(self):
        row_selected = self.zone_table.currentRow()
        with QSignalBlocker(self.zone_table):
            self.zone_table.setRowCount(len(self.zones))
            for r, zone in enumerate(self.zones):
                for c, (header, key) in enumerate(ZONE_COLUMNS):
                    if key in CHECK_KEYS:
                        item = QTableWidgetItem()
                        item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled
                                      | Qt.ItemFlag.ItemIsSelectable)
                        item.setCheckState(Qt.CheckState.Checked if getattr(zone, key) else Qt.CheckState.Unchecked)
                    else:
                        item = QTableWidgetItem(self._cell_text(zone, key, header))
                        if not self._cell_editable(zone, key):
                            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    self.zone_table.setItem(r, c, item)
            if 0 <= row_selected < len(self.zones):
                self.zone_table.selectRow(row_selected)

    def _on_zone_cell_changed(self, row, column):
        zone, key = self.zones[row], ZONE_COLUMNS[column][1]
        if key in CHECK_KEYS:
            setattr(zone, key, self.zone_table.item(row, column).checkState() == Qt.CheckState.Checked)
            self._refresh_zone_summary()
            return
        text = self.zone_table.item(row, column).text().strip()
        if key == "name":
            zone.name = text or zone.name
        else:
            try:
                value = float(text.replace(",", "."))
            except ValueError:
                value = None
            if value is None or (key.startswith("step") and value < MIN_STEP_MM):
                self.log(f"Valeur refusée : « {text} » (pas minimal {MIN_STEP_MM} mm).")
            else:
                setattr(zone, key, value)
                if zone.kind == "point":
                    zone.x1, zone.y1 = zone.x0, zone.y0
                zone.clamp(self.limits)
        self.view.update_zone(zone)
        self._rebuild_zone_table()
        self._refresh_zone_summary()

    def _select_zone(self, zone):
        if zone in self.zones:
            self.zone_table.selectRow(self.zones.index(zone))

    def _on_zone_selection(self):
        rows = self.zone_table.selectionModel().selectedRows()
        self.view.highlight(self.zones[rows[0].row()] if rows else None)

    def _delete_selected_zone(self):
        rows = self.zone_table.selectionModel().selectedRows()
        if rows:
            self.view.remove_zone(self.zones.pop(rows[0].row()))
            self._rebuild_zone_table()
            self._refresh_zone_summary()
            self._refresh_run_ui()

    def _refresh_zone_summary(self):
        n = sum(z.count() for z in self.zones)
        duration = format_duration(estimate_seconds(self.zones, self.exposure_spin.value()))
        self.zone_summary.setText(f"{n:,} points — durée estimée {duration}".replace(",", " "))
        if self.zones and n <= 500000:
            points = np.vstack([z.points() for z in self.zones])
            self.view.set_points(points[::max(1, len(points) // MAX_PREVIEW_POINTS)])
        else:
            self.view.set_points(None)

    def _zones_dir(self):
        d = PROJECT_ROOT / self.cfg.get("data_dir", "data") / "zones"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    def _save_zones(self):
        path, _ = QFileDialog.getSaveFileName(self, "Enregistrer les zones", self._zones_dir(), "Zones (*.json)")
        if path:
            save_zones(self.zones, path)
            self.log(f"Zones enregistrées : {path}")

    def _load_zones(self):
        path, _ = QFileDialog.getOpenFileName(self, "Charger des zones", self._zones_dir(), "Zones (*.json)")
        if not path:
            return
        try:
            loaded = load_zones(path)
        except (OSError, ValueError, TypeError) as e:
            QMessageBox.warning(self, "Chargement impossible", str(e))
            return
        for zone in list(self.zones):
            self.view.remove_zone(zone)
        self.zones.clear()
        for zone in loaded:
            self._add_zone(zone)
        self._zone_counter = max(self._zone_counter, len(loaded))
        self.log(f"{len(loaded)} zones chargées : {path}")

    # ------------------------------------------------------------------ platine
    def _refresh_stage_ui(self):
        connected, busy = self.stage is not None, self.stage_thread.busy or self.runner is not None
        self.stage_connect.setText("Déconnecter la platine" if connected else "Connecter la platine")
        self.stage_connect.setEnabled(not busy)
        self.stage_home.setEnabled(connected and not busy)
        can_move = connected and self.homed and not busy
        for widget in self.jog_buttons + [self.goto_xy, self.goto_z]:
            widget.setEnabled(can_move)
        if not connected:
            status = "Déconnectée"
        elif self.runner is not None:
            status = "Exécution des zones en cours"
        elif busy:
            status = "En mouvement…"
        else:
            status = "Prête" if self.homed else "Homing requis avant tout déplacement"
        self.stage_status.setText(status)
        self._refresh_run_ui()

    def _toggle_stage(self):
        if self.stage is None:
            self.log("Connexion à la platine…")
            self.stage_thread.submit("connect", self._job_stage_connect)
        else:
            stage, self.stage = self.stage, None
            self.homed = False
            self.stage_thread.submit("disconnect", stage.close)

    def _job_stage_connect(self):
        stage = SimStage(self.ucfg) if self.simulate else UltimakerStage(self.ucfg)
        stage.connect()
        return stage, stage.position()

    def _stage_job(self, tag, action):
        stage = self.stage

        def job():
            action(stage)
            return stage.position()

        self.stage_thread.submit(tag, job)

    def _home(self):
        self._stage_job("home", lambda s: s.home())

    def _jog(self, axis, sign):
        if self.position is None:
            return
        step = self.jog_step.currentData() * sign
        target = {axis: self.position["xyz".index(axis)] + step}
        # Sous 0,1 mm, un mouvement direct risque d'être ignoré par le firmware (< 5 pas)
        if abs(step) < 0.1:
            self._stage_job("move", lambda s: s.move_precise(**target))
        else:
            self._stage_job("move", lambda s: s.move_to(**target))

    def _goto_xy(self):
        x, y = self.goto["x"].value(), self.goto["y"].value()
        self._stage_job("move", lambda s: s.move_precise(x=x, y=y))

    def _goto_z(self):
        z = self.goto["z"].value()
        self._stage_job("move", lambda s: s.move_precise(z=z))

    def _on_position_picked(self, x, y):
        """Double-clic sur la vue : préremplit la destination, sans déplacer la platine."""
        for ax, v in (("x", x), ("y", y)):
            self.goto[ax].setValue(v)
        self.log(f"Destination préremplie : X {self.goto['x'].value():.3f}, Y {self.goto['y'].value():.3f}")

    def _show_position(self, result):
        pos, counts = result
        self.position = counts or pos
        x, y, z = self.position
        self.position_label.setText(f"X {x:.3f}    Y {y:.3f}    Z {z:.3f} mm")
        self.view.set_probe(x, y)

    def _on_stage_done(self, tag, result):
        if tag.startswith("scan_"):  # géré par ScanRunner ; on affiche seulement la position
            self._show_position(result)
            return
        if tag == "connect":
            self.stage, reading = result
            self._show_position(reading)
            # Tant que le homing n'a pas été fait, le firmware rapporte exactement (0, 0, 0)
            self.homed = any(abs(v) > NEAR_ZERO_MM for v in reading[0])
            self.log("Platine connectée." + ("" if self.homed else " Homing requis."))
        elif tag == "disconnect":
            self.position = None
            self.position_label.setText("X —    Y —    Z —")
            self.log("Platine déconnectée.")
        else:
            self._show_position(result)
            if tag == "home":
                self.homed = True
                self.log("Homing terminé.")
        self._refresh_stage_ui()

    def _on_stage_failed(self, tag, message):
        if tag.startswith("scan_"):
            return
        self.log(f"Platine — erreur ({tag}) : {message}")
        self.statusBar().showMessage(f"Erreur platine : {message}", 10000)
        self._refresh_stage_ui()

    # ------------------------------------------------------------------ spectromètre
    def _refresh_spectro_ui(self):
        connected, busy = self.spectro is not None, self.spectro_thread.busy or self.runner is not None
        self.spectro_connect.setText("Déconnecter le spectromètre" if connected else "Connecter le spectromètre")
        self.spectro_connect.setEnabled(not busy)
        for widget in (self.grating_combo, self.center_spin, self.exposure_spin, self.acquire):
            widget.setEnabled(connected and not busy)
        self.spectro_status.setText("Déconnecté" if not connected else "Occupé…" if busy else "Prêt")
        self._refresh_run_ui()

    def _toggle_spectro(self):
        if self.spectro is None:
            self.log("Connexion au spectromètre (LightField peut mettre une minute à démarrer)…")
            self.spectro_thread.submit("connect", self._job_spectro_connect)
        else:
            spectro, self.spectro = self.spectro, None
            self.spectro_thread.submit("disconnect", spectro.close)

    def _job_spectro_connect(self):
        if self.simulate:
            spectro = SimSpectrometer()
        else:
            lf = self.cfg["lightfield"]
            spectro = LightFieldSpectrometer(visible=lf.get("visible", True), experiment=lf.get("experiment"))
        spectro.open()
        return spectro, self._read_spectro(spectro)

    @staticmethod
    def _read_spectro(spectro):
        return {"gratings": spectro.gratings(), "grating": spectro.grating,
                "center": spectro.center_wavelength_nm, "exposure": spectro.exposure_ms}

    def _show_spectro_state(self, info):
        self.applied = info
        with QSignalBlocker(self.grating_combo):
            self.grating_combo.clear()
            for raw in info["gratings"]:
                self.grating_combo.addItem(format_grating(raw), raw)
            self.grating_combo.setCurrentIndex(self.grating_combo.findData(info["grating"]))
        with QSignalBlocker(self.center_spin):
            self.center_spin.setValue(info["center"])
        self.exposure_spin.setValue(info["exposure"])

    def _spectro_setter(self, tag, attribute, value):
        spectro = self.spectro

        def job():
            setattr(spectro, attribute, value)
            return self._read_spectro(spectro)

        self.spectro_thread.submit(tag, job)

    def _apply_grating(self, _index):
        raw = self.grating_combo.currentData()
        if raw != self.applied.get("grating"):
            self.log(f"Changement de réseau : {self.grating_combo.currentText()}")
            self._spectro_setter("grating", "grating", raw)

    def _apply_center(self):
        if self.spectro is not None and abs(self.center_spin.value() - self.applied.get("center", 0.0)) > 1e-3:
            self._spectro_setter("center", "center_wavelength_nm", self.center_spin.value())

    def _apply_exposure(self):
        if self.spectro is not None and abs(self.exposure_spin.value() - self.applied.get("exposure", 0.0)) > 1e-3:
            self._spectro_setter("exposure", "exposure_ms", self.exposure_spin.value())

    def _acquire(self):
        spectro = self.spectro
        self.spectro_thread.submit("acquire", spectro.acquire_spectrum)

    def _plot_spectrum(self):
        if self.last_spectrum is not None:
            wavelengths, spectrum = self.last_spectrum
            self.spectrum_curve.setData(raman_shift(wavelengths, self.laser_combo.currentData()), spectrum)

    def _on_spectro_done(self, tag, result):
        if tag.startswith("scan_"):  # géré par ScanRunner
            return
        if tag == "connect":
            self.spectro, info = result
            self._show_spectro_state(info)
            self.log(f"Spectromètre connecté, réseau : {format_grating(info['grating'])}")
        elif tag == "disconnect":
            self.applied = {}
            self.log("Spectromètre déconnecté.")
        elif tag == "acquire":
            wavelengths, spectrum, duration = result
            self.last_spectrum = (wavelengths, spectrum)
            self._plot_spectrum()
            self.tabs.setCurrentWidget(self.spectrum_plot)
            self.log(f"Spectre acquis en {duration:.2f} s (max {spectrum.max():.0f} counts).")
        else:
            self._show_spectro_state(result)
            self.log(f"Réglage appliqué ({tag}).")
        self._refresh_spectro_ui()

    def _on_spectro_failed(self, tag, message):
        if tag.startswith("scan_"):
            return
        self.log(f"Spectromètre — erreur ({tag}) : {message}")
        self.statusBar().showMessage(f"Erreur spectromètre : {message}", 10000)
        if self.applied:  # le matériel n'a pas changé : on remet l'affichage en accord
            self._show_spectro_state(self.applied)
        self._refresh_spectro_ui()

    # ------------------------------------------------------------------ exécution des zones
    def _refresh_run_ui(self):
        running = self.runner is not None
        # Les appareils requis par les zones cochées sont vérifiés au lancement, avec un message explicite
        ready = (self.stage is not None and self.homed and bool(self.zones)
                 and not self.stage_thread.busy and not self.spectro_thread.busy)
        self.run_start.setEnabled(ready and not running)
        self.run_pause.setEnabled(running)
        self.run_stop.setEnabled(running)
        self.run_name.setEnabled(not running)
        self.zone_table.setEnabled(not running)
        for action in self.tool_actions.values():
            action.setEnabled(not running)
        if not running:
            self.run_pause.setText("Pause")

    def _start_run(self):
        empty = [z.name for z in self.zones if not (z.raman or z.power)]
        if empty:
            QMessageBox.warning(self, "Lancer les zones",
                                "Aucune mesure cochée (Raman ou Wattmètre) pour : " + ", ".join(empty))
            return
        n = sum(z.count() for z in self.zones)
        n_raman = sum(z.count() for z in self.zones if z.raman)
        n_power = sum(z.count() for z in self.zones if z.power)
        missing = []
        if n_raman and self.spectro is None:
            missing.append("le spectromètre (zones Raman)")
        if n_power and self.power.pm is None:
            missing.append("le wattmètre (zones Wattmètre)")
        if missing:
            QMessageBox.warning(self, "Lancer les zones", "Connecter d'abord " + " et ".join(missing) + ".")
            return

        exposure = self.applied.get("exposure", self.exposure_spin.value())
        laser = self.laser_combo.currentData()
        reference = self.power.reference_w
        lines = [f"{n} points, durée estimée {format_duration(estimate_seconds(self.zones, exposure))}.",
                 f"Raman : {n_raman} points" + (f", fichier jusqu'à {format_bytes(image_bytes(n_raman))} "
                                                f"(images complètes)" if n_raman else ""),
                 f"Wattmètre : {n_power} points", ""]
        if n_raman:
            lines += [f"Réseau : {format_grating(self.applied.get('grating', '?'))}",
                      f"Exposition : {exposure:g} ms"]
        lines += [f"Z actuel : {self.position[2]:.3f} mm",
                  f"Puissance de référence : {format_power(reference) if reference is not None else 'non mesurée'}",
                  "", f"Laser {laser:g} nm allumé, obturateur ouvert et filtre de sonde {laser:g} nm installé ?"]
        if QMessageBox.question(self, "Lancer les zones", "\n".join(lines)) != QMessageBox.StandardButton.Yes:
            return

        name = "".join(c for c in self.run_name.text().strip() if c.isalnum() or c in "-_") or "acquisition"
        path = output_dir(self.cfg, "runs") / f"{name}_{timestamp()}.h5"
        zones_json = json.dumps([asdict(z) for z in self.zones], ensure_ascii=False)
        attrs = {"laser_nm": laser, "power_reference_W": reference if reference is not None else float("nan"),
                 "points_planned": n, "zones": zones_json, "simulation": self.simulate,
                 "z_mm": self.position[2]}

        def make_writer(wavelengths, metadata):
            return RunWriter(path, wavelengths, {**{f"lightfield_{k}": v for k, v in metadata.items()}, **attrs})

        self.runner = ScanRunner(self.stage_thread, self.spectro_thread, self.power.thread, self.stage,
                                 self.spectro, self.power.pm, list(self.zones), make_writer)
        self.runner.progress.connect(self._on_run_progress)
        self.runner.point_done.connect(self._on_run_point)
        self.runner.finished.connect(self._on_run_finished)
        self._done_points = []
        self.view.set_done_points(None)
        self.run_progress.setRange(0, n)
        self.run_progress.setValue(0)
        self.run_status.setText(f"Préparation… fichier : {path}")
        self.power.set_running(True)
        self.tabs.setCurrentWidget(self.spectrum_plot)
        self.log(f"Exécution lancée : {n} points → {path}")
        self.runner.start()
        self._refresh_stage_ui()
        self._refresh_spectro_ui()

    def _toggle_pause(self):
        if self.runner.paused:
            self.runner.resume()
            self.run_pause.setText("Pause")
            self.log("Exécution reprise.")
        else:
            self.runner.pause()
            self.run_pause.setText("Reprendre")
            self.log("Pause demandée : arrêt après le point en cours.")

    def _stop_run(self):
        self.log("Arrêt demandé : fin après le point en cours.")
        self.runner.stop()

    def _on_run_progress(self, done, total, remaining_s):
        self.run_progress.setValue(done)
        self.run_status.setText(f"{done}/{total} points — reste environ {format_duration(remaining_s)}")

    def _on_run_point(self, index, x, y, spectrum):
        self._done_points.append((x, y))
        self.view.set_done_points(np.array(self._done_points))
        if spectrum is not None:
            self.last_spectrum = (self.runner.wavelengths, spectrum)
            self._plot_spectrum()

    def _on_run_finished(self, message):
        self.runner = None
        self.power.set_running(False)
        self.run_status.setText(message)
        self.log(message)
        self._refresh_stage_ui()
        self._refresh_spectro_ui()

    # ------------------------------------------------------------------ fermeture
    def closeEvent(self, event):
        if self.runner is not None:
            if QMessageBox.question(self, "Quitter", "Une exécution est en cours. L'arrêter et quitter ?") \
                    != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.runner.stop()
            self.runner._finish("Exécution arrêtée à la fermeture.")
        self.power.shutdown()
        self.stage_thread.shutdown(self.stage.close if self.stage else None)
        self.spectro_thread.shutdown(self.spectro.close if self.spectro else None, timeout_s=60.0)
        super().closeEvent(event)
