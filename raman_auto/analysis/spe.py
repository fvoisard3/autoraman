"""Lecture des fichiers LightField SPE 3.0 : données, axe en longueur d'onde et réglages (bloc XML final).

Format : en-tête binaire de 4100 octets, données, puis un document XML dont l'adresse est à l'octet 678.
Le XML décrit le format des données (cadres, régions), les calibrations (WavelengthMapping…) et tous les
réglages de l'expérience au moment de l'acquisition.
"""
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

HEADER_BYTES = 4100
PIXEL_FORMATS = {
    "MonochromeUnsigned16": np.uint16,
    "MonochromeUnsigned32": np.uint32,
    "MonochromeFloating32": np.float32,
}


def _strip(tag):
    return tag.split("}")[-1]


def _find(root, *names):
    """Premier élément dont le chemin de balises (sans espace de noms) se termine par names."""
    for e in root.iter():
        if _strip(e.tag) == names[-1]:
            return e
    return None


class SpeFile:
    def __init__(self, path):
        self.path = Path(path)
        raw = self.path.read_bytes()
        xml_offset = struct.unpack_from("<Q", raw, 678)[0]
        if not HEADER_BYTES <= xml_offset <= len(raw):
            raise ValueError(f"{self.path.name} : pas de bloc XML (fichier SPE 2.x ?)")
        self.root = ET.fromstring(raw[xml_offset:])
        self.frames = self._read_data(raw[HEADER_BYTES:xml_offset])

    # ------------------------------------------------------------------ données
    def _read_data(self, data):
        fmt = _find(self.root, "DataFormat")
        frame_block = next(e for e in fmt if _strip(e.tag) == "DataBlock" and e.get("type") == "Frame")
        dtype = np.dtype(PIXEL_FORMATS[frame_block.get("pixelFormat")])
        n_frames = int(frame_block.get("count"))
        frame_size = int(frame_block.get("size"))
        stride = int(frame_block.get("stride", frame_size))
        regions = [(int(r.get("height")), int(r.get("width")), int(r.get("size")))
                   for r in frame_block if _strip(r.tag) == "DataBlock" and r.get("type") == "Region"]
        frames = []
        for k in range(n_frames):
            offset = k * stride
            frame = []
            for h, w, size in regions:
                frame.append(np.frombuffer(data, dtype, count=h * w, offset=offset).reshape(h, w))
                offset += size
            frames.append(frame)
        return frames  # frames[k][région] : tableau (lignes, colonnes)

    def region(self, index=0):
        """Toutes les images d'une région : tableau (cadres, lignes, colonnes)."""
        return np.stack([f[index] for f in self.frames])

    # ------------------------------------------------------------------ calibrations
    @property
    def wavelengths(self):
        """Longueur d'onde de chaque colonne (nm), ou None."""
        e = _find(self.root, "WavelengthMapping")
        if e is None:
            return None
        values = next((c for c in e if _strip(c.tag) in ("Wavelength", "WavelengthError")), None)
        if values is None or not (values.text or "").strip():
            return None
        return np.array([float(v) for v in values.text.split(",")])

    @property
    def wavelength_calibration(self):
        e = _find(self.root, "WavelengthMapping")
        return dict(e.attrib) if e is not None else {}

    def sensor_mapping(self):
        """Régions du capteur (x, y, largeur, hauteur, binning) telles qu'acquises."""
        cal = _find(self.root, "Calibrations")
        return [dict(c.attrib) for c in cal if _strip(c.tag) == "SensorMapping"] if cal is not None else []

    # ------------------------------------------------------------------ réglages
    def setting(self, *path):
        """Valeur texte d'un réglage, ex. setting('Grating', 'Selected') : premier élément dont le chemin de
        balises se termine par path (les appareils passent avant les options d'export)."""
        return setting(self.root, *path)

    def summary(self):
        s = self.setting
        return {
            "grating": s("Grating", "Selected"),
            "center_nm": _float(s("Grating", "CenterWavelength")),
            "exposure_ms": _float(s("ShutterTiming", "ExposureTime")),
            "frames_combined": s("FrameCombination", "FramesCombined"),
            "combination": s("FrameCombination", "Method"),
            "intensity_calibration": s("IntensityCalibration", "Enabled") == "True",
            "intensity_calibration_date": s("IntensityCalibration", "Date"),
            "step_and_glue": s("StepAndGlue", "Enabled") == "True",
            "step_centers_nm": [float(v) for v in (s("StepAndGlue", "CenterWavelengths") or "").split(",") if v],
            "background": s("BackgroundCorrection", "Enabled") == "True",
            "background_file": s("BackgroundCorrection", "ReferenceFile"),
            "flatfield": s("FlatfieldCorrection", "Enabled") == "True",
            "flatfield_file": s("FlatfieldCorrection", "ReferenceFile"),
            "cosmic": s("CosmicRayCorrection", "Enabled") == "True",
            "save_raw": s("SaveRawData") == "True",
            "wavelength_calibration_date": self.wavelength_calibration.get("date"),
            "laser_line_nm": _float(self.wavelength_calibration.get("laserLine")),
        }


def setting(root, *path):
    """Recherche d'un réglage par fin de chemin dans un arbre XML LightField (.spe ou .lfe)."""
    path = list(path)
    matches = []

    def walk(e, trail):
        trail = trail + [_strip(e.tag)]
        if trail[-len(path):] == path:
            matches.append((("Devices" not in trail), (e.text or "").strip()))
        for ch in e:
            walk(ch, trail)

    walk(root, [])
    if not matches:
        return None
    matches.sort(key=lambda m: m[0])  # éléments sous Devices en premier
    return matches[0][1]


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
