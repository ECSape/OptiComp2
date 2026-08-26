# -*- coding: utf-8 -*-
"""Angle-resolved reflectance of the 2026-08-26 ASA 3D-printed specimens.

Reproduces every number, table and figure used in the measurements thesis chapter
from the archived session directories. Self-contained: reads the six sample
sessions in this directory and the shared white / silicon references archived
alongside in ../2026-08-26_si_validation/.

    python3 analyze_asa.py            # writes figures + CSVs + LaTeX table fragments

Figures are written as vector PDF (for LaTeX) and PNG (for quick view) into
../../thesis/figures/ in a Nature-style layout (89 mm / 183 mm columns,
7 pt sans, no top/right spines, Wong colour-blind-safe palette).
"""
import os
import sys

import numpy as np
import matplotlib as mpl
mpl.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, "..", "..", ".."))
SIVAL = os.path.join(HERE, "..", "2026-08-26_si_validation")
FIGDIR = os.path.normpath(os.path.join(HERE, "..", "..", "thesis", "figures"))
os.makedirs(FIGDIR, exist_ok=True)
sys.path.insert(0, REPO)
from analysis import var, standards

# ----------------------------------------------------------------------------- Nature style
MM = 1.0 / 25.4
COL1, COL2 = 89 * MM, 183 * MM                    # Nature single / double column width
# Wong (2011) colour-blind-safe palette
WONG = ["#000000", "#E69F00", "#56B4E9", "#009E73", "#F0E442", "#0072B2", "#D55E00", "#CC79A7"]
mpl.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["Arial", "Helvetica", "Liberation Sans", "DejaVu Sans"],
    "font.size": 7, "axes.labelsize": 7, "axes.titlesize": 7,
    "xtick.labelsize": 6, "ytick.labelsize": 6, "legend.fontsize": 6,
    "axes.linewidth": 0.5, "axes.labelpad": 2.0,
    "xtick.direction": "out", "ytick.direction": "out",
    "xtick.major.width": 0.5, "ytick.major.width": 0.5,
    "xtick.major.size": 2.5, "ytick.major.size": 2.5,
    "xtick.minor.width": 0.4, "ytick.minor.width": 0.4,
    "lines.linewidth": 1.0, "lines.markersize": 3, "legend.frameon": False,
    "legend.handlelength": 1.4, "legend.handletextpad": 0.5, "legend.labelspacing": 0.25,
    "savefig.dpi": 600, "savefig.bbox": "tight", "savefig.pad_inches": 0.01,
    "pdf.fonttype": 42, "ps.fonttype": 42, "svg.fonttype": "none",
})


def nice(ax):
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.tick_params(length=2.5, width=0.5)


def panel_label(ax, s, x=-0.22, y=1.04):
    ax.text(x, y, s, transform=ax.transAxes, fontsize=8, fontweight="bold", va="bottom", ha="right")


def save(fig, stem):
    for ext in ("pdf", "png"):
        fig.savefig(os.path.join(FIGDIR, stem + "." + ext))
    plt.close(fig)
    print("  wrote figures/%s.{pdf,png}" % stem)


# ----------------------------------------------------------------------------- data
white = var.Session(os.path.join(SIVAL, "white_new"))
si = var.Session(os.path.join(SIVAL, "si_new"))
const = standards.ConstantStandard(0.99)
fres = standards.SiliconStandard(os.path.join(REPO, "standards"))

SAMPLES = ["asa_3d_01", "asa_3d_02", "asa_3d_03", "asa_3d_04", "asa_3d_pei", "asa_3d_std"]
COLOR = {s: WONG[1 + i] for i, s in enumerate(SAMPLES)}
BAND = None                                        # set after first result


def band_mean(r, lo=450, hi=900):
    m = (r.wl >= lo) & (r.wl <= hi)
    return np.array([np.nanmean(r.R[i][m]) for i in range(len(r.thetas))])


def band_std(r, lo=450, hi=900):
    m = (r.wl >= lo) & (r.wl <= hi)
    return np.array([np.nanstd(r.R[i][m]) for i in range(len(r.thetas))])


R = {}                                             # R[sample][pol] = Result
for s in SAMPLES:
    ses = var.Session(os.path.join(HERE, s))
    R[s] = {p: var.compute_reflectance(ses, white, const, p, use_db=True) for p in ("S", "P")}
Rsi = {p: var.compute_reflectance(si, white, const, p, use_db=True) for p in ("S", "P")}
thetas = R[SAMPLES[0]]["S"].thetas
th_fine = np.arange(4, 82, 0.5)

# ----------------------------------------------------------------------------- Figure 1: Si validation
print("Figure 1 (silicon validation)")
fig, axes = plt.subplots(1, 2, figsize=(COL2, 0.42 * COL2))
ax = axes[0]
for pol, c in (("S", WONG[5]), ("P", WONG[6])):
    for w, mk in ((550, "o"), (800, "s")):
        r = Rsi[pol]
        meas = r.at_wavelength(w)
        ax.plot(th_fine, [fres.reflectance(np.array([w]), t, pol)[0] for t in th_fine], "-", color=c, lw=0.9)
        ax.plot(r.thetas, meas, mk, color=c, ms=3, mfc="none", mew=0.8)
ax.set_xlabel("angle of incidence (deg)"); ax.set_ylabel(r"reflectance $R$")
ax.set_xlim(0, 82); ax.set_ylim(0, 1.0); nice(ax); panel_label(ax, "a")
ax.text(10, 0.92, "Si, $\\lambda=550,800$ nm", fontsize=6)
# legend proxies
from matplotlib.lines import Line2D
prox = [Line2D([], [], color=WONG[5], lw=0.9, label="s-pol"),
        Line2D([], [], color=WONG[6], lw=0.9, label="p-pol"),
        Line2D([], [], color="0.4", marker="o", mfc="none", ls="none", ms=3, label="measured"),
        Line2D([], [], color="0.4", lw=0.9, label="Fresnel")]
ax.legend(handles=prox, loc="upper left", bbox_to_anchor=(0.02, 0.86), ncol=1)

ax = axes[1]
for pol, c, mk in (("S", WONG[5], "o"), ("P", WONG[6], "s")):
    r = Rsi[pol]
    ratio = []
    for i, t in enumerate(r.thetas):
        m = (r.wl >= 450) & (r.wl <= 900)
        f = fres.reflectance(r.wl, t, pol)
        ratio.append(np.nanmedian((r.R[i] / f)[m]))
    ax.plot(r.thetas, ratio, mk + "-", color=c, ms=3, mfc="none", mew=0.8, lw=0.8)
ax.axhspan(0.95, 1.05, color="0.85", zorder=0, lw=0)
ax.axvline(60, color="0.5", ls=":", lw=0.6)
ax.set_xlabel("angle of incidence (deg)"); ax.set_ylabel(r"measured $/$ Fresnel")
ax.set_xlim(0, 82); ax.set_ylim(0.6, 1.7); nice(ax); panel_label(ax, "b")
ax.text(61, 1.55, "overfill", fontsize=6, color="0.4")
ax.legend(handles=[Line2D([], [], color=WONG[5], marker="o", mfc="none", ms=3, lw=0.8, label="s-pol"),
                   Line2D([], [], color=WONG[6], marker="s", mfc="none", ms=3, lw=0.8, label="p-pol")],
          loc="lower left")
save(fig, "meas_si_validation")

# ----------------------------------------------------------------------------- Figure 2: ASA R(theta)
print("Figure 2 (ASA angle dependence)")
fig, axes = plt.subplots(1, 2, figsize=(COL2, 0.42 * COL2), sharey=True)
for ax, pol, tag in ((axes[0], "S", "s-polarisation"), (axes[1], "P", "p-polarisation")):
    for s in SAMPLES:
        r = R[s][pol]
        ax.plot(r.thetas, band_mean(r), "o-", color=COLOR[s], ms=2.5, lw=0.9, label=s.replace("asa_3d_", ""))
    ax.axvspan(60, 82, color="0.9", zorder=0, lw=0)
    ax.set_xlabel("angle of incidence (deg)"); ax.set_xlim(0, 82); ax.set_ylim(0, None); nice(ax)
    ax.text(0.03, 0.94, tag, transform=ax.transAxes, fontsize=6.5)
axes[0].set_ylabel(r"reflectance $R$ (450--900 nm)")
axes[0].text(62, axes[0].get_ylim()[1] * 0.62, "beam\noverfill", fontsize=6, color="0.5", ha="left", va="center")
axes[0].legend(loc="upper left", bbox_to_anchor=(0.03, 0.86), title="specimen",
               title_fontsize=6, ncol=2, columnspacing=1.0)
panel_label(axes[0], "a"); panel_label(axes[1], "b", x=-0.06)
save(fig, "meas_asa_angle")

# ----------------------------------------------------------------------------- Figure 3: ASA spectra
print("Figure 3 (ASA spectral reflectance at 40 deg, boxcar-smoothed)")


def smooth(y, win=41):
    """Reflectance-domain boxcar (~20 nm) that ignores NaNs at the band edges."""
    y = np.asarray(y, float)
    k = np.ones(win) / win
    ok = np.isfinite(y).astype(float)
    ys = np.where(np.isfinite(y), y, 0.0)
    num = np.convolve(ys, k, mode="same")
    den = np.convolve(ok, k, mode="same")
    out = num / np.where(den > 0, den, np.nan)
    return out


fig, ax = plt.subplots(figsize=(COL1, 0.72 * COL1))
i40 = thetas.index(40.0)
m = (R[SAMPLES[0]]["S"].wl >= 450) & (R[SAMPLES[0]]["S"].wl <= 950)
for s in SAMPLES:
    r = R[s]["S"]
    ax.plot(r.wl[m], smooth(r.R[i40])[m], color=COLOR[s], lw=1.0, label=s.replace("asa_3d_", ""))
ax.set_xlabel("wavelength (nm)"); ax.set_ylabel(r"reflectance $R$ (s-pol, $40^\circ$)")
ax.set_xlim(450, 950); ax.set_ylim(0, None); nice(ax)
ax.text(0.97, 0.03, "20 nm boxcar", transform=ax.transAxes, fontsize=5.5, color="0.5", ha="right")
ax.legend(loc="lower left", bbox_to_anchor=(0.02, 0.02), ncol=3, title="specimen",
          title_fontsize=6, columnspacing=1.0, handlelength=1.2)
save(fig, "meas_asa_spectra")

# ----------------------------------------------------------------------------- repeatability 01/02/04
print("\nRepeatability of the near-identical specimens 01/02/04 (450-900 nm mean R):")
rep = ["asa_3d_01", "asa_3d_02", "asa_3d_04"]
for pol in ("S", "P"):
    stack = np.array([band_mean(R[s][pol]) for s in rep])            # (3, n_theta)
    mean = stack.mean(0); sd = stack.std(0, ddof=1)
    usable = np.array(thetas) <= 60
    rel = 100 * np.nanmean((sd / mean)[usable])
    print("  %s: mean pairwise CV over theta<=60 deg = %.1f%%  (per-angle sd/mean)" % (pol, rel))
    for t, mu, s_ in zip(thetas, mean, sd):
        if t in (8, 20, 40, 60, 80):
            print("     %2.0f deg  R=%.4f +- %.4f (%.1f%%)" % (t, mu, s_, 100 * s_ / mu))

# ----------------------------------------------------------------------------- SNR diagnostics
print("\nNet signal (peak - dark) per specimen:")
import json
for s in SAMPLES:
    m = json.load(open(os.path.join(HERE, s, "manifest.json")))
    dk = [e["peak"] for e in m["spectra"] if e["kind"] == "dark"][0]
    nets = [e["peak"] - dk for e in m["spectra"] if e["kind"] == "var"]
    print("  %-11s dark=%d  net %d..%d counts" % (s, dk, min(nets), max(nets)))

# ----------------------------------------------------------------------------- CSV exports
for s in SAMPLES:
    for pol in ("S", "P"):
        R[s][pol].save_csv(os.path.join(HERE, "R_%s_%s.csv" % (s, pol)))
print("\nwrote R_<specimen>_<pol>.csv for all specimens")

# ----------------------------------------------------------------------------- LaTeX table of R values
print("writing tab_asa_reflectance.tex")
angles_tab = [8, 20, 40, 60, 80]
lines = []
lines.append("% auto-generated by analyze_asa.py -- reflectance (450-900 nm mean), white=0.99, DB corrected")
lines.append(r"\begin{tabular}{l" + "c" * len(angles_tab) + "}")
lines.append(r"\toprule")
lines.append("specimen & " + " & ".join(r"$%d^\circ$" % a for a in angles_tab) + r" \\")
lines.append(r"\midrule")
for pol in ("S", "P"):
    lines.append(r"\multicolumn{%d}{l}{\emph{%s-polarisation}}\\" % (len(angles_tab) + 1, pol.lower()))
    for s in SAMPLES:
        r = R[s][pol]; bm = band_mean(r)
        row = {t: bm[i] for i, t in enumerate(thetas)}
        cells = " & ".join("%.3f" % row[a] for a in angles_tab)
        lines.append("\\quad %s & %s \\\\" % (s.replace("asa_3d_", r"\texttt{").replace("_", r"\_") + "}", cells))
lines.append(r"\bottomrule")
lines.append(r"\end{tabular}")
with open(os.path.join(FIGDIR, "tab_asa_reflectance.tex"), "w") as f:
    f.write("\n".join(lines) + "\n")
print("done.")
