"""Exécution des zones : pour chaque point, déplacement → mesures cochées dans la zone → sauvegarde.

Mesures par point selon la zone : image complète de la caméra (« Raman ») et/ou lecture du wattmètre
(« Wattmètre ») ; quand les deux sont demandées, la lecture du wattmètre se fait pendant l'exposition.
Les appels aux appareils passent par leurs DeviceThread (étiquettes "scan_…") ; l'enchaînement est piloté
depuis le thread de l'interface, ce qui permet pause et arrêt entre deux points.
"""
import math
import time

import numpy as np
from PySide6.QtCore import QObject, Signal


class ScanRunner(QObject):
    progress = Signal(int, int, float)               # points faits, total, temps restant estimé (s)
    point_done = Signal(int, float, float, object)   # indice, x, y, spectre (somme des lignes) ou None
    finished = Signal(str)

    def __init__(self, stage_thread, spectro_thread, power_thread, stage, spectro, pm, zones, make_writer):
        super().__init__()
        self.status_text = "Exécution des zones en cours"
        self.stage_thread, self.spectro_thread, self.power_thread = stage_thread, spectro_thread, power_thread
        self.stage, self.spectro, self.pm = stage, spectro, pm
        self.points, self.zone_index = scan_points(zones)
        self.do_raman = np.array([zones[i].raman for i in self.zone_index], dtype=bool)
        self.do_power = np.array([zones[i].power for i in self.zone_index], dtype=bool)
        self.make_writer = make_writer
        self.writer = None
        self.wavelengths = None
        self.i = 0
        self.paused = False
        self._stop = False
        self._waiting = False
        self._done = False
        self._position = None
        self._pending = set()
        self._results = {}
        self._t0 = None
        self._threads = [(stage_thread, self._on_stage_done)]
        if self.do_raman.any():
            self._threads.append((spectro_thread, self._on_spectro_done))
        if self.do_power.any():
            self._threads.append((power_thread, self._on_power_done))
        for thread, slot in self._threads:
            thread.done.connect(slot)
            thread.failed.connect(self._on_failed)

    # ------------------------------------------------------------------ commandes
    def start(self):
        if self.do_raman.any():
            spectro = self.spectro
            self.spectro_thread.submit("scan_prepare", lambda: (spectro.wavelengths(), spectro.metadata()))
        else:
            self._open_writer(np.empty(0), {})

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False
        if self._waiting:
            self._waiting = False
            self._next()

    def stop(self):
        self._stop = True
        if self._waiting:
            self._waiting = False
            self._next()

    # ------------------------------------------------------------------ enchaînement
    def _open_writer(self, wavelengths, metadata):
        self.wavelengths = wavelengths
        try:
            self.writer = self.make_writer(wavelengths, metadata)
        except Exception as e:  # dossier inaccessible, disque plein…
            self._finish(f"Erreur à la création du fichier : {e}")
            return
        self._t0 = time.monotonic()
        self._next()

    def _next(self):
        if self._stop or self.i >= len(self.points):
            n = len(self.points)
            self._finish("Exécution terminée." if self.i >= n else f"Exécution arrêtée ({self.i}/{n} points).")
            return
        if self.paused:
            self._waiting = True
            return
        x, y = (float(v) for v in self.points[self.i])
        stage = self.stage

        def move():
            stage.move_precise(x=x, y=y)
            return stage.position()

        self.stage_thread.submit("scan_move", move)

    def _measure(self):
        """Lance en parallèle les mesures demandées pour le point courant."""
        self._results = {}
        self._pending = set()
        if self.do_raman[self.i]:
            self._pending.add("image")
            self.spectro_thread.submit("scan_acquire", self.spectro.acquire_image)
        if self.do_power[self.i]:
            self._pending.add("power")
            pm = self.pm
            self.power_thread.submit("scan_power", lambda: (time.monotonic(), pm.power_w()))

    def _received(self, key, value):
        self._results[key] = value
        self._pending.discard(key)
        if not self._pending:
            self._save_point()

    def _on_stage_done(self, tag, result):
        if tag == "scan_move" and not self._done:
            self._position = result
            self._measure()

    def _on_spectro_done(self, tag, result):
        if self._done:
            return
        if tag == "scan_prepare":
            self._open_writer(*result)
        elif tag == "scan_acquire":
            self._received("image", result)

    def _on_power_done(self, tag, result):
        if tag == "scan_power" and not self._done:
            self._received("power", result[1])

    def _save_point(self):
        pos, counts = self._position
        target = self.points[self.i]
        image, duration = self._results.get("image", (None, float("nan")))
        self.writer.append(image, position_mm=counts or pos, target_mm=target,
                           zone_index=int(self.zone_index[self.i]), timestamp=time.time(),
                           power_W=self._results.get("power", float("nan")), acquisition_s=duration)
        spectrum = image.sum(axis=0).astype(float) if image is not None else None
        self.point_done.emit(self.i, float(target[0]), float(target[1]), spectrum)
        self.i += 1
        elapsed = time.monotonic() - self._t0
        self.progress.emit(self.i, len(self.points), elapsed / self.i * (len(self.points) - self.i))
        self._next()

    def _on_failed(self, tag, message):
        if tag.startswith("scan_"):
            self._finish(f"Exécution interrompue par une erreur : {message}")

    def abort(self, message):
        """Fin immédiate (fermeture de l'application) : le fichier est fermé avec les points déjà mesurés."""
        self._finish(message)

    def _finish(self, message):
        if self._done:
            return
        self._done = True
        for thread, slot in self._threads:
            thread.done.disconnect(slot)
            thread.failed.disconnect(self._on_failed)
        if self.writer is not None:
            self.writer.close(completed=self.i >= len(self.points), end_message=message)
            message += f" Fichier : {self.writer.path}"
        self.finished.emit(message)


def scan_points(zones):
    """Points de toutes les zones, dans l'ordre, et indice de zone de chaque point."""
    blocks = [z.points() for z in zones]
    if not blocks:
        return np.empty((0, 2)), np.empty(0, dtype=int)
    index = np.concatenate([np.full(len(b), i) for i, b in enumerate(blocks)])
    return np.vstack(blocks), index


def image_bytes(n_points, rows=100, columns=1340):
    return n_points * rows * columns * 2


def format_bytes(n):
    units = ["o", "ko", "Mo", "Go", "To"]
    k = min(int(math.log(max(n, 1), 1024)), len(units) - 1)
    return f"{n / 1024 ** k:.1f} {units[k]}"
