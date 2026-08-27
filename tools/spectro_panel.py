# -*- coding: utf-8 -*-
"""Tk page for manual spectrometer control (used by manual_gui.py).

One dedicated worker thread owns every DLL call, so acquisitions never run concurrently.
"""
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
from hw import bwtek
import ui_theme
from ui_theme import SPACE, COLORS, Card, StatusPill, Readout, form_row, bind_enter, tooltip

PAGE_PAD = (SPACE["xl"], SPACE["md"], SPACE["xl"], SPACE["md"])
SUBTITLE = "B&W Tek spectrometer: initialise, integration time and reads. Manual reads are disabled while a sequence runs."

TIPS = {
    "open": "InitDevices + bwtekTestUSB, then write the integration time set on the right.",
    "close": "Close the DLL session (not allowed while a sequence runs).",
    "recover": "When the spectrometer hangs (reads -99 / device count 0): close and reopen first; if that still fails, PnP-restart the spectrometer USB device (needs admin), never the hub the Elliptec bus is on.",
    "set_it": "Write the integration time (bwtekSetTimeUSB). Once set, the status bar marks the integration time as confirmed.",
    "smooth": "The same DLL smoothing as the original (type 3, width 5); sequence acquisitions use no smoothing.",
    "read": "Read one frame (averaged over the Averages count).",
    "live": "Read a frame every 250 ms and refresh the plot; click again to stop.",
    "auto_it": "Read and adjust the integration time until the active-region peak lands in 78-92 % of full scale (target 85 %); at most 8 steps, aborts if there is no light.",
    "monitor": "Read continuously and append each frame peak and the mean of three bands (450-550 / 600-700 / 800-900 nm) to data/monitor/*.csv, to watch source or detector drift.",
    "save": "Save the latest frame as CSV (wavelength_nm, counts).",
    "auto_y": "Checked: the Y axis scales to the peak; unchecked: fixed to full scale 65535.",
}


class SpectroWorker(threading.Thread):
    def __init__(self, results):
        threading.Thread.__init__(self, daemon=True)
        self.jobs = queue.Queue()
        self.results = results
        self.live = threading.Event()

    def run(self):
        while True:
            label, fn, callback = self.jobs.get()
            try:
                self.results.put(("ok", label, fn(), callback))
            except Exception as e:
                self.results.put(("err", label, e, callback))

    def submit(self, label, fn, callback=None):
        self.jobs.put((label, fn, callback))


class SpectrometerPanel(ttk.Frame):
    def __init__(self, master, app):
        ttk.Frame.__init__(self, master, style="Page.TFrame", padding=PAGE_PAD)
        self.app = app                       # provides _log_line(text), theme, data_root
        self.spec = None
        self.spec_factory = lambda log: bwtek.BWTek(log=log)     # replaced by the demo spectrometer in --demo
        self.results = queue.Queue()
        self.worker = SpectroWorker(self.results)
        self.worker.start()
        self.wl = bwtek.wavelengths()
        self.last = None
        self._empty_text = None
        self._buttons = []
        self._build()
        self.after(100, self._poll)

    def _build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        self.header = ui_theme.PageHeader(self, "Spectrometer", SUBTITLE)
        self.header.grid(row=0, column=0, sticky="ew", pady=(0, SPACE["lg"]))

        # ---- device card
        dev = Card(self, title="Device")
        dev.grid(row=1, column=0, sticky="ew", pady=(0, SPACE["md"]))
        db = dev.body
        row = ttk.Frame(db, style="CardBody.TFrame")
        row.grid(row=0, column=0, sticky="ew", pady=(0, SPACE["sm"]))
        self.btn_open = ttk.Button(row, text="Initialise", command=self.open_dev, style="Primary.TButton")
        self.btn_open.pack(side="left")
        self.btn_close = ttk.Button(row, text="Close", command=self.close_dev, state="disabled")
        self.btn_close.pack(side="left", padx=(SPACE["sm"], 0))
        self.btn_recover = ttk.Button(row, text="Recover", command=self.recover_dev, state="disabled")
        self.btn_recover.pack(side="left", padx=(SPACE["sm"], 0))
        tooltip(self.btn_open, TIPS["open"])
        tooltip(self.btn_close, TIPS["close"])
        tooltip(self.btn_recover, TIPS["recover"])
        self.state_var = tk.StringVar(value="Not initialised")
        self.state_pill = StatusPill(row, text="Not initialised", tone="neutral", on_card=True)
        self.state_pill.pack(side="left", padx=(SPACE["lg"], 0))
        self.state_var.trace_add("write", lambda *_a: self._sync_state_pill())
        self._buttons += [self.btn_open, self.btn_close, self.btn_recover]

        form = ttk.Frame(db, style="CardBody.TFrame")
        form.grid(row=1, column=0, sticky="ew")
        self.it_var = tk.StringVar(value="100")
        self.it_chosen = False          # True once the user / a sequence set the integration time deliberately
        e_it = ttk.Entry(form, textvariable=self.it_var, width=8, justify="right")
        bind_enter(e_it, self.set_it)
        e_it.grid(row=0, column=1, sticky="w", pady=SPACE["xs"])
        ttk.Label(form, text="Integration", style="FormLabel.TLabel", width=11, anchor="e").grid(row=0, column=0, sticky="e", padx=(0, SPACE["sm"]))
        ttk.Label(form, text="ms", style="Card.Caption.TLabel").grid(row=0, column=2, sticky="w", padx=(SPACE["xs"], SPACE["sm"]))
        self.btn_set_it = ttk.Button(form, text="Set", command=self.set_it)
        self.btn_set_it.grid(row=0, column=3, sticky="w")
        tooltip(self.btn_set_it, TIPS["set_it"])
        ttk.Label(form, text="Averages", style="FormLabel.TLabel", anchor="e").grid(row=0, column=4, sticky="e", padx=(SPACE["xl"], SPACE["sm"]))
        self.avg_var = tk.StringVar(value="1")
        ttk.Spinbox(form, from_=1, to=50, textvariable=self.avg_var, width=4, justify="right").grid(row=0, column=5, sticky="w")
        ttk.Label(form, text="x", style="Card.Caption.TLabel").grid(row=0, column=6, sticky="w", padx=(SPACE["xs"], SPACE["sm"]))
        self.smooth_var = tk.BooleanVar(value=False)
        cbx = ttk.Checkbutton(form, text="DLL smoothing (3,5)", variable=self.smooth_var, style="Card.TCheckbutton")
        cbx.grid(row=0, column=7, sticky="w", padx=(SPACE["lg"], 0))
        tooltip(cbx, TIPS["smooth"])
        self._buttons.append(self.btn_set_it)

        # ---- acquisition card
        acq = Card(self, title="Acquisition")
        acq.grid(row=2, column=0, sticky="nsew")
        ab = acq.body
        ab.columnconfigure(0, weight=1)
        ab.rowconfigure(3, weight=1)
        row = ttk.Frame(ab, style="CardBody.TFrame")
        row.grid(row=0, column=0, sticky="ew", pady=(0, SPACE["md"]))
        self.btn_read = ttk.Button(row, text="Read once", command=self.read_once, style="Primary.TButton")
        self.btn_read.pack(side="left")
        self.btn_live = ttk.Button(row, text="Live", command=self.toggle_live, style="Toggle.TButton")
        self.btn_live.pack(side="left", padx=(SPACE["sm"], 0))
        self.btn_auto = ttk.Button(row, text="Auto integration time", command=self.auto_it)
        self.btn_auto.pack(side="left", padx=(SPACE["sm"], 0))
        self.btn_mon = ttk.Button(row, text="Drift monitor", command=self.toggle_monitor, style="Toggle.TButton")
        self.btn_mon.pack(side="left", padx=(SPACE["sm"], 0))
        self.btn_save = ttk.Button(row, text="Save spectrum CSV…", command=self.save_csv)
        self.btn_save.pack(side="left", padx=(SPACE["sm"], 0))
        self.mon = None                   # stability monitor state (see toggle_monitor)
        self.auto_y = tk.BooleanVar(value=True)
        cby = ttk.Checkbutton(row, text="Y auto-scale", variable=self.auto_y, style="Card.TCheckbutton")
        cby.pack(side="right")
        for b, key in ((self.btn_read, "read"), (self.btn_live, "live"), (self.btn_auto, "auto_it"), (self.btn_mon, "monitor"),
                       (self.btn_save, "save"), (cby, "auto_y")):
            tooltip(b, TIPS[key])
        self._buttons += [self.btn_read, self.btn_live, self.btn_auto, self.btn_mon]

        self.readout = Readout(ab, [("peak", "Peak"), ("peak_wl", "Peak λ"), ("sat", "Sat. px"), ("mean", "Active mean")])
        self.readout.grid(row=1, column=0, sticky="w")
        self.stats_var = tk.StringVar(value="")
        ttk.Label(ab, textvariable=self.stats_var, style="Card.Caption.TLabel").grid(row=2, column=0, sticky="w", pady=(0, SPACE["sm"]))

        try:
            import matplotlib
            matplotlib.use("TkAgg")
            from matplotlib.figure import Figure
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        except Exception as e:
            ttk.Label(ab, text="matplotlib unavailable: %s" % e, style="Card.TLabel").grid(row=3, column=0, sticky="w")
            self.canvas = None
            return
        self.fig = Figure(figsize=(6.4, 3.0), dpi=100)
        self.fig.patch.set_edgecolor(COLORS["card"])
        self.ax = self.fig.add_subplot(111)
        ui_theme.mpl_style_axes(self.ax)
        self.ax.set_xlabel("wavelength (nm)")
        self.ax.set_ylabel("counts")
        self.ax.axvspan(self.wl[bwtek.ACTIVE_FIRST], self.wl[bwtek.ACTIVE_LAST], color=COLORS["plot_active"], zorder=0)
        self.ax.axhline(bwtek.ADC_MAX, color=COLORS["plot_limit"], lw=0.8, ls="--")
        (self.line,) = self.ax.plot(self.wl, np.zeros_like(self.wl), lw=0.9, color=COLORS["accent"])
        self.ax.set_xlim(self.wl[0], self.wl[-1])
        self.ax.set_ylim(0, bwtek.ADC_MAX * 1.02)
        self._empty_text = ui_theme.mpl_empty(self.ax, "No spectrum yet. Initialise, then click Read once.")
        self.fig.tight_layout(pad=1.2)
        self.canvas = FigureCanvasTkAgg(self.fig, master=ab)
        w = self.canvas.get_tk_widget()
        w.configure(width=self.app.theme.px(480), height=self.app.theme.px(200), highlightthickness=0)
        w.grid(row=3, column=0, sticky="nsew")
        ui_theme.mpl_bind_resize(self.canvas, self.fig)

    # ---- presentation helpers ------------------------------------------------
    def _sync_state_pill(self):
        s = self.state_var.get()
        if s.startswith("Connected") or s.startswith("Recovered"):
            tone = "success"
        elif s.endswith("…"):
            tone = "warning"
        elif s.startswith("Init failed"):
            tone = "danger"
        else:
            tone = "neutral"
        self.state_pill.set(s, tone)

    def _set_live_button(self, active):
        if active:
            self.btn_live.config(text="Stop live", style="Destructive.TButton")
        else:
            self.btn_live.config(text="Live", style="Toggle.TButton")
            if getattr(self.app, "sequence_running", False):
                self.btn_live.config(state="disabled")    # stopped while locked: no restart until the sequence ends

    def _set_mon_button(self, active):
        if active:
            self.btn_mon.config(text="Stop monitor", style="Destructive.TButton")
        else:
            self.btn_mon.config(text="Drift monitor", style="Toggle.TButton")
            if getattr(self.app, "sequence_running", False):
                self.btn_mon.config(state="disabled")

    def set_locked(self, locked):
        live = self.worker.live.is_set()
        for b in self._buttons:
            if b is self.btn_open:
                continue                                  # its state is owned by open_dev/_opened/close_dev
            if locked and ((b is self.btn_live and live) or (b is self.btn_mon and self.mon)):
                continue                                  # a running live read / monitor must stay stoppable
            b.configure(state="disabled" if locked else "normal")
        if not locked:
            self.btn_close.configure(state="normal" if self.spec else "disabled")
            self.btn_recover.configure(state="normal" if self.spec else "disabled")
        self.header.set_subtitle(SUBTITLE.rstrip(".") + " · sequence running, manual reads locked" if locked else SUBTITLE)

    # ---- device ------------------------------------------------------------
    def _log(self, text):
        self.results.put(("ok", "log", None, lambda _v, t=text: self.app._log_line("SPEC " + t)))

    def open_dev(self):
        try:
            ms = int(self.it_var.get())               # parsed on the Tk thread, never inside the worker
        except ValueError:
            messagebox.showerror("Invalid input", "Integration time must be an integer in ms")
            return
        if ms < 1:
            messagebox.showerror("Invalid input", "Integration time must be ≥ 1 ms")
            return
        factory = self.spec_factory

        def job():
            spec = factory(self._log)
            n = spec.open()
            try:
                spec.set_integration_time(ms)
            except Exception:
                try:
                    spec.close()                      # never leave an orphaned, initialised DLL handle
                except Exception:
                    pass
                self.results.put(("ok", "spec open failed", None, lambda _v: self.btn_open.config(state="normal")))
                raise
            return spec, n
        self.state_var.set("Initialising…")
        self.btn_open.config(state="disabled")        # one open job at a time
        self.worker.submit("spec open", job, self._opened)

    def _opened(self, res):
        self.spec, n = res
        self.state_var.set("Connected (%d dev), IT %d ms" % (n, self.spec.integration_ms))
        self.btn_open.config(state="disabled")
        self.btn_close.config(state="normal")
        self.btn_recover.config(state="normal")
        self.app.refresh_status()

    def recover_dev(self):
        """Spectrometer hung (-99 / not found): close + reopen, then a PnP restart of its USB device
        (pnputil, admin) - the software equivalent of a replug that leaves the Elliptec hub alone."""
        if not self._need():
            return
        self.worker.live.clear()
        self._stop_monitor()
        self._set_live_button(False)
        self.state_var.set("Recovering…")
        spec = self.spec
        self.worker.submit("spec recover", lambda: spec.recover(),
                           lambda how: self.state_var.set("Recovered (%s), IT %s ms" % (how, spec.integration_ms)))

    def close_dev(self):
        if getattr(self.app, "sequence_running", False):
            messagebox.showwarning("Sequence running", "Abort the sequence first")
            return
        self.worker.live.clear()
        self._stop_monitor()
        if self.spec:
            s = self.spec
            self.spec = None
            self.worker.submit("spec close", s.close, lambda _: self.state_var.set("Closed"))
        self.btn_open.config(state="normal")
        self.btn_close.config(state="disabled")
        self.btn_recover.config(state="disabled")
        self._set_live_button(False)
        self.app.refresh_status()

    def _need(self):
        if self.spec is None:
            messagebox.showwarning("Not initialised", "Initialise the spectrometer first")
            return False
        if getattr(self.app, "sequence_running", False):
            messagebox.showwarning("Sequence running", "Manual reads are disabled while a sequence runs")
            return False
        return True

    def set_it(self):
        if not self._need():
            return
        try:
            ms = int(self.it_var.get())
        except ValueError:
            messagebox.showerror("Invalid input", "Integration time must be an integer in ms")
            return

        def done(r):
            self.it_chosen = True                     # only once the device really accepted it
            self.state_var.set("Connected, IT %d ms" % r)
            self.app.refresh_status()
        self.worker.submit("spec set IT", lambda: self.spec.set_integration_time(ms), done)

    def _read_args(self):
        avg = max(1, int(self.avg_var.get() or 1))
        sm = (3, 5) if self.smooth_var.get() else (0, 0)
        return avg, sm

    def read_once(self):
        if not self._need():
            return
        avg, sm = self._read_args()
        self.worker.submit("spec read", lambda: self.spec.read(avg, *sm), self._show)

    def toggle_live(self):
        if self.worker.live.is_set():
            self.worker.live.clear()
            self._set_live_button(False)
            return
        if not self._need():
            return
        self.worker.live.set()
        self._set_live_button(True)
        self._live_step()

    def _live_step(self):
        if not self.worker.live.is_set() or self.spec is None:
            return
        avg, sm = self._read_args()
        self.worker.submit("spec live", lambda: self.spec.read(avg, *sm), self._show_and_continue)

    def _show_and_continue(self, counts):
        self._show(counts)
        if self.worker.live.is_set():
            self.after(250, self._live_step)

    # ---- stability monitor -------------------------------------------------
    MON_BANDS = ((450, 550), (600, 700), (800, 900))

    def toggle_monitor(self):
        """Continuous reads with every spectrum's peak / band means appended to a CSV.

        Used to look for illumination or detection drift: leave the sample, shutter and stages
        untouched and watch the relative change against the first frame.
        """
        if self.mon:
            self._stop_monitor()
            return
        if not self._need():
            return
        root = os.path.join(getattr(self.app, "data_root", os.path.join(HERE, "..", "data")), "monitor")
        os.makedirs(root, exist_ok=True)
        path = os.path.join(root, time.strftime("monitor_%Y%m%d_%H%M%S.csv"))
        fh = open(path, "w")
        fh.write("time,elapsed_s,peak,baseline," + ",".join("mean_%d_%d" % b for b in self.MON_BANDS) + "\n")
        self.mon = {"path": path, "fh": fh, "t0": time.time(), "n": 0, "first": None}
        self._set_mon_button(True)
        self.app._log_line("SPEC monitor start -> %s (IT %s ms)" % (path, self.spec.integration_ms))
        if not self.worker.live.is_set():
            self.toggle_live()

    def _stop_monitor(self):
        if not self.mon:
            return
        m = self.mon
        self.mon = None
        try:
            m["fh"].close()
        except Exception:
            pass
        self._set_mon_button(False)
        self.app._log_line("SPEC monitor stop: %d frames, %.0f s -> %s" % (m["n"], time.time() - m["t0"], m["path"]))
        if self.worker.live.is_set():
            self.toggle_live()

    def _monitor_frame(self, counts, st):
        m = self.mon
        active = counts[bwtek.ACTIVE_FIRST:bwtek.ACTIVE_LAST + 1].astype(float)
        wl = self.wl[bwtek.ACTIVE_FIRST:bwtek.ACTIVE_LAST + 1]
        base = float(np.percentile(counts[:bwtek.ACTIVE_FIRST], 50))          # masked pixels ~ dark level
        means = [float(np.mean(active[(wl >= a) & (wl < b)])) - base for a, b in self.MON_BANDS]
        el = time.time() - m["t0"]
        m["fh"].write("%s,%.1f,%d,%.0f,%s\n" % (time.strftime("%H:%M:%S"), el, st["max"], base, ",".join("%.1f" % v for v in means)))
        m["fh"].flush()
        if m["first"] is None:
            m["first"] = means
        rel = ["%+.1f%%" % (100.0 * (v / f - 1.0) if f else 0.0) for v, f in zip(means, m["first"])]
        m["n"] += 1
        txt = "Monitor #%d  %.0f s  vs first 450-550/600-700/800-900: %s" % (m["n"], el, " / ".join(rel))
        if m["n"] == 1 or m["n"] % 10 == 0:
            self.app._log_line("SPEC monitor #%d t=%.0fs peak=%d base=%.0f bands=%s rel=%s"
                               % (m["n"], el, st["max"], base, ["%.0f" % v for v in means], rel))
        return txt

    def _show(self, counts):
        self.last = counts
        st = bwtek.spectrum_stats(counts)
        it = getattr(self.spec, "integration_ms", None)
        txt = "Last read %s · IT %s ms · peak %.1f%% of full scale" % (
            time.strftime("%H:%M:%S"), it if it else "—", 100.0 * st["max"] / bwtek.ADC_MAX)
        if self.mon:
            txt = self._monitor_frame(counts, st) + "   |   " + txt
        self.stats_var.set(txt)
        pct = 100.0 * st["max"] / bwtek.ADC_MAX
        self.readout.set("peak", "%d (%.0f %%)" % (st["max"], pct),
                         "danger" if st["saturated_active"] else ("warning" if pct >= 92 else None))
        self.readout.set("peak_wl", "%.1f nm" % self.wl[st["argmax"]])
        self.readout.set("sat", "%d (active %d)" % (st["saturated"], st["saturated_active"]), "danger" if st["saturated_active"] else None)
        self.readout.set("mean", "%.0f" % st["mean_active"])
        if self.canvas:
            if self._empty_text is not None:
                try:
                    self._empty_text.remove()
                except Exception:
                    pass
                self._empty_text = None
            self.line.set_ydata(counts)
            if self.auto_y.get():
                self.ax.set_ylim(0, max(1000, st["max"] * 1.1))
            else:
                self.ax.set_ylim(0, bwtek.ADC_MAX * 1.02)
            self.canvas.draw_idle()

    def auto_it(self):
        """Iterate integration time until the active-region peak sits at ~85 % of full scale."""
        if not self._need():
            return
        avg, sm = self._read_args()
        spec = self.spec

        def job():
            it = spec.integration_ms or int(self.it_var.get())
            for step in range(8):
                counts = spec.read(1, *sm)
                active = counts[bwtek.ACTIVE_FIRST:bwtek.ACTIVE_LAST + 1]
                peak, base = int(active.max()), int(counts.min())
                self._log("auto-IT step %d: IT %d ms -> peak %d (%.0f%%), baseline %d" % (step, it, peak, 100.0 * peak / bwtek.ADC_MAX, base))
                if bwtek.peak_in_band(peak):
                    return it, counts
                if peak - base < 50 and it >= bwtek.AUTO_IT_DARK_MS:
                    raise RuntimeError("auto-IT: no light at %d ms (shutter closed? lamp off? fibre?)" % it)
                it = bwtek.next_integration_time(it, peak, base)
                spec.set_integration_time(it)
            raise RuntimeError("auto-IT did not converge in 8 steps (last %d ms, peak %d)" % (it, peak))

        self.state_var.set("Auto-calibrating…")
        self.worker.submit("spec auto-IT", job, self._auto_it_done)

    def _auto_it_done(self, res):
        it, counts = res
        self.it_chosen = True
        self.it_var.set(str(it))
        self.state_var.set("Connected, IT %d ms (auto)" % it)
        self._show(counts)
        self.app.refresh_status()

    def save_csv(self):
        if self.last is None:
            messagebox.showwarning("No data", "Read a spectrum first")
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", initialfile=time.strftime("spectrum_%Y%m%d_%H%M%S.csv"))
        if path:
            np.savetxt(path, np.column_stack([self.wl, self.last]), fmt="%.3f,%d",
                       header="wavelength_nm,counts", comments="")
            self.app._log_line("saved %s" % path)

    def _poll(self):
        try:
            while True:
                status, label, value, cb = self.results.get_nowait()
                if status == "ok":
                    if cb:
                        cb(value)
                else:
                    self.app._log_line("!! %s failed: %s" % (label, value))
                    if label == "spec open":
                        self.state_var.set("Init failed")
                        self.btn_open.config(state="normal")
                    if label == "spec auto-IT" and self.spec is not None:
                        self.state_var.set("Connected, IT %s ms" % self.spec.integration_ms)
                    if label == "spec live":
                        self.worker.live.clear()
                        self._set_live_button(False)
                    if label == "sequence":
                        self.app.sequence._finish("Failed/aborted: %s" % value)
                        messagebox.showerror("Sequence failed", "%s\n\nThe queue is kept (completed steps marked ✓); fix the issue and re-run the whole queue." % value)
                    else:
                        messagebox.showerror("Spectrometer error", "%s\n%s" % (label, value))
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def shutdown(self, timeout=5.0):
        """Close the device on the spectrometer worker (the DLL is not thread-safe); wait at most
        `timeout` s so a wedged DLL cannot keep the window open forever."""
        self.worker.live.clear()
        self._stop_monitor()
        if self.spec:
            s, done = self.spec, threading.Event()
            self.spec = None

            def job():
                try:
                    s.close()
                finally:
                    done.set()
            self.worker.submit("spec close (exit)", job)
            if not done.wait(timeout):
                self.app._log_line("!! spectrometer close did not finish within %.0f s (DLL busy?)" % timeout)
