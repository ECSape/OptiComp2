# -*- coding: utf-8 -*-
"""Reference-standard reflectance models Ry(lambda, theta, pol) and Fresnel helpers."""
import os

import numpy as np

STD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "standards")
SI_VALID_MIN_NM = 380.0      # tables were computed with real n only; Si absorbs strongly below this


def fresnel(n, theta_deg, pol, n0=1.0):
    """Single-interface Fresnel power reflectance from medium n0 into n (real n), pol 'S' or 'P'."""
    n = np.asarray(n, dtype=float)
    th = np.deg2rad(np.asarray(theta_deg, dtype=float))
    ci = np.cos(th)
    st = n0 * np.sin(th) / n
    ct = np.sqrt(np.clip(1.0 - st ** 2, 0.0, None))
    if pol.upper() == "S":
        r = (n0 * ci - n * ct) / (n0 * ci + n * ct)
    else:
        r = (n * ci - n0 * ct) / (n * ci + n0 * ct)
    return r ** 2


def bk7_index(wl_nm):
    """Schott N-BK7 Sellmeier (wavelength in nm)."""
    l2 = (np.asarray(wl_nm, dtype=float) / 1000.0) ** 2
    n2 = 1 + 1.03961212 * l2 / (l2 - 0.00600069867) + 0.231792344 * l2 / (l2 - 0.0200179144) + 1.01046945 * l2 / (l2 - 103.560653)
    return np.sqrt(n2)


def slab_to_single_surface(R_meas, R_back):
    """Invert the incoherent, non-absorbing slab model  R = R1 + (1-R1)^2 R2 / (1 - R1 R2)
    to the front-surface reflectance R1, given the measured total R and the back-surface
    Fresnel reflectance R2 (bare substrate/air interface, same external angle by reciprocity).
    Closed form: R1 = (R - R2) / (1 - 2 R2 + R R2)."""
    R, R2 = np.asarray(R_meas, dtype=float), np.asarray(R_back, dtype=float)
    return (R - R2) / (1.0 - 2.0 * R2 + R * R2)


def slab_total(R1, R2):
    return R1 + (1 - R1) ** 2 * R2 / (1 - R1 * R2)


class Standard(object):
    name = "standard"

    def reflectance(self, wl_nm, theta_deg, pol):
        raise NotImplementedError

    def valid_mask(self, wl_nm):
        return np.ones(len(wl_nm), dtype=bool)


class ConstantStandard(Standard):
    def __init__(self, value=0.99, name=None):
        self.value = float(value)
        self.name = name or "constant %.3f" % value

    def reflectance(self, wl_nm, theta_deg, pol):
        return np.full(len(wl_nm), self.value)


class SiliconStandard(Standard):
    """Polished crystalline silicon Fresnel tables shipped with the original OptiComp.

    silicon_TE.csv: header row ',0,1,...,80'; rows: wavelength, R(0..80 deg)
    silicon_TM.csv: header row '0,1,...,80' (no wavelength column); same wavelength rows
    """
    name = "polished crystalline silicon (Fresnel tables, real n only)"

    def __init__(self, directory=STD_DIR):
        te = np.genfromtxt(os.path.join(directory, "silicon_TE.csv"), delimiter=",", encoding="utf-8-sig")
        tm = np.genfromtxt(os.path.join(directory, "silicon_TM.csv"), delimiter=",", encoding="utf-8-sig")
        self.wl = te[1:, 0]
        self.angles = te[0, 1:]
        self.R = {"S": te[1:, 1:], "P": tm[1:, :]}
        if self.R["P"].shape != self.R["S"].shape:
            raise ValueError("TE/TM tables differ in shape: %s vs %s" % (self.R["S"].shape, self.R["P"].shape))
        if not np.allclose(tm[0, :], self.angles):
            raise ValueError("TM header angles do not match TE header")

    def reflectance(self, wl_nm, theta_deg, pol):
        """Bilinear interpolation in wavelength and angle."""
        tab = self.R[pol.upper()]
        th = float(theta_deg)
        j = int(np.clip(np.searchsorted(self.angles, th) - 1, 0, len(self.angles) - 2))
        f = (th - self.angles[j]) / (self.angles[j + 1] - self.angles[j])
        col = (1 - f) * tab[:, j] + f * tab[:, j + 1]
        return np.interp(np.asarray(wl_nm, dtype=float), self.wl, col)

    def valid_mask(self, wl_nm):
        wl = np.asarray(wl_nm, dtype=float)
        return (wl >= SI_VALID_MIN_NM) & (wl <= self.wl[-1])
