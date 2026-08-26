import os, sys
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
sys.path.insert(0, REPO)
from analysis import var, standards
SP = os.path.dirname(os.path.abspath(__file__))
si = var.Session(os.path.join(SP, "si_new"))
white = var.Session(os.path.join(SP, "white_new"))
const = standards.ConstantStandard(0.99)
fres = standards.SiliconStandard(os.path.join(REPO, "standards"))
res = {}
for pol in ("S", "P"):
    r = var.compute_reflectance(si, white, const, pol, use_db=True)
    r_nodb = var.compute_reflectance(si, white, const, pol, use_db=False)
    res[pol] = (r, r_nodb)
    print("pol", pol, "notes:", r.notes)
wls = [400, 450, 500, 550, 650, 800, 1000]
print("\n== measured R_si (white=0.99, with DB) / Fresnel table, ratio at selected wavelengths")
for pol in ("S", "P"):
    r, r0 = res[pol]
    print("\n-- %s --   theta | " % pol + " | ".join("%4d nm meas/fres" % w for w in wls))
    for i, th in enumerate(r.thetas):
        cells = []
        for w in wls:
            k = int(np.argmin(np.abs(r.wl - w)))
            m = np.nanmean(r.R[i, max(0, k-5):k+6]); f = fres.reflectance(np.array([w]), th, pol)[0]
            cells.append("%.3f/%.3f=%.2f" % (m, f, m / f))
        print("  %4.0f | " % th + " | ".join(cells))
# summary stats over 400-1000 nm
print("\n== ratio meas/Fresnel, mean +- sd over 420-1000 nm, per angle")
for pol in ("S", "P"):
    r, r0 = res[pol]
    band = (r.wl >= 420) & (r.wl <= 1000)
    for i, th in enumerate(r.thetas):
        f = fres.reflectance(r.wl, th, pol)
        q = (r.R[i] / f)[band]; q0 = (r0.R[i] / f)[band]
        print("  %s %4.0f  with DB %.3f +- %.3f   without DB %.3f +- %.3f" % (pol, th, np.nanmean(q), np.nanstd(q), np.nanmean(q0), np.nanstd(q0)))
# DB factor magnitude
for pol in ("S", "P"):
    dbx, dby = si.db(pol), white.db(pol)
    fac = white.net(dby) / si.net(dbx)
    band = (si.wl >= 420) & (si.wl <= 1000)
    print("DB factor (Scy-Sd)/(Scx-Sd) %s: %.4f +- %.4f over 420-1000 nm" % (pol, np.nanmean(fac[band]), np.nanstd(fac[band])))
# plot
fig, axes = plt.subplots(2, 2, figsize=(12, 8.5))
for ax, w in zip(axes[0], (550, 800)):
    for pol, c in (("S", "C0"), ("P", "C3")):
        r, _ = res[pol]
        k = int(np.argmin(np.abs(r.wl - w)))
        meas = [np.nanmean(r.R[i, max(0, k-5):k+6]) for i in range(len(r.thetas))]
        th = np.arange(0, 81, 1.0)
        ax.plot(th, [fres.reflectance(np.array([w]), t, pol)[0] for t in th], c + "-", lw=1.2, label="Fresnel %s" % pol)
        ax.plot(r.thetas, meas, c + "o", ms=5, mfc="none", label="measured %s" % pol)
    ax.set_title("Si reflectance vs angle at %d nm" % w); ax.set_xlabel("angle of incidence (deg)"); ax.set_ylabel("R"); ax.set_ylim(0, 1); ax.grid(alpha=0.3); ax.legend()
for ax, pol in zip(axes[1], ("S", "P")):
    r, _ = res[pol]
    for th, c in ((8.0, "C0"), (44.0, "C2"), (64.0, "C1"), (80.0, "C3")):
        if th not in r.thetas: continue
        i = r.thetas.index(th)
        ax.plot(r.wl, r.R[i], c, lw=1, label="measured %g°" % th)
        ax.plot(r.wl, fres.reflectance(r.wl, th, pol), c + "--", lw=1)
    ax.axvspan(200, 380, color="gray", alpha=0.15)
    ax.set_title("Si %s: measured (solid) vs Fresnel (dashed)" % pol); ax.set_xlabel("wavelength (nm)"); ax.set_ylabel("R"); ax.set_ylim(0, 1); ax.set_xlim(340, 1050); ax.grid(alpha=0.3); ax.legend(fontsize=8)
fig.suptitle("si_new vs white_new (white = 0.99 constant, double-beam corrected), 2026-08-26")
fig.tight_layout()
out = os.path.join(SP, "si_vs_fresnel.png"); fig.savefig(out, dpi=110); print("saved", out)
for pol in ("S", "P"):
    res[pol][0].save_csv(os.path.join(SP, "R_si_%s.csv" % pol))
print("saved R_si_S.csv / R_si_P.csv")
