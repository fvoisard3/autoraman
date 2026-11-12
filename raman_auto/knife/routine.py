"""Routine knife-edge : carte → bord et orientation → lignes perpendiculaires au bord → balayage en Z.

1. Carte rectangulaire du signal → ajustement d'un bord droit (position, orientation, largeur grossière).
2. Ligne perpendiculaire au bord, centrée sur le bord → profil erf → rayon w à 1/e².
3. Une ligne par hauteur Z (recentrée sur le bord trouvé à la ligne précédente, longueur et pas adaptés
   au spot) → w(z) → caustique : foyer z0, waist w0, longueur de Rayleigh zR.
   Option : second passage plus fin autour du minimum, puis retour au foyer.

Signaux :
  - "power"  : puissance transmise à côté d'une lame de rasoir (wattmètre sous la lame) ;
  - "raman"  : aire du pic du Si (≈ 520 cm⁻¹) sur un bord clivé de wafer : résolution effective
               excitation × collection de la sonde ;
  - "laser"  : aire de la raie laser résiduelle (≈ 0 cm⁻¹, le filtre n'est pas parfait), réfléchie par le wafer.
Pour les signaux spectraux, chaque point est une image complète de la caméra (réglages courants du spectromètre :
réseau, centre, exposition), sommée sur les lignes ; la valeur est l'aire dans la fenêtre spectrale, avec fond
linéaire tiré entre les bords de la fenêtre. Les spectres sont enregistrés avec chaque point.

La routine est écrite comme un générateur : chaque appel d'appareil est « cédé » (yield) et exécuté
dans le DeviceThread de l'appareil ; le résultat est renvoyé au générateur. Tout l'enchaînement reste
dans le thread de l'interface, ce qui permet l'arrêt propre entre deux mesures.
"""
import datetime
import json
import math
import time
import traceback
from dataclasses import asdict, dataclass, field

import h5py
import numpy as np
from PySide6.QtCore import QObject, QTimer, Signal

from ..analysis.integration import Window, evaluate
from ..analysis.runfile import raman_shift
from ..core.zones import ACQUISITION_DEAD_TIME_S, MIN_STEP_MM, MOVE_TIME_S, POWER_READ_S
from .fit import edge_point_near, fit_caustic, fit_edge_map, fit_line

TAGS = {"stage": "scan_move", "power": "scan_power", "spectro": "scan_acquire"}
MAX_LINE_POINTS = 400
SATURATION = 65000  # counts par pixel (caméra 16 bits)
SIGNALS = {"power": "Wattmètre (puissance transmise)",
           "raman": "Raman — pic du Si",
           "laser": "Raie laser résiduelle"}
DEFAULT_WINDOWS = {"raman": (495.0, 545.0), "laser": (-40.0, 40.0)}


@dataclass
class KnifeParams:
    # carte de recherche du bord
    x0: float = 0.0
    y0: float = 0.0
    x1: float = 0.0
    y1: float = 0.0
    map_step: float = 0.5
    # lignes (0 = automatique)
    line_length: float = 0.0
    line_step: float = 0.0
    readings: int = 1                    # lectures moyennées par point
    # hauteurs
    z_values: list = field(default_factory=list)   # vide : une seule ligne au Z actuel
    refine: bool = True                  # second passage fin autour du minimum
    goto_focus: bool = True              # finir au foyer trouvé
    reuse_edge: dict = None              # bord d'une mesure précédente : pas de carte
    signal: str = "power"                # "power", "raman" ou "laser"
    laser_nm: float = 514.5
    # signaux spectraux
    window_lo: float = 495.0             # cm⁻¹
    window_hi: float = 545.0
    baseline: bool = True
    exposure_ms: float = 1000.0          # pour l'estimation de durée (réglage du spectromètre)

    @property
    def spectral(self):
        return self.signal in ("raman", "laser")

    def point_s(self):
        if self.spectral:
            return MOVE_TIME_S + self.readings * (self.exposure_ms / 1000.0 + ACQUISITION_DEAD_TIME_S)
        return MOVE_TIME_S + POWER_READ_S * self.readings

    def map_points(self):
        xs = _axis(self.x0, self.x1, self.map_step)
        ys = _axis(self.y0, self.y1, self.map_step)
        rows = [np.column_stack((xs[::-1] if i % 2 else xs, np.full(xs.size, y))) for i, y in enumerate(ys)]
        return np.vstack(rows)

    def estimate_s(self):
        per_point = self.point_s()
        n_map = 0 if self.reuse_edge else len(self.map_points())
        n_lines = max(len(self.z_values), 1) + (6 if self.refine and len(self.z_values) >= 4 else 0)
        return per_point * (n_map + n_lines * 80) + 2.0 * n_lines


def actual_xy(target, read):
    """Meilleure estimation de la position XY atteinte.

    M114 ne donne que 0,01 mm, alors que le moteur se place sur des pas exacts de 12,5 µm : si la lecture
    concorde avec la cible arrondie au pas moteur, c'est cette valeur qui est la bonne (erreur de lecture
    jusqu'à ±5 µm sinon, ce qui élargit ou rétrécit artificiellement les profils de quelques µm).
    """
    target = np.asarray(target, float)
    read = np.asarray(read, float)[:2]
    snapped = np.round(target / MIN_STEP_MM) * MIN_STEP_MM
    return snapped if np.all(np.abs(snapped - read) <= 0.0075) else read


def _axis(a, b, step):
    lo, hi = min(a, b), max(a, b)
    return lo + np.arange(math.floor((hi - lo) / step + 1e-9) + 1) * step


class Stopped(Exception):
    pass


class RoutineError(Exception):
    pass


class DeviceError(Exception):
    pass


# ====================================================================== enregistrement
class KnifeRecorder:
    """Fichier HDF5 d'une mesure knife-edge, vidé sur disque après chaque point.

    /map        target_mm (N,2), position_mm (N,3), signal (N) ; attributs : ajustement du bord
    /lines/000  z_mm, center_mm, normal, target_mm, position_mm, s_mm, signal ; attributs : ajustement erf
    /caustic    z_mm, w_mm, w_err_mm, s0_mm, ok (une ligne par profil) ; attributs racine focus_* : caustique
    """

    def __init__(self, path, params, attrs):
        self.path = path
        self.f = h5py.File(path, "w")
        self.f.attrs["started"] = datetime.datetime.now().isoformat(timespec="seconds")
        self.f.attrs["params"] = json.dumps(asdict(params), ensure_ascii=False, default=str)
        for k, v in attrs.items():
            self.f.attrs[k] = v
        self.map = self.f.create_group("map")
        self.lines = self.f.create_group("lines")
        self.rows = []

    @staticmethod
    def _append(group, name, value, dtype="f8"):
        value = np.asarray(value, dtype)
        if name not in group:
            chunk = 64 if value.size < 64 else 8
            group.create_dataset(name, shape=(0,) + value.shape, maxshape=(None,) + value.shape, dtype=dtype,
                                 chunks=(chunk,) + value.shape, compression="lzf" if value.size > 64 else None)
        ds = group[name]
        ds.resize(ds.shape[0] + 1, axis=0)
        ds[-1] = value

    def set_spectral(self, wavelengths, shift, metadata, window):
        """Axe spectral et réglages du spectromètre (signaux Raman / raie laser)."""
        self.f.create_dataset("wavelength_nm", data=np.asarray(wavelengths, float))
        self.f.create_dataset("raman_shift_cm-1", data=np.asarray(shift, float))
        for k, v in metadata.items():
            self.f.attrs[f"lightfield_{k}"] = v if isinstance(v, (bool, int, float, str, np.number)) else str(v)
        self.f.attrs["window_cm-1"] = [window.lo, window.hi]
        self.f.attrs["window_baseline"] = window.baseline
        self.f.flush()

    def _spectrum(self, group, spectrum):
        if spectrum is not None:
            self._append(group, "spectra", spectrum, "f4")

    def map_point(self, target, position, signal, spectrum=None):
        for name, v in (("target_mm", target), ("position_mm", position), ("signal", signal)):
            self._append(self.map, name, v)
        self._spectrum(self.map, spectrum)
        self.f.flush()

    def map_fit(self, fit):
        for k, v in fit.items():
            self.map.attrs[k] = v if v is not None else ""
        self.f.flush()

    def new_line(self, index, z, center, normal):
        g = self.lines.create_group(f"{index:03d}")
        g.attrs.update(z_mm=z, center_mm=center, normal=normal)
        return g

    def line_point(self, g, target, position, s, signal, spectrum=None):
        for name, v in (("target_mm", target), ("position_mm", position), ("s_mm", s), ("signal", signal)):
            self._append(g, name, v)
        self._spectrum(g, spectrum)
        self.f.flush()

    def line_fit(self, g, fit, z):
        for k, v in fit.items():
            g.attrs[k] = v
        self.rows.append((z, fit["w"] if fit["ok"] else math.nan, fit["w_err"], fit["s0"], fit["ok"]))
        self.f.flush()

    def close(self, caustic=None, **attrs):
        if not self.f:
            return
        if self.rows:
            table = np.array(self.rows, dtype=float)
            for i, name in enumerate(("z_mm", "w_mm", "w_err_mm", "s0_mm", "ok")):
                self.f.create_dataset(f"caustic/{name}", data=table[:, i])
        for k, v in (caustic or {}).items():
            self.f.attrs[f"focus_{k}"] = v
        self.f.attrs["finished"] = datetime.datetime.now().isoformat(timespec="seconds")
        for k, v in attrs.items():
            self.f.attrs[k] = v
        self.f.close()


# ====================================================================== routine
class KnifeEdgeRoutine(QObject):
    status = Signal(str)
    map_started = Signal(object)              # points prévus (N, 2)
    map_point = Signal(float, float, float)   # x, y, signal
    map_done = Signal(object)                 # ajustement du bord
    line_started = Signal(int, float, object, object)  # n° de ligne, z, centre, normale
    line_point = Signal(float, float)         # s, signal
    line_done = Signal(int, float, object, object, object)  # n°, z, ajustement, s, signal
    caustic_done = Signal(object, object, object)  # z, w, ajustement
    spectrum = Signal(object, object, bool)   # décalage Raman, dernier spectre, saturé
    finished = Signal(str, object)            # message, résultat (dict)

    def __init__(self, stage_thread, power_thread, stage, pm, params, make_recorder,
                 spectro_thread=None, spectro=None):
        super().__init__()
        self.status_text = "Knife-edge en cours"
        self.threads = {"stage": stage_thread}
        if params.spectral:
            self.threads["spectro"] = spectro_thread
        else:
            self.threads["power"] = power_thread
        self.stage, self.pm, self.spectro, self.params = stage, pm, spectro, params
        self.make_recorder = make_recorder
        self.recorder = None
        self.result = {}
        self.shift = None
        self.window = Window(SIGNALS.get(params.signal, params.signal), params.window_lo, params.window_hi,
                             unit="cm-1", measure="aire", baseline=params.baseline)
        self.saturated_points = 0
        self._gen = None
        self._expect = None
        self._stop = False
        self._done = False
        for thread in self.threads.values():
            thread.done.connect(self._on_done)
            thread.failed.connect(self._on_failed)

    # ------------------------------------------------------------------ pilotage
    def start(self):
        self._gen = self._run()
        self._advance(None)

    def stop(self):
        self._stop = True
        self.status.emit("Arrêt demandé : fin après la mesure en cours.")

    def abort(self, message):
        """Fermeture de l'application : on termine tout de suite (le fichier est fermé proprement)."""
        if self._gen is not None and not self._done:
            try:
                self._gen.throw(Stopped())
            except (Stopped, StopIteration, Exception):
                pass
        self._finish(message)

    def _advance(self, value, exc=None):
        if self._done:
            return
        if self._stop and exc is None:
            exc = Stopped()
        try:
            request = self._gen.throw(exc) if exc is not None else self._gen.send(value)
        except StopIteration as s:
            self._finish(s.value or "Knife-edge terminé.")
            return
        except Stopped:
            self._finish("Knife-edge arrêté.")
            return
        except (RoutineError, DeviceError) as e:
            self._finish(f"Knife-edge interrompu : {e}")
            return
        except Exception as e:  # erreur de programmation : la garder visible
            traceback.print_exc()
            self._finish(f"Knife-edge interrompu par une erreur : {e!r}")
            return
        device, fn = request
        self._expect = TAGS[device]
        self.threads[device].submit(self._expect, fn)

    def _on_done(self, tag, result):
        if tag == self._expect and not self._done:
            self._expect = None
            QTimer.singleShot(0, lambda: self._advance(result))

    def _on_failed(self, tag, message):
        if tag == self._expect and not self._done:
            self._expect = None
            QTimer.singleShot(0, lambda: self._advance(None, DeviceError(message)))

    def _finish(self, message):
        if self._done:
            return
        self._done = True
        for thread in self.threads.values():
            thread.done.disconnect(self._on_done)
            thread.failed.disconnect(self._on_failed)
        if self.recorder is not None:
            self.recorder.close(self.result.get("caustic"), end_message=message)
            message = message.rstrip(". ") + f". Fichier : {self.recorder.path}"
        self.finished.emit(message, self.result)

    # ------------------------------------------------------------------ requêtes aux appareils
    def _position(self):
        stage = self.stage
        return "stage", stage.position

    def _move(self, **target):
        stage = self.stage

        def job():
            stage.move_precise(**target)
            return stage.position()
        return "stage", job

    def _measure(self):
        """Requête de mesure : le résultat est (t, valeur, spectre ou None, saturé)."""
        n = max(1, int(self.params.readings))
        if not self.params.spectral:
            pm = self.pm

            def job():
                values = [pm.power_w() for _ in range(n)]
                return time.monotonic(), float(np.mean(values))
            return "power", job

        spectro, shift, window = self.spectro, self.shift, self.window

        def job():
            spectrum, saturated = None, False
            for _ in range(n):  # n acquisitions additionnées
                img, _dt = spectro.acquire_image()
                saturated |= bool(img.max() >= SATURATION)
                s = img.sum(axis=0, dtype=np.float64)
                spectrum = s if spectrum is None else spectrum + s
            value = float(evaluate(shift, spectrum, window)[0][0])
            return time.monotonic(), value, spectrum, saturated
        return "spectro", job

    def _measured(self, result):
        """Valeur et spectre d'un résultat de mesure ; signale la saturation de la caméra."""
        if len(result) == 2:
            return result[1], None
        _, value, spectrum, saturated = result
        if saturated:
            self.saturated_points += 1
            if self.saturated_points == 1:
                self.status.emit("Attention : caméra saturée (≥ 65000 counts) — réduire l'exposition.")
        self.spectrum.emit(self.shift, spectrum, saturated)
        return value, spectrum

    # ------------------------------------------------------------------ déroulement
    def _run(self):
        P = self.params
        pos, counts = yield self._position()
        z_start = (counts or pos)[2]
        self.result = {"z_start": z_start}
        if P.spectral:
            spectro = self.spectro
            wavelengths, metadata = yield "spectro", lambda: (spectro.wavelengths(), spectro.metadata())
            self.shift = raman_shift(wavelengths, P.laser_nm)
            lo, hi = float(np.min(self.shift)), float(np.max(self.shift))
            if not (lo <= self.window.lo and self.window.hi <= hi):
                raise RoutineError(f"fenêtre {self.window.lo:g}–{self.window.hi:g} cm⁻¹ hors de l'axe du "
                                   f"spectromètre ({lo:.0f} à {hi:.0f} cm⁻¹) : changer le centre ou le réseau")
        self.recorder = self.make_recorder(P)  # fichier créé seulement une fois les réglages vérifiés
        if P.spectral:
            self.recorder.set_spectral(wavelengths, self.shift, metadata, self.window)

        # ---- 1. carte
        if P.reuse_edge:
            edge = dict(P.reuse_edge)
            self.status.emit("Bord repris de la mesure précédente.")
        else:
            points = P.map_points()
            self.map_started.emit(points)
            xy, values = [], []
            for i, (x, y) in enumerate(points):
                self.status.emit(f"Carte : point {i + 1}/{len(points)}")
                pos, counts = yield self._move(x=float(x), y=float(y))
                real = counts or pos
                v, spectrum = self._measured((yield self._measure()))
                xy.append(actual_xy((x, y), real))
                values.append(v)
                self.recorder.map_point((x, y), real, v, spectrum)
                self.map_point.emit(float(real[0]), float(real[1]), v)
            edge = fit_edge_map(np.array(xy), np.array(values))
            edge["center_hint"] = [float((P.x0 + P.x1) / 2), float((P.y0 + P.y1) / 2)]
            self.recorder.map_fit({k: v for k, v in edge.items() if not isinstance(v, list)})
            self.map_done.emit(edge)
            if not edge["ok"]:
                raise RoutineError(f"bord non trouvé sur la carte ({edge['message']})")
            self.status.emit(f"Bord trouvé : direction {edge['edge_angle_deg']:.1f}°, "
                             f"largeur grossière {edge['w'] * 1000:.0f} µm")
        self.result["edge"] = edge
        normal = np.array([math.cos(math.radians(edge["theta_deg"])), math.sin(math.radians(edge["theta_deg"]))])
        center = edge_point_near(edge, edge.get("center_hint") or [(P.x0 + P.x1) / 2, (P.y0 + P.y1) / 2])

        # ---- 2. et 3. lignes, une par hauteur
        map_step = P.map_step if not P.reuse_edge else edge.get("map_step", 0.5)
        length = P.line_length or max(8 * edge["w"], 2 * map_step, 0.2)
        step = P.line_step or max(length / 80, MIN_STEP_MM)
        z_list = list(P.z_values) or [z_start]
        measured = []  # (z, ajustement)
        line_index = 0
        for pass_index in range(2):
            for z in z_list:
                if any(abs(z - mz) < 1e-6 for mz, _ in measured):
                    continue
                if P.z_values:
                    self.status.emit(f"Déplacement à Z = {z:.3f} mm")
                    yield self._move(z=float(z))
                fit = None
                for attempt in range(2):
                    fit, center_used, s, values = yield from self._line(line_index, z, center, normal, length, step)
                    line_index += 1
                    retry = (not fit["ok"] and math.isfinite(fit["w"]) and P.line_length == 0
                             and ("extrémité" in fit["message"] or "courte" in fit["message"]))
                    if not retry:
                        break
                    if math.isfinite(fit["s0"]) and abs(fit["s0"]) < length:
                        center = center + fit["s0"] * normal
                    length = min(2 * max(length, 6 * fit["w"]), 20.0)
                    step = P.line_step or max(length / 80, MIN_STEP_MM)
                    self.status.emit(f"Ligne recommencée plus longue ({length:.2f} mm)")
                measured.append((z, fit))
                if fit["ok"]:
                    center = center + fit["s0"] * normal  # recentrer sur le bord
                    if P.line_length == 0:
                        length = min(max(10 * fit["w"], 0.15), 20.0)
                    if P.line_step == 0:
                        step = min(max(fit["w"] / 5, MIN_STEP_MM), length / 30)
                zs = np.array([m[0] for m in measured])
                ws = np.array([m[1]["w"] if m[1]["ok"] else math.nan for m in measured])
                caustic = fit_caustic(zs, ws, P.laser_nm) if P.z_values else None
                self.result["caustic"] = caustic
                self.result["lines"] = [(float(a), float(b)) for a, b in zip(zs, ws)]
                self.caustic_done.emit(zs, ws, caustic)
            # second passage fin autour du minimum
            if pass_index == 1 or not (P.refine and P.z_values and len(P.z_values) >= 4):
                break
            caustic = self.result.get("caustic") or {}
            if not math.isfinite(caustic.get("z0", math.nan)):
                break
            dz = float(np.median(np.abs(np.diff(sorted(P.z_values)))))
            z_list = [round(caustic["z0"] + k * dz / 4, 4) for k in range(-6, 7)]
            # On reste dans la plage validée par l'opérateur (pas de Z plus proche de la sonde que prévu).
            z_list = [z for z in z_list if min(P.z_values) <= z <= max(P.z_values)]
            self.status.emit(f"Passage fin autour de Z = {caustic['z0']:.3f} mm (pas {dz / 4:.3f} mm)")

        # ---- fin : retour au foyer (ou à la hauteur de départ)
        caustic = self.result.get("caustic")
        z_end = z_start
        if caustic and P.goto_focus and math.isfinite(caustic.get("z0", math.nan)):
            z_end = round(caustic["z0"], 3)
        if P.z_values:
            self.status.emit(f"Retour à Z = {z_end:.3f} mm")
            yield self._move(z=float(z_end))
        yield self._move(x=float(center[0]), y=float(center[1]))
        self.result["center"] = [float(center[0]), float(center[1])]
        self.result["z_end"] = z_end
        return self._summary()

    def _line(self, index, z, center, normal, length, step):
        n = min(int(round(length / step)) + 1, MAX_LINE_POINTS)
        s_target = np.linspace(-length / 2, length / 2, n)
        targets = center + np.outer(s_target, normal)
        g = self.recorder.new_line(index, z, center, normal)
        self.line_started.emit(index, z, center.copy(), normal.copy())
        s_list, values = [], []
        for i, (x, y) in enumerate(targets):
            self.status.emit(f"Ligne {index + 1} (Z = {z:.3f} mm) : point {i + 1}/{n}, pas {step * 1000:.1f} µm")
            pos, counts = yield self._move(x=float(x), y=float(y))
            real = np.asarray(counts or pos, float)
            v, spectrum = self._measured((yield self._measure()))
            s = float((actual_xy((x, y), real) - center) @ normal)  # position atteinte projetée sur la normale
            s_list.append(s)
            values.append(v)
            self.recorder.line_point(g, (x, y), real, s, v, spectrum)
            self.line_point.emit(s, v)
        fit = fit_line(np.array(s_list), np.array(values))
        self.recorder.line_fit(g, fit, z)
        self.line_done.emit(index, z, fit, np.array(s_list), np.array(values))
        return fit, center, s_list, values

    def _summary(self):
        text = self._summary_core()
        if self.saturated_points:
            text += f" Attention : {self.saturated_points} points avec la caméra saturée."
        return text

    def _summary_core(self):
        caustic = self.result.get("caustic")
        lines = self.result.get("lines") or []
        if caustic and math.isfinite(caustic.get("w0", math.nan)):
            text = (f"Foyer Z = {caustic['z0']:.3f} mm, waist w0 = {caustic['w0'] * 1000:.1f} µm "
                    f"(diamètre 1/e² {2 * caustic['w0'] * 1000:.1f} µm)")
            if math.isfinite(caustic.get("zr", math.nan)):
                text += f", zR = {caustic['zr']:.3f} mm"
            if not caustic["ok"]:
                text += f" — {caustic['message']}"
            return text + "."
        if lines and math.isfinite(lines[-1][1]):
            return f"Rayon du spot w = {lines[-1][1] * 1000:.1f} µm (diamètre 1/e² {2 * lines[-1][1] * 1000:.1f} µm)."
        return "Knife-edge terminé, sans largeur valide."
