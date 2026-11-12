"""Zones d'analyse dessinées sur la platine, et points de mesure qu'elles génèrent."""
import json
import math
from dataclasses import asdict, dataclass

import numpy as np

MIN_STEP_MM = 0.0125  # pas moteur XY de l'UM3
# Mesurés en phase 0 : temps mort LightField, déplacement avec pré-position, lecture PM100D
ACQUISITION_DEAD_TIME_S = 0.12
MOVE_TIME_S = 0.3
POWER_READ_S = 0.15


def _axis(a, b, step):
    lo, hi = min(a, b), max(a, b)
    return lo + np.arange(math.floor((hi - lo) / step + 1e-9) + 1) * step


@dataclass(eq=False)
class Zone:
    name: str
    kind: str  # "rect", "line" ou "point"
    x0: float
    y0: float
    x1: float
    y1: float
    step_x: float = 1.0
    step_y: float = 1.0
    raman: bool = True    # acquérir l'image de la caméra à chaque point
    power: bool = False   # enregistrer une lecture du wattmètre à chaque point

    def clamp(self, limits):
        """Ramène la zone dans les limites de la platine."""
        (xlo, xhi), (ylo, yhi) = limits["x"], limits["y"]
        self.x0, self.x1 = (min(max(v, xlo), xhi) for v in (self.x0, self.x1))
        self.y0, self.y1 = (min(max(v, ylo), yhi) for v in (self.y0, self.y1))

    def count(self):
        if self.kind == "point":
            return 1
        if self.kind == "line":
            return math.floor(math.hypot(self.x1 - self.x0, self.y1 - self.y0) / self.step_x + 1e-9) + 1
        return _axis(self.x0, self.x1, self.step_x).size * _axis(self.y0, self.y1, self.step_y).size

    def points(self):
        """Points de mesure (N, 2) dans l'ordre de balayage ; rectangle parcouru en serpentin."""
        if self.kind == "point":
            return np.array([[self.x0, self.y0]])
        if self.kind == "line":
            length = math.hypot(self.x1 - self.x0, self.y1 - self.y0)
            direction = np.array([self.x1 - self.x0, self.y1 - self.y0]) / length if length else np.zeros(2)
            return np.array([self.x0, self.y0]) + np.outer(np.arange(self.count()) * self.step_x, direction)
        xs, ys = _axis(self.x0, self.x1, self.step_x), _axis(self.y0, self.y1, self.step_y)
        rows = [np.column_stack((xs[::-1] if i % 2 else xs, np.full(xs.size, y))) for i, y in enumerate(ys)]
        return np.vstack(rows)


def estimate_seconds(zones, exposure_ms):
    """Durée estimée ; la lecture du wattmètre se fait pendant l'exposition quand les deux sont cochés."""
    raman_s = exposure_ms / 1000.0 + ACQUISITION_DEAD_TIME_S
    total = 0.0
    for z in zones:
        per_point = max(raman_s if z.raman else 0.0, POWER_READ_S if z.power else 0.0)
        total += z.count() * (MOVE_TIME_S + per_point)
    return total


def save_zones(zones, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(z) for z in zones], f, indent=2, ensure_ascii=False)


def load_zones(path):
    with open(path, encoding="utf-8") as f:
        return [Zone(**d) for d in json.load(f)]
