"""Simulateurs de la platine et du spectromètre, pour utiliser l'interface sans matériel."""
import re
import time

import numpy as np

from .ultimaker import UltimakerStage

_AXIS_RE = re.compile(r"([XYZ])(-?[\d.]+)")
_LASER_NM = 514.5
_SI_PEAK_NM = 1e7 / (1e7 / _LASER_NM - 520.7)


class SimWorld:
    """Banc simulé partagé : lame de rasoir sur le plateau, capteur dessous, faisceau gaussien.

    La platine simulée met à jour la position ; le wattmètre simulé en déduit la puissance transmise
    à côté de la lame. Sans platine connectée, le wattmètre donne une puissance constante.
    """

    def __init__(self):
        self.position = None                  # (x, y, z) mm, mis à jour par SimStage
        self.power_w = 12e-3
        self.edge_point = (100.0, 100.0)      # point du bord de la lame (mm)
        self.edge_angle_deg = 20.0            # direction du bord par rapport à l'axe X
        self.waist_mm = 0.030                 # rayon 1/e² au foyer
        self.focus_z_mm = 25.0
        self.rayleigh_mm = 0.40
        self.noise = 0.002
        self._rng = np.random.default_rng()

    def beam_radius(self, z):
        return self.waist_mm * np.sqrt(1.0 + ((z - self.focus_z_mm) / self.rayleigh_mm) ** 2)

    def fraction(self):
        """Part du faisceau du côté positif du bord : libre (lame) ou sur le wafer de Si (même géométrie)."""
        if self.position is None:
            return 1.0
        from math import erf
        x, y, z = self.position
        a = np.radians(self.edge_angle_deg)
        normal = (-np.sin(a), np.cos(a))
        d = (x - self.edge_point[0]) * normal[0] + (y - self.edge_point[1]) * normal[1]
        return 0.5 * (1.0 + erf(np.sqrt(2.0) * d / self.beam_radius(z)))

    def transmitted(self):
        if self.position is None:
            return self.power_w * (1.0 + self._rng.normal(0.0, 0.005))
        return self.power_w * (0.02 + 0.98 * self.fraction()) * (1.0 + self._rng.normal(0.0, self.noise))


WORLD = SimWorld()


class SimStage(UltimakerStage):
    """Remplace le transport SSH : interprète le G-code localement, avec les mêmes limites."""

    def __init__(self, ucfg):
        super().__init__(ucfg)
        self._pos = {"X": 0.0, "Y": 0.0, "Z": 0.0}

    def connect(self, ready_timeout=30.0):
        return self

    def close(self):
        WORLD.position = None

    def send(self, gcode, timeout=None):
        if gcode.startswith("G0"):
            # Comme le firmware : X et Y sur des pas moteur de 12,5 µm (80 pas/mm) ; M114 lu à 0,01 mm.
            words = {ax: (round(float(v) * 80) / 80 if ax in "XY" else float(v))
                     for ax, v in _AXIS_RE.findall(gcode)}
            distance = max(abs(v - self._pos[ax]) for ax, v in words.items())
            time.sleep(min(distance / 50.0, 1.0))  # ~3000 mm/min
            self._pos.update(words)
        elif gcode.startswith("G28"):
            time.sleep(0.5)
            self._pos = {"X": 0.0, "Y": self.limits["y"][1], "Z": self.limits["z"][1]}
        WORLD.position = (self._pos["X"], self._pos["Y"], self._pos["Z"])
        if gcode.startswith("M114"):
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
        """Image 100 × 1340 : niveau de base ~590 counts par pixel, pic du Si et raie laser résiduelle.

        Les deux raies sont proportionnelles à la part du spot sur le wafer simulé (SimWorld) ;
        la raie laser garde un fond de 15 % hors du wafer (réflexion du plateau).
        """
        time.sleep(self.exposure_ms / 1000.0 + 0.12)
        wl = self.wavelengths()
        f = WORLD.fraction()
        t = self.exposure_ms / 1000.0
        peak = 40.0 * t * f * np.exp(-0.5 * ((wl - _SI_PEAK_NM) / 0.35) ** 2)
        peak += 25.0 * t * (0.15 + 0.85 * f) * np.exp(-0.5 * ((wl - _LASER_NM) / 0.3) ** 2)
        rows = np.exp(-0.5 * ((np.arange(100) - 50) / 15.0) ** 2)[:, None]
        img = 590.0 + 2.5 * rows * peak + self._rng.normal(0.0, 6.0, (100, wl.size))
        return np.clip(img, 0, 65535).astype(np.uint16), self.exposure_ms / 1000.0 + 0.12

    def acquire_spectrum(self):
        img, dt = self.acquire_image()
        return self.wavelengths(), img.sum(axis=0).astype(float), dt


class SimPowerMeter:
    """Mêmes méthodes que PM100D ; 12 mW, ou puissance transmise par la lame simulée (voir SimWorld)."""

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
        return WORLD.transmitted() - self._offset
