"""Panneau du wattmètre PM100D : connexion, lecture continue, zéro, puissance de référence."""
import collections
import time

import pyqtgraph as pg
from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QGridLayout, QGroupBox, QLabel, QMessageBox, QPushButton

from ..drivers.pm100d import PM100D, format_power
from ..drivers.sim import SimPowerMeter
from .stage_view import INK, PLATE_EDGE, ZONE_COLOR
from .workers import DeviceThread

POLL_MS = 500
HISTORY_S = 120


class PowerPanel(QGroupBox):
    logged = Signal(str)

    def __init__(self, cfg, simulate, wavelength_nm, parent=None):
        super().__init__("Wattmètre", parent)
        self.pm_cfg = cfg["pm100d"]
        self.simulate = simulate
        self.wavelength_nm = wavelength_nm
        self.pm = None
        self.running = False
        self.last_power_w = float("nan")
        self.reference_w = None
        self._history = collections.deque()
        self._operation = None  # "connect" ou "zero" en cours

        self.thread = DeviceThread(self)
        self.thread.done.connect(self._on_done)
        self.thread.failed.connect(self._on_failed)

        self.connect_button = QPushButton("Connecter le wattmètre")
        self.connect_button.clicked.connect(self._toggle)
        self.value = QLabel("—")
        self.value.setStyleSheet("font-size: 18pt; font-weight: 600;")
        self.info = QLabel()
        self.zero_button = QPushButton("Zéro…")
        self.zero_button.clicked.connect(self._zero)
        self.ref_button = QPushButton("Mémoriser comme référence")
        self.ref_button.clicked.connect(self._set_reference)
        self.ref_label = QLabel("Référence : —")

        self.plot = pg.PlotWidget(background="w")
        self.plot.setFixedHeight(110)
        self.plot.setMouseEnabled(x=False, y=False)
        self.plot.hideButtons()
        self.curve = self.plot.plot(pen=pg.mkPen(ZONE_COLOR, width=1.5))
        for side, label in (("bottom", "Temps (s)"), ("left", "P (mW)")):
            axis = self.plot.getAxis(side)
            axis.setPen(pg.mkPen(PLATE_EDGE))
            axis.setTextPen(pg.mkPen(INK))
            axis.setLabel(label, color=INK)
        self.plot.setXRange(-HISTORY_S, 0, padding=0)

        layout = QGridLayout(self)
        layout.addWidget(self.connect_button, 0, 0, 1, 2)
        layout.addWidget(self.value, 1, 0)
        layout.addWidget(self.info, 1, 1)
        layout.addWidget(self.zero_button, 2, 0)
        layout.addWidget(self.ref_button, 2, 1)
        layout.addWidget(self.ref_label, 3, 0, 1, 2)
        layout.addWidget(self.plot, 4, 0, 1, 2)

        self.timer = QTimer(self, interval=POLL_MS)
        self.timer.timeout.connect(self._poll)
        self._refresh()

    # ------------------------------------------------------------------ état
    def _refresh(self):
        # Les lectures périodiques occupent le thread un court instant : elles ne doivent pas griser les
        # boutons (les commandes sont de toute façon exécutées à la suite, dans l'ordre).
        connected = self.pm is not None
        self.connect_button.setText("Déconnecter le wattmètre" if connected else "Connecter le wattmètre")
        self.connect_button.setEnabled(not self.running and not self._operation)
        self.zero_button.setEnabled(connected and not self.running and not self._operation)
        self.ref_button.setEnabled(connected and self.last_power_w == self.last_power_w)
        self.info.setText(f"λ = {self.wavelength_nm:g} nm" if connected else "Déconnecté")

    def set_running(self, running):
        self.running = running
        self._refresh()

    def set_wavelength(self, nm):
        self.wavelength_nm = nm
        if self.pm is not None:
            pm = self.pm
            self.thread.submit("wavelength", lambda: setattr(pm, "wavelength_nm", nm))
        self._refresh()

    # ------------------------------------------------------------------ actions
    def _toggle(self):
        if self.pm is None:
            self.logged.emit("Connexion au wattmètre…")
            self._operation = "connect"
            self.thread.submit("connect", self._job_connect)
            self._refresh()
        else:
            self.timer.stop()
            pm, self.pm = self.pm, None
            self.last_power_w = float("nan")
            self.value.setText("—")
            self.thread.submit("disconnect", pm.close)

    def _job_connect(self):
        pm = SimPowerMeter() if self.simulate else PM100D(self.pm_cfg["dll"])
        pm.open(self.pm_cfg.get("resource"))
        pm.wavelength_nm = self.wavelength_nm
        pm.avg_time_s = float(self.pm_cfg.get("avg_time_s", 0.1))
        pm.set_auto_range(True)
        return pm, pm.sensor_info()

    def _poll(self):
        if self.pm is not None and not self.thread.busy:
            pm = self.pm
            self.thread.submit("read", lambda: (time.monotonic(), pm.power_w()))

    def _zero(self):
        answer = QMessageBox.question(
            self, "Zéro du wattmètre",
            "Le faisceau est-il bloqué (obturateur fermé ou capteur couvert) ?\n"
            "Un zéro fait sous éclairement fausse toutes les mesures suivantes.")
        if answer == QMessageBox.StandardButton.Yes:
            pm = self.pm
            self._operation = "zero"
            self.thread.submit("zero", pm.zero)
            self._refresh()

    def _set_reference(self):
        self.reference_w = self.last_power_w
        self.ref_label.setText(f"Référence : {format_power(self.reference_w)} à {self.wavelength_nm:g} nm "
                               f"({time.strftime('%H:%M:%S')})")
        self.logged.emit(f"Puissance de référence : {format_power(self.reference_w)}")

    # ------------------------------------------------------------------ résultats
    def _on_done(self, tag, result):
        if tag == self._operation:
            self._operation = None
        if tag == "connect":
            self.pm, sensor = result
            self._history.clear()
            self.timer.start()
            self.logged.emit(f"Wattmètre connecté, capteur {sensor.get('name', '?')}.")
        elif tag == "disconnect":
            self.logged.emit("Wattmètre déconnecté.")
        elif tag in ("read", "scan_power") and self.pm is not None:
            t, power = result
            self.last_power_w = power
            self.value.setText(format_power(power))
            self._history.append((t, power))
            while self._history and t - self._history[0][0] > HISTORY_S:
                self._history.popleft()
            self.curve.setData([h[0] - t for h in self._history], [h[1] * 1e3 for h in self._history])
        elif tag == "zero":
            self.logged.emit(f"Zéro du wattmètre fait (offset {format_power(result)}).")
        self._refresh()

    def _on_failed(self, tag, message):
        if tag == self._operation:
            self._operation = None
        self.logged.emit(f"Wattmètre — erreur ({tag}) : {message}")
        self._refresh()

    def shutdown(self):
        self.timer.stop()
        self.thread.shutdown(self.pm.close if self.pm else None)
