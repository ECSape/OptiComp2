# -*- coding: utf-8 -*-
import json
import os
import sys
import tempfile
import threading
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
from hw import config as cfg
from hw import bwtek
import sequence as sq


class FakeInfo(object):
    pulses, travel = 143360, 360


class FakeBus(object):
    def __init__(self):
        self.moves, self.shutter = [], []

    def info(self, addr):
        return FakeInfo()

    def move_abs(self, addr, pulses):
        self.moves.append((addr, pulses))
        return pulses

    def forward(self, addr):
        self.shutter.append("open")
        return 0

    def backward(self, addr):
        self.shutter.append("close")
        return 0


class FakeSpec(object):
    def __init__(self, level=30000):
        self.integration_ms = 100
        self.level = level
        self.reads = []

    def set_integration_time(self, ms):
        self.integration_ms = ms

    def read(self, avg, st=0, sv=0):
        self.reads.append((self.integration_ms, avg))
        c = np.full(2048, 900, dtype=np.uint16)
        c[254:2031] = min(65535, 900 + int(self.level * self.integration_ms / 100.0))
        return c


class BuilderTests(unittest.TestCase):
    def test_gatekeeper(self):
        self.assertEqual(sq.check_theta_range(8, 80, 4), list(np.arange(8, 81, 4)))
        for bad in [(80, 8, 4), (-1, 80, 4), (0, 81, 1), (0, 80, 0), (10, 10, 1)]:
            with self.assertRaises(ValueError):
                sq.check_theta_range(*bad)

    def test_scan_expansion(self):
        steps = sq.build_scan(0, 80, 40, ["S", "P"], 3, "glass")
        acq = [s for s in steps if s.kind == "acquire"]
        self.assertEqual([a.params["tag"] for a in acq], ["glass_S_0", "glass_S_40", "glass_S_80", "glass_P_0", "glass_P_40", "glass_P_80"])
        st = [s for s in steps if s.kind == "stage" and s.params["addr"] == cfg.SAMPLE]
        self.assertEqual(st[0].params["deg"], 0 + cfg.SAMPLE_VAR_OFFSET)
        self.assertEqual(steps[-1].kind, "shutter")
        self.assertFalse(steps[-1].params["open"])

    def test_reference_calibration_is_80_S(self):
        steps = sq.build_reference_calibration()
        pol = [s for s in steps if s.params.get("addr") == cfg.POLARISER][0]
        smp = [s for s in steps if s.params.get("addr") == cfg.SAMPLE][0]
        self.assertEqual(pol.params["deg"], cfg.POL_DEG["S"])
        self.assertEqual(smp.params["deg"], 80 + cfg.SAMPLE_VAR_OFFSET)
        self.assertEqual([s.kind for s in steps[-5:]], ["stage", "auto_it", "stage", "auto_it", "apply_min_it"])


class RunnerTests(unittest.TestCase):
    def test_full_run_writes_files_and_manifest(self):
        bus, spec = FakeBus(), FakeSpec()
        out = tempfile.mkdtemp()
        prompts = []
        r = sq.Runner(bus, spec, out, ask_user=lambda m: prompts.append(m) or True)
        steps = sq.build_reference_calibration() + sq.build_dark(2) + sq.build_single_angle(45, ["S"], 2, "x") + sq.build_double_beam(["S"], 1, "x")
        man = r.run(steps)
        self.assertTrue(bwtek.peak_in_band(900 + 30000 * spec.integration_ms / 100.0))      # auto-IT converged
        tags = [m["tag"] for m in man]
        self.assertEqual(tags, ["dark", "x_S_45", "x_DB_S"])
        self.assertEqual(man[0]["shutter_open"], False)
        self.assertEqual(man[1]["theta"], 45.0)
        self.assertAlmostEqual(man[1]["sample_deg"], 45 + cfg.SAMPLE_VAR_OFFSET, places=2)   # pulse quantisation
        self.assertEqual(man[0]["integration_ms"], man[1]["integration_ms"])                  # dark at the calibrated IT
        self.assertEqual(len(prompts), 2)
        with open(os.path.join(out, "manifest.json")) as f:
            self.assertEqual(len(json.load(f)["spectra"]), 3)
        d = np.loadtxt(os.path.join(out, "dark.csv"), delimiter=",", skiprows=1)
        self.assertEqual(d.shape, (2048, 2))

    def test_manifest_appends_across_runs_and_preview_hook(self):
        out = tempfile.mkdtemp()
        seen = []
        sq.Runner(FakeBus(), FakeSpec(), out, on_spectrum=lambda r, c: seen.append(r["tag"])).run(sq.build_dark(1))
        sq.Runner(FakeBus(), FakeSpec(), out).run(sq.build_single_angle(10, ["S"], 1, "x"))
        self.assertEqual([m["tag"] for m in sq.Runner.load_manifest(out)], ["dark", "x_S_10"])
        self.assertEqual(seen, ["dark"])

    def test_wrong_angle_aborts(self):
        class StuckBus(FakeBus):
            def move_abs(self, addr, pulses):
                return pulses - 1000
        with self.assertRaises(RuntimeError):
            sq.Runner(StuckBus(), FakeSpec(), tempfile.mkdtemp()).run([sq.sample_theta(45)])

    def test_soft_limit_and_abort(self):
        bus, spec = FakeBus(), FakeSpec()
        out = tempfile.mkdtemp()
        with self.assertRaises(ValueError):
            sq.Runner(bus, spec, out).run([sq.stage(cfg.SYSTEM, 250, "bad")])
        ev = threading.Event()
        ev.set()
        with self.assertRaises(sq.SequenceAbort):
            sq.Runner(bus, spec, out, abort=ev).run(sq.build_dark(1))
        with self.assertRaises(sq.SequenceAbort):
            sq.Runner(bus, spec, out, ask_user=lambda m: False).run([sq.pause("x")])


if __name__ == "__main__":
    unittest.main()
