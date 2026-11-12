"""Pilote de la platine XYZ (Ultimaker 3) par SSH + D-Bus.

Fonctionnement : OpenSSH de Windows (options de config.yaml, clé SSH installée par
scripts/install_ssh_key.ps1) copie remote/um3_bridge.py sur l'imprimante, puis une connexion
persistante le lance et dialogue avec lui par stdin/stdout. Le pont transmet chaque ligne de G-code à griffin par D-Bus (debugSendGCode), qui
l'envoie au contrôleur de mouvement et renvoie sa réponse (~30 ms par commande).

Repère (mm) : X = Y = 0 après G28 ; Z = 0 → plateau proche de la sonde, G28 met Z au maximum
(plateau en bas, aucun risque de collision). limits.z empêche le plateau de toucher la sonde.
"""
import queue
import re
import subprocess
import threading
import time
from pathlib import Path

BRIDGE_SOURCE = Path(__file__).resolve().parent.parent / "remote" / "um3_bridge.py"
# Réponse M114 du firmware UM3 : "X:0.00Y:0.00Z:0.00E:0.00 count X:0.00Y:0.00Z:0.00"
_POS_RE = re.compile(r"X:\s*(-?[\d.]+)\s*Y:\s*(-?[\d.]+)\s*Z:\s*(-?[\d.]+)")
_COUNT_RE = re.compile(r"count\s+X:\s*(-?[\d.]+)\s*Y:\s*(-?[\d.]+)\s*Z:\s*(-?[\d.]+)", re.I)


class UltimakerError(RuntimeError):
    pass


def ssh_base_command(ucfg, extra_options=()):
    cmd = ["ssh"]
    for opt in list(ucfg.get("ssh_options", [])) + list(extra_options):
        cmd += ["-o", opt]
    cmd.append(f"{ucfg.get('user', 'root')}@{ucfg['host']}")
    return cmd


class UltimakerStage:
    def __init__(self, ucfg):
        self.cfg = ucfg
        self.limits = {ax: tuple(v) for ax, v in ucfg["limits"].items()}
        self.timeout = float(ucfg.get("command_timeout_s", 120))
        self._proc = None
        self._lines = queue.Queue()
        self._lock = threading.Lock()
        self.startup_log = []

    # ------------------------------------------------------------------ connexion
    def _bridge_path(self):
        return self.cfg.get("remote_bridge_path", "/tmp/um3_bridge.py")

    def _upload_bridge(self):
        # Le serveur SSH de l'UM3 coupe les commandes de plus de ~1000 caractères : envoi par stdin.
        cmd = ssh_base_command(self.cfg, ["BatchMode=yes"]) + [f"cat > {self._bridge_path()}"]
        r = subprocess.run(cmd, input=BRIDGE_SOURCE.read_bytes(), capture_output=True, timeout=30)
        if r.returncode != 0:
            raise UltimakerError("Copie du pont impossible : " + r.stderr.decode(errors="replace").strip())

    def _remote_command(self):
        return f"exec python3 -u {self._bridge_path()} --timeout {self.timeout:g}"

    def connect(self, ready_timeout=30.0):
        self._upload_bridge()
        cmd = ssh_base_command(self.cfg, ["BatchMode=yes"]) + [self._remote_command()]
        self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, bufsize=0)
        threading.Thread(target=self._reader, daemon=True).start()
        deadline = time.monotonic() + ready_timeout
        while True:
            line = self._next_line(deadline - time.monotonic())
            if line is None:
                self.close()
                raise UltimakerError("Le pont n'a pas démarré. Sortie :\n" + "\n".join(self.startup_log))
            self.startup_log.append(line)
            if line.startswith("#READY"):
                return self
            if line.startswith("#ERR"):
                self.close()
                raise UltimakerError("\n".join(self.startup_log))

    def _reader(self):
        for raw in iter(self._proc.stdout.readline, b""):
            self._lines.put(raw.decode(errors="replace").rstrip("\r\n"))
        self._lines.put(None)  # fin de flux

    def _next_line(self, timeout):
        try:
            return self._lines.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None

    def close(self):
        if self._proc is None:
            return
        try:
            if self._proc.poll() is None:
                self._proc.stdin.write(b"#QUIT\n")
                self._proc.stdin.flush()
                self._proc.wait(timeout=10)
        except Exception:
            self._proc.kill()
        self._proc = None

    def __enter__(self):
        return self if self._proc is not None else self.connect()

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------ G-code
    def send(self, gcode, timeout=None):
        """Envoie une ligne de G-code ; retourne les lignes de réponse du contrôleur."""
        if self._proc is None or self._proc.poll() is not None:
            raise UltimakerError("Pont non connecté")
        with self._lock:
            self._proc.stdin.write((gcode.strip() + "\n").encode())
            self._proc.stdin.flush()
            deadline = time.monotonic() + (timeout or self.timeout) + 5.0
            replies = []
            while True:
                line = self._next_line(deadline - time.monotonic())
                if line is None:
                    raise UltimakerError(f"Pas de réponse à '{gcode}' (connexion perdue ?)")
                if line == "#OK":
                    return replies
                if line.startswith("#ERR"):
                    raise UltimakerError(f"{gcode} → {line[5:]}")
                replies.append(line[2:] if line.startswith("< ") else line)

    def wait_moves(self):
        self.send("M400")

    def position(self):
        """Position (x, y, z) en mm, et compteurs moteurs si disponibles."""
        text = " ".join(self.send("M114"))
        m = _POS_RE.search(text)
        if not m:
            raise UltimakerError(f"Réponse M114 illisible : {text}")
        pos = tuple(float(v) for v in m.groups())
        c = _COUNT_RE.search(text)
        counts = tuple(float(v) for v in c.groups()) if c else None
        return pos, counts

    def home(self, axes=""):
        """G28 (tous les axes par défaut) : X = Y = 0, plateau en bas (Z max)."""
        self.send(f"G28 {axes}".strip(), timeout=max(self.timeout, 180))
        self.wait_moves()

    def _check_limits(self, target):
        for ax, v in target.items():
            lo, hi = self.limits[ax]
            if not lo <= v <= hi:
                raise UltimakerError(f"{ax.upper()} = {v:.4f} mm hors limites [{lo}, {hi}]")

    def move_to(self, x=None, y=None, z=None, feed=None, wait=True):
        target = {ax: float(v) for ax, v in (("x", x), ("y", y), ("z", z)) if v is not None}
        if not target:
            return
        self._check_limits(target)
        if feed is None:
            feed = self.cfg["feedrate_z_mm_min"] if "z" in target else self.cfg["feedrate_xy_mm_min"]
        words = " ".join(f"{ax.upper()}{v:.4f}" for ax, v in target.items())
        self.send("G90")
        self.send(f"G0 {words} F{feed:.0f}")
        if wait:
            self.wait_moves()

    def move_precise(self, x=None, y=None, z=None, feed=None):
        """Déplacement par pré-position puis approche finale (voir approach_mm dans config.yaml)."""
        target = {ax: float(v) for ax, v in (("x", x), ("y", y), ("z", z)) if v is not None}
        offsets = self.cfg["approach_mm"]
        pre = {ax: v + offsets[ax] for ax, v in target.items()}
        self._check_limits(target)
        try:
            self._check_limits(pre)
        except UltimakerError:  # cible au bord de la course : pas de place pour la pré-position
            pre = None
        if pre is not None:
            self.move_to(feed=feed, **pre)
        self.move_to(feed=feed, **target)

    def move_by(self, dx=0.0, dy=0.0, dz=0.0, feed=None, wait=True):
        (x, y, z), _ = self.position()
        self.move_to(x + dx if dx else None, y + dy if dy else None, z + dz if dz else None,
                     feed=feed, wait=wait)
