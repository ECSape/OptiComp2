# -*- coding: utf-8 -*-
"""Variable-angle reflectance from OptiComp2 session directories (thesis eq. 4.10).

    Rx = (Sx - Sd) / (Sy - Sd) * (Scy - Sd) / (Scx - Sd) * Ry

x = sample session, y = reference session, Sc = double-beam (sphere wall) spectra,
Sd = dark taken at the same integration time as the spectrum it is subtracted from.
Sessions may use different integration times: net counts are then divided by the
integration time (linear detector assumption) and this is reported in `notes`.
"""
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from hw import bwtek

ACTIVE = slice(bwtek.ACTIVE_FIRST, bwtek.ACTIVE_LAST + 1)     # 254..2030 inclusive = 1777 pixels


class AnalysisError(Exception):
    pass


class Session(object):
    def __init__(self, path):
        self.path = path
        mpath = os.path.join(path, "manifest.json")
        if not os.path.isfile(mpath):
            raise AnalysisError("no manifest.json in %s" % path)
        with open(mpath, encoding="utf-8") as f:
            self.manifest = json.load(f)
        self.records = self.manifest.get("spectra", [])
        self._cache = {}
        self.wl = bwtek.wavelengths()[ACTIVE]

    @property
    def name(self):
        return os.path.basename(os.path.normpath(self.path))

    def counts(self, rec):
        f = rec["file"]
        if f not in self._cache:
            data = np.loadtxt(os.path.join(self.path, f), delimiter=",", skiprows=1)
            self._cache[f] = data[:, 1].astype(float)
        return self._cache[f]

    def of_kind(self, kind, pol=None):
        return [r for r in self.records if r.get("kind") == kind and (pol is None or r.get("pol") == pol)]

    def dark_for(self, rec):
        """Dark with the same integration time, closest in time (later records win ties)."""
        darks = [d for d in self.of_kind("dark") if d["integration_ms"] == rec["integration_ms"]]
        if not darks:
            raise AnalysisError("no dark at %s ms for %s in %s" % (rec["integration_ms"], rec["tag"], self.name))
        return min(darks, key=lambda d: abs(_t(d) - _t(rec)))

    def net(self, rec, per_ms=False):
        """Dark-subtracted counts on the active pixels; optionally per millisecond."""
        s = self.counts(rec)[ACTIVE] - self.counts(self.dark_for(rec))[ACTIVE]
        return s / rec["integration_ms"] if per_ms else s

    def thetas(self, pol):
        return sorted(set(float(r["theta"]) for r in self.of_kind("var", pol)))

    def var(self, pol, theta):
        recs = [r for r in self.of_kind("var", pol) if abs(float(r["theta"]) - theta) < 1e-6]
        if not recs:
            raise AnalysisError("no %s spectrum at theta=%g in %s" % (pol, theta, self.name))
        return recs[-1]                      # latest acquisition of that angle wins

    def db(self, pol):
        recs = self.of_kind("db", pol)
        return recs[-1] if recs else None

    def saturated(self, rec):
        return int(rec.get("saturated_active", 0)) > 0


def _t(rec):
    import time
    return time.mktime(time.strptime(rec["time"], "%Y-%m-%d %H:%M:%S"))


class Result(object):
    def __init__(self, wl, thetas, R, pol, notes, valid):
        self.wl = wl                  # (n_lambda,)
        self.thetas = thetas          # list
        self.R = R                    # (n_theta, n_lambda)
        self.pol = pol
        self.notes = notes
        self.valid = valid            # (n_lambda,) bool mask from the standard

    def at_wavelength(self, wl_nm):
        i = int(np.argmin(np.abs(self.wl - wl_nm)))
        return self.R[:, i]

    def save_csv(self, path):
        head = "wavelength_nm," + ",".join("%g" % t for t in self.thetas)
        np.savetxt(path, np.column_stack([self.wl, self.R.T]), delimiter=",", header=head, comments="", fmt="%.6g")


def compute_reflectance(sample, reference, standard, pol, use_db=True, thetas=None):
    pol = pol.upper()
    notes = []
    if thetas is None:
        thetas = sorted(set(sample.thetas(pol)) & set(reference.thetas(pol)))
    if not thetas:
        raise AnalysisError("no common angles for polarisation %s" % pol)
    wl = sample.wl
    # double-beam factor (Scy - Sd) / (Scx - Sd)
    db_factor = np.ones(len(wl))
    dbx, dby = sample.db(pol), reference.db(pol)
    if use_db and dbx is not None and dby is not None:
        per_ms = dbx["integration_ms"] != dby["integration_ms"]
        if per_ms:
            notes.append("DB spectra use different integration times (%s vs %s ms): normalised per ms" % (dbx["integration_ms"], dby["integration_ms"]))
        db_factor = reference.net(dby, per_ms) / sample.net(dbx, per_ms)
    elif use_db:
        notes.append("no double-beam spectra in %s -> substitution correction skipped" % ("both" if dbx is None and dby is None else ("sample" if dbx is None else "reference")))
    R = np.zeros((len(thetas), len(wl)))
    for i, th in enumerate(thetas):
        rx, ry = sample.var(pol, th), reference.var(pol, th)
        per_ms = rx["integration_ms"] != ry["integration_ms"]
        if per_ms and i == 0:
            notes.append("sample/reference integration times differ (%s vs %s ms): normalised per ms" % (rx["integration_ms"], ry["integration_ms"]))
        if sample.saturated(rx) or reference.saturated(ry):
            notes.append("theta %g: saturated pixels present" % th)
        with np.errstate(divide="ignore", invalid="ignore"):
            R[i] = sample.net(rx, per_ms) / reference.net(ry, per_ms) * db_factor * standard.reflectance(wl, th, pol)
    return Result(wl, thetas, R, pol, notes, standard.valid_mask(wl))
