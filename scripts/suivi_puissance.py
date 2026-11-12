"""Suivi de la puissance laser à point fixe sur une longue durée (PM100D), sortie CSV.

Fermer l'utilitaire Thorlabs et l'interface Raman si elle est connectée au wattmètre (une seule
application à la fois).

    python scripts/suivi_puissance.py                               # durée et intervalle demandés au clavier
    python scripts/suivi_puissance.py --duree 8h --intervalle 10s
    python scripts/suivi_puissance.py --duree 2h30 --intervalle 1min --moyenner --raie 488
    python scripts/suivi_puissance.py --duree 30s --intervalle 1 --sim   # essai sans matériel

Durées : 3600, 90s, 45min, 2h, 2h30, 1j. Ctrl+C arrête la mesure : les lignes déjà écrites sont conservées
(chaque ligne est écrite sur le disque dès la mesure).

Par intervalle, une ligne CSV :
  - par défaut : une lecture (moyennée par le wattmètre sur --avg secondes) ;
  - avec --moyenner : lectures en continu pendant tout l'intervalle → moyenne, écart-type, min, max, nombre
    (temps_s = milieu de l'intervalle).
Le fichier n'écrase jamais un fichier existant : un suffixe _1, _2… est ajouté si le nom est déjà pris.
Aucun zéro n'est fait par ce script (à faire faisceau bloqué, depuis l'interface ou test_pm100d.py --zero).
"""
import argparse
import csv
import datetime
import os
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from raman_auto.config import PROJECT_ROOT, confirm, load_config, timestamp  # noqa: E402
from raman_auto.drivers.pm100d import PM100D, format_power  # noqa: E402
from raman_auto.drivers.sim import SimPowerMeter  # noqa: E402

UNITS = {"j": 86400, "d": 86400, "h": 3600, "min": 60, "m": 60, "s": 1, "ms": 0.001}
SYNC_S = 60.0  # forcer l'écriture physique sur le disque au moins toutes les 60 s


def parse_duration(text):
    """'3600', '90s', '45min', '2h', '2h30', '1h30min15s', '1,5h' → secondes."""
    t = text.strip().lower().replace(",", ".").replace(" ", "")
    try:
        return float(t)
    except ValueError:
        pass
    parts = re.findall(r"(\d+(?:\.\d+)?)(ms|min|j|d|h|m|s)?", t)
    if not parts or "".join(n + u for n, u in parts) != t:
        raise ValueError(f"durée illisible : {text!r} (ex. 3600, 90s, 45min, 2h30)")
    total, previous = 0.0, None
    for number, unit in parts:
        if not unit:  # « 2h30 » : le nombre sans unité prend l'unité inférieure à la précédente
            unit = {"j": "h", "d": "h", "h": "min", "min": "s", "m": "s"}.get(previous, "s")
        total += float(number) * UNITS[unit]
        previous = unit
    return total


def format_duration(s):
    s = int(round(s))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h{m:02d}min{sec:02d}s" if h else (f"{m}min{sec:02d}s" if m else f"{sec}s")


def unique_path(path):
    """Ouvre un nouveau fichier sans jamais écraser : nom, nom_1, nom_2…"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    for k in range(10000):
        candidate = path if k == 0 else path.with_name(f"{path.stem}_{k}{path.suffix}")
        try:
            return candidate, open(candidate, "x", newline="", encoding="utf-8-sig")  # "x" : échoue s'il existe
        except FileExistsError:
            continue
    raise RuntimeError(f"aucun nom libre pour {path}")


def ask(prompt, parse):
    while True:
        try:
            return parse(input(prompt))
        except ValueError as e:
            print("  ", e)


def main():
    cfg = load_config()
    pm_cfg = cfg["pm100d"]
    p = argparse.ArgumentParser(description="Suivi de la puissance laser à point fixe (PM100D).")
    p.add_argument("--duree", help="durée totale (ex. 8h, 2h30, 45min, 3600)")
    p.add_argument("--intervalle", help="temps entre deux lignes sauvegardées (ex. 10s, 1min)")
    p.add_argument("--moyenner", action="store_true",
                   help="lire en continu et sauvegarder moyenne/écart-type/min/max de chaque intervalle")
    p.add_argument("--raie", type=float, default=cfg["laser"]["default_nm"], help="longueur d'onde (nm)")
    p.add_argument("--avg", type=float, default=pm_cfg.get("avg_time_s", 0.1),
                   help="moyennage interne du wattmètre par lecture (s)")
    p.add_argument("--sortie", help="fichier CSV (défaut : data/puissance/<date>/puissance_<raie>nm_<heure>.csv)")
    p.add_argument("--note", default="", help="commentaire écrit en tête du fichier")
    p.add_argument("--resource", default=pm_cfg.get("resource"))
    p.add_argument("--sim", action="store_true", help="wattmètre simulé")
    args = p.parse_args()

    duration = parse_duration(args.duree) if args.duree else ask("Durée totale (ex. 8h, 2h30, 45min) : ",
                                                                   parse_duration)
    interval = parse_duration(args.intervalle) if args.intervalle else ask(
        "Intervalle de sauvegarde (ex. 10s, 1min) : ", parse_duration)
    if interval <= 0 or duration <= 0:
        sys.exit("La durée et l'intervalle doivent être positifs.")
    if interval < args.avg + 0.05:
        sys.exit(f"Intervalle trop court : une lecture prend au moins ~{args.avg + 0.05:.2f} s "
                 f"(moyennage --avg {args.avg:g} s).")
    n_lines = max(int(duration // interval), 1) if args.moyenner else int(duration // interval) + 1
    print(f"Durée {format_duration(duration)}, une ligne toutes les {interval:g} s → ~{n_lines} lignes"
          f"{' (moyenne de chaque intervalle)' if args.moyenner else ''}.")

    pm = SimPowerMeter() if args.sim else PM100D(pm_cfg.get("dll"))
    pm.open(args.resource)
    try:
        sensor = pm.sensor_info()
        pm.wavelength_nm = args.raie
        pm.avg_time_s = args.avg
        pm.set_auto_range(True)
        print(f"Wattmètre : capteur {sensor.get('name', '?')}, λ = {args.raie:g} nm, moyennage {args.avg:g} s")
        print(f"Lecture actuelle : {format_power(pm.power_w())}")
        if not confirm("Capteur en place et obturateur ouvert ? Lancer l'enregistrement"):
            return

        default = (PROJECT_ROOT / cfg.get("data_dir", "data") / "puissance" / datetime.date.today().isoformat()
                   / f"puissance_{args.raie:g}nm_{timestamp()}.csv")
        path, f = unique_path(args.sortie or default)
        with f:
            out = csv.writer(f, delimiter=";")
            for line in (f"# Suivi de puissance PM100D{' (SIMULATION)' if args.sim else ''}",
                         f"# debut={datetime.datetime.now().isoformat(timespec='seconds')}",
                         f"# capteur={sensor.get('name', '?')} raie_nm={args.raie:g} moyennage_s={args.avg:g}",
                         f"# duree_prevue_s={duration:g} intervalle_s={interval:g} moyenner={args.moyenner}",
                         f"# note={args.note}"):
                f.write(line + "\n")
            columns = ["horodatage", "temps_s", "puissance_W"]
            if args.moyenner:
                columns += ["ecart_type_W", "min_W", "max_W", "n_lectures"]
            out.writerow(columns)
            f.flush()
            print(f"Fichier : {path}")
            print("Ctrl+C pour arrêter (les données déjà écrites sont conservées).\n")

            first = None
            n = 0
            reason = "durée atteinte"
            t0 = time.monotonic()
            last_sync = t0
            try:
                k = 0
                while True:
                    t_start = t0 + k * interval  # échéancier fixe : pas de dérive sur une longue durée
                    # Sans moyenne : une lecture à t = 0, intervalle, …, durée. Avec moyenne : des intervalles
                    # complets [k·intervalle, (k+1)·intervalle] qui couvrent la durée.
                    if (t_start - t0 > duration + 1e-9 if not args.moyenner
                            else t_start - t0 > duration - interval + 1e-9):
                        break
                    now = time.monotonic()
                    if now < t_start:
                        time.sleep(t_start - now)
                    elif now - t_start > interval:  # lecture en retard (USB lent) : on saute les créneaux perdus
                        k = int((now - t0) // interval)
                        continue
                    if args.moyenner:
                        readings = [pm.power_w()]
                        end = min(t0 + (k + 1) * interval, t0 + duration) - args.avg
                        while time.monotonic() < end:
                            readings.append(pm.power_w())
                        value = statistics.fmean(readings)
                        extra = [f"{statistics.pstdev(readings):.6e}", f"{min(readings):.6e}",
                                 f"{max(readings):.6e}", len(readings)]
                        t = (t_start + time.monotonic()) / 2 - t0  # milieu de l'intervalle moyenné
                    else:
                        value = pm.power_w()
                        extra = []
                        t = time.monotonic() - t0
                    out.writerow([datetime.datetime.now().isoformat(timespec="milliseconds"), f"{t:.3f}",
                                  f"{value:.6e}"] + extra)
                    f.flush()
                    if time.monotonic() - last_sync > SYNC_S:
                        os.fsync(f.fileno())
                        last_sync = time.monotonic()
                    n += 1
                    first = value if first is None else first
                    drift = f"{100 * (value / first - 1):+.2f} %" if first else "—"
                    print(f"\r  {format_duration(t):>12s} / {format_duration(duration)}   "
                          f"P = {format_power(value):>12s}   écart au début {drift:>9s}   lignes {n}   ",
                          end="", flush=True)
                    k += 1
            except KeyboardInterrupt:
                reason = "arrêt demandé (Ctrl+C)"
            f.write(f"# fin={datetime.datetime.now().isoformat(timespec='seconds')} lignes={n} raison={reason}\n")
    finally:
        pm.close()

    print(f"\n\nTerminé : {reason}. {n} lignes dans\n  {path}")
    if n:
        summarize(path)


def summarize(path):
    values, times = [], []
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.reader((line for line in f if not line.startswith("#")), delimiter=";"):
            if row and row[0] != "horodatage":
                times.append(float(row[1]))
                values.append(float(row[2]))
    mean = statistics.fmean(values)
    std = statistics.pstdev(values)
    print(f"P moyenne {format_power(mean)} ± {format_power(std)} (σ/P = {100 * std / mean:.2f} %), "
          f"min {format_power(min(values))}, max {format_power(max(values))}")
    if len(values) > 10 and times[-1] > times[0]:
        slope = statistics.linear_regression(times, values).slope
        print(f"Dérive linéaire : {slope * 3600 / mean * 100:+.2f} %/h")


if __name__ == "__main__":
    main()
