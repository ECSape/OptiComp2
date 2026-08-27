# -*- coding: utf-8 -*-
import json
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from analysis import standards as sd
from analysis import var
from hw import bwtek

WL = bwtek.wavelengths()


class FresnelTests(unittest.TestCase):
    def test_normal_incidence_and_brewster(self):
        self.assertAlmostEqual(float(sd.fresnel(1.5, 0, "S")), 0.04, places=4)
        self.assertAlmostEqual(float(sd.fresnel(1.5, 0, "P")), 0.04, places=4)
        self.assertLess(float(sd.fresnel(1.5, np.degrees(np.arctan(1.5)), "P")), 1e-9)
        self.assertGreater(float(sd.fresnel(1.5, 80, "S")), 0.3)

    def test_slab_inversion_roundtrip(self):
        R1, R2 = 0.12, 0.04
        self.assertAlmostEqual(float(sd.slab_to_single_surface(sd.slab_total(R1, R2), R2)), R1, places=12)
        self.assertAlmostEqual(float(sd.slab_total(0.04, 0.04)), 0.076923, places=5)

    def test_bk7(self):
        self.assertAlmostEqual(float(sd.bk7_index(587.6)), 1.5168, places=3)


class SiliconTests(unittest.TestCase):
    def test_tables(self):
        si = sd.SiliconStandard()
        self.assertEqual(si.R["S"].shape, (1210, 81))
        self.assertAlmostEqual(float(si.reflectance([600.0], 0, "S")[0]), 0.3533, places=3)
        self.assertAlmostEqual(float(si.reflectance([600.0], 0, "P")[0]), float(si.reflectance([600.0], 0, "S")[0]), places=6)
        angs = np.arange(0, 81)
        rp = [float(si.reflectance([600.0], a, "P")[0]) for a in angs]
        self.assertTrue(72 <= angs[int(np.argmin(rp))] <= 78)           # Brewster of Si n~3.9
        self.assertAlmostEqual(float(si.reflectance([600.0], 44.5, "S")[0]),
                               0.5 * (si.reflectance([600.0], 44, "S")[0] + si.reflectance([600.0], 45, "S")[0]), places=9)
        m = si.valid_mask(np.array([300.0, 500.0, 1500.0]))
        self.assertEqual(list(m), [False, True, False])


def make_session(root, name, spectra, it=1000, db=None, dark_level=900.0):
    """spectra: {(pol, theta): counts_per_ms_signal}; writes CSVs + manifest like the runner."""
    d = os.path.join(root, name)
    os.makedirs(d)
    recs = []

    def write(tag, sig, extra):
        counts = np.clip(dark_level + sig * extra.get("integration_ms", it), 0, 65535)
        np.savetxt(os.path.join(d, tag + ".csv"), np.column_stack([WL, counts]), fmt="%.3f,%d", header="wavelength_nm,counts", comments="")
        r = {"tag": tag, "file": tag + ".csv", "time": "2026-08-25 12:00:%02d" % len(recs), "integration_ms": it,
             "average": 1, "peak": int(counts.max()), "saturated_active": int((counts[254:2031] >= 65535).sum())}
        r.update(extra)
        recs.append(r)

    write("dark", np.zeros(2048), {"kind": "dark"})
    for (pol, th), sig in spectra.items():
        write("%s_%s_%g" % (name, pol, th), sig, {"kind": "var", "pol": pol, "theta": float(th)})
    if db:
        for pol, sig in db.items():
            write("%s_DB_%s" % (name, pol), sig, {"kind": "db", "pol": pol})
    with open(os.path.join(d, "manifest.json"), "w") as f:
        json.dump({"spectra": recs}, f)
    return d


class ReflectanceTests(unittest.TestCase):
    def test_recovers_sample_reflectance_with_db_and_different_it(self):
        root = tempfile.mkdtemp()
        lamp = 40.0 * np.exp(-((WL - 600) / 150.0) ** 2) + 5.0           # counts per ms
        si = sd.SiliconStandard()
        ry = {th: si.reflectance(WL, th, "S") for th in (20, 40)}
        true_R = 0.5 + 0.0002 * (WL - 600)                                # the unknown sample
        ref = make_session(root, "ref", {("S", th): lamp * ry[th] for th in (20, 40)}, it=1000, db={"S": lamp * 0.9})
        sam = make_session(root, "sam", {("S", th): lamp * true_R * 0.8 for th in (20, 40)}, it=1500, db={"S": lamp * 0.9 * 0.8})
        res = var.compute_reflectance(var.Session(sam), var.Session(ref), si, "S")
        self.assertEqual(res.thetas, [20.0, 40.0])
        ok = res.valid
        np.testing.assert_allclose(res.R[0][ok], true_R[254:2031][ok], rtol=5e-3)
        np.testing.assert_allclose(res.R[1][ok], true_R[254:2031][ok], rtol=5e-3)
        self.assertTrue(any("integration times differ" in n for n in res.notes))
        out = os.path.join(root, "R.csv")
        res.save_csv(out)
        self.assertEqual(np.loadtxt(out, delimiter=",", skiprows=1).shape, (1777, 3))

    def test_saturated_pixels_are_masked(self):
        root = tempfile.mkdtemp()
        lamp = np.full(2048, 10.0)
        big = lamp.copy()
        big[1000:1010] = 200.0                                            # clips at 65535 in the sample DB
        ref = make_session(root, "ref", {("P", 30): lamp}, db={"P": lamp})
        sam = make_session(root, "sam", {("P", 30): lamp * 0.5}, db={"P": big})
        res = var.compute_reflectance(var.Session(sam), var.Session(ref), sd.ConstantStandard(1.0), "P")
        r = res.R[0]
        self.assertEqual(int(np.isnan(r).sum()), 10)
        self.assertTrue(np.isnan(r[1000 - 254]))
        self.assertAlmostEqual(float(np.nanmedian(r)), 0.5, places=6)
        self.assertTrue(any("masked" in n for n in res.notes))

    def test_missing_dark_and_missing_db(self):
        root = tempfile.mkdtemp()
        lamp = np.full(2048, 10.0)
        ref = make_session(root, "ref", {("P", 30): lamp}, db=None)
        sam = make_session(root, "sam", {("P", 30): lamp * 0.5}, db=None)
        res = var.compute_reflectance(var.Session(sam), var.Session(ref), sd.ConstantStandard(0.99), "P")
        self.assertAlmostEqual(float(np.median(res.R[0])), 0.495, places=6)
        self.assertTrue(any("substitution correction skipped" in n for n in res.notes))
        s = var.Session(sam)
        s.records[0]["integration_ms"] = 5                                # dark IT no longer matches
        with self.assertRaises(var.AnalysisError):
            s.net(s.records[1])




class MaskRegressionTests(unittest.TestCase):
    def test_invalid_wavelengths_exported_as_nan(self):
        # Si tabulation is invalid below 380 nm: save_csv / at_wavelength must emit NaN there,
        # never a physically-meaningless reflectance (Bug: standard valid mask was ignored)
        root = tempfile.mkdtemp()
        lamp = np.full(2048, 20.0)
        si = sd.SiliconStandard()
        ref = make_session(root, "ref", {("S", 20): lamp * si.reflectance(WL, 20, "S")})
        sam = make_session(root, "sam", {("S", 20): lamp * 0.5})
        res = var.compute_reflectance(var.Session(sam), var.Session(ref), si, "S")
        valid = np.asarray(res.valid, dtype=bool)
        inv = np.where(~valid)[0]
        self.assertGreater(len(inv), 0)                          # some invalid wavelengths exist
        band_wl = WL[254:2031]
        self.assertTrue(np.all(np.isnan(res.at_wavelength(float(band_wl[inv[0]])))))
        self.assertTrue(np.all(np.isfinite(res.at_wavelength(600.0))))
        out = os.path.join(root, "Rmask.csv")
        res.save_csv(out)
        data = np.loadtxt(out, delimiter=",", skiprows=1)
        self.assertTrue(np.all(np.isnan(data[inv, 1])))          # invalid rows NaN in the export
        self.assertFalse(np.any(np.isnan(data[np.where(valid)[0], 1])))   # valid rows finite

    def test_zero_db_denominator_is_masked_not_inf(self):
        # a zero double-beam denominator must yield NaN (masked), never inf leaking into R
        root = tempfile.mkdtemp()
        lamp = np.full(2048, 10.0)
        sam_db = lamp.copy()
        sam_db[500:505] = 0.0                                    # zero sample-DB net -> divide by zero
        ref = make_session(root, "ref", {("P", 30): lamp}, db={"P": lamp})
        sam = make_session(root, "sam", {("P", 30): lamp * 0.5}, db={"P": sam_db})
        res = var.compute_reflectance(var.Session(sam), var.Session(ref), sd.ConstantStandard(1.0), "P")
        r = res.R[0]
        self.assertFalse(np.any(np.isinf(r)))
        self.assertTrue(np.all(np.isnan(r[500 - 254:505 - 254])))
        self.assertTrue(any("masked" in n for n in res.notes))

if __name__ == "__main__":
    unittest.main()
