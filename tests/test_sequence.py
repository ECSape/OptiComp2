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
from hw import stagestate
import sequence as sq

cfg.STATE_FILE = os.path.join(tempfile.mkdtemp(), "stage_state.json")   # never touch the real record


class FakeInfo(object):
    pulses, travel = 143360, 360


class FakeBus(object):
    def __init__(self):
        self.moves, self.shutter = [], []
        self.pos = {}

    def info(self, addr):
        return FakeInfo()

    def position(self, addr):
        return self.pos.get(addr, 0)

    def move_abs(self, addr, pulses):
        self.moves.append((addr, pulses))
        self.pos[addr] = pulses
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
        self.assertEqual([s.kind for s in steps[-6:]], ["stage", "auto_it", "stage", "auto_it", "apply_min_it", "shutter"])
        self.assertFalse(steps[-1].params["open"])                                         # never leaves the shutter open


class RunnerTests(unittest.TestCase):
    def setUp(self):
        cfg.STATE_FILE = os.path.join(tempfile.mkdtemp(), "stage_state.json")   # fresh record per test

    def test_reacquired_tag_replaces_earlier_record(self):
        bus, spec = FakeBus(), FakeSpec()
        out = tempfile.mkdtemp()
        r = sq.Runner(bus, spec, out)
        r.run(sq.build_dark(1) + sq.build_single_angle(60, ["S"], 1, "x"))
        man = sq.Runner(bus, spec, out).run(sq.build_single_angle(60, ["S"], 1, "x"))   # second run, same tag
        self.assertEqual([m["tag"] for m in man], ["dark", "x_S_60"])
        self.assertEqual(len(sq.Runner.load_manifest(out)), 2)

    def test_full_run_writes_files_and_manifest(self):
        bus, spec = FakeBus(), FakeSpec()
        out = tempfile.mkdtemp()
        prompts = []
        r = sq.Runner(bus, spec, out, ask_user=lambda m: prompts.append(m) or True)
        steps = sq.build_reference_calibration() + sq.build_dark(2) + sq.build_single_angle(45, ["S"], 2, "x") + sq.build_double_beam(["S"], 1, "x")
        man = r.run(steps)
        self.assertTrue(bwtek.peak_in_band(900 + 30000 * spec.integration_ms / 100.0))      # auto-IT converged
        tags = [m["tag"] for m in man]
        self.assertEqual(tags, ["dark", "x_S_45", "x_DB_S", "dark_db"])
        self.assertEqual(man[3]["kind"], "dark")
        self.assertEqual(man[3]["shutter_open"], False)
        self.assertEqual(man[0]["shutter_open"], False)
        self.assertEqual(man[1]["theta"], 45.0)
        self.assertAlmostEqual(man[1]["sample_deg"], 45 + cfg.SAMPLE_VAR_OFFSET, places=2)   # pulse quantisation
        self.assertEqual(man[0]["integration_ms"], man[1]["integration_ms"])                  # dark at the calibrated IT
        self.assertEqual(len(prompts), 2)
        # DB spectra at the fixed DB integration time, session integration time restored afterwards
        self.assertEqual(man[2]["integration_ms"], cfg.DB_IT_MS)
        self.assertEqual(man[3]["integration_ms"], cfg.DB_IT_MS)
        self.assertEqual(spec.integration_ms, man[1]["integration_ms"])
        self.assertNotEqual(spec.integration_ms, cfg.DB_IT_MS)
        with open(os.path.join(out, "manifest.json")) as f:
            self.assertEqual(len(json.load(f)["spectra"]), 4)
        self.assertEqual(man[0]["file"], "dark_%dms.csv" % man[0]["integration_ms"])       # darks are per integration time
        self.assertEqual(man[3]["file"], "dark_db_%dms.csv" % cfg.DB_IT_MS)
        d = np.loadtxt(os.path.join(out, man[0]["file"]), delimiter=",", skiprows=1)
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
                return pulses - 1000            # 2.5 deg short
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


class StateBookkeepingTests(unittest.TestCase):
    def setUp(self):
        self.state = os.path.join(tempfile.mkdtemp(), "stage_state.json")

    def test_moves_and_shutter_are_recorded(self):
        bus, out = FakeBus(), tempfile.mkdtemp()
        sq.Runner(bus, FakeSpec(), out, state_path=self.state).run(sq.build_single_angle(60, ["S"], 1, "x"))
        doc = json.load(open(self.state))
        self.assertEqual(doc["modules"][cfg.SAMPLE]["position"], int(round(165 * 143360 / 360.0)))
        self.assertEqual(doc["modules"][cfg.SYSTEM]["position"], int(round(44 * 143360 / 360.0)))
        self.assertIn(cfg.SHUTTER, doc["modules"])

    def test_arm_moved_without_us_aborts(self):
        bus, out = FakeBus(), tempfile.mkdtemp()
        stagestate.save({cfg.SYSTEM: {"position": int(round(44 * 143360 / 360.0)), "status": 0}}, self.state)
        bus.pos[cfg.SYSTEM] = int(round(11 * 143360 / 360.0))          # module reports 11 deg (auto-home blocked)
        logs = []
        with self.assertRaises(RuntimeError) as cm:
            sq.Runner(bus, FakeSpec(), out, log=logs.append, state_path=self.state).run([sq.stage(cfg.SYSTEM, 44, "arm")])
        self.assertIn("moved without us", str(cm.exception))
        self.assertEqual(bus.moves, [])                                 # nothing was moved

    def test_sample_moved_without_us_warns_and_continues(self):
        bus, out = FakeBus(), tempfile.mkdtemp()
        stagestate.save({cfg.SAMPLE: {"position": int(round(185 * 143360 / 360.0)), "status": 0}}, self.state)
        bus.pos[cfg.SAMPLE] = int(round(103 * 143360 / 360.0))
        logs = []
        sq.Runner(bus, FakeSpec(), out, log=logs.append, state_path=self.state).run([sq.sample_theta(60)])
        self.assertTrue(any("moved without us" in l for l in logs))
        self.assertEqual(len(bus.moves), 1)

    def test_consistent_position_is_silent(self):
        bus, out = FakeBus(), tempfile.mkdtemp()
        stagestate.save({cfg.SAMPLE: {"position": int(round(185 * 143360 / 360.0)), "status": 0}}, self.state)
        bus.pos[cfg.SAMPLE] = int(round(185.1 * 143360 / 360.0))
        logs = []
        sq.Runner(bus, FakeSpec(), out, log=logs.append, state_path=self.state).run([sq.sample_theta(60)])
        self.assertFalse(any("moved without us" in l for l in logs))

    def test_state_disabled(self):
        bus, out = FakeBus(), tempfile.mkdtemp()
        sq.Runner(bus, FakeSpec(), out, state_path="").run([sq.sample_theta(60)])
        self.assertFalse(os.path.exists(self.state))


class SafetyTests(unittest.TestCase):
    """The Runner must never leave the shutter open or lose the manifest when a run fails."""

    def setUp(self):
        cfg.STATE_FILE = os.path.join(tempfile.mkdtemp(), "stage_state.json")
        self.out = tempfile.mkdtemp()

    def test_error_with_shutter_open_closes_it(self):
        bus, spec = FakeBus(), FakeSpec()

        def boom(*a, **k):
            raise RuntimeError("DLL hang")
        spec.read = boom
        r = sq.Runner(bus, spec, self.out, state_path="")
        with self.assertRaises(RuntimeError):
            r.run([sq.shutter(True), sq.acquire("x", 1, kind="var")])
        self.assertEqual(bus.shutter, ["open", "close"])
        self.assertFalse(r.shutter_open)
        self.assertTrue(os.path.isfile(os.path.join(self.out, "manifest.json")))

    def test_abort_at_pause_closes_shutter(self):
        bus, spec = FakeBus(), FakeSpec()
        r = sq.Runner(bus, spec, self.out, ask_user=lambda m: False, state_path="")
        with self.assertRaises(sq.SequenceAbort):
            r.run([sq.shutter(True), sq.pause("swap")])
        self.assertEqual(bus.shutter[-1], "close")

    def test_shutter_known_closed_is_not_touched(self):
        bus, spec = FakeBus(), FakeSpec()
        r = sq.Runner(bus, spec, self.out, ask_user=lambda m: False, state_path="")
        with self.assertRaises(sq.SequenceAbort):
            r.run([sq.shutter(False), sq.pause("swap")])
        self.assertEqual(bus.shutter, ["close"])

    def test_auto_it_without_light_aborts(self):
        bus, spec = FakeBus(), FakeSpec(level=0)
        r = sq.Runner(bus, spec, self.out, state_path="")
        with self.assertRaises(RuntimeError) as cm:
            r.run([sq.shutter(True), sq.auto_it()])
        self.assertIn("no light", str(cm.exception))
        self.assertLessEqual(spec.integration_ms, 4 * bwtek.AUTO_IT_DARK_MS)   # gave up early, not at 60 s
        self.assertEqual(bus.shutter[-1], "close")

    def test_darks_per_integration_time_do_not_collide(self):
        bus, spec = FakeBus(), FakeSpec()
        r = sq.Runner(bus, spec, self.out, state_path="")
        r.run([sq.set_it(100), sq.shutter(False), sq.acquire("dark", 1, kind="dark"),
               sq.set_it(997), sq.acquire("dark", 1, kind="dark"),
               sq.set_it(100), sq.acquire("dark", 1, kind="dark")])
        files = sorted(rec["file"] for rec in r.manifest)
        self.assertEqual(files, ["dark_100ms.csv", "dark_997ms.csv"])          # the 100 ms dark was replaced, 997 kept
        self.assertTrue(os.path.isfile(os.path.join(self.out, "dark_997ms.csv")))

    def test_abort_inside_double_beam_restores_integration_time(self):
        bus, spec = FakeBus(), FakeSpec()
        spec.set_integration_time(500)                                           # calibrated session IT
        r = sq.Runner(bus, spec, self.out, ask_user=lambda m: False, state_path="")
        with self.assertRaises(sq.SequenceAbort):
            r.run(sq.build_double_beam(["S"], 1, "x"))                          # cancelled at the first pause
        self.assertEqual(spec.integration_ms, 500)                               # not left at DB_IT_MS
        self.assertIsNone(r._saved_it)
        self.assertEqual(bus.shutter[-1], "close")

    def test_pause_without_operator_aborts(self):
        bus, spec = FakeBus(), FakeSpec()
        r = sq.Runner(bus, spec, self.out, state_path="")                        # no ask_user: unattended
        with self.assertRaises(sq.SequenceAbort) as cm:
            r.run(sq.build_double_beam(["S"], 1, "x"))
        self.assertIn("needs an operator", str(cm.exception))
        self.assertEqual([m.get("kind") for m in r.manifest], [])               # nothing acquired under the wrong port cover
        self.assertEqual(bus.shutter[-1], "close")

    def test_soft_limits_helper(self):
        self.assertEqual(sq.check_soft_limits(cfg.SYSTEM, 44.0), 44.0)
        with self.assertRaises(ValueError):
            sq.check_soft_limits(cfg.SYSTEM, 220.0)
        with self.assertRaises(ValueError):
            sq.check_soft_limits(cfg.SAMPLE, -1.0)
        self.assertEqual(sq.check_soft_limits(cfg.POLARISER, 359.0), 359.0)     # unlimited module

    def test_corrupt_manifest_is_kept_aside(self):
        path = os.path.join(self.out, "manifest.json")
        with open(path, "w") as f:
            f.write("{not json")
        self.assertEqual(sq.Runner.load_manifest(self.out), [])
        self.assertFalse(os.path.exists(path))
        self.assertTrue([n for n in os.listdir(self.out) if n.startswith("manifest.json.corrupt_")])
        r = sq.Runner(FakeBus(), FakeSpec(), self.out, state_path="")
        r.run([sq.shutter(False), sq.acquire("dark", 1, kind="dark")])
        self.assertFalse(os.path.exists(path + ".tmp"))                          # atomic write leaves no temp file
        with open(path) as f:
            self.assertEqual(len(json.load(f)["spectra"]), 1)


class ReliabilityFixTests(unittest.TestCase):
    """2026-08-27 operability fixes: reconnect the spectrometer and zero every stage at the start
    of each run, and revive a hung spectrometer between a failed read and its retry."""

    def test_build_reset_is_absolute_and_never_homes(self):
        steps = sq.build_reset()
        self.assertEqual([s.kind for s in steps], ["shutter", "stage", "stage", "stage"])
        self.assertFalse(steps[0].params["open"])                 # shutter closed first
        self.assertNotIn("home", [s.kind for s in steps])         # the fibre arm is moved, never homed
        stages = [s for s in steps if s.kind == "stage"]
        self.assertEqual([s.params["addr"] for s in stages], [cfg.SYSTEM, cfg.SAMPLE, cfg.POLARISER])
        self.assertEqual(next(s for s in stages if s.params["addr"] == cfg.SYSTEM).params["deg"], cfg.SYSTEM_ZERO)
        self.assertEqual(next(s for s in stages if s.params["addr"] == cfg.SAMPLE).params["deg"], cfg.SAMPLE_ZERO)

    def test_preflight_runs_before_the_first_step(self):
        out = tempfile.mkdtemp()
        order = []
        bus = FakeBus()

        class Watched(FakeSpec):
            def read(self, avg, st=0, sv=0):
                order.append("read")
                return super().read(avg, st, sv)

        sq.Runner(bus, Watched(), out, state_path="",
                  preflight=lambda: order.append("preflight")).run(sq.build_dark(1))
        self.assertEqual(order[0], "preflight")                   # reconnect happens before any acquisition
        self.assertIn("read", order)

    def test_read_recovers_after_spectrometer_error(self):
        out = tempfile.mkdtemp()
        recovered = []

        class FlakySpec(FakeSpec):
            def __init__(self):
                super(FlakySpec, self).__init__()
                self.fail_left = 1
            def read(self, avg, st=0, sv=0):
                if self.fail_left > 0:
                    self.fail_left -= 1
                    raise bwtek.BWTekError("USB read -99")
                return super(FlakySpec, self).read(avg, st, sv)

        sq.Runner(FakeBus(), FlakySpec(), out, state_path="",
                  spec_recover=lambda: recovered.append(True) or "reopened").run(
                      [sq.shutter(False), sq.acquire("dark", 1, kind="dark")])
        self.assertEqual(recovered, [True])                       # recovery hook fired exactly once
        self.assertEqual([m["tag"] for m in sq.Runner.load_manifest(out)], ["dark"])   # retry produced the file

    def test_read_error_without_recover_hook_propagates(self):
        class FlakySpec(FakeSpec):
            def read(self, avg, st=0, sv=0):
                raise bwtek.BWTekError("USB read -99")

        r = sq.Runner(FakeBus(), FlakySpec(), tempfile.mkdtemp(), state_path="")
        with self.assertRaises(bwtek.BWTekError):
            r.run([sq.shutter(False), sq.acquire("dark", 1, kind="dark")])




class StabilityRegressionTests(unittest.TestCase):
    def setUp(self):
        cfg.STATE_FILE = os.path.join(tempfile.mkdtemp(), "stage_state.json")

    def test_open_shutter_commits_state_before_move_for_failsafe(self):
        # if forward() raises mid-open, shutter_open must already read True so that a later
        # close_shutter_safely() actually closes it instead of no-opping (SAFETY)
        class BoomBus(FakeBus):
            def forward(self, addr):
                raise RuntimeError("motor jam mid-open")
        r = sq.Runner(BoomBus(), FakeSpec(), tempfile.mkdtemp())
        with self.assertRaises(RuntimeError):
            r.run_step(sq.Step("shutter", "open", open=True))
        self.assertTrue(r.shutter_open)
        r.bus = FakeBus()                                       # a healthy bus for the safety close
        self.assertTrue(r.close_shutter_safely())
        self.assertFalse(r.shutter_open)
        self.assertIn("close", r.bus.shutter)

    def test_close_shutter_clears_state_only_after_completed_close(self):
        r = sq.Runner(FakeBus(), FakeSpec(), tempfile.mkdtemp())
        r.run_step(sq.Step("shutter", "open", open=True))
        self.assertTrue(r.shutter_open)
        r.run_step(sq.Step("shutter", "close", open=False))
        self.assertFalse(r.shutter_open)

    def test_load_manifest_tolerates_non_dict_json(self):
        # a corrupt manifest that parses as a list (not a dict) must not raise AttributeError
        out = tempfile.mkdtemp()
        with open(os.path.join(out, "manifest.json"), "w") as f:
            f.write("[1, 2, 3]")
        self.assertEqual(sq.Runner.load_manifest(out), [])
        self.assertFalse(os.path.isfile(os.path.join(out, "manifest.json")))    # set aside, not overwritten



class HWF02StabilityGateTests(unittest.TestCase):
    def test_reference_calibration_stabilise_flag_controls_wait_step(self):
        on = sq.build_reference_calibration(stabilise=True)
        off = sq.build_reference_calibration(stabilise=False)
        self.assertEqual(sum(s.kind == "wait_stable" for s in on), 1)
        self.assertEqual(sum(s.kind == "wait_stable" for s in off), 0)
        kinds = [s.kind for s in on]
        i = kinds.index("wait_stable")
        self.assertEqual(kinds[i - 1], "shutter")               # right after the shutter opens
        self.assertLess(i, kinds.index("auto_it"))              # before the S/P calibration
        self.assertEqual([s.kind for s in on[-6:]], [s.kind for s in off[-6:]])   # tail unchanged

    def test_wait_stable_passes_on_a_steady_lamp(self):
        # in-band, constant output -> drift 0 -> gate clears after `need` reads (no _auto_it needed)
        r = sq.Runner(FakeBus(), FakeSpec(level=55000), tempfile.mkdtemp())
        self.assertTrue(r._wait_stable(threshold_pct=0.5, need=3, max_reads=20))

    def test_wait_stable_warns_but_never_blocks_on_drift(self):
        class DriftSpec(FakeSpec):
            def read(self, avg, st=0, sv=0):
                out = FakeSpec.read(self, avg, st, sv)
                self.level += 200                                # steady upward drift, stays in band
                return out
        logs = []
        r = sq.Runner(FakeBus(), DriftSpec(level=55000), tempfile.mkdtemp())
        r.log = lambda t: logs.append(t)
        self.assertFalse(r._wait_stable(threshold_pct=0.1, need=5, max_reads=8))   # warns, returns False
        self.assertTrue(any("not stable" in t for t in logs))



class AutoITCeilingTests(unittest.TestCase):
    def test_too_dim_target_fails_fast_at_the_hw_ceiling(self):
        # a non-dark but too-dim target (below the accept band even at the 60 s hardware ceiling) must
        # fail after ONE ceiling read - not repeat several multi-second ceiling reads (which looks like
        # a hang) nor be rejected below the ceiling (a legitimately dim reference, e.g. Si at 80 deg P,
        # still calibrates at tens of seconds)
        spec = FakeSpec(level=80)               # peak ~48900 (75%) at the 60000 ms ceiling: below 78%
        r = sq.Runner(FakeBus(), spec, tempfile.mkdtemp())
        with self.assertRaises(RuntimeError) as cm:
            r._auto_it()
        self.assertIn("too dim", str(cm.exception))
        self.assertLessEqual(len(spec.reads), 3)                  # fail-fast, not 8 blind ceiling reads
        self.assertGreaterEqual(spec.integration_ms, bwtek.IT_MAX_MS)   # it did reach the hardware ceiling

    def test_flat_dark_still_aborts_with_no_light(self):
        r = sq.Runner(FakeBus(), FakeSpec(level=0), tempfile.mkdtemp())      # peak == baseline
        with self.assertRaises(RuntimeError) as cm:
            r._auto_it()
        self.assertIn("no light", str(cm.exception))

if __name__ == "__main__":
    unittest.main()
