#!/usr/bin/env python3
"""Pont G-code exécuté SUR l'Ultimaker 3 (copié par le PC dans /tmp via SSH).

Passe par la méthode D-Bus debugSendGCode du service griffin.printer : le G-code est envoyé
directement au contrôleur de mouvement (sans les traitements d'impression de griffin) et la
réponse est attendue. griffin reste actif (écran, suivi de position) ; rien n'est arrêté.

Protocole stdin/stdout avec le PC :
    PC → pont   : une ligne de G-code, ou "#PING", "#QUIT"
    pont → PC   : "< <ligne de réponse>" (zéro ou plusieurs), puis "#OK" ou "#ERR <message>"
Messages en ASCII (la locale de l'imprimante peut ne pas être UTF-8).
"""
import argparse
import re
import sys

import dbus

# Acquittement du protocole Ultimaker : "o<séquence><checksum>", ex. "o860F35"
_ACK_RE = re.compile(r"^o[0-9A-Fa-f]+$")


def out(line):
    sys.stdout.write(line.encode("ascii", "replace").decode("ascii") + "\n")
    sys.stdout.flush()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--timeout", type=float, default=120.0, help="délai max d'une commande (s)")
    args = p.parse_args()

    try:
        obj = dbus.SystemBus().get_object("nl.ultimaker.printer", "/nl/ultimaker/printer")
        printer = dbus.Interface(obj, "nl.ultimaker")
        # debugDriverInfo échoue par intermittence (bug de sérialisation de griffin) : on teste un vrai échange
        reply = str(printer.debugSendGCode("M115", timeout=10))
    except Exception as e:
        out("#ERR dbus: {}".format(str(e).replace("\n", " ")))
        return 1
    if reply == "NOT CONNECTED":
        out("#ERR motion controller not connected")
        return 1
    out("#READY dbus")

    for raw in sys.stdin:
        cmd = raw.strip()
        if not cmd:
            continue
        if cmd == "#PING":
            out("#PONG")
            continue
        if cmd == "#QUIT":
            break
        try:
            reply = str(printer.debugSendGCode(cmd, timeout=args.timeout))
        except Exception as e:
            out("#ERR {}: {}".format(cmd, str(e).replace("\n", " ")))
            continue
        if reply in ("NOT CONNECTED", "ABORT"):
            out("#ERR {}: {}".format(cmd, reply))
            continue
        error = None
        for line in reply.splitlines():
            line = line.strip()
            if not line or _ACK_RE.match(line):
                continue
            if line.lower().startswith(("error", "!!")):
                error = line
            out("< " + line)
        out("#ERR " + error if error else "#OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
