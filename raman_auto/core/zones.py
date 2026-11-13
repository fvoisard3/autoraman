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


def rotate(xy, angle_deg, center):
    """Rotation de points (N, 2) autour de center, angle en degrés (sens trigonométrique)."""
    if not angle_deg:
        return np.asarray(xy, float)
    a = math.radians(angle_deg)
    r = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
    c = np.asarray(center, float)
    return (np.asarray(xy, float) - c) @ r.T + c


@dataclass(eq=False)
class Zone:
    """Zone d'analyse. Pour un rectangle, (x0, y0)–(x1, y1) sont les coins avant rotation ; angle (degrés,
    sens trigonométrique) tourne le rectangle et sa grille de points autour de son centre."""
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
    angle: float = 0.0    # rotation d'un rectangle (degrés)

    @property
    def center(self):
        return ((self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2)

    def corners(self):
        """Coins du rectangle tourné (4, 2), dans l'ordre du contour."""
        xlo, xhi = min(self.x0, self.x1), max(self.x0, self.x1)
        ylo, yhi = min(self.y0, self.y1), max(self.y0, self.y1)
        box = np.array([[xlo, ylo], [xhi, ylo], [xhi, yhi], [xlo, yhi]])
        return rotate(box, self.angle, self.center) if self.kind == "rect" else box

    def clamp(self, limits):
        """Ramène la zone dans les limites de la platine (un rectangle tourné est translaté en bloc)."""
        (xlo, xhi), (ylo, yhi) = limits["x"], limits["y"]
        if self.kind == "rect" and self.angle:
            c = self.corners()
            dx = max(0.0, xlo - c[:, 0].min()) - max(0.0, c[:, 0].max() - xhi)
            dy = max(0.0, ylo - c[:, 1].min()) - max(0.0, c[:, 1].max() - yhi)
            self.x0, self.x1, self.y0, self.y1 = self.x0 + dx, self.x1 + dx, self.y0 + dy, self.y1 + dy
            return
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
        return rotate(np.vstack(rows), self.angle, self.center)

    def to_local(self, xy):
        """Coordonnées dans le repère du rectangle non tourné (inverse de la rotation)."""
        return rotate(xy, -self.angle, self.center)


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
