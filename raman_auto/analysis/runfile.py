"""Lecture d'un fichier d'exécution HDF5 (format décrit dans raman_auto/core/storage.py).

Le fichier est ouvert sans verrou : on peut relire un fichier encore en cours d'écriture par l'interface
d'acquisition (les points déjà vidés sur disque sont lisibles).
"""
import json
from pathlib import Path

import h5py
import numpy as np

from ..core.zones import Zone

BLOCK = 64  # images lues à la fois pour calculer les spectres


def raman_shift(wavelength_nm, laser_nm):
    return 1e7 / laser_nm - 1e7 / np.asarray(wavelength_nm, dtype=float)


def _text(v):
    return v.decode() if isinstance(v, bytes) else v


class RunFile:
    def __init__(self, path):
        self.path = Path(path)
        self.reload()

    def _open(self):
        try:
            return h5py.File(self.path, "r", locking=False)
        except TypeError:  # h5py ancien
            return h5py.File(self.path, "r")

    def reload(self):
        """(Re)lit les colonnes ; les spectres déjà calculés sont oubliés."""
        with self._open() as f:
            self.attrs = {k: _text(v) for k, v in f.attrs.items()}
            n = f["target_mm"].shape[0]
            self.target = f["target_mm"][:n]
            self.position = f["position_mm"][:n]
            self.zone_index = f["zone_index"][:n].astype(int)
            self.timestamp = f["timestamp"][:n]
            self.power_W = f["power_W"][:n]
            self.acquisition_s = f["acquisition_s"][:n] if "acquisition_s" in f else np.full(n, np.nan)
            self.wavelength_nm = f["wavelength_nm"][:]
            self.n_images = f["images"].shape[0] if "images" in f else 0
            self.image_shape = f["images"].shape[1:] if "images" in f else (0, 0)
            if "image_index" in f:
                self.image_index = f["image_index"][:n].astype(int)
            else:  # premiers fichiers : une image par point
                self.image_index = np.where(np.arange(n) < self.n_images, np.arange(n), -1)
        self.n = n
        self.image_index[self.image_index >= self.n_images] = -1  # point écrit avant son image (lecture en cours)
        self.zones = [Zone(**d) for d in json.loads(self.attrs.get("zones") or "[]")]
        self.laser_nm = float(self.attrs.get("laser_nm") or "nan")
        self._spectra = {}

    # ------------------------------------------------------------------ propriétés
    @property
    def name(self):
        return self.path.name

    @property
    def has_images(self):
        return self.n_images > 0 and self.wavelength_nm.size > 0

    @property
    def has_power(self):
        return bool(np.isfinite(self.power_W).any())

    @property
    def shift_cm1(self):
        return raman_shift(self.wavelength_nm, self.laser_nm)

    def zone_name(self, k):
        return self.zones[k].name if 0 <= k < len(self.zones) else f"Zone {k + 1}"

    def elapsed_s(self):
        return self.timestamp - self.timestamp[0] if self.n else self.timestamp

    def distance_along_zone(self):
        """Distance (mm) de chaque point au premier point de sa zone (début de la ligne dessinée)."""
        d = np.zeros(self.n)
        for k in np.unique(self.zone_index):
            sel = self.zone_index == k
            if 0 <= k < len(self.zones):
                z = self.zones[k]
                origin = np.array([z.x0, z.y0]) if z.kind == "line" else self.target[sel][0]
            else:
                origin = self.target[sel][0]
            d[sel] = np.hypot(*(self.target[sel] - origin).T)
        return d

    # ------------------------------------------------------------------ images et spectres
    def image(self, point):
        j = self.image_index[point]
        if j < 0:
            return None
        with self._open() as f:
            return f["images"][j]

    def spectra(self, rows=None, progress=None):
        """Spectres (n_images, colonnes) : somme des lignes rows=(début, fin incluse) de chaque image."""
        rows = tuple(rows) if rows else (0, self.image_shape[0] - 1)
        if rows not in self._spectra:
            out = np.empty((self.n_images, self.image_shape[1]))
            with self._open() as f:
                ds = f["images"]
                for start in range(0, self.n_images, BLOCK):
                    stop = min(start + BLOCK, self.n_images)
                    out[start:stop] = ds[start:stop, rows[0]:rows[1] + 1].sum(axis=1, dtype=np.float64)
                    if progress is not None and progress(stop, self.n_images) is False:
                        raise InterruptedError("calcul interrompu")
            self._spectra = {rows: out}  # un seul jeu en mémoire
        return self._spectra[rows]

    def summary(self):
        a = self.attrs
        lines = [f"{self.n} points ({a.get('points_planned', '?')} prévus), {self.n_images} images",
                 f"Début {a.get('started', '?')} — fin {a.get('finished', 'en cours ?')}"]
        if a.get("end_message"):
            lines.append(str(a["end_message"]))
        lines.append(f"Raie laser {self.laser_nm:g} nm, Z = {a.get('z_mm', '?')} mm"
                     + (" (simulation)" if str(a.get("simulation")) == "True" else ""))
        if self.has_images:
            lines.append(f"Réseau {a.get('lightfield_grating', '?')}, centre "
                         f"{float(a.get('lightfield_center_wavelength_nm') or 'nan'):.1f} nm, exposition "
                         f"{a.get('lightfield_exposure_ms', '?')} ms")
            lines.append(f"IntelliCal : {'activé' if str(a.get('lightfield_intensity_calibration_enabled')) == 'True' else 'désactivé'}")
        ref = float(a.get("power_reference_W") or "nan")
        if np.isfinite(ref):
            lines.append(f"Puissance de référence {ref * 1e3:.3f} mW")
        return "\n".join(lines)
