"""Préréglages du spectromètre (réseau, centre, exposition) enregistrés dans un fichier JSON.

Le centre est gardé dans l'unité où il a été saisi : « nm » (longueur d'onde absolue) ou « cm-1 »
(décalage Raman, relatif à la raie laser enregistrée avec le préréglage).
"""
import json
from pathlib import Path


def center_nm(value, unit, laser_nm):
    """Longueur d'onde de centrage (nm) à partir d'une valeur en nm ou en décalage Raman (cm⁻¹)."""
    if unit == "nm":
        return float(value)
    return 1.0 / (1.0 / laser_nm - float(value) * 1e-7)


def center_shift(nm, laser_nm):
    """Décalage Raman (cm⁻¹) correspondant à une longueur d'onde de centrage."""
    return 1e7 / laser_nm - 1e7 / nm


def load_presets(path):
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {name: p for name, p in data.items() if isinstance(p, dict)}


def save_presets(path, presets):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(presets, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
