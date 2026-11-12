"""Phase 0 — exploration de l'Ultimaker 3 par SSH (LECTURE SEULE : aucun mouvement, aucun service arrêté).

Objectif : trouver comment envoyer du G-code au contrôleur de mouvement.
  - port série interne et vitesse (pour um3_bridge.py)
  - nom du service griffin qui occupe ce port
  - méthodes D-Bus / API locale qui acceptent du G-code
Toutes les sorties sont enregistrées dans data/phase0/ultimaker/<date>/explore_<heure>.log.

    python scripts/explore_ultimaker.py
Un seul mot de passe est demandé (aucun si une clé SSH est installée).
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from raman_auto.config import load_config, output_dir, timestamp  # noqa: E402
from raman_auto.drivers.ultimaker import ssh_base_command  # noqa: E402

# Script shell exécuté sur l'imprimante. Uniquement des commandes de lecture.
REMOTE_SCRIPT = r"""
section() { echo; echo "=================== $1"; }
section "système";        uname -a; cat /etc/os-release 2>/dev/null; hostname; uptime
section "versions UM";    ls /etc | grep -i -E 'version|ultimaker|jedi|griffin'; cat /etc/*version* 2>/dev/null | head -20
section "python";         python3 --version; python3 -c "import serial; print('pyserial', serial.VERSION)" 2>&1
section "dbus python";    python3 -c "import dbus; print('dbus-python OK')" 2>&1
section "ports série";    ls -l /dev/ttyS* /dev/ttyUSB* /dev/ttyACM* /dev/ttymxc* /dev/serial* 2>/dev/null
section "qui utilise les ports"; for f in /proc/[0-9]*/fd/*; do t=$(readlink "$f" 2>/dev/null); case "$t" in /dev/tty[SAU]*|/dev/ttymxc*) echo "$f -> $t  ($(cat /proc/$(echo $f | cut -d/ -f3)/cmdline | tr '\0' ' '))";; esac; done 2>/dev/null | sort -u
section "services";       systemctl list-units --type=service --no-pager 2>/dev/null | grep -i -E 'griffin|printer|ultimaker|okuda|cura'
section "processus";      ps aux 2>/dev/null | grep -i -E 'griffin|python' | grep -v grep
section "arbo griffin";   ls -d /usr/share/griffin* /usr/lib/python3*/dist-packages/griffin* /etc/griffin* 2>/dev/null
section "config série";   grep -r -h -i -E '/dev/tty|baud' /usr/share/griffin /etc/griffin /usr/lib/python3*/dist-packages/griffin 2>/dev/null | sort -u | head -40
section "méthodes gcode"; grep -r -n -E 'def [A-Za-z_]*([Gg][Cc]ode|queue|Queue)[A-Za-z_]*\(' /usr/share/griffin /usr/lib/python3*/dist-packages/griffin 2>/dev/null | head -80
section "bus D-Bus";      (busctl --system list --no-pager 2>/dev/null || dbus-send --system --print-reply --dest=org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus.ListNames) | grep -i ultimaker
section "introspection printer"; dbus-send --system --print-reply --dest=nl.ultimaker.printer /nl/ultimaker/printer org.freedesktop.DBus.Introspectable.Introspect 2>&1 | grep -i -E 'method name|interface name' | head -100
section "API locale";     for u in printer/heads/0/position printer/status system/firmware; do echo "--- $u"; curl -s -m 5 http://localhost/api/v1/$u 2>&1 || wget -q -O - http://localhost/api/v1/$u 2>&1; echo; done
section "fin"
"""


def main():
    cfg = load_config()
    ucfg = cfg["ultimaker"]
    out = output_dir(cfg, "phase0", "ultimaker")
    log = out / f"explore_{timestamp()}.log"
    cmd = ssh_base_command(ucfg) + ["sh -s"]
    print("Commande :", " ".join(cmd))
    proc = subprocess.run(cmd, input=REMOTE_SCRIPT.encode(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    text = proc.stdout.decode(errors="replace")
    log.write_text(text, encoding="utf-8")
    print(text)
    print(f"\nCode de retour SSH : {proc.returncode}")
    print(f"Journal enregistré : {log}")


if __name__ == "__main__":
    main()
