"""Enregistrement d'une exécution de zones dans un fichier HDF5.

Contenu du fichier (N points mesurés, M ≤ N images) :
    images          (M, lignes, colonnes)  image complète de la caméra, points des zones « Raman » (uint16)
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
Le fichier est vidé sur disque après chaque point : un arrêt brutal conserve les points déjà mesurés.
"""
import datetime
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
        self._images = None
        self._columns = {}
        self.f = h5py.File(self.path, "w")
        self.f.attrs["started"] = datetime.datetime.now().isoformat(timespec="seconds")
        for key, value in attrs.items():
            self.f.attrs[key] = _attr(value)
        self.f.create_dataset("wavelength_nm", data=np.asarray(wavelengths_nm, dtype=float))

        for name, (shape, dtype) in _COLUMNS.items():
            self._columns[name] = self.f.create_dataset(
                name, shape=(0,) + shape, maxshape=(None,) + shape, dtype=dtype, chunks=(256,) + shape)

    def append(self, image=None, **values):
        """Ajoute un point ; image=None pour un point sans acquisition Raman."""
        values["image_index"] = -1
        if image is not None:
            if self._images is None:
                self._images = self.f.create_dataset(
                    "images", shape=(0,) + image.shape, maxshape=(None,) + image.shape, dtype=image.dtype,
                    chunks=(1,) + image.shape, compression="lzf")
            self._images.resize(self.n_images + 1, axis=0)
            self._images[self.n_images] = image
            values["image_index"] = self.n_images
            self.n_images += 1
        i = self.n
        for name, ds in self._columns.items():
            ds.resize(i + 1, axis=0)
            ds[i] = values[name]
        self.n = i + 1
        self.f.flush()

    def close(self, **attrs):
        if not self.f:
            return
        self.f.attrs["finished"] = datetime.datetime.now().isoformat(timespec="seconds")
        self.f.attrs["points_saved"] = self.n
        self.f.attrs["images_saved"] = self.n_images
        for key, value in attrs.items():
            self.f.attrs[key] = _attr(value)
        self.f.close()
