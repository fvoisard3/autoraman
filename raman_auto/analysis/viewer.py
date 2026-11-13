"""Fenêtre d'analyse : cartes et graphiques à partir d'un fichier d'exécution, sans aucun appareil.

Signal par point :
  - « Puissance (brute) » : colonne power_W du fichier, sans traitement ;
  - une fenêtre d'intégration Raman : aire, hauteur ou position du maximum dans une plage (cm⁻¹ ou nm),
    avec fond linéaire optionnel, calculée sur la somme des lignes choisies de l'image de la caméra.
Vues : carte (rectangles en image, lignes et points en pastilles), graphique (signal en fonction de la
distance, de X, de Y, du n° de point ou du temps), spectre et image caméra du point sélectionné.
"""
import csv
import os
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QProgressDialog, QPushButton,
    QScrollArea, QSpinBox, QSplitter, QStyledItemDelegate, QTableWidget, QTableWidgetItem, QTabWidget, QToolBar,
    QVBoxLayout, QWidget)

from ..config import PROJECT_ROOT
from ..gui.map_layer import place_image
from .integration import MEASURES, Window, evaluate, grid_rect, load_windows, save_windows
from .runfile import RunFile

SURFACE = "#fcfcfb"
INK = "#374151"
MUTED = "#9CA3AF"
# Ordre catégoriel fixe (validé daltonisme) : la couleur suit la zone, pas son rang.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
BLUES = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
COLORMAPS = {
    "Bleus": lambda: pg.ColorMap(np.linspace(0, 1, len(BLUES)), BLUES),
    "Viridis": lambda: pg.colormap.get("viridis"),
    "Gris": lambda: pg.ColorMap([0, 1], ["#f3f4f6", "#111827"]),
}
X_AXES = {"distance": "Distance le long de la zone (mm)", "x": "X (mm)", "y": "Y (mm)",
          "index": "N° de point", "time": "Temps depuis le début (s)"}
WIN_COLUMNS = ["Nom", "Début", "Fin", "Unité", "Mesure", "Fond"]
POWER = "power"


def _style(plot, bottom, left):
    plot.setBackground(SURFACE)
    plot.showGrid(x=True, y=True, alpha=0.15)
    for side, label in (("bottom", bottom), ("left", left)):
        axis = plot.getAxis(side)
        axis.setPen(pg.mkPen(MUTED))
        axis.setTextPen(pg.mkPen(INK))
        axis.setLabel(label, color=INK)


class ChoiceDelegate(QStyledItemDelegate):
    """Édition d'une cellule par liste déroulante ; la clé choisie est rangée dans UserRole."""

    def __init__(self, choices, parent=None):
        super().__init__(parent)
        self.choices = choices  # {clé: texte}

    def createEditor(self, parent, option, index):
        combo = QComboBox(parent)
        for key, text in self.choices.items():
            combo.addItem(text, key)
        combo.activated.connect(lambda _: self.commitData.emit(combo))
        return combo

    def setEditorData(self, editor, index):
        i = editor.findData(index.data(Qt.ItemDataRole.UserRole))
        editor.setCurrentIndex(max(i, 0))

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentData(), Qt.ItemDataRole.UserRole)
        model.setData(index, editor.currentText(), Qt.ItemDataRole.DisplayRole)


UNITS = {"cm-1": "cm⁻¹", "nm": "nm"}


def _series_color(k):
    return SERIES[k % len(SERIES)]


class AnalysisWindow(QMainWindow):
    def __init__(self, cfg, path=None):
        super().__init__()
        self.cfg = cfg
        self.run = None
        self.values = None
        self.baselines = None
        self.selected = None
        self.analysis_dir = PROJECT_ROOT / cfg.get("data_dir", "data") / "analyse"
        self.runs_dir = PROJECT_ROOT / cfg.get("data_dir", "data") / "runs"
        self.windows = self._load_default_windows()
        self._updating = False
        self.setWindowTitle("Analyse des acquisitions Raman")
        self.resize(1500, 950)
        self._build()
        if path:
            self.open_file(path)
        else:
            self._refresh_all()

    # ================================================================== construction
    def _build(self):
        bar = QToolBar("Fichier")
        bar.setMovable(False)
        self.addToolBar(bar)
        for text, slot in (("Ouvrir…", self._choose_file), ("Recharger", self._reload),
                           ("Exporter les valeurs (CSV)…", self._export_csv),
                           ("Exporter la vue (PNG)…", self._export_png)):
            action = QAction(text, self)
            action.triggered.connect(slot)
            bar.addAction(action)

        controls = QWidget()
        col = QVBoxLayout(controls)

        box = QGroupBox("Fichier")
        lay = QVBoxLayout(box)
        self.file_label = QLabel("Aucun fichier")
        self.file_label.setStyleSheet("font-weight: 600;")
        self.file_label.setWordWrap(True)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.file_label)
        lay.addWidget(self.summary)
        col.addWidget(box)

        box = QGroupBox("Zones affichées")
        lay = QVBoxLayout(box)
        self.zone_list = QListWidget()
        self.zone_list.setMaximumHeight(130)
        self.zone_list.itemChanged.connect(lambda _: self._refresh_views())
        lay.addWidget(self.zone_list)
        col.addWidget(box)

        box = QGroupBox("Signal")
        form = QFormLayout(box)
        self.signal_combo = QComboBox()
        self.signal_combo.currentIndexChanged.connect(lambda _: self._recompute())
        form.addRow("Valeur", self.signal_combo)
        self.normalize = QCheckBox("Diviser par la puissance mesurée au point")
        self.normalize.toggled.connect(lambda _: self._recompute())
        form.addRow(self.normalize)
        rows = QHBoxLayout()
        self.row_start, self.row_end = QSpinBox(), QSpinBox()
        for spin in (self.row_start, self.row_end):
            spin.setRange(0, 99)
            spin.editingFinished.connect(self._rows_changed)
        self.row_end.setValue(99)
        rows.addWidget(self.row_start)
        rows.addWidget(QLabel("à"))
        rows.addWidget(self.row_end)
        form.addRow("Lignes caméra", rows)
        self.unit_combo = QComboBox()
        self.unit_combo.addItem("Décalage Raman (cm⁻¹)", "cm-1")
        self.unit_combo.addItem("Longueur d'onde (nm)", "nm")
        self.unit_combo.currentIndexChanged.connect(lambda _: self._refresh_spectrum())
        form.addRow("Axe du spectre", self.unit_combo)
        col.addWidget(box)

        box = QGroupBox("Fenêtres d'intégration Raman")
        lay = QVBoxLayout(box)
        self.win_table = QTableWidget(0, len(WIN_COLUMNS))
        self.win_table.setHorizontalHeaderLabels(WIN_COLUMNS)
        self.win_table.verticalHeader().setVisible(False)
        self.win_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.win_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        header = self.win_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c, width in ((1, 56), (2, 56), (3, 50), (4, 108), (5, 42)):
            self.win_table.setColumnWidth(c, width)
        self.win_table.setItemDelegateForColumn(3, ChoiceDelegate(UNITS, self.win_table))
        self.win_table.setItemDelegateForColumn(4, ChoiceDelegate(MEASURES, self.win_table))
        self.win_table.setMinimumHeight(150)
        self.win_table.itemChanged.connect(self._on_window_item_changed)
        self.win_table.itemSelectionChanged.connect(self._on_window_selected)
        lay.addWidget(self.win_table)
        buttons = QHBoxLayout()
        for text, slot in (("Ajouter", self._add_window), ("Supprimer", self._remove_window),
                           ("Charger…", self._load_windows), ("Enregistrer…", self._save_windows)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            buttons.addWidget(b)
        lay.addLayout(buttons)
        hint = QLabel("Les bornes se règlent aussi en glissant la plage dans l'onglet « Spectre ».")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {INK};")
        lay.addWidget(hint)
        col.addWidget(box)

        box = QGroupBox("Couleurs de la carte")
        form = QFormLayout(box)
        self.cmap_combo = QComboBox()
        self.cmap_combo.addItems(list(COLORMAPS))
        self.cmap_combo.currentTextChanged.connect(self._set_colormap)
        form.addRow("Palette", self.cmap_combo)
        auto = QPushButton("Échelle automatique (1–99 %)")
        auto.clicked.connect(self._auto_levels)
        form.addRow(auto)
        col.addWidget(box)
        col.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(controls)
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(480)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        # ---- vues
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_map(), "Carte")
        self.tabs.addTab(self._build_graph(), "Graphique")
        self.tabs.addTab(self._build_spectrum(), "Spectre")
        self.tabs.addTab(self._build_camera(), "Image caméra")
        self.point_info = QLabel("Cliquer sur un point de la carte ou du graphique pour voir son spectre.")
        self.point_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.tabs, 1)
        rl.addWidget(self.point_info)

        split = QSplitter()
        split.addWidget(scroll)
        split.addWidget(right)
        split.setSizes([500, 1000])
        self.setCentralWidget(split)

    def _build_map(self):
        gui = self.cfg.get("gui", {})
        self.map_plot = pg.PlotWidget()
        _style(self.map_plot, "X (mm)", "Y (mm)")
        self.map_plot.setAspectLocked(True)
        vb = self.map_plot.getViewBox()
        vb.invertX(bool(gui.get("invert_x", False)))
        vb.invertY(bool(gui.get("invert_y", False)))
        self.colorbar = pg.ColorBarItem(values=(0, 1), colorMap=COLORMAPS["Bleus"](), interactive=True,
                                        width=18)
        item = self.map_plot.getPlotItem()
        item.layout.addItem(self.colorbar, 2, 5)
        item.layout.setColumnFixedWidth(4, 5)
        self.colorbar.sigLevelsChanged.connect(lambda _: self._color_scatter())
        self.map_images = []
        self.map_scatter = pg.ScatterPlotItem(pxMode=True, size=11, hoverable=True,
                                              pen=pg.mkPen(SURFACE, width=1))
        self.map_scatter.sigClicked.connect(self._on_scatter_clicked)
        self.map_plot.addItem(self.map_scatter)
        self.map_marker = pg.ScatterPlotItem(size=18, symbol="s", pen=pg.mkPen("#111827", width=2),
                                             brush=None)
        self.map_marker.setZValue(10)
        self.map_plot.addItem(self.map_marker)
        self.map_plot.scene().sigMouseClicked.connect(self._on_map_clicked)
        return self.map_plot

    def _build_graph(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        top.addWidget(QLabel("Axe horizontal :"))
        self.xaxis_combo = QComboBox()
        for key, text in X_AXES.items():
            self.xaxis_combo.addItem(text, key)
        self.xaxis_combo.currentIndexChanged.connect(lambda _: self._refresh_graph())
        top.addWidget(self.xaxis_combo)
        self.graph_lines = QCheckBox("Relier les points")
        self.graph_lines.setChecked(True)
        self.graph_lines.toggled.connect(lambda _: self._refresh_graph())
        top.addWidget(self.graph_lines)
        top.addStretch(1)
        lay.addLayout(top)
        self.graph = pg.PlotWidget()
        _style(self.graph, X_AXES["distance"], "")
        self.graph_legend = self.graph.addLegend(offset=(-10, 10), labelTextColor=INK)
        self.graph_marker = pg.ScatterPlotItem(size=16, pen=pg.mkPen("#111827", width=2), brush=None)
        self.graph_marker.setZValue(10)
        self.graph.addItem(self.graph_marker)
        lay.addWidget(self.graph)
        return w

    def _build_spectrum(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        self.show_mean = QCheckBox("Afficher le spectre moyen des zones affichées")
        self.show_mean.toggled.connect(lambda _: self._refresh_spectrum())
        top.addWidget(self.show_mean)
        top.addStretch(1)
        lay.addLayout(top)
        self.spec_plot = pg.PlotWidget()
        _style(self.spec_plot, "Décalage Raman (cm⁻¹)", "Counts (somme des lignes)")
        self.spec_legend = self.spec_plot.addLegend(offset=(-10, 10), labelTextColor=INK)
        self.spec_curve = self.spec_plot.plot(pen=pg.mkPen(SERIES[0], width=2), name="Point sélectionné")
        self.mean_curve = self.spec_plot.plot(pen=pg.mkPen(SERIES[1], width=2), name="Moyenne")
        self.base_curve = self.spec_plot.plot(pen=pg.mkPen(INK, width=1.5, style=Qt.PenStyle.DashLine))
        self.region = pg.LinearRegionItem(brush=QColor(42, 120, 214, 40), pen=pg.mkPen(SERIES[0], width=1.5))
        self.region.setZValue(-5)
        self.region.sigRegionChangeFinished.connect(self._on_region_moved)
        self.spec_plot.addItem(self.region)
        self.other_regions = []
        lay.addWidget(self.spec_plot)
        return w

    def _build_camera(self):
        self.cam_plot = pg.PlotWidget()
        _style(self.cam_plot, "Colonne (pixel)", "Ligne (pixel)")
        self.cam_image = pg.ImageItem(axisOrder="row-major")
        self.cam_image.setColorMap(pg.colormap.get("viridis"))
        self.cam_plot.addItem(self.cam_image)
        self.row_lines = [pg.InfiniteLine(angle=0, movable=True, pen=pg.mkPen("#e34948", width=1.5))
                          for _ in range(2)]
        for line in self.row_lines:
            line.sigPositionChangeFinished.connect(self._on_row_line_moved)
            self.cam_plot.addItem(line)
        return self.cam_plot

    # ================================================================== fichiers
    def _choose_file(self):
        start = str(self.run.path.parent if self.run else self.runs_dir)
        path, _ = QFileDialog.getOpenFileName(self, "Ouvrir un fichier d'exécution", start, "HDF5 (*.h5 *.hdf5)")
        if path:
            self.open_file(path)

    def open_file(self, path):
        try:
            run = RunFile(path)
        except Exception as e:
            QMessageBox.warning(self, "Ouverture impossible", f"{path}\n\n{e}")
            return
        self.run = run
        self.selected = None
        self.point_info.setText("Cliquer sur un point de la carte ou du graphique pour voir son spectre.")
        self._refresh_markers()
        self.setWindowTitle(f"Analyse — {run.name}")
        if run.image_shape[0]:
            for spin in (self.row_start, self.row_end):
                spin.setMaximum(run.image_shape[0] - 1)
            self.row_end.setValue(run.image_shape[0] - 1)
        self._fill_zones()
        self._refresh_all()
        # Signal par défaut : la première fenêtre Raman s'il y a des images, sinon la puissance.
        self.signal_combo.setCurrentIndex(1 if run.has_images and self.windows else 0)
        self._recompute()

    def _reload(self):
        if self.run is None:
            return
        try:
            self.run.reload()
        except Exception as e:
            QMessageBox.warning(self, "Rechargement impossible", str(e))
            return
        checked = self._checked_zones()
        self._fill_zones(checked)
        self._refresh_all()
        self._recompute()

    def _fill_zones(self, checked=None):
        self._updating = True
        self.zone_list.clear()
        kinds = {"rect": "rectangle", "line": "ligne", "point": "point"}
        for k in np.unique(self.run.zone_index):
            n = int((self.run.zone_index == k).sum())
            kind = kinds.get(self.run.zones[k].kind, "?") if k < len(self.run.zones) else "?"
            item = QListWidgetItem(f"{self.run.zone_name(k)} — {kind}, {n} points")
            item.setData(Qt.ItemDataRole.UserRole, int(k))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if checked is None or k in checked
                               else Qt.CheckState.Unchecked)
            item.setForeground(QColor(_series_color(int(k))))
            self.zone_list.addItem(item)
        self._updating = False

    def _checked_zones(self):
        return {self.zone_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.zone_list.count())
                if self.zone_list.item(i).checkState() == Qt.CheckState.Checked}

    # ================================================================== fenêtres
    def _autosave_path(self):
        return self.analysis_dir / "fenetres.json"

    def _load_default_windows(self):
        try:
            return load_windows(self._autosave_path())
        except Exception:
            return [Window("Si", 500.0, 540.0)]

    def _autosave_windows(self):
        try:
            self.analysis_dir.mkdir(parents=True, exist_ok=True)
            save_windows(self.windows, self._autosave_path())
        except OSError:
            pass

    def _fill_windows_table(self):
        self._updating = True
        current = self._current_window_row()
        self.win_table.setRowCount(len(self.windows))
        for r, w in enumerate(self.windows):
            self.win_table.setItem(r, 0, QTableWidgetItem(w.name))
            self.win_table.setItem(r, 1, QTableWidgetItem(f"{w.start:g}"))
            self.win_table.setItem(r, 2, QTableWidgetItem(f"{w.end:g}"))
            for c, (key, choices) in ((3, (w.unit, UNITS)), (4, (w.measure, MEASURES))):
                item = QTableWidgetItem(choices[key])
                item.setData(Qt.ItemDataRole.UserRole, key)
                item.setToolTip("Double-cliquer pour changer")
                self.win_table.setItem(r, c, item)
            base = QTableWidgetItem()
            base.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            base.setCheckState(Qt.CheckState.Checked if w.baseline else Qt.CheckState.Unchecked)
            base.setToolTip("Retirer un fond linéaire tiré entre les deux bords de la fenêtre")
            self.win_table.setItem(r, 5, base)
        if self.windows:
            self.win_table.selectRow(min(max(current, 0), len(self.windows) - 1))
        self._updating = False

    def _fill_signal_combo(self):
        self._updating = True
        index = self.signal_combo.currentIndex()
        self.signal_combo.clear()
        self.signal_combo.addItem("Puissance (brute)", POWER)
        for i, w in enumerate(self.windows):
            self.signal_combo.addItem(w.label(), i)
        self.signal_combo.setCurrentIndex(min(max(index, 0), self.signal_combo.count() - 1))
        self._updating = False

    def _current_window_row(self):
        rows = self.win_table.selectionModel().selectedRows() if self.win_table.selectionModel() else []
        return rows[0].row() if rows else -1

    def _windows_changed(self):
        self._autosave_windows()
        self._fill_signal_combo()
        self._recompute()

    def _on_window_item_changed(self, item):
        if self._updating:
            return
        w = self.windows[item.row()]
        c = item.column()
        if c == 0:
            w.name = item.text().strip() or w.name
        elif c in (1, 2):
            try:
                value = float(item.text().replace(",", "."))
            except ValueError:
                self._fill_windows_table()
                return
            setattr(w, "start" if c == 1 else "end", value)
        elif c in (3, 4):
            key = item.data(Qt.ItemDataRole.UserRole)
            if key is None or key == getattr(w, "unit" if c == 3 else "measure"):
                return  # le texte et la clé arrivent en deux temps
            if c == 3:  # garder la même plage spectrale dans la nouvelle unité
                lo, hi = (self._convert(np.array([w.start, w.end]), w.unit, key) if self.run is not None
                          else (w.start, w.end))
                digits = 4 if key == "nm" else 2
                w.start, w.end = round(float(lo), digits), round(float(hi), digits)
                w.unit = key
                self._fill_windows_table()
            else:
                w.measure = key
        elif c == 5:
            w.baseline = item.checkState() == Qt.CheckState.Checked
        self._windows_changed()

    def _on_window_selected(self):
        if self._updating:
            return
        r = self._current_window_row()
        # Choisir une fenêtre dans le tableau l'affiche comme signal.
        if r >= 0 and self.signal_combo.currentData() != r:
            self.signal_combo.setCurrentIndex(r + 1)
        self._refresh_spectrum()

    def _add_window(self):
        unit = self.unit_combo.currentData()
        lo, hi = self.spec_plot.getViewBox().viewRange()[0]
        span = hi - lo
        self.windows.append(Window(f"Fenêtre {len(self.windows) + 1}", round(lo + 0.4 * span, 1),
                                   round(lo + 0.6 * span, 1), unit=unit))
        self._fill_windows_table()
        self.win_table.selectRow(len(self.windows) - 1)
        self._windows_changed()
        self.signal_combo.setCurrentIndex(len(self.windows))

    def _remove_window(self):
        r = self._current_window_row()
        if r >= 0:
            del self.windows[r]
            self._fill_windows_table()
            self._windows_changed()

    def _load_windows(self):
        path, _ = QFileDialog.getOpenFileName(self, "Charger des fenêtres", str(self.analysis_dir), "JSON (*.json)")
        if path:
            try:
                self.windows = load_windows(path)
            except Exception as e:
                QMessageBox.warning(self, "Lecture impossible", str(e))
                return
            self._fill_windows_table()
            self._windows_changed()

    def _save_windows(self):
        self.analysis_dir.mkdir(parents=True, exist_ok=True)
        path, _ = QFileDialog.getSaveFileName(self, "Enregistrer les fenêtres", str(self.analysis_dir / "fenetres_.json"),
                                              "JSON (*.json)")
        if path:
            save_windows(self.windows, path)

    def _on_region_moved(self):
        if self._updating:
            return
        r = self._current_window_row()
        if r < 0:
            return
        w = self.windows[r]
        lo, hi = self.region.getRegion()
        lo, hi = self._from_display_unit(np.array([lo, hi]), w.unit)
        w.start, w.end = (round(float(v), 2) for v in sorted((lo, hi)))
        self._fill_windows_table()
        self._windows_changed()

    # ================================================================== calcul
    def _rows(self):
        a, b = self.row_start.value(), self.row_end.value()
        return (min(a, b), max(a, b))

    def _rows_changed(self):
        self._recompute()
        self._refresh_camera()

    def _axis_in(self, unit):
        return self.run.shift_cm1 if unit == "cm-1" else self.run.wavelength_nm

    def _convert(self, values, src, dst):
        """Conversion cm⁻¹ ↔ nm avec la raie laser du fichier."""
        if src == dst or self.run is None or not np.isfinite(self.run.laser_nm):
            return values
        with np.errstate(divide="ignore", invalid="ignore"):
            if dst == "nm":
                return 1e7 / (1e7 / self.run.laser_nm - values)
            return 1e7 / self.run.laser_nm - 1e7 / values

    def _from_display_unit(self, values, unit):
        return self._convert(values, self.unit_combo.currentData(), unit)

    def _to_display_unit(self, values, unit):
        return self._convert(values, unit, self.unit_combo.currentData())

    def _spectra(self):
        """Spectres de toutes les images (avec une barre de progression la première fois)."""
        dialog = None

        def progress(done, total):
            nonlocal dialog
            if dialog is None and total > 64:
                dialog = QProgressDialog("Lecture des images…", "Annuler", 0, total, self)
                dialog.setWindowModality(Qt.WindowModality.WindowModal)
                dialog.setMinimumDuration(300)
            if dialog is not None:
                dialog.setValue(done)
                QApplication.processEvents()
                return not dialog.wasCanceled()
            return True

        try:
            return self.run.spectra(self._rows(), progress)
        except InterruptedError:
            return None
        finally:
            if dialog is not None:
                dialog.close()

    def _set_value_label(self, axis):
        """Étiquette de l'axe des valeurs ; la puissance profite des préfixes automatiques (µW, mW)."""
        if self.signal_combo.currentData() in (POWER, None):
            axis.setLabel("Puissance", units="W", color=INK)
        else:
            axis.setLabel(self._signal_label(), units=None, color=INK)

    def _signal_label(self):
        key = self.signal_combo.currentData()
        if key == POWER or key is None:
            return "Puissance (W)"
        w = self.windows[key]
        unit = "cm⁻¹" if w.unit == "cm-1" else "nm"
        label = {"aire": f"Aire (counts·{unit})", "hauteur": "Hauteur (counts)", "position": f"Position ({unit})"}
        text = f"{w.name} — {label[w.measure]}"
        return text + " / W" if self.normalize.isChecked() and w.measure != "position" else text

    def _recompute(self):
        if self._updating or self.run is None:
            return
        run = self.run
        key = self.signal_combo.currentData()
        is_raman = key not in (POWER, None) and key < len(self.windows)
        self.normalize.setEnabled(is_raman and run.has_power)
        self.baselines = None
        if not is_raman:
            self.values = run.power_W.copy()
        elif not run.has_images:
            self.values = np.full(run.n, np.nan)
        else:
            spectra = self._spectra()
            if spectra is None:
                return
            w = self.windows[key]
            per_image, self.baselines = evaluate(self._axis_in(w.unit), spectra, w)
            self.values = np.full(run.n, np.nan)
            has = run.image_index >= 0
            self.values[has] = per_image[run.image_index[has]]
            if self.normalize.isChecked() and run.has_power and w.measure != "position":
                with np.errstate(divide="ignore", invalid="ignore"):
                    self.values = self.values / run.power_W
        self._refresh_views(auto_levels=True)

    # ================================================================== affichage
    def _refresh_all(self):
        self._fill_windows_table()
        self._fill_signal_combo()
        if self.run is None:
            self.file_label.setText("Aucun fichier — « Ouvrir… » (dossier data/runs)")
            self.summary.setText("")
            return
        self.file_label.setText(f"{self.run.path.parent.name} / {self.run.name}")
        self.file_label.setToolTip(str(self.run.path))
        self.summary.setText(self.run.summary())

    def _visible_mask(self):
        if self.run is None:
            return np.zeros(0, dtype=bool)
        return np.isin(self.run.zone_index, list(self._checked_zones()))

    def _refresh_views(self, auto_levels=False):
        if self._updating or self.run is None or self.values is None:
            return
        self._refresh_map(auto_levels)
        self._refresh_graph()
        self._refresh_spectrum()
        self._refresh_camera()
        self._refresh_point_info()

    # ---- carte
    def _refresh_map(self, auto_levels):
        run, values = self.run, self.values
        for img in self.map_images:
            self.map_plot.removeItem(img)
        self.map_images = []
        mask = self._visible_mask()
        scatter_mask = mask.copy()
        for k in self._checked_zones():
            if k < len(run.zones) and run.zones[k].kind == "rect":
                sel = run.zone_index == k
                zone = run.zones[k]
                grid, rect = grid_rect(zone, zone.to_local(run.target[sel]), values[sel])
                img = pg.ImageItem(grid, axisOrder="row-major")
                place_image(img, rect, zone.angle, zone.center)  # rectangle éventuellement tourné
                img.setZValue(-1)
                self.map_plot.addItem(img)
                self.map_images.append(img)
                scatter_mask &= ~sel
        self.colorbar.setImageItem(self.map_images)
        self._scatter_index = np.flatnonzero(scatter_mask)
        self.map_scatter.setData(x=run.target[self._scatter_index, 0], y=run.target[self._scatter_index, 1],
                                 data=self._scatter_index)
        self._set_value_label(self.colorbar.axis)
        if auto_levels:
            self._auto_levels()
        else:
            self.colorbar.setLevels(self.colorbar.levels())
            self._color_scatter()
        self._set_colormap(self.cmap_combo.currentText())
        self.map_plot.getViewBox().autoRange(padding=0.08)

    def _auto_levels(self):
        if self.values is None:
            return
        v = self.values[self._visible_mask()]
        v = v[np.isfinite(v)]
        if v.size == 0:
            lo, hi = 0.0, 1.0
        else:
            lo, hi = np.percentile(v, [1, 99])
            if hi <= lo:
                lo, hi = lo - abs(lo) * 0.01 - 1e-12, hi + abs(hi) * 0.01 + 1e-12
        self.colorbar.setLevels((float(lo), float(hi)))
        self._color_scatter()

    def _set_colormap(self, name):
        self.colorbar.setColorMap(COLORMAPS[name]())
        self._color_scatter()

    def _color_scatter(self):
        if self.run is None or self.values is None or not len(getattr(self, "_scatter_index", [])):
            return
        lo, hi = self.colorbar.levels()
        v = self.values[self._scatter_index]
        t = np.clip((v - lo) / (hi - lo if hi > lo else 1.0), 0, 1)
        cmap = self.colorbar.colorMap()
        brushes = [pg.mkBrush(cmap.map(x, mode="qcolor")) if np.isfinite(x) else pg.mkBrush(0, 0, 0, 0)
                   for x in t]
        pens = [pg.mkPen(SURFACE, width=1) if np.isfinite(x) else pg.mkPen(MUTED, width=1) for x in t]
        self.map_scatter.setBrush(brushes)
        self.map_scatter.setPen(pens)

    def _on_scatter_clicked(self, _item, points, _ev=None):
        if len(points):
            self.select_point(int(points[0].data()))

    def _on_map_clicked(self, ev):
        """Clic dans une image de rectangle : point mesuré le plus proche."""
        if self.run is None or not self.map_images or ev.button() != Qt.MouseButton.LeftButton:
            return
        pos = self.map_plot.getViewBox().mapSceneToView(ev.scenePos())
        for img in self.map_images:
            if img.boundingRect().contains(img.mapFromParent(pos)):
                mask = self._visible_mask()
                d = np.hypot(self.run.target[:, 0] - pos.x(), self.run.target[:, 1] - pos.y())
                d[~mask] = np.inf
                self.select_point(int(np.argmin(d)))
                return

    # ---- graphique
    def _x_values(self):
        run = self.run
        key = self.xaxis_combo.currentData()
        if key == "x":
            return run.target[:, 0]
        if key == "y":
            return run.target[:, 1]
        if key == "index":
            return np.arange(run.n, dtype=float)
        if key == "time":
            return run.elapsed_s()
        return run.distance_along_zone()

    def _refresh_graph(self):
        if self.run is None or self.values is None:
            return
        for item in list(self.graph.getPlotItem().listDataItems()):
            self.graph.removeItem(item)
        self.graph_legend.clear()
        x = self._x_values()
        self.graph.getAxis("bottom").setLabel(self.xaxis_combo.currentText(), color=INK)
        self._set_value_label(self.graph.getAxis("left"))
        for k in sorted(self._checked_zones()):
            idx = np.flatnonzero((self.run.zone_index == k) & np.isfinite(self.values))
            if idx.size == 0:
                continue  # zone sans cette mesure (ex. pas d'image pour une fenêtre Raman)
            color = _series_color(k)
            pen = pg.mkPen(color, width=2) if self.graph_lines.isChecked() else None
            curve = pg.PlotDataItem(x[idx], self.values[idx], pen=pen, symbol="o", symbolSize=8,
                                    symbolBrush=color, symbolPen=pg.mkPen(SURFACE, width=1),
                                    name=self.run.zone_name(k), connect="finite")
            curve.setCurveClickable(False)
            curve.scatter.setData(x=x[idx], y=self.values[idx], data=idx, size=8, brush=color,
                                  pen=pg.mkPen(SURFACE, width=1))
            curve.sigPointsClicked.connect(self._on_graph_clicked)
            self.graph.addItem(curve)
        self.graph.getPlotItem().enableAutoRange()

    def _on_graph_clicked(self, _item, points, _ev=None):
        if len(points):
            data = points[0].data()
            if data is not None:
                self.select_point(int(data))

    # ---- spectre
    def select_point(self, i):
        self.selected = i
        self._refresh_spectrum()
        self._refresh_camera()
        self._refresh_point_info()
        self._refresh_markers()

    def _refresh_markers(self):
        if self.selected is None or self.run is None:
            self.map_marker.setData([], [])
            self.graph_marker.setData([], [])
            return
        x, y = self.run.target[self.selected]
        self.map_marker.setData([x], [y])
        gx = self._x_values()[self.selected]
        gy = self.values[self.selected] if self.values is not None else np.nan
        self.graph_marker.setData([gx], [gy] if np.isfinite(gy) else [])

    def _display_axis(self):
        return self._axis_in(self.unit_combo.currentData())

    def _refresh_spectrum(self):
        for r in self.other_regions:
            self.spec_plot.removeItem(r)
        self.other_regions = []
        run = self.run
        empty = run is None or not run.has_images
        unit = self.unit_combo.currentData()
        self.spec_plot.getAxis("bottom").setLabel(
            "Décalage Raman (cm⁻¹)" if unit == "cm-1" else "Longueur d'onde (nm)", color=INK)
        if empty:
            self.spec_curve.setData([], [])
            self.mean_curve.setData([], [])
            self.base_curve.setData([], [])
            self.region.setVisible(False)
            return
        x = self._display_axis()
        spectra = run._spectra.get(self._rows())
        j = run.image_index[self.selected] if self.selected is not None else -1
        if spectra is not None and j >= 0:
            self.spec_curve.setData(x, spectra[j])
        else:
            self.spec_curve.setData([], [])
        if self.show_mean.isChecked() and spectra is not None:
            idx = run.image_index[self._visible_mask()]
            idx = idx[idx >= 0]
            self.mean_curve.setData(x, spectra[idx].mean(axis=0)) if idx.size else self.mean_curve.setData([], [])
        else:
            self.mean_curve.setData([], [])

        # Fenêtres : la sélectionnée est éditable, les autres en gris.
        current = self._current_window_row()
        self._updating = True
        for i, w in enumerate(self.windows):
            lo, hi = sorted(self._to_display_unit(np.array([w.lo, w.hi]), w.unit))
            if i == current:
                self.region.setRegion((lo, hi))
                self.region.setVisible(True)
            else:
                r = pg.LinearRegionItem((lo, hi), movable=False, brush=QColor(156, 163, 175, 35),
                                        pen=pg.mkPen(MUTED, width=1))
                r.setZValue(-6)
                self.spec_plot.addItem(r)
                self.other_regions.append(r)
        if current < 0:
            self.region.setVisible(False)
        self._updating = False

        # Fond linéaire de la fenêtre affichée comme signal, pour le point sélectionné.
        key = self.signal_combo.currentData()
        self.base_curve.setData([], [])
        if self.baselines is not None and key not in (POWER, None) and j >= 0 and key < len(self.windows):
            w = self.windows[key]
            if w.baseline:
                slope, intercept = self.baselines[j]
                xs = np.linspace(w.lo, w.hi, 50)
                self.base_curve.setData(self._to_display_unit(xs, w.unit), slope * xs + intercept)

    # ---- image caméra
    def _refresh_camera(self):
        if self.run is None:
            return
        rows = self._rows()
        for line, r in zip(self.row_lines, rows):
            line.setValue(r + (0.0 if line is self.row_lines[0] else 1.0))
        img = self.run.image(self.selected) if self.selected is not None else None
        if img is None:
            self.cam_image.clear()
            return
        self.cam_image.setImage(img.astype(float), autoLevels=False,
                                levels=tuple(np.percentile(img, [1, 99.8])))

    def _on_row_line_moved(self):
        a, b = sorted(int(round(line.value())) for line in self.row_lines)
        self.row_start.setValue(max(a, 0))
        self.row_end.setValue(max(min(b - 1, self.row_end.maximum()), self.row_start.value()))
        self._rows_changed()

    # ---- informations
    def _refresh_point_info(self):
        if self.selected is None or self.run is None:
            return
        run, i = self.run, self.selected
        pos = run.position[i]
        text = (f"Point {i} — {run.zone_name(run.zone_index[i])} — demandé X {run.target[i, 0]:.4f}, "
                f"Y {run.target[i, 1]:.4f} mm ; lu X {pos[0]:.4f}, Y {pos[1]:.4f}, Z {pos[2]:.3f} mm")
        if np.isfinite(run.power_W[i]):
            text += f" — P = {run.power_W[i] * 1e3:.4f} mW"
        if self.values is not None and np.isfinite(self.values[i]):
            text += f" — {self._signal_label()} : {self.values[i]:.5g}"
        if run.image_index[i] < 0:
            text += " — pas d'image"
        self.point_info.setText(text)

    # ================================================================== export
    def _export_csv(self):
        if self.run is None or self.values is None:
            return
        default = self.run.path.with_name(self.run.path.stem + "_valeurs.csv")
        path, _ = QFileDialog.getSaveFileName(self, "Exporter les valeurs", str(default), "CSV (*.csv)")
        if not path:
            return
        run = self.run
        # Toutes les fenêtres sont exportées, en plus de la puissance brute.
        columns = {}
        if run.has_images and self.windows:
            spectra = self._spectra()
            if spectra is not None:
                has = run.image_index >= 0
                for w in self.windows:
                    per_image, _ = evaluate(self._axis_in(w.unit), spectra, w)
                    col = np.full(run.n, np.nan)
                    col[has] = per_image[run.image_index[has]]
                    columns[w.label()] = col
        dist = run.distance_along_zone()
        t = run.elapsed_s()
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            out = csv.writer(f, delimiter=";")
            out.writerow(["point", "zone", "x_demande_mm", "y_demande_mm", "x_lu_mm", "y_lu_mm", "z_lu_mm",
                          "distance_mm", "temps_s", "puissance_W"] + list(columns))
            for i in range(run.n):
                out.writerow([i, run.zone_name(run.zone_index[i]), *(f"{v:.6f}" for v in run.target[i]),
                              *(f"{v:.6f}" for v in run.position[i]), f"{dist[i]:.6f}", f"{t[i]:.3f}",
                              f"{run.power_W[i]:.6e}"] + [f"{c[i]:.6g}" for c in columns.values()])
        QMessageBox.information(self, "Export", f"{run.n} points exportés dans\n{path}")

    def _export_png(self):
        if self.run is None:
            return
        tab = self.tabs.tabText(self.tabs.currentIndex()).lower().replace(" ", "_").replace("é", "e")
        default = self.run.path.with_name(f"{self.run.path.stem}_{tab}.png")
        path, _ = QFileDialog.getSaveFileName(self, "Exporter la vue", str(default), "PNG (*.png)")
        if path:
            self.tabs.currentWidget().grab().save(path)


def run_viewer(cfg, path=None):
    os.environ.setdefault("PYQTGRAPH_QT_LIB", "PySide6")
    app = QApplication.instance() or QApplication([])
    window = AnalysisWindow(cfg, path)
    window.show()
    return app, window
