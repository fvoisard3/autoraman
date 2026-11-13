"""Fenêtre principale : platine 2D, zones d'analyse, spectromètre (dont choix du réseau)."""
import json
import time
from dataclasses import asdict

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QSignalBlocker, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSplitter,
    QTableWidget, QTableWidgetItem, QTabWidget, QToolBar, QVBoxLayout, QWidget)

from ..analysis.integration import Window, evaluate, load_windows
from ..analysis.runfile import RunFile
from ..config import PROJECT_ROOT, output_dir, timestamp
from ..core.presets import center_nm, center_shift, load_presets, save_presets
from ..core.storage import RunWriter
from ..core.zones import MIN_STEP_MM, Zone, estimate_seconds, load_zones, save_zones
from ..drivers.lightfield import LightFieldSpectrometer, format_grating, raman_shift
from ..drivers.pm100d import format_power
from ..drivers.sim import SimSpectrometer, SimStage
from ..drivers.ultimaker import UltimakerStage
from .knife_window import KnifeEdgeWindow
from .power_panel import PowerPanel
from .scan import ScanRunner, format_bytes, image_bytes
from .stage_view import INK, MIN_DRAWN_MM, PLATE_EDGE, ZONE_COLOR, StageView
from .workers import DeviceThread

ZONE_COLUMNS = [("Nom", "name"), ("Type", None), ("X0", "x0"), ("Y0", "y0"), ("X1", "x1"), ("Y1", "y1"),
                ("Pas X", "step_x"), ("Pas Y", "step_y"), ("Angle (°)", "angle"), ("Raman", "raman"),
                ("Wattmètre", "power"), ("Points", None)]
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
        self.runner = None        # tâche automatique en cours : exécution des zones ou knife-edge
        self.knife_window = None
        self._filling_editor = False
        self.map_data = None      # carte affichée sur la platine
        self._map_refresh_pending = False
        self._map_wavelengths = np.empty(0)

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
        knife = QAction("Knife-edge…", self, toolTip="Mesure de la taille du spot et recherche du foyer")
        knife.triggered.connect(self._open_knife)
        bar.addAction(knife)

        bar.addSeparator()
        bar.addWidget(QLabel(" Carte : "))
        self.map_combo = QComboBox()
        self.map_combo.setToolTip("Valeur affichée en couleur sur la platine pour les points mesurés. Les fenêtres "
                                  "Raman sont celles de l'outil d'analyse (data/analyse/fenetres.json).")
        self.map_combo.activated.connect(lambda _: self._refresh_map(auto_levels=True))
        bar.addWidget(self.map_combo)
        load_map = QAction("Afficher un fichier…", self, toolTip="Afficher sur la platine la carte d'un fichier "
                                                                 "d'exécution déjà enregistré")
        load_map.triggered.connect(self._load_map_file)
        bar.addAction(load_map)
        clear_map = QAction("Effacer la carte", self)
        clear_map.triggered.connect(self._clear_map)
        bar.addAction(clear_map)
        self._fill_map_combo()

    # ------------------------------------------------------------------ carte sur la platine
    def _map_windows(self):
        try:
            return load_windows(PROJECT_ROOT / self.cfg.get("data_dir", "data") / "analyse" / "fenetres.json")
        except Exception:
            return [Window("Si", 500.0, 540.0)]

    def _fill_map_combo(self):
        current = self.map_combo.currentData()
        self._windows = self._map_windows()
        with QSignalBlocker(self.map_combo):
            self.map_combo.clear()
            self.map_combo.addItem("Puissance (brute)", "power")
            self.map_combo.addItem("Signal total (counts)", "total")
            for i, w in enumerate(self._windows):
                self.map_combo.addItem(f"Raman : {w.label()}", f"win{i}")
            i = self.map_combo.findData(current)
            self.map_combo.setCurrentIndex(i if i >= 0 else (2 if self._windows else 0))

    def _point_values(self, spectrum, shift):
        """Valeurs d'un point pour chaque choix de carte (à partir du spectre sommé sur les lignes)."""
        out = {"total": float((spectrum - np.percentile(spectrum, 5)).sum())}
        for i, w in enumerate(self._windows):
            axis = shift if w.unit == "cm-1" else self._map_wavelengths
            out[f"win{i}"] = float(evaluate(axis, spectrum, w)[0][0])
        return out

    def _new_map(self, zones, source, wavelengths, laser_nm):
        self._fill_map_combo()
        self._map_wavelengths = np.asarray(wavelengths, float)
        shift = raman_shift(self._map_wavelengths, laser_nm) if self._map_wavelengths.size else None
        self.map_data = {"zones": list(zones), "targets": [], "zone_index": [], "values": {}, "source": source,
                         "shift": shift}

    def _add_map_point(self, target, zone_index, power, spectrum):
        d = self.map_data
        d["targets"].append(target)
        d["zone_index"].append(zone_index)
        values = {"power": power}
        if spectrum is not None and d["shift"] is not None:
            values.update(self._point_values(spectrum, d["shift"]))
        n = len(d["targets"])
        for key in set(values) | set(d["values"]):
            col = d["values"].setdefault(key, [np.nan] * (n - 1))
            col.append(values.get(key, np.nan))

    def _refresh_map(self, auto_levels=False):
        d = self.map_data
        key = self.map_combo.currentData()
        if not d or not d["targets"] or key not in d["values"]:
            self.view.set_map(None, None, None, None)
            return
        values = np.array(d["values"][key], float)
        label = self.map_combo.currentText()
        if key == "power":
            label = "Puissance (W)"
        self.view.set_map(d["zones"], np.array(d["targets"]), np.array(d["zone_index"]), values,
                          label=label, keep_levels=not auto_levels)

    def _schedule_map_refresh(self):
        if not self._map_refresh_pending:
            self._map_refresh_pending = True

            def run():
                self._map_refresh_pending = False
                self._refresh_map(auto_levels=True)
            QTimer.singleShot(300, run)

    def _clear_map(self):
        self.map_data = None
        self.view.set_map(None, None, None, None)

    def _load_map_file(self):
        start = str(PROJECT_ROOT / self.cfg.get("data_dir", "data") / "runs")
        path, _ = QFileDialog.getOpenFileName(self, "Afficher la carte d'un fichier", start, "HDF5 (*.h5)")
        if path:
            self.show_map_file(path)

    def show_map_file(self, path):
        try:
            run = RunFile(path)
            spectra = run.spectra() if run.has_images else None
        except Exception as e:
            QMessageBox.warning(self, "Lecture impossible", f"{path}\n\n{e}")
            return
        self._new_map(run.zones, path, run.wavelength_nm, run.laser_nm)
        for i in range(run.n):
            j = run.image_index[i]
            spectrum = spectra[j] if spectra is not None and j >= 0 else None
            self._add_map_point(run.target[i], int(run.zone_index[i]), float(run.power_W[i]), spectrum)
        self._refresh_map(auto_levels=True)
        if run.n:  # recentrer la vue sur la carte
            t = run.target
            pad = max(1.0, 0.15 * float(np.ptp(t[:, 0]) + np.ptp(t[:, 1])))
            self.view.getViewBox().setRange(xRange=(t[:, 0].min() - pad, t[:, 0].max() + pad),
                                            yRange=(t[:, 1].min() - pad, t[:, 1].max() + pad))
        self.log(f"Carte affichée : {run.name} ({run.n} points).")

    def _open_knife(self):
        if self.knife_window is None:
            self.knife_window = KnifeEdgeWindow(self)
        self.knife_window.refresh_zones()
        self.knife_window.show()
        self.knife_window.raise_()
        self.knife_window.activateWindow()

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

        # Édition directe de la zone sélectionnée : pas et rotation
        self.edit_label = QLabel("Zone sélectionnée : —")
        self.edit_step_x = self._spin(MIN_STEP_MM, 200.0, 1.0, 4, " mm")
        self.edit_step_y = self._spin(MIN_STEP_MM, 200.0, 1.0, 4, " mm")
        self.edit_angle = self._spin(-180.0, 180.0, 0.0, 2, " °")
        self.edit_step_x.setToolTip("Pas entre deux points le long du rectangle (ou de la ligne)")
        self.edit_step_y.setToolTip("Pas entre deux rangées du rectangle")
        self.edit_angle.setToolTip("Rotation du rectangle autour de son centre (sens trigonométrique). "
                                   "La poignée ronde sur le bord droit du rectangle permet aussi de le tourner.")
        for spin, key in ((self.edit_step_x, "step_x"), (self.edit_step_y, "step_y"), (self.edit_angle, "angle")):
            spin.valueChanged.connect(lambda value, k=key: self._edit_selected_zone(k, value))
        edit_row = QHBoxLayout()
        edit_row.addWidget(self.edit_label)
        for text, spin in (("Pas X", self.edit_step_x), ("Pas Y", self.edit_step_y), ("Angle", self.edit_angle)):
            edit_row.addWidget(QLabel(text))
            edit_row.addWidget(spin)
        edit_row.addStretch(1)
        self._show_zone_editor(None)

        box = QWidget()
        layout = QVBoxLayout(box)
        layout.addWidget(QLabel("Coordonnées et pas en mm. Double-clic sur une cellule pour la modifier, "
                                "ou sélectionner une zone et régler pas et angle ci-dessous."))
        layout.addWidget(self.zone_table)
        layout.addLayout(edit_row)
        layout.addLayout(row)
        return box

    def _selected_zone(self):
        rows = self.zone_table.selectionModel().selectedRows() if self.zone_table.selectionModel() else []
        return self.zones[rows[0].row()] if rows and rows[0].row() < len(self.zones) else None

    def _show_zone_editor(self, zone):
        """Remplit les champs pas / angle avec la zone sélectionnée (sans déclencher de modification)."""
        self._filling_editor = True
        self.edit_label.setText(f"Zone sélectionnée : {zone.name}" if zone else "Zone sélectionnée : —")
        for spin, key in ((self.edit_step_x, "step_x"), (self.edit_step_y, "step_y"), (self.edit_angle, "angle")):
            enabled = zone is not None and self._cell_editable(zone, key) and self.runner is None
            spin.setEnabled(enabled)
            if zone is not None and enabled:
                spin.setValue(getattr(zone, key))
        self._filling_editor = False

    def _edit_selected_zone(self, key, value):
        zone = self._selected_zone()
        if self._filling_editor or zone is None or abs(getattr(zone, key) - value) < 1e-9:
            return
        setattr(zone, key, value)
        zone.clamp(self.limits)
        self.view.update_zone(zone)
        self._rebuild_zone_table()
        self._refresh_zone_summary()

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
        self._center_nm = 585.0  # centre affiché, toujours en nm (le champ peut montrer un décalage Raman)
        self.center_unit = QComboBox()
        self.center_unit.addItem("nm", "nm")
        self.center_unit.addItem("cm⁻¹ (Raman)", "cm-1")
        self.center_unit.setToolTip("Centrage en longueur d'onde absolue ou en décalage Raman par rapport à la raie laser")
        self.center_unit.currentIndexChanged.connect(lambda _: self._show_center(self._center_nm))
        self.exposure_spin = self._spin(1.0, 3.6e6, 1000.0, 1, " ms")
        self.exposure_spin.editingFinished.connect(self._apply_exposure)
        self.exposure_spin.valueChanged.connect(lambda _: self._refresh_zone_summary())
        self.laser_combo = QComboBox()
        for nm in self.cfg["laser"]["lines_nm"]:
            self.laser_combo.addItem(f"{nm:g} nm", float(nm))
        self.laser_combo.setCurrentIndex(self.laser_combo.findData(float(self.cfg["laser"]["default_nm"])))
        self.laser_combo.currentIndexChanged.connect(lambda _: self._plot_spectrum())
        self.laser_combo.currentIndexChanged.connect(lambda _: self._show_center(self._center_nm))
        self.acquire = QPushButton("Acquérir un spectre")
        self.acquire.clicked.connect(self._acquire)

        self.preset_combo = QComboBox()
        self.preset_combo.setMinimumContentsLength(12)
        self.preset_load = QPushButton("Charger")
        self.preset_load.setToolTip("Applique le réseau, le centre et l'exposition du préréglage")
        self.preset_load.clicked.connect(self._load_preset)
        self.preset_save = QPushButton("Sauver…")
        self.preset_save.setToolTip("Enregistre le réseau, le centre et l'exposition affichés")
        self.preset_save.clicked.connect(self._save_preset)
        self.preset_delete = QPushButton("Suppr.")
        self.preset_delete.clicked.connect(self._delete_preset)
        presets = QHBoxLayout()
        presets.addWidget(self.preset_combo, 1)
        for b in (self.preset_load, self.preset_save, self.preset_delete):
            presets.addWidget(b)
        self._fill_preset_combo()

        center = QHBoxLayout()
        center.addWidget(self.center_spin, 1)
        center.addWidget(self.center_unit)
        form = QFormLayout()
        form.addRow("Préréglage", presets)
        form.addRow("Réseau", self.grating_combo)
        form.addRow("Centre", center)
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
        if zone is self._selected_zone():
            self._show_zone_editor(zone)  # angle modifié à la souris

    def _cell_editable(self, zone, key):
        if key is None:
            return False
        if zone.kind == "point":
            return key in ("name", "x0", "y0")
        if zone.kind == "line":
            return key not in ("step_y", "angle")
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
        zone = self._selected_zone()
        self.view.highlight(zone)
        self._show_zone_editor(zone)

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
            status = self.runner.status_text
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
        self._refresh_preset_buttons()
        self.spectro_status.setText("Déconnecté" if not connected else "Occupé…" if busy else "Prêt")
        self._refresh_run_ui()
        if self.knife_window is not None:
            self.knife_window._refresh()  # réglages du spectromètre affichés dans la fenêtre knife-edge

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
        self._show_center(info["center"])
        self.exposure_spin.setValue(info["exposure"])

    # centre : affiché en nm ou en décalage Raman, toujours envoyé en nm
    def _center_unit(self):
        return self.center_unit.currentData()

    def _show_center(self, nm):
        self._center_nm = float(nm)
        with QSignalBlocker(self.center_spin):
            if self._center_unit() == "nm":
                self.center_spin.setRange(100.0, 2000.0)
                self.center_spin.setDecimals(3)
                self.center_spin.setSuffix(" nm")
                self.center_spin.setValue(self._center_nm)
            else:
                self.center_spin.setRange(-20000.0, 20000.0)
                self.center_spin.setDecimals(2)
                self.center_spin.setSuffix(" cm⁻¹")
                self.center_spin.setValue(center_shift(self._center_nm, self.laser_combo.currentData()))

    def _center_target_nm(self):
        return center_nm(self.center_spin.value(), self._center_unit(), self.laser_combo.currentData())

    # préréglages
    def _presets_path(self):
        return PROJECT_ROOT / self.cfg.get("data_dir", "data") / "presets" / "spectrometre.json"

    def _fill_preset_combo(self, select=None):
        current = select or self.preset_combo.currentText()
        presets = load_presets(self._presets_path())
        with QSignalBlocker(self.preset_combo):
            self.preset_combo.clear()
            for name in sorted(presets, key=str.lower):
                p = presets[name]
                self.preset_combo.addItem(name)
                self.preset_combo.setItemData(self.preset_combo.count() - 1, self._describe_preset(p),
                                              Qt.ItemDataRole.ToolTipRole)
            i = self.preset_combo.findText(current)
            if i >= 0:
                self.preset_combo.setCurrentIndex(i)
        has = self.preset_combo.count() > 0
        self.preset_delete.setEnabled(has)
        self._refresh_preset_buttons()

    def _refresh_preset_buttons(self):
        busy = self.spectro_thread.busy or self.runner is not None if hasattr(self, "spectro_thread") else False
        self.preset_load.setEnabled(self.preset_combo.count() > 0 and self.spectro is not None and not busy)

    @staticmethod
    def _describe_preset(p):
        unit = " cm⁻¹" if p.get("unit") == "cm-1" else " nm"
        parts = [format_grating(p["grating"]) if p.get("grating") else "réseau inchangé",
                 f"centre {p['center']:g}{unit}", f"exposition {p['exposure_ms']:g} ms"]
        text = ", ".join(parts)
        if p.get("unit") == "cm-1":
            text += f" (enregistré avec la raie {p.get('laser_nm', 0):g} nm)"
        return text

    def _save_preset(self):
        name, ok = QInputDialog.getText(self, "Sauver un préréglage", "Nom du préréglage :",
                                        text=self.preset_combo.currentText())
        name = name.strip()
        if not ok or not name:
            return
        presets = load_presets(self._presets_path())
        if name in presets and QMessageBox.question(
                self, "Sauver un préréglage", f"Remplacer le préréglage « {name} » ?") != QMessageBox.StandardButton.Yes:
            return
        presets[name] = {"grating": self.grating_combo.currentData() or self.applied.get("grating"),
                         "center": round(self.center_spin.value(), self.center_spin.decimals()),
                         "unit": self._center_unit(),
                         "laser_nm": self.laser_combo.currentData(),
                         "exposure_ms": self.exposure_spin.value()}
        save_presets(self._presets_path(), presets)
        self._fill_preset_combo(select=name)
        self.log(f"Préréglage « {name} » enregistré : {self._describe_preset(presets[name])}.")

    def _delete_preset(self):
        name = self.preset_combo.currentText()
        presets = load_presets(self._presets_path())
        if name not in presets or QMessageBox.question(
                self, "Supprimer un préréglage", f"Supprimer le préréglage « {name} » ?") != QMessageBox.StandardButton.Yes:
            return
        del presets[name]
        save_presets(self._presets_path(), presets)
        self._fill_preset_combo()
        self.log(f"Préréglage « {name} » supprimé.")

    def _load_preset(self):
        name = self.preset_combo.currentText()
        p = load_presets(self._presets_path()).get(name)
        if p is None or self.spectro is None:
            return
        grating = p.get("grating")
        if grating and self.grating_combo.findData(grating) < 0:
            QMessageBox.warning(self, "Préréglage", f"Le réseau du préréglage « {name} » n'existe pas sur ce "
                                                   f"spectromètre :\n{grating}\nLe réseau ne sera pas changé.")
            grating = None
        laser = self.laser_combo.currentData()
        unit = p.get("unit", "nm")
        target = center_nm(p["center"], unit, laser)  # un décalage Raman suit la raie laser actuelle
        with QSignalBlocker(self.center_unit):
            self.center_unit.setCurrentIndex(self.center_unit.findData(unit))
        self._show_center(target)
        self.exposure_spin.setValue(p["exposure_ms"])
        if grating:
            with QSignalBlocker(self.grating_combo):
                self.grating_combo.setCurrentIndex(self.grating_combo.findData(grating))
        spectro, exposure = self.spectro, float(p["exposure_ms"])

        def job():
            if grating and spectro.grating != grating:
                spectro.grating = grating
            spectro.center_wavelength_nm = target
            spectro.exposure_ms = exposure
            return self._read_spectro(spectro)

        extra = f" (raie {laser:g} nm → {target:.3f} nm)" if unit == "cm-1" else ""
        self.log(f"Chargement du préréglage « {name} » : {self._describe_preset(p)}{extra}.")
        self.spectro_thread.submit("préréglage", job)

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
        target = self._center_target_nm()
        self._center_nm = target
        if self.spectro is not None and abs(target - self.applied.get("center", 0.0)) > 1e-3:
            self._spectro_setter("center", "center_wavelength_nm", target)

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
        zones_running = isinstance(self.runner, ScanRunner)  # pause/arrêt du knife-edge : dans sa fenêtre
        # Les appareils requis par les zones cochées sont vérifiés au lancement, avec un message explicite
        ready = (self.stage is not None and self.homed and bool(self.zones)
                 and not self.stage_thread.busy and not self.spectro_thread.busy)
        self.run_start.setEnabled(ready and not running)
        self.run_pause.setEnabled(zones_running)
        self.run_stop.setEnabled(zones_running)
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
        self._new_map(self.zones, str(path), [], laser)  # carte en direct ; axe spectral au premier spectre
        self.view.set_map(None, None, None, None)
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

    def _on_run_point(self, index, x, y, spectrum, power):
        self._done_points.append((x, y))
        self.view.set_done_points(np.array(self._done_points))
        if spectrum is not None:
            self.last_spectrum = (self.runner.wavelengths, spectrum)
            self._plot_spectrum()
        d = self.map_data
        if d is not None:
            if d["shift"] is None and spectrum is not None and self.runner.wavelengths is not None \
                    and len(self.runner.wavelengths):
                self._map_wavelengths = np.asarray(self.runner.wavelengths, float)
                d["shift"] = raman_shift(self._map_wavelengths, self.laser_combo.currentData())
            self._add_map_point((x, y), int(self.runner.zone_index[index]), power, spectrum)
            self._schedule_map_refresh()

    def _on_run_finished(self, message):
        self.runner = None
        if self.map_data and self.map_data["targets"]:
            self.view.set_done_points(None)  # la carte remplace les points mesurés
            self._refresh_map(auto_levels=True)
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
            self.runner.abort("Exécution arrêtée à la fermeture.")
        self.power.shutdown()
        self.stage_thread.shutdown(self.stage.close if self.stage else None)
        self.spectro_thread.shutdown(self.spectro.close if self.spectro else None, timeout_s=60.0)
        super().closeEvent(event)
