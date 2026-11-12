"""Chargement de la configuration et utilitaires communs."""
import datetime
import os
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "config.yaml"


def load_config(path=None):
    with open(path or DEFAULT_CONFIG, encoding="utf-8") as f:
        return yaml.safe_load(f)


def output_dir(cfg, *parts):
    """Dossier de sortie daté, créé au besoin : <data_dir>/<parts...>/<AAAA-MM-JJ>."""
    d = PROJECT_ROOT / cfg.get("data_dir", "data")
    for p in parts:
        d = d / p
    d = d / datetime.date.today().isoformat()
    d.mkdir(parents=True, exist_ok=True)
    return d


def timestamp():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def confirm(message):
    """Demande une confirmation explicite à l'opérateur (laser manuel, mouvements).

    RAMAN_AUTO_CONFIRM=1 répond oui automatiquement (exécution sans console interactive).
    """
    if os.environ.get("RAMAN_AUTO_CONFIRM") == "1":
        print(f"\n>>> {message} [confirmation automatique]")
        return True
    answer = input(f"\n>>> {message} [o/N] ").strip().lower()
    return answer in ("o", "oui", "y", "yes")
