"""Phase 0 — test de mouvement de l'Ultimaker 3 via le pont D-Bus (um3_bridge.py).

Chaque étape qui bouge la machine demande une confirmation.

    python scripts/test_ultimaker_motion.py --info-only        # connexion + infos, aucun mouvement
    python scripts/test_ultimaker_motion.py --home --axis x     # homing (G28) puis tests sur X
    python scripts/test_ultimaker_motion.py --axis z --cycles 5 # tests sur Z (vérifier limits.z avant !)

Tests :
  1. latence : aller-retour M114 et petit déplacement + M400
  2. résolution : pas de 10 µm à 200 µm, position planifiée et compteurs moteurs (M114 « Count »)
  3. aller-retour : A → B → A répété (la répétabilité réelle se mesurera au knife-edge)
"""
import argparse
import csv
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from raman_auto.config import load_config, output_dir, timestamp, confirm  # noqa: E402
from raman_auto.drivers.ultimaker import UltimakerStage  # noqa: E402

STEPS_MM = [0.010, 0.0125, 0.025, 0.050, 0.100, 0.200]


def main():
    cfg = load_config()
    ucfg = cfg["ultimaker"]
    p = argparse.ArgumentParser()
    p.add_argument("--info-only", action="store_true")
    p.add_argument("--home", action="store_true", help="faire G28 (X = Y = 0, plateau en bas) avant les tests")
    p.add_argument("--axis", choices=["x", "y", "z"], default="x")
    p.add_argument("--n", type=int, default=10, help="répétitions pour la latence")
    p.add_argument("--cycles", type=int, default=5, help="cycles aller-retour")
    p.add_argument("--travel", type=float, default=5.0, help="amplitude de l'aller-retour (mm)")
    args = p.parse_args()

    out = output_dir(cfg, "phase0", "ultimaker")
    tag = timestamp()
    ax = args.axis
    idx = "xyz".index(ax)

    print(f"Connexion à {ucfg['host']} ...")
    with UltimakerStage(ucfg) as stage:
        print("Démarrage du pont :")
        for line in stage.startup_log:
            print("   ", line)
        print("Firmware :", " ".join(stage.send("M115")))
        pos, counts = stage.position()
        print(f"Position : {pos}   compteurs : {counts}")
        if args.info_only:
            return

        if args.home and confirm("Zone dégagée autour de la sonde ? Lancer le homing (G28)"):
            stage.home()
            pos, counts = stage.position()
            print(f"Après homing : {pos}")

        # Point de départ à l'intérieur des limites, avec de la marge pour la course et l'approche
        lo, hi = stage.limits[ax]
        margin = args.travel + 1.0
        start = min(max(pos[idx], lo + margin), hi - margin)
        if not confirm(f"Tests de mouvement sur {ax.upper()} autour de {start:.3f} mm (+{args.travel:.3f} mm) ?"):
            return
        stage.move_to(**{ax: start})

        rows = []

        # 1. Latence
        t = []
        for _ in range(args.n):
            t0 = time.perf_counter()
            stage.position()
            t.append(time.perf_counter() - t0)
        print(f"\n[1] M114 aller-retour : {statistics.mean(t) * 1e3:.1f} ± {statistics.pstdev(t) * 1e3:.1f} ms")
        t = []
        for i in range(args.n):
            target = start + (0.1 if i % 2 == 0 else 0.0)
            t0 = time.perf_counter()
            stage.move_to(**{ax: target})
            t.append(time.perf_counter() - t0)
        print(f"    déplacement 0.1 mm + M400 : {statistics.mean(t) * 1e3:.1f} ± {statistics.pstdev(t) * 1e3:.1f} ms")
        rows.append(["latence_move_0.1mm_ms", statistics.mean(t) * 1e3, statistics.pstdev(t) * 1e3, "", ""])

        # 2. Résolution : position planifiée et compteurs moteurs après chaque pas
        print("\n[2] Pas (mm)   commandé       planifié       compteur")
        stage.move_to(**{ax: start})
        for step in STEPS_MM:
            for k in range(1, 4):
                target = start + k * step
                stage.move_to(**{ax: target})
                pos, counts = stage.position()
                c = counts[idx] if counts else ""
                print(f"    {step:7.4f}   {target:10.4f}   {pos[idx]:10.4f}   {c}")
                rows.append([f"pas_{step}", target, pos[idx], c, ""])
            stage.move_to(**{ax: start})

        # 3. Aller-retour (toujours approché dans le même sens)
        print(f"\n[3] Aller-retour {start:.3f} ↔ {start + args.travel:.3f} mm, {args.cycles} cycles")
        for cyc in range(args.cycles):
            stage.move_to(**{ax: start + args.travel})
            stage.move_to(**{ax: start})
            pos, counts = stage.position()
            c = counts[idx] if counts else ""
            print(f"    cycle {cyc + 1} : planifié {pos[idx]:.4f}   compteur {c}")
            rows.append([f"cycle_{cyc + 1}", start, pos[idx], c, ""])

        # 4. Petits pas avec pré-position (mouvements < 5 pas ignorés par le firmware sinon)
        print("\n[4] Pas avec pré-position : commandé     compteur")
        for step in (0.0125, 0.025, 0.050) if ax != "z" else (0.005, 0.010, 0.025):
            for k in range(1, 5):
                target = start + k * step
                stage.move_precise(**{ax: target})
                _, counts = stage.position()
                c = counts[idx] if counts else ""
                print(f"    {step:7.4f}   {target:10.4f}   {c}")
                rows.append([f"precis_{step}", target, "", c, ""])

        # 5. M400 attend-il la fin du mouvement ? Durée mesurée vs durée théorique
        dist = args.travel * (1 if ax == "z" else 10)
        feed = ucfg["feedrate_z_mm_min"] if ax == "z" else ucfg["feedrate_xy_mm_min"]
        stage.move_to(**{ax: start})
        t0 = time.perf_counter()
        stage.move_to(**{ax: start + (dist if start < (lo + hi) / 2 else -dist)})
        dt = time.perf_counter() - t0
        print(f"\n[5] {dist:.0f} mm à {feed} mm/min : {dt:.3f} s mesurées, {dist / feed * 60:.3f} s sans accélération")
        rows.append(["sync_M400_s", dist, dt, dist / feed * 60, ""])
        stage.move_to(**{ax: start})

    path = out / f"motion_{ax}_{tag}.csv"
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["test", "commande_mm", "planifie_mm", "compteur", "note"])
        w.writerows(rows)
    print(f"\nRésultats : {path}")


if __name__ == "__main__":
    main()
