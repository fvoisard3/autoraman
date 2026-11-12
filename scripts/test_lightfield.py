"""Phase 0 — test LightField : connexion, réglages, acquisition, axe spectral, temps morts.

Fermer LightField avant de lancer ce script.

    python scripts/test_lightfield.py --exposure 1000 --n 5 --laser 514.5
    python scripts/test_lightfield.py --experiment "Raman_514"
"""
import argparse
import csv
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from raman_auto.config import load_config, output_dir, timestamp, confirm  # noqa: E402
from raman_auto.drivers.lightfield import LightFieldSpectrometer, raman_shift  # noqa: E402


def main():
    cfg = load_config()
    lf_cfg = cfg["lightfield"]
    p = argparse.ArgumentParser()
    p.add_argument("--experiment", default=lf_cfg.get("experiment"), help="expérience LightField à charger")
    p.add_argument("--exposure", type=float, help="temps d'exposition (ms)")
    p.add_argument("--n", type=int, default=3, help="nombre d'acquisitions successives")
    p.add_argument("--laser", type=float, default=cfg["laser"]["default_nm"], help="raie laser (nm)")
    args = p.parse_args()

    out = output_dir(cfg, "phase0", "lightfield")
    tag = timestamp()

    with LightFieldSpectrometer(visible=lf_cfg.get("visible", True), experiment=args.experiment) as lf:
        print("Appareils de l'expérience :")
        for dev in lf.devices():
            print("   ", dev)
        print("Prêt :", lf.is_ready())

        if args.exposure:
            lf.exposure_ms = args.exposure
        md = lf.metadata()
        md["laser_nm"] = args.laser
        print("Réglages :")
        for k, v in md.items():
            print(f"    {k:32s} {v}")

        wl = lf.wavelengths()
        if wl.size:
            print(f"Axe spectral : {wl.size} pixels, {wl[0]:.3f} → {wl[-1]:.3f} nm "
                  f"({raman_shift(wl[0], args.laser):.0f} → {raman_shift(wl[-1], args.laser):.0f} cm⁻¹)")
        else:
            print("ATTENTION : axe spectral vide (spectromètre absent de l'expérience ?)")
        ic = lf.intensity_correction()
        print(f"Courbe IntelliCal : {ic.size} points" if ic.size else "Courbe IntelliCal : aucune")

        if not confirm(f"Laser {args.laser} nm prêt et obturateur dans l'état voulu ? Lancer {args.n} acquisitions"):
            return

        spectra, durations = [], []
        for i in range(args.n):
            wl, s, dt = lf.acquire_spectrum()
            spectra.append(s)
            durations.append(dt)
            print(f"  acquisition {i + 1}/{args.n} : {dt:.3f} s, max = {s.max():.0f}, moyenne = {s.mean():.1f}")

    spectra = np.array(spectra)
    durations = np.array(durations)
    overhead = durations - md["exposure_ms"] * (md.get("accumulations") or 1) / 1000.0
    print(f"\nDurée par acquisition : {durations.mean():.3f} ± {durations.std():.3f} s "
          f"(temps mort ≈ {overhead.mean():.3f} s)")

    # Sauvegarde : CSV (λ, cm⁻¹, spectres) + métadonnées + figure
    csv_path = out / f"lf_{tag}.csv"
    shift = raman_shift(wl, args.laser) if wl.size else np.full(spectra.shape[1], np.nan)
    wl_col = wl if wl.size else np.arange(spectra.shape[1])
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["wavelength_nm", "raman_shift_cm-1"] + [f"spectrum_{i}" for i in range(len(spectra))])
        for j in range(spectra.shape[1]):
            w.writerow([wl_col[j], shift[j]] + list(spectra[:, j]))
    md["durations_s"] = durations.tolist()
    (out / f"lf_{tag}.json").write_text(json.dumps(md, indent=2, default=str), encoding="utf-8")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(9, 4))
    x = shift if wl.size else wl_col
    for s in spectra:
        ax.plot(x, s, lw=0.8)
    ax.set_xlabel("Décalage Raman (cm⁻¹)" if wl.size else "pixel")
    ax.set_ylabel("counts")
    ax.set_title(f"LightField — {md['exposure_ms']} ms, laser {args.laser} nm")
    fig.tight_layout()
    fig.savefig(out / f"lf_{tag}.png", dpi=120)
    print(f"Résultats enregistrés dans {out}")


if __name__ == "__main__":
    main()
