"""Pilote Thorlabs PM100D via la DLL VXIpnp PM100D_64.dll (ctypes).

Signatures reprises de C:\\Program Files\\IVI Foundation\\VISA\\Win64\\Include\\PM100D.h.
"""
import ctypes
import os
import time
from ctypes import byref, c_char_p, c_double, c_int16, c_int32, c_uint16, c_uint32, create_string_buffer

ViStatus = c_int32
ViSession = c_uint32
ViBoolean = c_uint16

VI_NULL = 0
ATTR_SET_VAL = 0
ATTR_MIN_VAL = 1
ATTR_MAX_VAL = 2
STAT_DARK_ADJUST_FINISHED = 0
POWER_UNIT_WATT = 0
BUFFER_SIZE = 256
ERR_DESCR_BUFFER_SIZE = 512

DEFAULT_DLL = r"C:\Program Files\IVI Foundation\VISA\Win64\Bin\PM100D_64.dll"


def format_power(watts):
    """Puissance avec l'unité adaptée (nW à W)."""
    for factor, unit in ((1.0, "W"), (1e-3, "mW"), (1e-6, "µW")):
        if abs(watts) >= factor:
            return f"{watts / factor:.4f} {unit}"
    return f"{watts * 1e9:.3f} nW"


class PM100DError(RuntimeError):
    pass


class PM100D:
    def __init__(self, dll_path=DEFAULT_DLL):
        dll_dir = os.path.dirname(dll_path)
        if dll_dir and hasattr(os, "add_dll_directory"):
            os.add_dll_directory(dll_dir)
        self._dll = ctypes.WinDLL(dll_path)
        self._handle = ViSession(0)
        self.resource = None

    # ------------------------------------------------------------------ outils
    def _check(self, status, handle=None):
        if status < 0:
            buf = create_string_buffer(ERR_DESCR_BUFFER_SIZE)
            h = self._handle if handle is None else handle
            self._dll.PM100D_errorMessage(h, ViStatus(status), buf)
            msg = buf.value.decode("mbcs", errors="replace")  # messages VISA dans la langue de Windows
            if status == -0x4000FFF1:  # VI_ERROR_RSRC_LOCKED
                msg += " — fermer Optical Power Meter Utility / Instrument Communicator"
            raise PM100DError(f"PM100D erreur {status:#x} : {msg}")
        return status

    # ------------------------------------------------------------------ découverte
    def find_resources(self):
        """Liste des wattmètres Thorlabs USB : (ressource VISA, modèle, n° de série, disponible).

        La DLL énumère aussi les ports série (ASRL...) : on ne garde que les ressources USB Thorlabs.
        """
        count = c_uint32(0)
        self._check(self._dll.PM100D_findRsrc(ViSession(VI_NULL), byref(count)), ViSession(VI_NULL))
        found = []
        for i in range(count.value):
            name = create_string_buffer(BUFFER_SIZE)
            model = create_string_buffer(BUFFER_SIZE)
            serial = create_string_buffer(BUFFER_SIZE)
            manuf = create_string_buffer(BUFFER_SIZE)
            avail = ViBoolean(0)
            self._check(self._dll.PM100D_getRsrcName(ViSession(VI_NULL), c_uint32(i), name), ViSession(VI_NULL))
            self._check(self._dll.PM100D_getRsrcInfo(ViSession(VI_NULL), c_uint32(i), model, serial, manuf,
                                                     byref(avail)), ViSession(VI_NULL))
            rsrc = name.value.decode()
            if rsrc.upper().startswith("USB") and "0X1313" in rsrc.upper():
                found.append((rsrc, model.value.decode(), serial.value.decode(), bool(avail.value)))
        return found

    # ------------------------------------------------------------------ connexion
    def open(self, resource=None, id_query=True, reset=False):
        if resource is None:
            res = self.find_resources()
            if not res:
                raise PM100DError("Aucun PM100D trouvé (câble USB ? utilitaire Thorlabs ouvert ?)")
            resource = res[0][0]
        self._check(self._dll.PM100D_init(c_char_p(resource.encode()), ViBoolean(id_query), ViBoolean(reset),
                                          byref(self._handle)), ViSession(VI_NULL))
        self.resource = resource
        self._check(self._dll.PM100D_setPowerUnit(self._handle, c_int16(POWER_UNIT_WATT)))
        return self

    def close(self):
        if self._handle.value:
            self._dll.PM100D_close(self._handle)
            self._handle = ViSession(0)

    def __enter__(self):
        return self if self._handle.value else self.open()

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------ réglages
    def sensor_info(self):
        name = create_string_buffer(BUFFER_SIZE)
        snr = create_string_buffer(BUFFER_SIZE)
        msg = create_string_buffer(BUFFER_SIZE)
        typ, styp, flags = c_int16(), c_int16(), c_int16()
        self._check(self._dll.PM100D_getSensorInfo(self._handle, name, snr, msg,
                                                   byref(typ), byref(styp), byref(flags)))
        return {"name": name.value.decode(), "serial": snr.value.decode(), "message": msg.value.decode(),
                "type": typ.value, "subtype": styp.value, "flags": flags.value}

    def _get_double(self, func, attribute=ATTR_SET_VAL):
        v = c_double()
        self._check(getattr(self._dll, func)(self._handle, c_int16(attribute), byref(v)))
        return v.value

    @property
    def wavelength_nm(self):
        return self._get_double("PM100D_getWavelength")

    @wavelength_nm.setter
    def wavelength_nm(self, value):
        self._check(self._dll.PM100D_setWavelength(self._handle, c_double(value)))

    def wavelength_range_nm(self):
        return (self._get_double("PM100D_getWavelength", ATTR_MIN_VAL),
                self._get_double("PM100D_getWavelength", ATTR_MAX_VAL))

    @property
    def avg_time_s(self):
        return self._get_double("PM100D_getAvgTime")

    @avg_time_s.setter
    def avg_time_s(self, value):
        self._check(self._dll.PM100D_setAvgTime(self._handle, c_double(value)))

    def set_auto_range(self, enabled=True):
        self._check(self._dll.PM100D_setPowerAutoRange(self._handle, ViBoolean(enabled)))

    def zero(self, timeout_s=60.0):
        """Ajustement du zéro (dark). Le faisceau doit être bloqué. Retourne l'offset (W)."""
        self._check(self._dll.PM100D_startDarkAdjust(self._handle))
        state = c_int16(1)
        t0 = time.monotonic()
        while True:
            self._check(self._dll.PM100D_getDarkAdjustState(self._handle, byref(state)))
            if state.value == STAT_DARK_ADJUST_FINISHED:
                break
            if time.monotonic() - t0 > timeout_s:
                self._dll.PM100D_cancelDarkAdjust(self._handle)
                raise PM100DError("Ajustement du zéro trop long, annulé")
            time.sleep(0.2)
        offset = c_double()
        self._check(self._dll.PM100D_getDarkOffset(self._handle, byref(offset)))
        return offset.value

    # ------------------------------------------------------------------ mesure
    def power_w(self):
        v = c_double()
        self._check(self._dll.PM100D_measPower(self._handle, byref(v)))
        return v.value
