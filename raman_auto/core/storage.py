"""Enregistrement d'une exécution de zones dans un fichier HDF5.

Contenu du fichier (N points mesurés, M ≤ N images) :
    images          (M, lignes, colonnes)  image de la caméra, points des zones « Raman » (uint16) ; lignes = 1
                                           si le capteur est binné dans LightField
    image_index     (N,)                   ligne de "images" pour chaque point, -1 si pas d'image
    wavelength_nm   (colonnes,)            axe spectral LightField au moment du lancement (vide sans Raman)
    position_mm     (N, 3)                 position X, Y, Z lue après le déplacement
    target_mm       (N, 2)                 position X, Y demandée
    zone_index      (N,)                   indice de la zone (dans l'attribut "zones")
    timestamp       (N,)                   heure de fin de mesure du point (s depuis 1970)
    power_W         (N,)                   lecture du wattmètre au point, zones « Wattmètre » (NaN sinon)
    acquisition_s   (N,)                   durée de l'acquisition Raman (NaN sans image)
    attributs       réglages (réseau, centre, exposition, raie laser, puissance de référence…),
                    zones (JSON), points prévus/enregistrés, état final.

Le fichier est ouvert puis refermé à chaque point : sous Windows, un fichier HDF5 ouvert en écriture ne peut
pas être lu par un autre programme ; ainsi l'outil d'analyse peut relire une exécution en cours, et un arrêt
brutal conserve les points déjà mesurés.
"""
import datetime
import time
from pathlib import Path

import h5py
import numpy as np

_COLUMNS = {
    "image_index": ((), "i4"),
    "position_mm": ((3,), "f8"),
    "target_mm": ((2,), "f8"),
    "zone_index": ((), "i4"),
    "timestamp": ((), "f8"),
    "power_W": ((), "f8"),
    "acquisition_s": ((), "f8"),
}
OPEN_RETRIES = 40  # × 50 ms : le fichier peut être lu au même moment par l'outil d'analyse


def _attr(value):
    if value is None:
        return ""
    if isinstance(value, (bool, int, float, str, np.number)):
        return value
    return str(value)


class RunWriter:
    def __init__(self, path, wavelengths_nm, attrs):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.n = 0
        self.n_images = 0
        self.closed = False
        with h5py.File(self.path, "w") as f:
            f.attrs["started"] = datetime.datetime.now().isoformat(timespec="seconds")
            for key, value in attrs.items():
                f.attrs[key] = _attr(value)
            f.create_dataset("wavelength_nm", data=np.asarray(wavelengths_nm, dtype=float))
            for name, (shape, dtype) in _COLUMNS.items():
                f.create_dataset(name, shape=(0,) + shape, maxshape=(None,) + shape, dtype=dtype,
                                 chunks=(256,) + shape)

    def _open(self):
        for attempt in range(OPEN_RETRIES):
            try:
                return h5py.File(self.path, "a")
            except OSError:
                if attempt == OPEN_RETRIES - 1:
                    raise
                time.sleep(0.05)

    def append(self, image=None, **values):
        """Ajoute un point ; image=None pour un point sans acquisition Raman."""
        values["image_index"] = -1
        with self._open() as f:
            if image is not None:
                if "images" not in f:
                    f.create_dataset("images", shape=(0,) + image.shape, maxshape=(None,) + image.shape,
                                     dtype=image.dtype, chunks=(1,) + image.shape, compression="lzf")
                ds = f["images"]
                ds.resize(self.n_images + 1, axis=0)
                ds[self.n_images] = image
                values["image_index"] = self.n_images
                self.n_images += 1
            i = self.n
            for name in _COLUMNS:
                ds = f[name]
                ds.resize(i + 1, axis=0)
                ds[i] = values[name]
            f.attrs["points_saved"] = i + 1
            f.attrs["images_saved"] = self.n_images
        self.n = i + 1

    def close(self, **attrs):
        if self.closed:
            return
        self.closed = True
        with self._open() as f:
            f.attrs["finished"] = datetime.datetime.now().isoformat(timespec="seconds")
            f.attrs["points_saved"] = self.n
            f.attrs["images_saved"] = self.n_images
            for key, value in attrs.items():
                f.attrs[key] = _attr(value)
