"""Extraction des calibrations LightField : longueur d'onde, correction d'intensité IntelliCal, fond, flatfield.

Où LightField range ses calibrations :
  - longueur d'onde : dans chaque fichier .spe (WavelengthMapping : longueur d'onde de chaque colonne) ;
    les expériences .lfe n'en contiennent pas ;
  - IntelliCal : facteurs par pixel (float32, normalisés à 1 au maximum) dans
    C:\\ProgramData\\Princeton Instruments\\Experiment Control\\Intensity Calibration\\<GUID>, un fichier par
    position du réseau ; l'expérience ne garde que la date de la calibration, d'où l'association par la date
    d'écriture des fichiers (quelques secondes après la date de calibration) ;
  - fond et flatfield : fichiers .spe de référence dont le chemin est dans les réglages.
Vérifié sur une paire corrigé / brut de LightField (mai 2025) :  corrigé = k · A · (brut − fond),
pixel par pixel, avec A le fichier IntelliCal de la position du réseau et k une constante de normalisation.
"""
import datetime
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import h5py
import numpy as np

from .spe import SpeFile, setting

STORE = Path(r"C:\ProgramData\Princeton Instruments\Experiment Control\Intensity Calibration")
COLUMNS = 1340


def parse_date(text):
    """Date LightField ('2022-02-28T09:39:06.0777133-05:00') → datetime avec fuseau."""
    if not text:
        return None
    head, _, rest = text.partition(".")
    tz = ""
    for sign in "+-":
        if sign in rest:
            tz = sign + rest.split(sign, 1)[1]
            break
    try:
        return datetime.datetime.fromisoformat(head + tz)
    except ValueError:
        return None


def load_factor(path):
    a = np.fromfile(path, dtype=np.float32)
    if a.size % COLUMNS == 0:
        return a.reshape(-1, COLUMNS)
    return a


def store_candidates(ic_date, store=STORE, window_s=600):
    """Fichiers IntelliCal écrits juste après la date de calibration (dans l'ordre d'écriture)."""
    if ic_date is None or not Path(store).exists():
        return []
    t0 = ic_date.timestamp()
    out = []
    for p in Path(store).iterdir():
        if p.is_file():
            t = p.stat().st_mtime
            if t0 - 5 <= t <= t0 + window_s:
                out.append((t, p))
    return [p for _, p in sorted(out)]


def verify(corrected, wl_corrected, raw, wl_raw, background, factor):
    """Compare corrigé / (brut − fond) au facteur A sur les pixels communs.

    Retourne (écart relatif médian, k) ; écart ≈ 0 si A est bien le facteur appliqué par LightField.
    """
    idx = np.clip(np.searchsorted(wl_corrected, wl_raw), 0, len(wl_corrected) - 1)
    exact = np.abs(wl_corrected[idx] - wl_raw) < 1e-6
    if exact.sum() < 100 or factor.shape[0] != raw.shape[0] or corrected.shape[0] != raw.shape[0]:
        return np.nan, np.nan
    rows = valid_rows(factor)
    num = corrected[rows][:, idx[exact]]
    den = raw[rows][:, exact] - (background[rows][:, exact] if background is not None else 0.0)
    a = factor[rows][:, exact]
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = num / den
        m = np.isfinite(ratio) & (a > 0) & (np.abs(den) > 50)
        if m.sum() < 100:
            return np.nan, np.nan
        k = float(np.median(ratio[m] / a[m]))
        return float(np.median(np.abs(ratio[m] / (k * a[m]) - 1))), k


def _mean_frame(path):
    try:
        f = SpeFile(path)
        return f.region().astype(float).mean(axis=0), f.summary()
    except Exception:
        return None, None


def _raw_steps(spe_path):
    """Fichiers bruts associés à un .spe : '-raw.spe' ou étapes '-SG-…-Step-N-raw.spe'."""
    p = Path(spe_path)
    single = p.with_name(p.stem + "-raw.spe")
    if single.exists():
        return [single]
    steps = sorted(p.parent.glob(p.stem + "-SG-*-Step-*-raw.spe"),
                   key=lambda q: int(q.stem.split("-Step-")[1].split("-")[0]))
    return steps


def find_recent_spe(documents, grating=None, center=None, intellical=None, steps=None):
    """Fichier .spe (corrigé, pas '-raw') le plus récent, avec filtres optionnels (steps : centres du
    step and glue)."""
    files = sorted((q for q in Path(documents).rglob("*.spe") if not q.stem.endswith("-raw")),
                   key=lambda q: q.stat().st_mtime, reverse=True)
    for q in files:
        try:
            s = SpeFile(q).summary()
        except Exception:
            continue
        if grating and s["grating"] != grating:
            continue
        if center is not None and (s["center_nm"] is None or abs(s["center_nm"] - center) > 0.01):
            continue
        if intellical is not None and s["intensity_calibration"] != intellical:
            continue
        if steps and not (s["step_and_glue"] and len(s["step_centers_nm"]) == len(steps) and
                          all(abs(a - b) < 0.01 for a, b in zip(s["step_centers_nm"], steps))):
            continue
        return q
    return None


# ====================================================================== extraction
def extract(source, out_dir, documents, laser_nm=None):
    """Extrait les calibrations d'un .spe ou d'une expérience .lfe ; écrit calibrations.h5, CSV et résumé.

    Retourne le résumé (dict).
    """
    source = Path(source)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    notes = []

    if source.suffix.lower() == ".lfe":
        root = ET.parse(source).getroot()
        s = {
            "grating": setting(root, "Grating", "Selected"),
            "center_nm": float(setting(root, "Grating", "CenterWavelength") or "nan"),
            "intensity_calibration": setting(root, "IntensityCalibration", "Enabled") == "True",
            "intensity_calibration_date": setting(root, "IntensityCalibration", "Date"),
            "step_and_glue": setting(root, "StepAndGlue", "Enabled") == "True",
            "step_centers_nm": [float(v) for v in (setting(root, "StepAndGlue", "CenterWavelengths") or "").split(",")
                                if v],
            "background": setting(root, "BackgroundCorrection", "Enabled") == "True",
            "background_file": setting(root, "BackgroundCorrection", "ReferenceFile"),
            "flatfield": setting(root, "FlatfieldCorrection", "Enabled") == "True",
            "flatfield_file": setting(root, "FlatfieldCorrection", "ReferenceFile"),
            "exposure_ms": float(setting(root, "ShutterTiming", "ExposureTime") or "nan"),
        }
        # Pas d'axe spectral dans un .lfe : on prend le .spe le plus récent avec le même réseau et centre.
        if s["step_and_glue"]:
            spe_path = find_recent_spe(documents, s["grating"], steps=s["step_centers_nm"])
        else:
            spe_path = find_recent_spe(documents, s["grating"], s["center_nm"])
        if spe_path is None:
            notes.append("aucun fichier .spe avec ce réseau et cette position : pas de calibration en longueur d'onde")
        else:
            notes.append(f"longueur d'onde prise dans {spe_path.name} (même réseau et même(s) position(s))")
        spe = SpeFile(spe_path) if spe_path else None
    else:
        spe = SpeFile(source)
        spe_path = source
        s = spe.summary()

    summary = {"source": str(source), "settings": s, "notes": notes, "steps": []}
    laser_nm = laser_nm or (spe.summary()["laser_line_nm"] if spe else None)

    with h5py.File(out_dir / "calibrations.h5", "w") as h:
        h.attrs["source"] = str(source)
        h.attrs["extracted"] = datetime.datetime.now().isoformat(timespec="seconds")
        h.attrs["settings"] = json.dumps(s, ensure_ascii=False, default=str)
        h.attrs["formula"] = "corrigé = k · A · (brut − fond), pixel par pixel (A = intensity/step_N/factor)"

        # ---- longueur d'onde
        wl = spe.wavelengths if spe else None
        raw_files = _raw_steps(spe_path) if spe_path else []
        step_wl = []
        for q in raw_files:
            try:
                step_wl.append(SpeFile(q).wavelengths)
            except Exception:
                step_wl.append(None)
        if wl is not None:
            g = h.create_group("wavelength")
            g.create_dataset("full_nm", data=wl)
            g.attrs.update({k: v for k, v in spe.wavelength_calibration.items()})
            for i, w in enumerate(step_wl):
                if w is not None and len(w) == COLUMNS and len(raw_files) > 1:
                    g.create_dataset(f"step_{i + 1}_nm", data=w)
            cols = {"longueur_onde_nm": wl}
            if laser_nm:
                cols["decalage_raman_cm-1"] = 1e7 / laser_nm - 1e7 / wl
            _write_csv(out_dir / "longueur_onde.csv", cols)
            summary["wavelength"] = {"pixels": int(len(wl)), "from_nm": float(wl[0]), "to_nm": float(wl[-1]),
                                     "date": spe.wavelength_calibration.get("date"),
                                     "laser_line_nm": spe.wavelength_calibration.get("laserLine")}

        # ---- fond et flatfield de référence
        background = None
        for key, name in (("background_file", "background"), ("flatfield_file", "flatfield")):
            path = s.get(key)
            if path and Path(path).exists():
                frame, info = _mean_frame(path)
                if frame is not None:
                    h.create_dataset(f"{name}/frame", data=frame)
                    h[name].attrs.update(file=path, enabled=bool(s.get(name)),
                                         exposure_ms=info["exposure_ms"] or np.nan)
                    summary[name] = {"file": path, "enabled": bool(s.get(name)), "shape": list(frame.shape),
                                     "exposure_ms": info["exposure_ms"]}
                    if name == "background":
                        background = frame

        # ---- IntelliCal
        ic_date = parse_date(s.get("intensity_calibration_date"))
        candidates = store_candidates(ic_date)
        centers = s["step_centers_nm"] if s.get("step_and_glue") else [s.get("center_nm")]
        summary["intensity_calibration_date"] = s.get("intensity_calibration_date")
        if ic_date is None:
            notes.append("pas de calibration d'intensité IntelliCal dans cette expérience")
        elif not candidates:
            notes.append("calibration IntelliCal introuvable dans le magasin de LightField")
        else:
            factors = {p: load_factor(p) for p in candidates}
            # Association étape ↔ fichier : vérifiée sur les données brutes si elles existent, sinon par ordre.
            corrected = spe.region()[0].astype(float) if spe is not None and spe_path == source else None
            assigned = []
            for i in range(len(centers)):
                best = (np.nan, np.nan, candidates[min(i, len(candidates) - 1)], "ordre d'écriture")
                if corrected is not None and i < len(raw_files) and step_wl[i] is not None:
                    raw = SpeFile(raw_files[i]).region()[0].astype(float)
                    scores = [(verify(corrected, wl, raw, step_wl[i], background, a), p) for p, a in factors.items()]
                    scores = [(r, k, p) for (r, k), p in scores if np.isfinite(r)]
                    if scores:
                        r, k, p = min(scores, key=lambda x: x[0])
                        best = (r, k, p, "vérifiée sur la paire corrigé / brut")
                assigned.append(best)
            for i, (resid, k, p, how) in enumerate(assigned):
                a = factors[p]
                g = h.create_group(f"intensity/step_{i + 1}")
                g.create_dataset("factor", data=a)
                g.attrs.update(store_file=str(p), center_nm=centers[i] if centers[i] is not None else np.nan,
                               association=how, residual=resid, k=k, date=s.get("intensity_calibration_date"))
                if i < len(step_wl) and step_wl[i] is not None:
                    g.create_dataset("wavelength_nm", data=step_wl[i])
                elif wl is not None and len(wl) == a.shape[-1]:
                    g.create_dataset("wavelength_nm", data=wl)
                rows = valid_rows(a)
                outliers = sorted(set(range(a.shape[0])) - set(rows.tolist())) if a.ndim == 2 else []
                profile = np.median(a[rows], axis=0) if a.ndim == 2 else a
                g.attrs["outlier_rows"] = outliers
                cols = {"facteur_median_lignes": profile, "facteur_normalise": profile / np.nanmax(profile)}
                w_step = g["wavelength_nm"][:] if "wavelength_nm" in g else None
                if w_step is not None:
                    cols = {"longueur_onde_nm": w_step, **cols}
                _write_csv(out_dir / f"intellical_etape_{i + 1}.csv", cols)
                summary["steps"].append({"step": i + 1, "center_nm": centers[i], "store_file": p.name,
                                         "shape": list(a.shape), "association": how,
                                         "residual": None if not np.isfinite(resid) else round(resid, 6),
                                         "k": None if not np.isfinite(k) else k,
                                         "outlier_rows": outliers,
                                         "variation": float(np.nanmax(profile) / np.nanmin(profile))})
        h.attrs["summary"] = json.dumps(summary, ensure_ascii=False, default=str)

    (out_dir / "resume.txt").write_text(format_summary(summary), encoding="utf-8")
    return summary


def valid_rows(a):
    """Lignes du facteur cohérentes entre elles ; écarte les lignes aberrantes (ex. dernière ligne du capteur,
    ~250 fois plus grande que les autres dans la calibration de 2022)."""
    if a.ndim != 2 or a.shape[0] == 1:
        return np.arange(a.shape[0] if a.ndim == 2 else 1)
    level = np.nanmedian(a, axis=1)
    ref = np.nanmedian(level)
    return np.flatnonzero(np.abs(level / ref - 1) < 0.5)


def _write_csv(path, columns):
    names = list(columns)
    data = np.column_stack([np.arange(len(next(iter(columns.values()))))] + [columns[n] for n in names])
    np.savetxt(path, data, delimiter=";", header=";".join(["pixel"] + names), comments="",
               fmt=["%d"] + ["%.8g"] * len(names), encoding="utf-8")


def format_summary(summary):
    s = summary["settings"]
    lines = [f"Source : {summary['source']}",
             f"Réseau : {s.get('grating')}  centre : {s.get('center_nm')} nm"
             + (f"  (step and glue : {s.get('step_centers_nm')})" if s.get("step_and_glue") else ""),
             f"Exposition : {s.get('exposure_ms')} ms", ""]
    w = summary.get("wavelength")
    if w:
        lines.append(f"Longueur d'onde : {w['pixels']} pixels, {w['from_nm']:.3f} → {w['to_nm']:.3f} nm, "
                     f"calibrée le {w['date']}, raie laser {w['laser_line_nm']} nm")
    for key, label in (("background", "Fond"), ("flatfield", "Flatfield")):
        if key in summary:
            b = summary[key]
            lines.append(f"{label} de référence : {b['file']} ({'activé' if b['enabled'] else 'désactivé'} "
                         f"dans l'expérience), {b['shape']}, exposition {b['exposure_ms']} ms")
    lines.append(f"IntelliCal : {summary.get('intensity_calibration_date') or 'aucune'}")
    for st in summary["steps"]:
        lines.append(f"  étape {st['step']} (centre {st['center_nm']} nm) : {st['store_file']} {st['shape']}, "
                     f"variation max/min {st['variation']:.3f}, lignes aberrantes {st['outlier_rows'] or 'aucune'}, "
                     f"association {st['association']}"
                     + (f", écart {st['residual']:.2e}, k = {st['k']:.4g}" if st["residual"] is not None else ""))
    for n in summary["notes"]:
        lines.append("Note : " + n)
    lines += ["", "Correction : corrigé = k · A · (brut − fond), pixel par pixel ; k est une constante de",
              "normalisation de LightField (sans effet sur les intensités relatives)."]
    return "\n".join(lines) + "\n"
