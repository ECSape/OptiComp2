# -*- coding: utf-8 -*-
"""Stage-state bookkeeping: detect modules that moved without us (bus power cycle -> auto-home)."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from hw import config as cfg
from hw import elliptec as ell
from hw import stagestate as ss

PPD = {cfg.POLARISER: 143360 / 360.0, cfg.SYSTEM: 143360 / 360.0, cfg.SAMPLE: 143360 / 360.0}


class FakeBus(object):
    def __init__(self, pos, status=None, vel=None):
        self.pos = dict(pos)
        self.stat = dict(status or {})
        self.vel = dict(vel or {})
        self.set_vel = []
        self.protected_home = set()

    def position(self, a):
        return self.pos[a]

    def status(self, a):
        return self.stat.get(a, 0)

    def velocity(self, a):
        return self.vel.get(a, 64)

    def set_velocity(self, a, pct):
        self.set_vel.append((a, pct))
        self.vel[a] = pct


def deg(d):
    return int(round(d * 143360 / 360.0))


class StateTests(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "stage_state.json")
        self.good = {"0": 0, "1": deg(236), "2": deg(44), "3": deg(185)}

    def test_roundtrip_and_no_record(self):
        bus = FakeBus(self.good)
        problems, live = ss.check(bus, self.path, ppd=PPD)
        self.assertEqual(problems, [])                       # no record yet -> nothing to compare
        ss.record(bus, self.path, note="t", ppd=PPD)
        doc = json.load(open(self.path))
        self.assertEqual(doc["modules"]["3"]["position"], deg(185))
        self.assertAlmostEqual(doc["modules"]["3"]["deg"], 185.0, places=2)
        problems, live = ss.check(bus, self.path, ppd=PPD)
        self.assertEqual(problems, [])

    def test_power_cycle_signature_is_reported(self):
        ss.record(FakeBus(self.good, vel={"2": 50}), self.path, ppd=PPD)
        # what the bus looked like on 2026-08-26 after the USB replug: polariser homed to 0,
        # sample at 102.9, arm at 11 deg with a mechanical time-out, arm speed back to 64 %
        after = FakeBus({"0": 0, "1": -7, "2": 0x1144, "3": 0xA00B}, status={"2": 2}, vel={"2": 64})
        problems, live = ss.check(after, self.path, ppd=PPD)
        text = "\n".join(problems)
        self.assertIn("module 2 status GS02", text)
        self.assertIn("zero is LOST", text)
        self.assertIn("module 1 moved", text)
        self.assertIn("module 3 moved", text)
        self.assertIn("module 2 moved", text)
        self.assertIn("velocity 50% -> 64%", text)
        self.assertTrue(ss.arm_reference_lost(live))

    def test_small_settling_is_not_an_anomaly(self):
        ss.record(FakeBus(self.good), self.path, ppd=PPD)
        wobble = dict(self.good)
        wobble["3"] += 40                                    # 0.1 deg
        problems, _ = ss.check(FakeBus(wobble), self.path, ppd=PPD)
        self.assertEqual(problems, [])

    def test_wraparound_compare(self):
        saved = {"modules": {"3": {"position": deg(359.9)}}}
        live = {"3": {"position": deg(0.1), "status": 0}}
        self.assertEqual(ss.compare(saved, live, ppd=PPD), [])

    def test_status_only_without_ppd(self):
        problems = ss.compare(None, {"2": {"position": 5, "status": ell.MECHANICAL_TIMEOUT}})
        self.assertEqual(len(problems), 1)
        self.assertIn("GS02", problems[0])

    def test_protect_and_velocities(self):
        bus = FakeBus(self.good, vel={"2": 64})
        self.assertEqual(ss.protect(bus), {cfg.SYSTEM})
        logs = []
        ss.apply_velocities(bus, log=logs.append)
        self.assertEqual(bus.set_vel, [(cfg.SYSTEM, 50)])
        ss.apply_velocities(bus, log=logs.append)            # already right -> no command
        self.assertEqual(bus.set_vel, [(cfg.SYSTEM, 50)])
        self.assertEqual(len(logs), 1)

    def test_corrupt_record_is_ignored(self):
        with open(self.path, "w") as f:
            f.write("{not json")
        self.assertIsNone(ss.load(self.path))
        problems, _ = ss.check(FakeBus(self.good), self.path, ppd=PPD)
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
