"""Simulateurs de la platine et du spectromètre, pour utiliser l'interface sans matériel."""
import re
import time

import numpy as np

from .ultimaker import UltimakerStage

_AXIS_RE = re.compile(r"([XYZ])(-?[\d.]+)")
_LASER_NM = 514.5
_SI_PEAK_NM = 1e7 / (1e7 / _LASER_NM - 520.7)


class SimStage(UltimakerStage):
    """Remplace le transport SSH : interprète le G-code localement, avec les mêmes limites."""

    def __init__(self, ucfg):
        super().__init__(ucfg)
        self._pos = {"X": 0.0, "Y": 0.0, "Z": 0.0}

    def connect(self, ready_timeout=30.0):
        return self

    def close(self):
        pass

    def send(self, gcode, timeout=None):
        if gcode.startswith("G0"):
            words = {ax: float(v) for ax, v in _AXIS_RE.findall(gcode)}
            distance = max(abs(v - self._pos[ax]) for ax, v in words.items())
            time.sleep(min(distance / 50.0, 1.0))  # ~3000 mm/min
            self._pos.update(words)
        elif gcode.startswith("G28"):
            time.sleep(0.5)
            self._pos = {"X": 0.0, "Y": self.limits["y"][1], "Z": self.limits["z"][1]}
        elif gcode.startswith("M114"):
            p = self._pos
            return [f"X:{p['X']:.2f}Y:{p['Y']:.2f}Z:{p['Z']:.2f}E:0.00 "
                    f"count X:{p['X']:.2f}Y:{p['Y']:.2f}Z:{p['Z']:.2f}"]
        return []


class SimSpectrometer:
    """Mêmes méthodes que LightFieldSpectrometer ; spectre : niveau de base + pic du Si."""

    _GRATINGS = ["[h-vis,2400][0][0]", "[500nm,1200][1][0]", "[500nm,300][2][0]"]  # ceux du SP-2-500i

    def __init__(self):
        self.exposure_ms = 1000.0
        self.center_wavelength_nm = 585.0
        self.grating = self._GRATINGS[2]
        self._rng = np.random.default_rng()

    def open(self):
        return self

    def close(self):
        pass

    def gratings(self):
        return list(self._GRATINGS)

    def wavelengths(self):
        density = float(re.match(r"\[[^,]+,(\d+)\]", self.grating).group(1))
        span = 174.2 * 300.0 / density
        return np.linspace(self.center_wavelength_nm - span / 2, self.center_wavelength_nm + span / 2, 1340)

    def metadata(self):
        return {"experiment": "simulation", "exposure_ms": self.exposure_ms, "grating": self.grating,
                "center_wavelength_nm": self.center_wavelength_nm}

    def acquire_image(self):
        """Image 100 × 1340 : niveau de base ~590 counts par pixel + pic du Si sur les lignes centrales."""
        time.sleep(self.exposure_ms / 1000.0 + 0.12)
        wl = self.wavelengths()
        peak = 40.0 * self.exposure_ms / 1000.0 * np.exp(-0.5 * ((wl - _SI_PEAK_NM) / 0.35) ** 2)
        rows = np.exp(-0.5 * ((np.arange(100) - 50) / 15.0) ** 2)[:, None]
        img = 590.0 + 2.5 * rows * peak + self._rng.normal(0.0, 6.0, (100, wl.size))
        return np.clip(img, 0, 65535).astype(np.uint16), self.exposure_ms / 1000.0 + 0.12

    def acquire_spectrum(self):
        img, dt = self.acquire_image()
        return self.wavelengths(), img.sum(axis=0).astype(float), dt


class SimPowerMeter:
    """Mêmes méthodes que PM100D ; ~12 mW avec un bruit de 0,5 %."""

    def __init__(self):
        self.wavelength_nm = _LASER_NM
        self.avg_time_s = 0.1
        self._offset = 0.0
        self._rng = np.random.default_rng()

    def open(self, resource=None):
        return self

    def close(self):
        pass

    def set_auto_range(self, enabled=True):
        pass

    def sensor_info(self):
        return {"name": "S121C (simulé)", "serial": "-"}

    def zero(self, timeout_s=60.0):
        time.sleep(1.0)
        self._offset = 0.0
        return 0.0

    def power_w(self):
        time.sleep(self.avg_time_s)
        return 12e-3 * (1.0 + self._rng.normal(0.0, 0.005)) - self._offset
