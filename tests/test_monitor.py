# -*- coding: utf-8 -*-
"""tools/monitor.py end-to-end in --dry mode: recovery after a read failure, stage-state gate."""
import glob
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import cycle_test
import monitor
import restore_stages


def run(root, *extra):
    argv = ["--dry", "--minutes", "0.03", "--interval", "0", "--root", root, "--quiet"] + list(extra)
    rc = monitor.main(argv)
    logs = sorted(glob.glob(os.path.join(root, "logs", "monitor_*.log")))
    text = open(logs[-1], encoding="utf-8").read() if logs else ""
    csvs = sorted(glob.glob(os.path.join(root, "data", "monitor", "*.csv")))
    return rc, text, csvs


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def test_clean_run_records_state(self):
        rc, text, csvs = run(self.root)
        self.assertEqual(rc, 0)
        self.assertIn("monitor end (clean)", text)
        self.assertEqual(len(csvs), 1)
        self.assertGreater(len(open(csvs[0]).read().splitlines()), 5)
        doc = json.load(open(os.path.join(self.root, "data", "stage_state.json")))
        self.assertEqual(doc["modules"]["0"]["position"], 0)           # shutter closed at the end
        self.assertEqual(doc["modules"]["3"]["position"], 73671)       # 185 deg

    def test_read_failure_is_recovered(self):
        rc, text, csvs = run(self.root, "--dry-fail-at", "5")
        self.assertEqual(rc, 0)
        self.assertIn("spectrometer read failed", text)
        self.assertIn("recovered (reopened [dry])", text)
        self.assertIn("monitor end (clean)", text)
        self.assertGreater(len(open(csvs[0]).read().splitlines()), 5)

    def test_read_failure_without_recovery_stops_cleanly(self):
        rc, text, csvs = run(self.root, "--dry-fail-at", "5", "--no-recover")
        self.assertEqual(rc, 1)
        self.assertIn("!! BWTekError", text)
        self.assertIn("monitor end (clean)", text)               # shutter closed, data saved
        self.assertEqual(len(csvs), 1)

    def test_state_anomaly_blocks_motion(self):
        state = os.path.join(self.root, "data", "stage_state.json")
        os.makedirs(os.path.dirname(state))
        with open(state, "w") as f:                               # last record: sample at 185, arm at 44
            json.dump({"time": "t", "modules": {"3": {"position": 73671, "status": 0}, "2": {"position": 17522, "status": 0}}}, f)
        # the fake bus starts at sample 185 / arm 44 -> consistent
        rc, text, csvs = run(self.root)
        self.assertEqual(rc, 0)
        with open(state, "w") as f:                               # pretend the sample stage is elsewhere
            json.dump({"time": "t", "modules": {"3": {"position": 40971, "status": 0}}}, f)
        rc, text, csvs = run(self.root)
        self.assertEqual(rc, 2)
        self.assertIn("aborting before any motion", text)
        self.assertEqual(len(csvs), 1)                            # no new CSV
        rc, text, csvs = run(self.root, "--force")
        self.assertEqual(rc, 0)
        self.assertEqual(len(csvs), 2)


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def _log(self):
        logs = sorted(glob.glob(os.path.join(self.root, "logs", "restore_*.log")))
        return open(logs[-1], encoding="utf-8").read()

    def test_report_only_moves_nothing(self):
        rc = restore_stages.main(["--dry", "--root", self.root, "--quiet"])
        self.assertEqual(rc, 0)
        self.assertIn("report only", self._log())
        self.assertFalse(os.path.exists(os.path.join(self.root, "data", "stage_state.json")))   # no baseline from a report

    def test_arm_requires_confirmation(self):
        rc = restore_stages.main(["--dry", "--root", self.root, "--quiet", "--arm"])   # stdin closed -> EOF -> abort
        self.assertEqual(rc, 2)
        self.assertIn("not confirmed", self._log())

    def test_safe_and_arm(self):
        rc = restore_stages.main(["--dry", "--root", self.root, "--quiet", "--safe", "--arm", "--yes", "--sample", "185"])
        self.assertEqual(rc, 0, self._log())
        text = self._log()
        self.assertIn("polariser S -> 236.00", text)
        self.assertIn("sample stage -> 185.00", text)
        self.assertIn("ARM HOME", text)
        self.assertIn("detector arm -> 44.00", text)
        doc = json.load(open(os.path.join(self.root, "data", "stage_state.json")))
        self.assertEqual(doc["modules"]["2"]["position"], 17522)


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def _log(self):
        logs = sorted(glob.glob(os.path.join(self.root, "logs", "cycle_*.log")))
        return open(logs[-1], encoding="utf-8").read()

    def test_move_sets_end_at_baseline(self):
        for kind in ("sample", "arm", "scan", "exchange", "both"):
            steps = cycle_test.move_set(kind, 185.0, 44.0, "S")
            self.assertEqual((steps[-2].params["addr"], steps[-2].params["deg"]), ("2", 44.0))
            self.assertEqual((steps[-1].params["addr"], steps[-1].params["deg"]), ("3", 185.0))
        scan = cycle_test.move_set("scan", 185.0, 44.0, "S")
        pols = [st.params["deg"] for st in scan if st.params.get("addr") == "1"]
        self.assertEqual(pols, [146.0, 236.0])                     # P then back to S

    def test_dry_run(self):
        rc = cycle_test.main(["--dry", "--root", self.root, "--quiet", "--cycles", "2", "--frames", "3", "--interval", "0", "--moves", "both"])
        text = self._log()
        self.assertEqual(rc, 0, text)
        self.assertIn("segment 0 baseline", text)
        self.assertIn("segment 2 after both #2", text)
        self.assertIn("cycle test end (clean)", text)
        csvs = glob.glob(os.path.join(self.root, "data", "cycle", "*.csv"))
        self.assertEqual(len(csvs), 1)
        lines = open(csvs[0]).read().splitlines()
        self.assertEqual(len(lines), 1 + 3 * 3)
        doc = json.load(open(os.path.join(self.root, "data", "stage_state.json")))
        self.assertEqual(doc["modules"]["0"]["position"], 0)       # shutter closed at the end
        self.assertEqual(doc["modules"]["3"]["position"], 73671)

    def test_read_failure_is_recovered(self):
        rc = cycle_test.main(["--dry", "--root", self.root, "--quiet", "--cycles", "1", "--frames", "4", "--interval", "0", "--dry-fail-at", "3"])
        text = self._log()
        self.assertEqual(rc, 0, text)
        self.assertIn("recovered (reopened [dry])", text)
        self.assertIn("cycle test end (clean)", text)
        lines = open(glob.glob(os.path.join(self.root, "data", "cycle", "*.csv"))[0]).read().splitlines()
        self.assertEqual(len(lines), 1 + 2 * 4)                    # no frame lost

    def test_read_failure_without_recovery_aborts_cleanly(self):
        rc = cycle_test.main(["--dry", "--root", self.root, "--quiet", "--cycles", "1", "--frames", "4", "--interval", "0", "--dry-fail-at", "3", "--no-recover"])
        text = self._log()
        self.assertEqual(rc, 1)
        self.assertIn("!! BWTekError", text)
        self.assertIn("cycle test end (clean)", text)               # shutter closed


if __name__ == "__main__":
    unittest.main()
