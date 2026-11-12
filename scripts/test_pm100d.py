"""Phase 0 — test PM100D : détection, capteur, longueur d'onde, zéro, enregistrement de la puissance.

Fermer l'utilitaire Thorlabs (Optical Power Meter Utility) avant de lancer ce script.

    python scripts/test_pm100d.py --list
    python scripts/test_pm100d.py --wavelength 514.5 --duration 60 --rate 10
    python scripts/test_pm100d.py --wavelength 488 --zero --duration 600   # suivi de dérive 10 min
"""
import argparse
import csv
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from raman_auto.config import load_config, output_dir, timestamp, confirm  # noqa: E402
from raman_auto.drivers.pm100d import PM100D, format_power as fmt_power  # noqa: E402


def main():
    cfg = load_config()
    pm_cfg = cfg["pm100d"]
    p = argparse.ArgumentParser()
    p.add_argument("--list", action="store_true", help="lister les appareils et quitter")
    p.add_argument("--resource", default=pm_cfg.get("resource"))
    p.add_argument("--wavelength", type=float, default=cfg["laser"]["default_nm"])
    p.add_argument("--avg", type=float, default=pm_cfg.get("avg_time_s", 0.1), help="temps de moyennage (s)")
    p.add_argument("--zero", action="store_true", help="faire le zéro (faisceau bloqué) avant la mesure")
    p.add_argument("--duration", type=float, default=30.0, help="durée d'enregistrement (s)")
    p.add_argument("--rate", type=float, default=5.0, help="lectures par seconde")
    args = p.parse_args()

    pm = PM100D(pm_cfg.get("dll"))
    devices = pm.find_resources()
    print("Appareils trouvés :")
    for d in devices:
        print("   ", d)
    if args.list or not devices:
        return

    with pm.open(args.resource):
        print("Ressource :", pm.resource)
        print("Capteur   :", pm.sensor_info())
        print("Plage λ   : {:.0f} – {:.0f} nm".format(*pm.wavelength_range_nm()))
        pm.wavelength_nm = args.wavelength
        pm.avg_time_s = args.avg
        pm.set_auto_range(True)
        print(f"λ réglée  : {pm.wavelength_nm:.1f} nm, moyennage {pm.avg_time_s:.3f} s")

        if args.zero:
            if confirm("Obturateur laser FERMÉ / capteur couvert ? Lancer le zéro"):
                print(f"Offset de zéro : {pm.zero():.3e} W")

        if not confirm(f"Obturateur ouvert, capteur au plan de l'échantillon ? Enregistrer {args.duration:.0f} s"):
            return

        out = output_dir(cfg, "phase0", "pm100d")
        path = out / f"pm100d_{args.wavelength:g}nm_{timestamp()}.csv"
        period = 1.0 / args.rate
        t_rel, powers = [], []
        t0 = time.monotonic()
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["t_s", "power_W"])
            try:
                while (t := time.monotonic() - t0) < args.duration:
                    pw = pm.power_w()
                    w.writerow([f"{t:.3f}", f"{pw:.6e}"])
                    t_rel.append(t)
                    powers.append(pw)
                    print(f"\r  t = {t:6.1f} s   P = {fmt_power(pw):>14s}", end="")
                    time.sleep(max(0.0, period - (time.monotonic() - t0 - t)))
            except KeyboardInterrupt:
                print("\n  interrompu")

    powers = np.array(powers)
    print(f"\n\n{len(powers)} lectures : P = {fmt_power(powers.mean())} ± {fmt_power(powers.std())} "
          f"(σ/P = {100 * powers.std() / powers.mean():.2f} %), "
          f"min {fmt_power(powers.min())}, max {fmt_power(powers.max())}")
    if len(powers) > 10:
        slope = np.polyfit(t_rel, powers, 1)[0]
        print(f"Dérive linéaire : {slope * 3600 / powers.mean() * 100:+.2f} %/h")
    print(f"Enregistré : {path}")


if __name__ == "__main__":
    main()
