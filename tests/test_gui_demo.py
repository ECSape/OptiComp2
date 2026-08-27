# -*- coding: utf-8 -*-
"""End-to-end GUI tests against the fake hardware (--demo). Skipped without a display.

The whole screenshot tour (connect -> spectrometer -> sequence -> analysis -> anomaly) is driven
inside the Tk main loop exactly like `manual_gui --demo --screenshot`, then the GUI-side state,
the behavioural parity list (spec §6) and the layout (no clipped widgets) are checked."""
import os
import shutil
import sys
import tempfile
import time
import unittest
import tkinter as tk

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))


def _has_display():
    try:
        r = tk.Tk()
        r.withdraw()
        r.destroy()
        return True
    except tk.TclError:
        return False


HAS_DISPLAY = _has_display()

# spec §6: attributes/methods the panels must keep (callers in other modules and the screenshot tour rely on them)
PARITY = [
    ("app", ["connect", "disconnect", "query_all", "bus_forward", "bus_backward", "bus_home", "close_shutter", "set_shutter",
             "update_module", "refresh_status", "submit", "toggle_log", "show_page", "_apply_lock", "_log_line", "_on_close",
             "panels", "statusbar", "sidebar", "log_drawer", "raw_section", "pages", "spectro", "sequence", "analysis",
             "bus", "bus_port", "sequence_running", "shutter_state", "stage_deg", "stage_status", "stage_vel", "health_problems",
             "health_done_count", "theme", "demo", "data_root", "state_path"]),
    ("app.spectro", ["open_dev", "set_it", "read_once", "it_var", "it_chosen", "last", "spec", "worker", "results", "btn_open",
                     "btn_close", "btn_read", "btn_set_it", "btn_auto", "btn_live", "btn_mon", "btn_recover", "btn_save",
                     "state_var", "avg_var", "smooth_var", "stats_var", "readout", "canvas", "set_locked"]),
    ("app.sequence", ["steps", "job_steps", "events", "abort", "running", "add_reference", "add_dark", "add_single", "add_scan",
                      "add_db", "add_set_it", "add_pause", "remove_selected", "clear", "run", "abort_run", "load_history",
                      "btn_run", "btn_abort", "btn_shutter_close", "btn_shutter_open", "btn_remove", "btn_clear", "listbox",
                      "done_tree", "done_tags", "queue_frame", "done_frame", "queue_card", "done_card", "prog_var", "bar",
                      "session_var", "prefix_var", "avg_var", "pol_var", "theta_var", "start_var", "stop_var", "step_var",
                      "it_var", "pause_var", "preview_var", "canvas", "set_locked", "_finish", "_poll"]),
    ("app.analysis", ["sample_cb", "ref_cb", "std_cb", "const_var", "db_var", "back_var", "theory_var", "lam_var", "note_var",
                      "results", "compute", "export", "save_fig", "refresh_sessions", "canvas", "btn_compute", "btn_export",
                      "btn_save_fig"]),
]


def _resolve(app, path):
    obj = app
    for part in path.split(".")[1:]:
        obj = getattr(obj, part)
    return obj


@unittest.skipUnless(HAS_DISPLAY, "needs a display")
class GuiDemoTests(unittest.TestCase):
    maxDiff = None
    @classmethod
    def setUpClass(cls):
        import manual_gui
        cls.mg = manual_gui
        cls.tmp = tempfile.mkdtemp(prefix="opticomp2_gui_")
        cls.app = manual_gui.App(demo=True, data_root=cls.tmp, state_path=os.path.join(cls.tmp, "stage_state.json"))
        cls.app._demo_bus.motion_seconds = 0
        cls.app.demo_fast = True
        manual_gui.install_autopilot(cls.app)
        cls.app.geometry("1280x820+20+20")
        cls.app.update()
        cls.close_seconds = None

    @classmethod
    def tearDownClass(cls):
        t0 = time.time()
        try:
            cls.app._on_close()
        finally:
            cls.close_seconds = time.time() - t0
        shutil.rmtree(cls.tmp, ignore_errors=True)
        assert cls.close_seconds < 5.0, "orderly close took %.1fs" % cls.close_seconds

    def _log_tail(self, n=25):
        return "\n".join(self.app.log_lines[-n:])

    def test_01_window(self):
        app = self.app
        self.assertIn("Demo mode", app.title())
        self.assertEqual(tuple(app.minsize()), (1100, 720))
        self.assertTrue(app.demo)
        self.assertEqual(set(app.pages), {"instrument", "motors", "spectro", "measure", "analysis"})
        for key in ("port", "spec", "it", "shutter", "arm", "seq"):
            self.assertIsNotNone(app.statusbar.get(key), key)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "demo_si", "manifest.json")))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "demo_white", "manifest.json")))

    def test_02_parity(self):
        missing = []
        for path, names in PARITY:
            obj = _resolve(self.app, path)
            for n in names:
                if not hasattr(obj, n):
                    missing.append("%s.%s" % (path, n))
        self.assertEqual(missing, [])

    def test_03_lock(self):
        app = self.app
        app._apply_lock(True)
        self.assertEqual(str(app.sequence.btn_remove["state"]), "disabled")
        self.assertEqual(str(app.sequence.btn_clear["state"]), "disabled")
        app._apply_lock(False)
        self.assertEqual(str(app.sequence.btn_remove["state"]), "normal")
        # refusals while a sequence is running (no bus needed: the guard fires first)
        app.sequence_running = True
        calls = []
        app.submit("probe", lambda bus: calls.append(1), None)
        app.sequence_running = False
        self.assertEqual(calls, [])

    def test_03a_lock_keeps_running_live_read_stoppable(self):
        # review regression: during a sequence the Stop live / Stop monitor buttons must stay usable
        sp = self.app.spectro
        sp.worker.live.set()
        try:
            sp._set_live_button(True)
            self.app._apply_lock(True)
            self.assertEqual(str(sp.btn_live["state"]), "normal")
            self.assertEqual(str(sp.btn_read["state"]), "disabled")
            self.assertEqual(str(sp.btn_auto["state"]), "disabled")
            self.app.sequence_running = True
            sp.worker.live.clear()
            sp._set_live_button(False)                   # stopped while locked -> no restart until unlock
            self.assertEqual(str(sp.btn_live["state"]), "disabled")
        finally:
            sp.worker.live.clear()
            self.app.sequence_running = False
            self.app._apply_lock(False)
        self.assertEqual(str(sp.btn_live["state"]), "normal")
        self.assertEqual(str(sp.btn_read["state"]), "normal")

    def test_03b_display_path_never_raises(self):
        import os.path as osp
        real = osp.relpath

        def boom(*a, **k):
            raise ValueError("path is on mount 'C:', start on mount 'D:'")
        osp.relpath = boom
        try:
            text = self.mg._display_path("D:\\data\\x")
        finally:
            osp.relpath = real
        self.assertTrue(text.endswith("data/x"), text)
        self.assertNotIn("\\", text)

    def test_04_tour(self):
        import sequence as sq
        app = self.app
        si_dir = os.path.join(self.tmp, "demo_si")
        before = len(sq.Runner.load_manifest(si_dir))
        state = {}

        def start():
            try:
                state["rc"] = self.mg.run_screenshot_tour(app, None, capture=False, close=False)
            finally:
                app.quit()
        app.after(50, start)
        app.mainloop()
        self.assertEqual(state.get("rc"), 0, self._log_tail())
        self.assertEqual(type(app.bus).__name__, "DemoBus")
        self.assertEqual(type(app.spectro.spec).__name__, "DemoSpec")
        self.assertEqual(app.shutter_state, "closed")
        self.assertTrue(app.statusbar.get("seq").startswith("Sequence Done"), app.statusbar.get("seq"))
        self.assertTrue(app.sequence.prog_var.get().startswith("Done"), app.sequence.prog_var.get())
        self.assertEqual(app.sequence.steps, [])
        self.assertGreater(len(sq.Runner.load_manifest(si_dir)), before)
        self.assertIn("demo_si", app.sequence.done_card.title_var.get())
        self.assertEqual(sorted(app.analysis.results), ["P", "S"])
        self.assertTrue(app.health_problems, "anomaly injected at the last station must be reported")
        self.assertTrue(any("SEQ Done" in l for l in app.log_lines), self._log_tail())

    def test_05_layout_audit(self):
        import ui_render
        app = self.app
        problems = {}
        for geometry in ("1280x820", "1100x720"):
            app.geometry(geometry)
            app.update()
            for key in app.pages:
                app.show_page(key)
                app.update_idletasks()
                found = ui_render.audit_layout(app)
                if found:
                    problems["%s@%s" % (key, geometry)] = found
        app.geometry("1280x820")
        app.update()
        self.assertEqual(problems, {})

    def test_06_render(self):
        import ui_render
        app = self.app
        app.show_page("measure")
        app.update()
        path = os.path.join(self.tmp, "render.png")
        ui_render.render_window(app, path)
        self.assertTrue(os.path.isfile(path))
        try:
            from PIL import Image
        except ImportError:
            return
        im = Image.open(path)
        self.assertEqual(im.size[0], app.winfo_width())
        self.assertEqual(im.size[1], app.winfo_height() + ui_render.TITLE_BAR)


if __name__ == "__main__":
    unittest.main()
