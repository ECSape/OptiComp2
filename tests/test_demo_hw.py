# -*- coding: utf-8 -*-
"""Tests for the fake hardware used by --demo (no display, no serial port, no DLL)."""
import io
import os
import shutil
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import demo_hw
from hw import bwtek
from hw import config as cfg
from hw import elliptec as ell
from hw import stagestate


class DemoBusTests(unittest.TestCase):
    def test_never_touches_real_hardware(self):
        src = io.open(demo_hw.__file__, encoding="utf-8").read()
        self.assertNotIn("import serial", src)
        self.assertNotIn("ctypes", src)
        bus = demo_hw.DemoBus(motion_seconds=0)
        self.assertEqual(bus.port, "DEMO")
        self.assertFalse(bus.closed)
        bus.close()
        self.assertTrue(bus.closed)

    def test_queries_and_motion(self):
        lines = []
        bus = demo_hw.DemoBus(log=lambda d, t: lines.append((d, t)), motion_seconds=0)
        for addr in "0123":
            self.assertEqual(bus.status(addr), 0)
            self.assertEqual(bus.position(addr), demo_hw.DEFAULT_POS[addr])
        info = bus.info("2")
        self.assertTrue(info.pulses_per_unit > 0)
        self.assertEqual(bus.move_abs("3", 1000), 1000)
        self.assertEqual(bus.position("3"), 1000)
        self.assertEqual(bus.move_rel("3", -400), 600)
        self.assertEqual(bus.forward("0"), 31)          # shutter open
        self.assertEqual(bus.backward("0"), 0)          # shutter closed
        self.assertEqual(bus.velocity("1"), 64)
        bus.set_velocity("1", 50)
        self.assertEqual(bus.velocity("1"), 50)
        self.assertTrue(any(d == "TX" and t.startswith("3ma") for d, t in lines))
        self.assertTrue(any(d == "RX" and t.startswith("3PO") for d, t in lines))
        self.assertIn(("3", "ma"), bus.calls)

    def test_home_protection(self):
        bus = demo_hw.DemoBus(motion_seconds=0)
        stagestate.protect(bus)
        with self.assertRaises(ell.ElliptecError):
            bus.home(cfg.SYSTEM)                        # the fibre-carrying arm must never be homed casually
        self.assertEqual(bus.home(cfg.SYSTEM, force=True), 0)
        self.assertEqual(bus.home(cfg.SAMPLE), 0)
        self.assertEqual(bus.position(cfg.SAMPLE), 0)

    def test_anomalies(self):
        bus = demo_hw.DemoBus(motion_seconds=0, anomaly="arm")
        self.assertEqual(bus.status("2"), ell.MECHANICAL_TIMEOUT)
        bus2 = demo_hw.DemoBus(motion_seconds=0)
        p0 = bus2.position("3")
        bus2.inject_anomaly("moved")
        self.assertNotEqual(bus2.position("3"), p0)
        self.assertEqual(bus2.anomaly, "moved")

    def test_raw_query(self):
        bus = demo_hw.DemoBus(motion_seconds=0)
        reply = bus.query("2", "gp")
        self.assertEqual(reply["kind"], "PO")
        self.assertTrue(reply["raw"].startswith("2PO"))
        self.assertEqual(reply["value"], demo_hw.DEFAULT_POS["2"])
        self.assertEqual(bus.query("2", "zz")["code"], 3)             # unknown command -> GS03


class DemoSpecTests(unittest.TestCase):
    def setUp(self):
        self.bus = demo_hw.DemoBus(motion_seconds=0)
        self.spec = demo_hw.DemoSpec(self.bus, fast=True, sample="white", seed=7)

    def test_open_and_integration_time(self):
        self.assertEqual(self.spec.open(), 1)
        self.assertIsNone(self.spec.integration_ms)
        self.assertEqual(self.spec.set_integration_time(250), 250)
        self.assertEqual(self.spec.integration_ms, 250)
        with self.assertRaises(bwtek.BWTekError):
            self.spec.set_integration_time(0)
        self.spec.close()
        with self.assertRaises(bwtek.BWTekError):
            self.spec.read()

    def test_frames(self):
        spec = self.spec
        spec.open()
        spec.set_integration_time(1000)
        dark = spec.read(average=3)                      # shutter closed at start
        self.assertEqual(dark.dtype, np.uint16)
        self.assertEqual(len(dark), len(bwtek.wavelengths()))
        self.assertLess(int(dark[bwtek.ACTIVE_FIRST:bwtek.ACTIVE_LAST].max()), 3000)
        self.bus.forward("0")                            # shutter open, defaults = reference geometry (44 deg / theta 80 / S)
        bright = spec.read(average=1)
        self.assertGreater(int(bright.max()), 0.5 * bwtek.ADC_MAX)
        self.assertLessEqual(int(bright.max()), bwtek.ADC_MAX)
        spec.sample = "si"
        si = spec.read(average=1)
        self.assertLess(int(si[bwtek.ACTIVE_FIRST:bwtek.ACTIVE_LAST].max()), int(bright[bwtek.ACTIVE_FIRST:bwtek.ACTIVE_LAST].max()))
        spec.set_integration_time(100)
        short = spec.read(average=1)
        self.assertLess(int(short.max()), int(bright.max()))

    def test_failure_and_recovery(self):
        spec = demo_hw.DemoSpec(self.bus, fast=True, fail_read_at=2, seed=1)
        spec.open()
        spec.set_integration_time(100)
        spec.read()
        with self.assertRaises(bwtek.BWTekError):
            spec.read()
        self.assertIn("reopened", spec.recover())
        self.assertEqual(spec.integration_ms, 100)
        self.assertEqual(len(spec.read()), len(bwtek.wavelengths()))


class SeedTests(unittest.TestCase):
    def test_seed_demo_data(self):
        tmp = tempfile.mkdtemp(prefix="opticomp2_seed_")
        try:
            state = os.path.join(tmp, "stage_state.json")
            made = demo_hw.seed_demo_data(tmp, state)
            self.assertEqual(sorted(os.path.basename(m) for m in made), ["demo_si", "demo_white"])
            import sequence as sq
            for name in ("demo_white", "demo_si"):
                recs = sq.Runner.load_manifest(os.path.join(tmp, name))
                kinds = set(r.get("kind") for r in recs)
                self.assertTrue({"var", "dark", "db"} <= kinds, kinds)
                self.assertTrue(any(r.get("pol") == "P" for r in recs))
            self.assertTrue(os.path.isfile(state))
            self.assertEqual(demo_hw.seed_demo_data(tmp, state), [])      # idempotent
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
