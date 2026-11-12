"""Pilote LightField (Teledyne Princeton Instruments) via le SDK Automation et pythonnet.

Important : l'objet Automation doit être créé, utilisé et libéré dans le même thread.
Dans l'interface graphique, toute la classe vivra dans un thread dédié.
LightField doit être fermé avant open() : l'automatisation lance sa propre instance.
"""
import ctypes
import os
import re
import sys
import time

import numpy as np

_clr_loaded = False


def _load_assemblies():
    global _clr_loaded
    if _clr_loaded:
        return
    import clr  # pythonnet

    root = os.environ.get("LIGHTFIELD_ROOT")
    if not root:
        raise RuntimeError("Variable d'environnement LIGHTFIELD_ROOT absente (LightField installé ?)")
    sys.path.append(root)
    sys.path.append(os.path.join(root, "AddInViews"))
    clr.AddReference("PrincetonInstruments.LightFieldViewV5")
    clr.AddReference("PrincetonInstruments.LightField.AutomationV5")
    clr.AddReference("PrincetonInstruments.LightFieldAddInSupportServices")
    _clr_loaded = True


def raman_shift(wavelength_nm, laser_nm):
    """Décalage Raman (cm⁻¹) à partir de la longueur d'onde absolue (nm)."""
    return 1e7 / laser_nm - 1e7 / np.asarray(wavelength_nm, dtype=float)


def format_grating(raw):
    """Libellé lisible d'un réseau : '[500nm,300][2][0]' → '300 traits/mm, blaze 500 nm'."""
    m = re.match(r"\[\s*([^,\]]+?)\s*,\s*(\d+)\s*\]", raw)
    if not m:
        return raw
    blaze = re.sub(r"(?<=\d)(?=[a-zA-Zµ])", " ", m.group(1))
    return f"{m.group(2)} traits/mm, blaze {blaze}"


class LightFieldSpectrometer:
    def __init__(self, visible=True, experiment=None):
        self.visible = visible
        self.experiment_name = experiment
        self._auto = None
        self.exp = None

    # ------------------------------------------------------------------ connexion
    def open(self):
        _load_assemblies()
        from System import String
        from System.Collections.Generic import List
        from PrincetonInstruments.LightField.Automation import Automation

        self._auto = Automation(self.visible, List[String]())
        self.exp = self._auto.LightFieldApplication.Experiment
        if self.experiment_name:
            if not self.exp.Load(self.experiment_name):
                raise RuntimeError(f"Impossible de charger l'expérience LightField '{self.experiment_name}'")
        return self

    def close(self):
        if self._auto is not None:
            self._auto.Dispose()
        self._auto = None
        self.exp = None

    def __enter__(self):
        return self if self._auto is not None else self.open()

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------ réglages
    @staticmethod
    def _keys():
        from PrincetonInstruments.LightField.AddIns import (
            CameraSettings, ExperimentSettings, SpectrometerSettings)
        return CameraSettings, ExperimentSettings, SpectrometerSettings

    def get(self, key, default=None):
        return self.exp.GetValue(key) if self.exp.Exists(key) else default

    def set(self, key, value):
        """SetValue en essayant les types .NET plausibles (LightField est strict sur le type)."""
        from System import Double, Int32, Int64

        if not self.exp.Exists(key):
            raise KeyError(f"Réglage inexistant dans l'expérience : {key}")
        if isinstance(value, bool):
            candidates = [value]
        elif isinstance(value, int):
            candidates = [Int32(value), Int64(value), Double(value)]
        elif isinstance(value, float):
            candidates = [Double(value)]
        else:
            candidates = [value]
        last = None
        for v in candidates:
            try:
                self.exp.SetValue(key, v)
                return
            except Exception as e:  # type .NET refusé, on essaie le suivant
                last = e
        raise last

    @property
    def exposure_ms(self):
        cam, _, _ = self._keys()
        return float(self.get(cam.ShutterTimingExposureTime))

    @exposure_ms.setter
    def exposure_ms(self, value):
        cam, _, _ = self._keys()
        self.set(cam.ShutterTimingExposureTime, float(value))

    @property
    def accumulations(self):
        cam, _, _ = self._keys()
        return int(self.get(cam.ReadoutControlAccumulations, 1))

    @accumulations.setter
    def accumulations(self, value):
        cam, _, _ = self._keys()
        self.set(cam.ReadoutControlAccumulations, int(value))

    @property
    def center_wavelength_nm(self):
        _, _, spec = self._keys()
        return float(self.get(spec.GratingCenterWavelength))

    @center_wavelength_nm.setter
    def center_wavelength_nm(self, value):
        _, _, spec = self._keys()
        self.set(spec.GratingCenterWavelength, float(value))

    def _wait_updating(self, timeout_s=120.0):
        """Attend la fin d'un changement matériel (tourelle du spectromètre, etc.)."""
        t0 = time.monotonic()
        while self.exp.IsUpdating:
            if time.monotonic() - t0 > timeout_s:
                raise RuntimeError("LightField : mise à jour trop longue")
            time.sleep(0.05)

    def gratings(self):
        """Réseaux disponibles, au format LightField : '[500nm,300][2][0]'."""
        _, _, spec = self._keys()
        return [str(g) for g in self.exp.GetCurrentCapabilities(spec.GratingSelected)]

    @property
    def grating(self):
        _, _, spec = self._keys()
        return str(self.get(spec.GratingSelected))

    @grating.setter
    def grating(self, value):
        _, _, spec = self._keys()
        self.set(spec.GratingSelected, value)
        self._wait_updating()

    def devices(self):
        return [(str(d.Type), str(d.Model), str(d.SerialNumber), bool(d.IsConnected))
                for d in self.exp.ExperimentDevices]

    def is_ready(self):
        return bool(self.exp.IsReadyToRun)

    def wavelengths(self):
        """Axe en longueur d'onde (nm) d'après la calibration LightField (vide si pas de spectromètre)."""
        axis = self.exp.SystemColumnCalibration
        return np.array(list(axis) if axis is not None else [], dtype=float)

    def intensity_correction(self):
        """Courbe IntelliCal appliquée (vide si aucune)."""
        curve = self.exp.SystemIntensityCalibration  # None si aucune correction chargée
        return np.array(list(curve) if curve is not None else [], dtype=float)

    def metadata(self):
        cam, exps, spec = self._keys()
        keys = {
            "exposure_ms": cam.ShutterTimingExposureTime,
            "accumulations": cam.ReadoutControlAccumulations,
            "center_wavelength_nm": spec.GratingCenterWavelength,
            "grating": spec.GratingSelected,
            "intensity_calibration_enabled": exps.IntensityCalibrationEnabled,
            "intensity_calibration_date": exps.IntensityCalibrationDate,
            "wavelength_calibration_date": exps.WavelengthCalibrationDate,
            "background_correction_enabled": exps.OnlineCorrectionsBackgroundCorrectionEnabled,
            "cosmic_ray_correction_enabled": exps.OnlineCorrectionsCosmicRayCorrectionEnabled,
        }
        md = {"experiment": str(self.exp.Name)}
        for name, key in keys.items():
            v = self.get(key)
            md[name] = v if isinstance(v, (int, float, bool)) or v is None else str(v)
        return md

    # ------------------------------------------------------------------ acquisition
    @staticmethod
    def _to_numpy(net_array, image_format):
        from System.Runtime.InteropServices import GCHandle, GCHandleType
        from PrincetonInstruments.LightField.AddIns import ImageDataFormat

        ctype = {
            ImageDataFormat.MonochromeUnsigned16: ctypes.c_ushort,
            ImageDataFormat.MonochromeUnsigned32: ctypes.c_uint,
            ImageDataFormat.MonochromeFloating32: ctypes.c_float,
        }[image_format]
        handle = GCHandle.Alloc(net_array, GCHandleType.Pinned)
        try:
            ptr = handle.AddrOfPinnedObject().ToInt64()
            buf = (ctype * len(net_array)).from_address(ptr)
            return np.frombuffer(buf, dtype=buf._type_).copy()
        finally:
            if handle.IsAllocated:
                handle.Free()

    def capture(self, frames=1, roi=0):
        """Acquisition synchrone. Retourne un tableau (frames, hauteur, largeur)."""
        from System import Int32
        _, exps, _ = self._keys()

        if not self.is_ready():
            raise RuntimeError("LightField n'est pas prêt (IsReadyToRun = False) : vérifier les erreurs dans LightField")
        self.exp.SetValue(exps.AcquisitionFramesToStore, Int32(frames))
        dataset = self.exp.Capture(frames)
        try:
            if dataset.Frames != frames:
                raise RuntimeError(f"{dataset.Frames} trames reçues au lieu de {frames}")
            out = []
            for i in range(frames):
                frame = dataset.GetFrame(roi, i)
                data = self._to_numpy(frame.GetData(), frame.Format)
                out.append(data.reshape(frame.Height, frame.Width))
            return np.stack(out)
        finally:
            dataset.Dispose()

    def acquire_image(self):
        """Une trame complète du capteur → (image (lignes, colonnes), durée s)."""
        t0 = time.perf_counter()
        img = self.capture(1)[0]
        return img, time.perf_counter() - t0

    def acquire_spectrum(self):
        """Une trame, sommée sur les lignes du capteur → (longueurs d'onde nm, spectre, durée s)."""
        img, dt = self.acquire_image()
        return self.wavelengths(), img.sum(axis=0).astype(float), dt
