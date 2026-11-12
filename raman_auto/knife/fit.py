"""Ajustements du knife-edge : bord sur une carte 2D, profil erf le long d'une ligne, caustique w(z).

Faisceau gaussien d'intensité I(r) ∝ exp(−2 r² / w²) (w = rayon à 1/e²). Un bord droit à la distance s du
centre du faisceau transmet  P(s) = P_bas + (P_haut − P_bas) · ½ [1 + erf(√2 (s − s0) / w)].
"""
import math

import numpy as np
from scipy.optimize import curve_fit, least_squares
from scipy.special import erf

SQRT2 = math.sqrt(2.0)
TEN_NINETY = 1.2816  # distance 10 % → 90 % = 1,2816 w


def edge_profile(s, s0, w, p_low, p_high):
    return p_low + (p_high - p_low) * 0.5 * (1.0 + erf(SQRT2 * (s - s0) / w))


def _noise(p):
    """Écart-type du bruit estimé par différences successives (insensible à la marche)."""
    if p.size < 4:
        return 0.0
    return float(np.median(np.abs(np.diff(p))) / 0.954)  # médiane |N(0, σ√2)| = 0,954 σ


# ---------------------------------------------------------------------- ligne
def fit_line(s, p):
    """Ajuste un profil erf ; s = position le long de la ligne (mm), p = signal.

    Retourne un dict : s0, w (rayon 1/e², mm), p_low, p_high, erreurs, contraste/bruit, ok, message.
    """
    s, p = np.asarray(s, float), np.asarray(p, float)
    good = np.isfinite(s) & np.isfinite(p)
    s, p = s[good], p[good]
    order = np.argsort(s)
    s, p = s[order], p[order]
    out = {"ok": False, "s0": math.nan, "w": math.nan, "p_low": math.nan, "p_high": math.nan,
           "s0_err": math.nan, "w_err": math.nan, "snr": math.nan, "message": ""}
    if s.size < 6:
        out["message"] = "trop peu de points"
        return out
    span = s[-1] - s[0]
    step = float(np.median(np.diff(s))) if s.size > 1 else span
    k = max(2, s.size // 10)
    first, last = float(np.median(p[:k])), float(np.median(p[-k:]))
    noise = _noise(p)
    contrast = abs(last - first)
    out["snr"] = contrast / noise if noise > 0 else math.inf
    if out["snr"] < 5:
        out["message"] = "pas de bord visible sur la ligne (contraste trop faible)"
        return out
    # Estimation initiale : 10 % / 50 % / 90 % du saut
    frac = (p - first) / (last - first)
    smooth = np.convolve(frac, np.ones(3) / 3, mode="same")
    def crossing(level):
        idx = np.flatnonzero(np.diff(np.sign(smooth - level)) != 0)
        return float(s[idx[len(idx) // 2]]) if idx.size else float(s[s.size // 2])
    s0 = crossing(0.5)
    w0 = max(abs(crossing(0.9) - crossing(0.1)) / TEN_NINETY, step)
    try:
        popt, pcov = curve_fit(edge_profile, s, p, p0=[s0, w0, first, last],
                               bounds=([s[0] - span, step / 20, -np.inf, -np.inf],
                                       [s[-1] + span, 4 * span, np.inf, np.inf]), maxfev=20000)
    except Exception as e:  # pas de convergence
        out["message"] = f"ajustement impossible : {e}"
        return out
    err = np.sqrt(np.diag(pcov)) if np.all(np.isfinite(pcov)) else np.full(4, math.nan)
    out.update(s0=float(popt[0]), w=float(popt[1]), p_low=float(popt[2]), p_high=float(popt[3]),
               s0_err=float(err[0]), w_err=float(err[1]))
    inside = s[0] + 0.5 * out["w"] <= out["s0"] <= s[-1] - 0.5 * out["w"]
    covered = span >= 3 * out["w"]
    if not inside:
        out["message"] = "le bord est trop près d'une extrémité de la ligne"
    elif not covered:
        out["message"] = "ligne trop courte par rapport au spot"
    elif out["w"] < step:
        out["message"] = f"spot non résolu : w < pas ({step * 1000:.1f} µm)"
    else:
        out["ok"] = True
    return out


# ---------------------------------------------------------------------- carte 2D
def fit_edge_map(xy, p):
    """Trouve un bord droit sur une carte : P = a + b · ½[1 + erf(√2 (n·r − d) / w)], n = (cos θ, sin θ).

    θ est la direction de la normale au bord, orientée vers le côté où le signal est haut.
    Retourne un dict : theta_deg, edge_angle_deg (direction du bord), d, w, p_low, p_high, ok, message.
    """
    xy, p = np.asarray(xy, float), np.asarray(p, float)
    good = np.all(np.isfinite(xy), axis=1) & np.isfinite(p)
    xy, p = xy[good], p[good]
    out = {"ok": False, "message": "", "theta_deg": math.nan, "edge_angle_deg": math.nan, "d": math.nan,
           "w": math.nan, "p_low": math.nan, "p_high": math.nan, "snr": math.nan}
    if p.size < 9:
        out["message"] = "trop peu de points dans la carte"
        return out
    lo, hi = np.percentile(p, [5, 95])
    noise_est = max(float(np.std(p[p <= np.percentile(p, 20)])), 1e-30)
    out["snr"] = (hi - lo) / noise_est
    if hi - lo <= 0 or out["snr"] < 5:
        out["message"] = "pas de bord visible : le signal est uniforme sur la carte"
        return out
    pn = (p - lo) / (hi - lo)
    # Direction initiale : gradient d'un plan ajusté sur la carte normalisée
    center = xy.mean(axis=0)
    A = np.column_stack((xy - center, np.ones(len(p))))
    gx, gy, _ = np.linalg.lstsq(A, pn, rcond=None)[0]
    theta = math.atan2(gy, gx)
    n = np.array([math.cos(theta), math.sin(theta)])
    proj = xy @ n
    mid = np.abs(pn - 0.5) < 0.3
    d0 = float(np.median(proj[mid])) if mid.any() else float(np.median(proj))
    extent = float(np.ptp(proj)) or 1.0
    spacing = _grid_spacing(xy)
    w0 = max(spacing, extent / 20)

    def residual(q):
        a, b, th, d, w = q
        s = xy @ np.array([math.cos(th), math.sin(th)]) - d
        return a + b * 0.5 * (1 + erf(SQRT2 * s / w)) - p

    best = None
    for dth in (0.0, 0.35, -0.35):  # quelques départs en angle pour éviter un minimum local
        q0 = [lo, hi - lo, theta + dth, d0, w0]
        try:
            r = least_squares(residual, q0, bounds=([-np.inf, -np.inf, -np.inf, -np.inf, spacing / 50],
                                                    [np.inf, np.inf, np.inf, np.inf, 2 * extent]))
        except Exception:
            continue
        if best is None or r.cost < best.cost:
            best = r
    if best is None:
        out["message"] = "ajustement impossible"
        return out
    a, b, th, d, w = best.x
    if b < 0:  # orienter la normale vers le côté haut
        a, b, th, d = a + b, -b, th + math.pi, -d
    th = math.atan2(math.sin(th), math.cos(th))
    out.update(theta_deg=math.degrees(th), edge_angle_deg=(math.degrees(th) + 90.0) % 180.0, d=float(d),
               w=float(w), p_low=float(a), p_high=float(a + b))
    proj = xy @ np.array([math.cos(th), math.sin(th)])
    if not (proj.min() < d < proj.max()):
        out["message"] = "le bord ajusté passe hors de la carte"
    else:
        out["ok"] = True
    return out


def _grid_spacing(xy):
    """Pas typique entre points voisins d'une carte."""
    steps = []
    for k in range(2):
        u = np.unique(np.round(xy[:, k], 6))
        if u.size > 1:
            steps.append(float(np.median(np.diff(u))))
    return min(steps) if steps else 1.0


def edge_point_near(fit, point):
    """Projection d'un point (x, y) sur le bord ajusté."""
    n = np.array([math.cos(math.radians(fit["theta_deg"])), math.sin(math.radians(fit["theta_deg"]))])
    r = np.asarray(point, float)
    return r - (r @ n - fit["d"]) * n


# ---------------------------------------------------------------------- caustique
def caustic(z, z0, w0, zr):
    return w0 * np.sqrt(1.0 + ((z - z0) / zr) ** 2)


def fit_caustic(z, w, wavelength_nm=None):
    """Ajuste w(z) = w0 √(1 + ((z − z0)/zR)²). Retourne z0, w0, zR (mm), M² si λ est donné, ok, message."""
    z, w = np.asarray(z, float), np.asarray(w, float)
    good = np.isfinite(z) & np.isfinite(w) & (w > 0)
    z, w = z[good], w[good]
    out = {"ok": False, "message": "", "z0": math.nan, "w0": math.nan, "zr": math.nan, "m2": math.nan,
           "z0_err": math.nan, "w0_err": math.nan}
    if z.size == 0:
        out["message"] = "aucune largeur mesurée"
        return out
    i = int(np.argmin(w))
    out.update(z0=float(z[i]), w0=float(w[i]))
    if z.size < 4:
        out["message"] = "moins de 4 hauteurs valides : minimum mesuré seulement"
        return out
    span = float(np.ptp(z)) or 1.0
    # zR initial : là où w ≈ √2 w0
    above = np.abs(z[w > math.sqrt(2) * w[i]] - z[i])
    zr0 = float(above.min()) if above.size else span
    try:
        popt, pcov = curve_fit(caustic, z, w, p0=[z[i], w[i], zr0],
                               bounds=([z.min() - span, 0, span / 1000], [z.max() + span, np.inf, 100 * span]),
                               maxfev=20000)
    except Exception as e:
        out["message"] = f"ajustement impossible : {e}"
        return out
    err = np.sqrt(np.diag(pcov)) if np.all(np.isfinite(pcov)) else np.full(3, math.nan)
    out.update(z0=float(popt[0]), w0=float(popt[1]), zr=float(popt[2]), z0_err=float(err[0]),
               w0_err=float(err[1]))
    if wavelength_nm:
        out["m2"] = math.pi * out["w0"] ** 2 / (wavelength_nm * 1e-6 * out["zr"])
    if not (z.min() <= out["z0"] <= z.max()):
        out["message"] = "foyer hors de la plage de Z mesurée : élargir la plage"
    else:
        out["ok"] = True
    return out
