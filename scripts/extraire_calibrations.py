"""Extrait les calibrations LightField d'une acquisition ou d'une expérience, pour les corrections en Python.

    python scripts/extraire_calibrations.py                         # dernier fichier .spe enregistré
    python scripts/extraire_calibrations.py --intellical            # dernier .spe acquis avec IntelliCal
    python scripts/extraire_calibrations.py --experience calib_data # expérience LightField sauvegardée
    python scripts/extraire_calibrations.py --spe "C:\\...\\fichier.spe"

Lecture seule : rien n'est modifié dans LightField. Résultat dans data/calibrations/<date>/<nom>/ :
calibrations.h5 (tout), longueur_onde.csv, intellical_etape_N.csv, resume.txt.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from raman_auto.analysis.lightfield_calib import extract, find_recent_spe, format_summary  # noqa: E402
from raman_auto.config import load_config, output_dir, timestamp  # noqa: E402

DEFAULT_DOCUMENTS = r"C:\Users\admin\Documents\LightField"


def main():
    cfg = load_config()
    documents = Path(cfg.get("lightfield", {}).get("documents_dir") or DEFAULT_DOCUMENTS)
    p = argparse.ArgumentParser(description="Extraction des calibrations LightField (lecture seule).")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--spe", help="fichier .spe (corrigé, pas '-raw')")
    g.add_argument("--experience", help="nom d'une expérience sauvegardée (.lfe), ex. calib_data")
    g.add_argument("--intellical", action="store_true", help="dernier .spe acquis avec IntelliCal activé")
    p.add_argument("--raie", type=float, help="raie laser (nm) pour l'axe en cm⁻¹ (défaut : celle du fichier)")
    args = p.parse_args()

    if args.experience:
        source = documents / "Experiments" / (args.experience if args.experience.endswith(".lfe")
                                               else args.experience + ".lfe")
        if not source.exists():
            sys.exit(f"Expérience introuvable : {source}")
    elif args.spe:
        source = Path(args.spe)
    else:
        print("Recherche du fichier .spe le plus récent…")
        source = find_recent_spe(documents, intellical=True if args.intellical else None)
        if source is None:
            sys.exit("Aucun fichier .spe trouvé.")
    print(f"Source : {source}")
    out = output_dir(cfg, "calibrations") / f"{source.stem.replace(' ', '_')}_{timestamp()}"
    summary = extract(source, out, documents, args.raie)
    print()
    print(format_summary(summary))
    print(f"Fichiers écrits dans {out}")


if __name__ == "__main__":
    main()
