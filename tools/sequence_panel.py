# -*- coding: utf-8 -*-
"""Tk page that builds and runs measurement sequences (uses sequence.py).

Layout: page header (Run / Abort) + progress band, a session card and an add-step card on the left,
the step queue on the right, and the completed-acquisition table + live preview below. The logic
(queue editing, the seven run() guards in their original order, the Runner on the spectrometer
worker, event polling) is unchanged from the notebook version; only presentation moved.
"""
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, ".."))
import sequence as sq
from hw import bwtek
from hw import config as cfg
import ui_theme
from ui_theme import SPACE, COLORS, Card, form_row, unit_label, bind_enter, tooltip, empty_state

DATA_ROOT = os.path.join(HERE, "..", "data")      # default; the App may point elsewhere (--demo)
PAGE_PAD = (SPACE["xl"], SPACE["md"], SPACE["xl"], SPACE["md"])
SUBTITLE = "Build steps, run the sequence, and review the acquisitions completed in this session."
GEOMETRY = "S=%g° P=%g° · θ+%g°" % (
    cfg.POL_DEG["S"], cfg.POL_DEG["P"], cfg.SAMPLE_VAR_OFFSET)

TIPS = {
    "reconnect": "Before each run, reopen the spectrometer DLL session (recovers a hung USB device) so you need not power-cycle between samples.",
    "zero": "Before each run, close the shutter and move every rotation stage to its zero (the fibre arm by absolute move, never homed).",
    "stabilise": "Before the reference calibration, hold until the lamp is steady (band-mean change under 0.5% for 5 reads, ~60-read timeout) so lamp drift between the reference and sample sessions cannot bias R.",
    "run": "Run the whole queue (Ctrl+R / ⌘R). Before running it checks in turn: serial port and spectrometer, integration time, existing data, duplicate tags, then a final confirm.",
    "abort": "Request an abort (Esc): the run stops after the current step; the Runner closes the shutter and restores any temporary integration time.",
    "load": "Read data/<session>/manifest.json and list the completed acquisitions in the table below; also read automatically before a run.",
    "ref": "Arm to 44°, sample to θ=80°, shutter open, auto-calibrate S and P, keep the smaller integration time, shutter close. The reference should be a white board / standard.",
    "dark": "With the shutter closed, acquire one dark; the file is named by integration time (dark_<IT>ms.csv).",
    "single": "At the given θ, acquire one frame per selected polarisation: arm zero -> polariser -> sample θ+105° -> shutter open -> acquire -> shutter close.",
    "scan": "Repeat the single-angle measurement from start..stop every step degrees; the polariser rotates once per polarisation. Requires 0 <= start < stop <= 80, step >= 1.",
    "db": "Double-beam substitution correction: go to the exchange position and pause for you to swap the integrating-sphere port cap, then to the DB position (arm 124° / sample 93°) to acquire S/P and a dark at 1000 ms, then swap back and reset.",
    "set_it": "Insert an 'Integration time N ms' step into the queue (a permanent setting, recorded as chosen).",
    "pause": "Insert a pause: when the run reaches it a prompt appears; OK continues, Cancel aborts.",
    "shutter": "Insert a standalone shutter step (usually unnecessary: measurement steps open/close the shutter themselves).",
    "geometry": "From hw/config.py: POL_DEG, SAMPLE_VAR_OFFSET, SYSTEM_ZERO, SYSTEM_DB, SAMPLE_DB.",
    "remove": "Remove the selected step from the queue (unavailable while running).",
    "clear": "Clear the whole queue (unavailable while running).",
}

_BAND_TONES = {"idle": "text2", "running": "text", "paused": "warning_text", "done": "success_text",
               "aborted": "warning_text", "failed": "danger_pressed"}


class SequencePanel(ttk.Frame):
    def __init__(self, master, app):
        ttk.Frame.__init__(self, master, style="Page.TFrame", padding=PAGE_PAD)
        self.app = app
        self.steps = []
        self.job_steps = []
        self.events = queue.Queue()
        self.abort = threading.Event()
        self.running = False
        self.done_tags = set()
        self._edit_buttons = []
        self._empty_text = None
        self._build()
        self.after(100, self._poll)

    # ---- layout ------------------------------------------------------------
    def _data_root(self):
        return getattr(self.app, "data_root", None) or DATA_ROOT

    def _build(self):
        px = self.app.theme.px
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)                   # steps | queue
        self.rowconfigure(4, weight=1)                   # done | preview
        self.header = ui_theme.PageHeader(self, "Measurement", SUBTITLE,
                                          actions=[("▶ Run", self.run, "Primary.TButton"), ("■ Stop", self.abort_run, "Destructive.TButton")])
        self.header.grid(row=0, column=0, sticky="ew", pady=(0, SPACE["md"]))
        self.btn_run = self.header.buttons["▶ Run"]
        self.btn_abort = self.header.buttons["■ Stop"]
        self.btn_abort.configure(state="disabled")
        tooltip(self.btn_run, TIPS["run"])
        tooltip(self.btn_abort, TIPS["abort"])

        # ---- progress band
        band = ttk.Frame(self, style="Page.TFrame")
        band.grid(row=1, column=0, sticky="ew", pady=(0, SPACE["md"]))
        band.columnconfigure(1, weight=1)
        self.prog_var = tk.StringVar(value="Idle")
        self.prog_label = ttk.Label(band, textvariable=self.prog_var, style="TLabel", anchor="w")
        self.prog_label.grid(row=0, column=0, sticky="w", padx=(0, SPACE["md"]))
        self.bar = ttk.Progressbar(band, mode="determinate")
        self.bar.grid(row=0, column=1, sticky="ew")
        # reliability toggles (2026-08-27): every run reconnects the spectrometer and zeros every stage,
        # so the operator no longer power-cycles the USB or reconnects between samples (kept on the band
        # row to stay within the 720 px height budget). The fibre arm is moved to its zero, never homed.
        self.autoreconnect_var = tk.BooleanVar(value=True)
        cb_rc = ttk.Checkbutton(band, text="Reconnect", variable=self.autoreconnect_var, style="TCheckbutton")
        cb_rc.grid(row=0, column=2, sticky="e", padx=(SPACE["md"], 0))
        tooltip(cb_rc, TIPS["reconnect"])
        self.autoreset_var = tk.BooleanVar(value=True)
        cb_z = ttk.Checkbutton(band, text="Zero", variable=self.autoreset_var, style="TCheckbutton")
        cb_z.grid(row=0, column=3, sticky="e", padx=(SPACE["sm"], 0))
        tooltip(cb_z, TIPS["zero"])
        self.stabilise_var = tk.BooleanVar(value=True)
        cb_s = ttk.Checkbutton(band, text="Stabilise", variable=self.stabilise_var, style="TCheckbutton")
        cb_s.grid(row=0, column=4, sticky="e", padx=(SPACE["sm"], 0))
        tooltip(cb_s, TIPS["stabilise"])
        self._set_band_tone("idle")

        # ---- session: one full-width row (keeps the page under the 720 px minimum height)
        self._build_session_card(self)

        # ---- main row: add-steps card (left, natural width) + queue (right, takes the slack)
        main = ttk.Frame(self, style="Page.TFrame")
        main.grid(row=3, column=0, sticky="nsew", pady=(0, SPACE["md"]))
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)
        self._build_steps_card(main)
        self._build_queue_card(main)

        # ---- bottom row: completed acquisitions (left) + live preview (right)
        bottom = ttk.Frame(self, style="Page.TFrame")
        bottom.grid(row=4, column=0, sticky="nsew")
        bottom.columnconfigure(0, weight=3)
        bottom.columnconfigure(1, weight=2)
        bottom.rowconfigure(0, weight=1)
        self._build_done_card(bottom, px)
        self._build_preview_card(bottom, px)

    def _build_session_card(self, parent):
        card = Card(parent)                              # one labelled row; a title would only repeat 'session'
        card.grid(row=2, column=0, sticky="ew", pady=(0, SPACE["md"]))
        self.session_card = card
        row = ttk.Frame(card.body, style="CardBody.TFrame")
        row.grid(row=0, column=0, sticky="w")
        ttk.Label(row, text="Session / sample", style="FormLabel.TLabel").pack(side="left", padx=(0, SPACE["sm"]))
        self.session_var = tk.StringVar(value=time.strftime("session_%Y%m%d_%H%M"))
        e = ttk.Entry(row, textvariable=self.session_var, width=18)
        e.pack(side="left")
        bind_enter(e, self.load_history)
        self.btn_load = ttk.Button(row, text="Load existing", command=self.load_history, style="Ghost.TButton")
        self.btn_load.pack(side="left", padx=(SPACE["xs"], 0))
        tooltip(self.btn_load, TIPS["load"])
        ttk.Label(row, text="Tag prefix", style="FormLabel.TLabel").pack(side="left", padx=(SPACE["xl"], SPACE["sm"]))
        self.prefix_var = tk.StringVar(value="sample")
        ttk.Entry(row, textvariable=self.prefix_var, width=8).pack(side="left")
        ttk.Label(row, text="Averages", style="FormLabel.TLabel").pack(side="left", padx=(SPACE["lg"], SPACE["sm"]))
        self.avg_var = tk.StringVar(value="3")
        ttk.Spinbox(row, from_=1, to=50, textvariable=self.avg_var, width=3, justify="right").pack(side="left")
        ttk.Label(row, text="x", style="Card.Caption.TLabel").pack(side="left", padx=(SPACE["xs"], 0))
        ttk.Label(row, text="Polarisation", style="FormLabel.TLabel").pack(side="left", padx=(SPACE["lg"], SPACE["sm"]))
        self.pol_var = tk.StringVar(value="S+P")
        ttk.Combobox(row, textvariable=self.pol_var, values=["S", "P", "S+P"], state="readonly", width=4).pack(side="left")

    def _build_steps_card(self, parent):
        card = Card(parent, title="Add steps", subtitle=GEOMETRY)
        card.grid(row=0, column=0, sticky="nsew", padx=(0, SPACE["md"]))
        tooltip(card.subtitle_label, TIPS["geometry"])
        self.steps_card = card
        f = card.body
        pady = 1

        def button(parent_, text, cmd, tip, style="TButton"):
            b = ttk.Button(parent_, text=text, command=cmd, style=style)
            tooltip(b, TIPS[tip])
            self._edit_buttons.append(b)
            return b

        # grid: column 0 label, column 1 field group (entries + hugging unit), column 2 the add button
        r0 = ttk.Frame(f, style="CardBody.TFrame")
        r0.grid(row=0, column=0, columnspan=3, sticky="w", pady=pady)
        button(r0, "① Reference · 80° S/P auto IT", self.add_reference, "ref").pack(side="left")
        button(r0, "② Dark · shutter closed, current IT", self.add_dark, "dark").pack(side="left", padx=(SPACE["sm"], 0))

        self.theta_var = tk.StringVar(value="45")
        g_th = ttk.Frame(f, style="CardBody.TFrame")
        e_th = ttk.Entry(g_th, textvariable=self.theta_var, width=6, justify="right")
        e_th.pack(side="left")
        bind_enter(e_th, self.add_single)
        unit_label(g_th, "°")
        form_row(f, 1, "θ", g_th, button(f, "③ Single angle", self.add_single, "single"), label_width=8, pady=pady)

        sc = ttk.Frame(f, style="CardBody.TFrame")
        self.start_var, self.stop_var, self.step_var = tk.StringVar(value="8"), tk.StringVar(value="80"), tk.StringVar(value="4")
        for i, (lab, v) in enumerate((("Start", self.start_var), ("Stop", self.stop_var), ("Step", self.step_var))):
            ttk.Label(sc, text=lab, style="FormLabel.TLabel").pack(side="left", padx=((0 if i == 0 else SPACE["sm"]), SPACE["xs"]))
            e = ttk.Entry(sc, textvariable=v, width=4, justify="right")
            bind_enter(e, self.add_scan)
            e.pack(side="left")
        unit_label(sc, "°")
        form_row(f, 2, "Scan range", sc, button(f, "④ Angle scan", self.add_scan, "scan"), label_width=8, pady=pady)

        r4 = ttk.Frame(f, style="CardBody.TFrame")
        r4.grid(row=3, column=0, columnspan=3, sticky="w", pady=pady)
        button(r4, "⑤ Double-beam DB · with port-cap pause", self.add_db, "db").pack(side="left")
        self.btn_shutter_close = button(r4, "Add: shutter close", lambda: self._add([sq.shutter(False)], dedupe=False), "shutter")
        self.btn_shutter_close.pack(side="left", padx=(SPACE["md"], 0))
        self.btn_shutter_open = button(r4, "Add: shutter open", lambda: self._add([sq.shutter(True)], dedupe=False), "shutter")
        self.btn_shutter_open.pack(side="left", padx=(SPACE["sm"], 0))

        self.it_var = tk.StringVar(value="1000")
        g_it = ttk.Frame(f, style="CardBody.TFrame")
        e_it = ttk.Entry(g_it, textvariable=self.it_var, width=8, justify="right")
        e_it.pack(side="left")
        bind_enter(e_it, self.add_set_it)
        unit_label(g_it, "ms")
        form_row(f, 4, "Integration", g_it, button(f, "Add: set IT", self.add_set_it, "set_it"), label_width=8, pady=pady)

        self.pause_var = tk.StringVar(value="Swap the sample, then click OK")
        e_pause = ttk.Entry(f, textvariable=self.pause_var, width=24)
        bind_enter(e_pause, self.add_pause)
        form_row(f, 5, "Pause prompt", e_pause, button(f, "Add: pause", self.add_pause, "pause"), label_width=8, pady=pady)
        # form_row gives the last used column the weight; one action column (2) for every row, the
        # fields keep their natural width and the wide button rows push their slack into column 2
        for c in range(2):
            f.columnconfigure(c, weight=0)
        f.columnconfigure(2, weight=1)

    def _build_queue_card(self, parent):
        card = Card(parent, title="Queue · 0 steps · 0 acq")
        card.grid(row=0, column=1, sticky="nsew")
        self.queue_card = card
        self.queue_frame = card                      # legacy name (the LabelFrame of the notebook version)
        body = card.body
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.listbox = tk.Listbox(body, width=30, height=8, activestyle="none", exportselection=False)
        self.listbox.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(body, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=sb.set)
        sb.grid(row=0, column=1, sticky="ns")
        self.queue_empty = empty_state(body, "Queue is empty.", "Use 'Add steps' on the left to add ①-⑤.\nTypical order: ① -> ② -> ④ (white board) -> swap sample -> ④ -> ⑤.")
        self.queue_empty.place(in_=self.listbox, relx=0.5, rely=0.42, anchor="center")
        b = ttk.Frame(body, style="CardBody.TFrame")
        b.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(SPACE["sm"], 0))
        self.btn_remove = ttk.Button(b, text="Remove", command=self.remove_selected)
        self.btn_remove.pack(side="left")
        self.btn_clear = ttk.Button(b, text="Clear", command=self.clear)
        self.btn_clear.pack(side="left", padx=(SPACE["sm"], 0))
        tooltip(self.btn_remove, TIPS["remove"])
        tooltip(self.btn_clear, TIPS["clear"])
        self._edit_buttons += [self.btn_remove, self.btn_clear]

    def _build_done_card(self, parent, px):
        card = Card(parent, title="Completed acquisitions · 0")
        card.grid(row=0, column=0, sticky="nsew", padx=(0, SPACE["md"]))
        self.done_card = card
        self.done_frame = card                       # legacy name
        body = card.body
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        cols = ("time", "tag", "theta", "pol", "it", "peak", "sat")
        self.done_tree = ttk.Treeview(body, columns=cols, show="headings", height=3, takefocus=0)   # grows with the window
        for c, w, t, a in zip(cols, (70, 130, 45, 40, 55, 55, 40), ("Time", "Tag", "θ°", "Pol", "IT ms", "Peak%", "Sat"),
                              ("w", "w", "e", "center", "e", "e", "e")):
            self.done_tree.heading(c, text=t, anchor=a)
            self.done_tree.column(c, width=px(w), minwidth=px(w), anchor=a, stretch=(c == "tag"))
        self.done_tree.tag_configure("odd", background=COLORS["row_alt"])
        sb = ttk.Scrollbar(body, orient="vertical", command=self.done_tree.yview)
        self.done_tree.configure(yscrollcommand=sb.set)
        self.done_tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.done_empty = empty_state(body, "No acquisitions in this session yet.", "Each spectrum appears here after a run.")
        self.done_empty.place(in_=self.done_tree, relx=0.5, rely=0.6, anchor="center")

    def _build_preview_card(self, parent, px):
        card = Card(parent, title="Live preview", subtitle="(no data yet)")
        card.grid(row=0, column=1, sticky="nsew")
        self.preview_card = card
        self.preview_var = tk.StringVar(value="(no data yet)")
        card.subtitle_label.configure(textvariable=self.preview_var)
        body = card.body
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.canvas = None
        try:
            import matplotlib
            matplotlib.use("TkAgg")
            from matplotlib.figure import Figure
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        except Exception as e:
            ttk.Label(body, text="matplotlib unavailable: %s" % e, style="Card.TLabel").grid(row=0, column=0, sticky="w")
            return
        self.wl = bwtek.wavelengths()
        self.fig = Figure(figsize=(3.6, 1.6), dpi=100)
        self.fig.patch.set_edgecolor(COLORS["card"])
        self.ax = self.fig.add_subplot(111)
        ui_theme.mpl_style_axes(self.ax)
        self.ax.set_xlabel("nm")
        # the active band, the ADC limit and the trace stay hidden behind the empty state until the
        # first spectrum arrives (an empty axis with a saturation line reads as a broken plot)
        self._guides = [self.ax.axvspan(self.wl[bwtek.ACTIVE_FIRST], self.wl[bwtek.ACTIVE_LAST], color=COLORS["plot_active"], zorder=0),
                        self.ax.axhline(bwtek.ADC_MAX, color=COLORS["plot_limit"], lw=0.8, ls="--")]
        (self.line,) = self.ax.plot(self.wl, [0] * len(self.wl), lw=0.9, color=COLORS["accent"])
        for a in self._guides + [self.line]:
            a.set_visible(False)
        self.ax.set_yticks([])
        self.ax.set_xlim(self.wl[0], self.wl[-1])
        self.ax.set_ylim(0, bwtek.ADC_MAX * 1.02)
        self._empty_text = ui_theme.mpl_empty(self.ax, "The latest spectrum appears here after a run")
        self.fig.tight_layout(pad=1.2)
        self.canvas = FigureCanvasTkAgg(self.fig, master=body)
        w = self.canvas.get_tk_widget()
        w.configure(width=px(300), height=px(76), highlightthickness=0)
        w.grid(row=0, column=0, sticky="nsew")
        ui_theme.mpl_bind_resize(self.canvas, self.fig)

    # ---- presentation helpers ------------------------------------------------
    def _set_band_tone(self, tone):
        self.prog_label.configure(foreground=COLORS[_BAND_TONES.get(tone, "text")])

    def _tone_for(self, text):
        for prefix, tone in (("Done", "done"), ("Aborted", "aborted"), ("Failed", "failed"), ("Aborting", "aborted"), ("Waiting", "paused"), ("Idle", "idle")):
            if text.startswith(prefix):
                return tone
        return "running"

    def set_locked(self, locked):
        """Called by the App whenever sequence_running changes; the messagebox guards stay in place."""
        st = "disabled" if locked else "normal"
        for b in self._edit_buttons:
            b.configure(state=st)
        self.header.set_subtitle(SUBTITLE.rstrip(".") + " · sequence running" if locked else SUBTITLE)

    def _sync_empty_states(self):
        if self.steps:
            self.queue_empty.place_forget()
        else:
            self.queue_empty.place(in_=self.listbox, relx=0.5, rely=0.42, anchor="center")
        if self.done_tree.get_children():
            self.done_empty.place_forget()
        else:
            self.done_empty.place(in_=self.done_tree, relx=0.5, rely=0.6, anchor="center")

    # ---- step editing ------------------------------------------------------
    def _pols(self):
        return ["S", "P"] if self.pol_var.get() == "S+P" else [self.pol_var.get()]

    def _avg(self):
        return max(1, int(self.avg_var.get() or 1))

    def _update_counts(self):
        n_acq = sum(1 for s in self.steps if s.kind == "acquire")
        self.queue_card.set_title("Queue · %d steps · %d acq" % (len(self.steps), n_acq))
        self._sync_empty_states()

    @staticmethod
    def _key(step):
        return (step.kind, repr(sorted(step.params.items())))

    def _already_queued(self, steps):
        """True if this exact block already appears contiguously in the queue."""
        new = [self._key(s) for s in steps]
        have = [self._key(s) for s in self.steps]
        k = len(new)
        return k > 0 and any(have[i:i + k] == new for i in range(len(have) - k + 1))

    def _add(self, steps, dedupe=True):
        if self.running:
            messagebox.showwarning("Sequence running", "The queue cannot be edited while running")
            return
        if dedupe and self._already_queued(steps):
            messagebox.showinfo("Already queued", "An identical step block is already in the queue; not added again.\nIf you really need a repeat, remove the existing one first.")
            return
        for s in steps:
            self.steps.append(s)
            self.listbox.insert("end", "%3d  %s" % (len(self.steps), s.text))
        self.listbox.see("end")
        self._update_counts()

    def add_reference(self):
        self._add(sq.build_reference_calibration(stabilise=self.stabilise_var.get()))

    def add_dark(self):
        self._add(sq.build_dark(self._avg()))

    def add_single(self):
        try:
            th = float(self.theta_var.get())
            if not (cfg.THETA_MIN <= th <= cfg.THETA_MAX):
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid input", "θ must be within %d-%d°" % (cfg.THETA_MIN, cfg.THETA_MAX))
            return
        self._add(sq.build_single_angle(th, self._pols(), self._avg(), self.prefix_var.get() or "sample"))

    def add_scan(self):
        try:
            a, b, c = float(self.start_var.get()), float(self.stop_var.get()), float(self.step_var.get())
            steps = sq.build_scan(a, b, c, self._pols(), self._avg(), self.prefix_var.get() or "sample")
        except ValueError as e:
            messagebox.showerror("Invalid input", str(e))
            return
        self._add(steps)

    def add_db(self):
        self._add(sq.build_double_beam(self._pols(), self._avg(), self.prefix_var.get() or "sample"))

    def add_set_it(self):
        try:
            self._add([sq.set_it(int(self.it_var.get()))], dedupe=False)
        except ValueError:
            messagebox.showerror("Invalid input", "Integration time must be an integer in ms")

    def add_pause(self):
        self._add([sq.pause(self.pause_var.get())], dedupe=False)

    def remove_selected(self):
        if self.running:
            return
        sel = list(self.listbox.curselection())
        for i in reversed(sel):
            del self.steps[i]
        self._refresh()

    def clear(self):
        if self.running:
            return
        self.steps = []
        self._refresh()

    def _refresh(self):
        self.listbox.delete(0, "end")
        for i, s in enumerate(self.steps):
            self.listbox.insert("end", "%3d  %s" % (i + 1, s.text))
        self._update_counts()

    # ---- running -----------------------------------------------------------
    def run(self):
        if self.running or not self.steps:
            return
        if self.app.bus is None or self.app.spectro.spec is None:
            messagebox.showwarning("Not ready", "Connect the serial port and initialise the spectrometer on the Instrument page first")
            return
        outdir = os.path.join(self._data_root(), self.session_var.get().strip() or time.strftime("session_%Y%m%d_%H%M%S"))
        # integration time consistency: a restarted GUI defaults to 100 ms, but the session's
        # spectra must all share one integration time unless the queue sets it explicitly
        first_acq = next((i for i, s in enumerate(self.steps) if s.kind == "acquire" and s.params.get("meta", {}).get("kind") in ("var", "dark")), None)
        sets_it = any((s.kind == "set_it" and not s.params.get("save")) or s.kind in ("auto_it", "apply_min_it")
                      for s in self.steps[:first_acq]) if first_acq is not None else True
        session_its = [r["integration_ms"] for r in sq.Runner.load_manifest(outdir) if r.get("kind") == "var"]
        # a spectrometer still at the double-beam time after an aborted DB block is not a chosen value
        db_leftover = self.app.spectro.spec.integration_ms == cfg.DB_IT_MS and cfg.DB_IT_MS not in session_its
        if first_acq is not None and not sets_it and not session_its and (not self.app.spectro.it_chosen or db_leftover):
            # new session, GUI restarted, nothing set the integration time yet -> offer the last one used anywhere
            last = self._last_session_it()
            hint = ("The most recent session %s used %d ms. Insert 'Integration time %d ms' at the start of the queue?\n(No: acquire at %s ms; Cancel: do not run)"
                    % (last[0], last[1], last[1], self.app.spectro.spec.integration_ms)) if last else "Set or calibrate the integration time on the Spectrometer page, or add ① at the start of the queue."
            if last:
                ans = messagebox.askyesnocancel("Integration time not set", "No integration time has been set since the GUI started (currently %s ms, the default).\n\n%s"
                                                % (self.app.spectro.spec.integration_ms, hint))
                if ans is None:
                    return
                if ans:
                    self.steps.insert(0, sq.set_it(last[1]))
                    self._refresh()
            else:
                messagebox.showwarning("Integration time not set", "No integration time has been set since the GUI started (currently %s ms, the default).\n%s" % (self.app.spectro.spec.integration_ms, hint))
                return
        if first_acq is not None and not sets_it and session_its:
            session_it = max(set(session_its), key=session_its.count)
            current = self.app.spectro.spec.integration_ms
            if current != session_it:
                if messagebox.askyesno("Integration time mismatch",
                                       "The spectrometer is at %s ms, but session %s already has data taken at %d ms.\n\n"
                                       "Insert 'Integration time %d ms' at the start of the queue? (No: acquire at %s ms)"
                                       % (current, os.path.basename(outdir), session_it, session_it, current)):
                    self.steps.insert(0, sq.set_it(session_it))
                    self._refresh()
        tags = [s.params["tag"] for s in self.steps if s.kind == "acquire"]
        n_acq = len(tags)
        existing = sorted(set(t for t in tags if t != "dark") & set(r.get("tag") for r in sq.Runner.load_manifest(outdir)))
        if existing:
            if not messagebox.askyesno("Already measured", "This session directory already has %d acquisitions with the same names (e.g. %s); they will be overwritten.\nRun anyway?"
                                       % (len(existing), ", ".join(existing[:4]))):
                return
        dups = sorted(set(t for t in tags if tags.count(t) > 1 and t != "dark"))
        if dups:
            if not messagebox.askyesno("Duplicate tags", "The queue has %d duplicate tags (e.g. %s); a later acquisition overwrites the earlier file.\n"
                                       "Usually this is from clicking Add several times - better to Clear and re-add.\n\nRun anyway?"
                                       % (len(dups), ", ".join(dups[:4]))):
                return
        est = sum(s.params["avg"] for s in self.steps if s.kind == "acquire")
        if not messagebox.askyesno("Run sequence", "%d steps, %d acquisitions (about %d exposures), data directory:\n%s\n\nThe stages will move automatically. Start?" % (len(self.steps), n_acq, est, outdir)):
            return
        self.running = True
        self.app.sequence_running = True
        self.abort.clear()
        self.btn_run.config(state="disabled")
        self.btn_abort.config(state="normal")
        steps = list(self.steps)
        if self.autoreset_var.get():
            steps = sq.build_reset() + steps          # shutter closed + every stage to its zero (arm via ma, never homed)
        self.job_steps = steps
        self.bar["maximum"] = len(steps)
        self.bar["value"] = 0
        bus, spec = self.app.bus, self.app.spectro.spec
        autoreconnect = self.autoreconnect_var.get()

        def preflight():
            if autoreconnect:
                spec.ensure_alive()                   # reopen the DLL session (escalates to recover() on failure)

        def spec_recover():
            return spec.recover()                     # revive a hung spectrometer between a failed read and its retry
        ppd = {p.addr: p.ppd for p in self.app.panels if p.ppd}

        def ask_user(msg):
            ev = threading.Event()
            box = {"ok": False}

            def show():
                prev = None
                try:
                    prev = self.prog_var.get()
                    self.prog_var.set("Waiting for operator: %s" % msg)
                    self._set_band_tone("paused")
                    self.app.refresh_status()
                    box["ok"] = messagebox.askokcancel("Sequence paused", msg)
                finally:
                    # the worker blocks on ev.wait() with no timeout: ev.set() must ALWAYS fire,
                    # otherwise a raise anywhere above deadlocks the sequence thread forever
                    if prev is not None:
                        self.prog_var.set(prev)
                    self._set_band_tone("running")
                    ev.set()
            self.events.put(("call", show))
            ev.wait()
            return box["ok"]

        def log(t):
            self.events.put(("log", t))

        def progress(i, n, step):
            self.events.put(("progress", (i, n, step.text if step else "Done")))

        def on_spectrum(rec, counts):
            self.events.put(("spectrum", (rec, counts)))

        self.load_history(outdir)
        self._mark_progress(0)
        self._set_band_tone("running")
        self.app.refresh_status()

        def job():
            runner = sq.Runner(bus, spec, outdir, log=log, ask_user=ask_user, abort=self.abort, progress=progress,
                               ppd=ppd, on_spectrum=on_spectrum, state_path=self.app.state_path,
                               preflight=preflight, spec_recover=spec_recover)
            # failures are reported here, not through the worker's generic error path: the panel
            # must always leave the "running" state (buttons, disconnect) whatever happened
            try:
                return runner.run(steps)
            except sq.SequenceAbort as e:
                self.events.put(("failed", ("Aborted", str(e), runner.shutter_open)))
            except Exception as e:
                self.events.put(("failed", ("Failed", "%s: %s" % (type(e).__name__, e), runner.shutter_open)))
            return None

        self.app.spectro.worker.submit("sequence", job, self._done)

    def abort_run(self):
        self.abort.set()
        self.prog_var.set("Aborting… (waiting for the current step to finish)")
        self._set_band_tone("aborted")
        self.app.refresh_status()

    def _done(self, manifest):
        if manifest is None:               # reported through the "failed" event
            return
        self._finish("Done: %d spectra saved" % len(manifest))
        self.clear()                       # a finished queue must not silently run again

    def _failed(self, title, msg, shutter_open):
        self._finish("%s: %s" % (title, msg))
        # the Runner closes the shutter on its way out; mirror what it reports (None = unknown)
        self.app.set_shutter({True: "open", False: "closed"}.get(shutter_open, "unknown"), "sequence %s" % title)
        extra = "" if shutter_open is False else "\n\nNote: shutter state unknown; check the status bar or confirm it is closed on the Motors & shutter page."
        if title == "Aborted":
            messagebox.showwarning("Sequence aborted", msg + extra)
        else:
            messagebox.showerror("Sequence failed", msg + "\n\nThe queue is kept; fix the issue and re-run." + extra)

    # ---- progress marks / history / preview ---------------------------------
    def _mark_progress(self, current):
        """Rewrite listbox rows: ✓ done, ▶ current, blank pending."""
        for i, st in enumerate(self.steps):
            mark = "✓" if i < current else ("▶" if i == current else " ")
            self.listbox.delete(i)
            self.listbox.insert(i, "%s %3d  %s" % (mark, i + 1, st.text))
            self.listbox.itemconfig(i, foreground=COLORS["text3"] if i < current else (COLORS["accent_pressed"] if i == current else COLORS["text"]))
        if 0 <= current < len(self.steps):
            self.listbox.see(current)

    def _derive_state(self, i):
        """Spec 2.4: the step before the one just announced has completed; mirror its effect in the
        GUI-side state (shutter / stage angle) without touching the hardware."""
        if 0 < i <= len(self.job_steps):
            st = self.job_steps[i - 1]
            if st.kind == "shutter":
                self.app.set_shutter("open" if st.params.get("open") else "closed", "sequence step %d" % i)
            elif st.kind == "stage":
                self.app.update_module(st.params["addr"], deg=st.params["deg"], source="sequence step %d" % i)

    def load_history(self, outdir=None):
        if outdir is None:
            outdir = os.path.join(self._data_root(), self.session_var.get().strip())
        recs = sq.Runner.load_manifest(outdir)
        self.done_tree.delete(*self.done_tree.get_children())
        self.done_tags = set()
        for r in recs:
            self._add_done(r)
        self.done_card.set_title("Completed acquisitions · %s · %d" % (os.path.basename(outdir), len(recs)))
        self._sync_empty_states()

    def _add_done(self, r):
        theta = r.get("theta")
        n = len(self.done_tree.get_children())
        self.done_tree.insert("", "end", values=(
            r.get("time", "")[11:], r.get("tag", ""), "" if theta is None else "%g" % theta, r.get("pol", ""),
            r.get("integration_ms", ""), "%.0f" % (100.0 * (r.get("peak") or 0) / bwtek.ADC_MAX),
            r.get("saturated_active", 0)), tags=("odd",) if n % 2 else ("even",))
        self.done_tags.add(r.get("tag"))
        self.done_tree.yview_moveto(1.0)
        self.done_empty.place_forget()

    def _show_spectrum(self, rec, counts):
        if not rec.get("preview_only"):
            self._add_done(rec)
            n = len(self.done_tree.get_children())
            self.done_card.set_title("Completed acquisitions · %s · %d" % (os.path.basename(self.session_var.get().strip()), n))
        peak = int(rec.get("peak") or 0)
        self.preview_var.set("%s   IT %s ms   peak %d (%.0f%%)" % (rec.get("tag"), rec.get("integration_ms"), peak, 100.0 * peak / bwtek.ADC_MAX))
        if self.canvas:
            if self._empty_text is not None:
                try:
                    self._empty_text.remove()
                except Exception:
                    pass
                self._empty_text = None
                for a in self._guides + [self.line]:
                    a.set_visible(True)
                from matplotlib import ticker as mticker      # matplotlib is optional: imported lazily
                self.ax.yaxis.set_major_locator(mticker.AutoLocator())
                self.fig.tight_layout(pad=1.2)                 # make room for the y tick labels
            self.line.set_ydata(counts)
            self.ax.set_ylim(0, max(1000, counts.max() * 1.1))
            self.canvas.draw_idle()

    def _last_session_it(self):
        """(session name, integration_ms) of the most recent VAR spectrum in any session, or None."""
        best = None
        root = self._data_root()
        try:
            for name in os.listdir(root):
                for r in sq.Runner.load_manifest(os.path.join(root, name)):
                    if r.get("kind") == "var" and (best is None or r.get("time", "") > best[2]):
                        best = (name, int(r["integration_ms"]), r.get("time", ""))
        except OSError:
            pass
        return best[:2] if best else None

    def _finish(self, text):
        self.running = False
        # only a deliberate (non-temporary) integration-time step counts as "chosen"
        if any((s.kind == "set_it" and not s.params.get("save")) or s.kind in ("auto_it", "apply_min_it") for s in self.job_steps):
            self.app.spectro.it_chosen = True
        self.app.sequence_running = False
        self.btn_run.config(state="normal")
        self.btn_abort.config(state="disabled")
        self.prog_var.set(text)
        self._set_band_tone(self._tone_for(text))
        self.app._log_line("SEQ " + text)
        # the spectrometer page's IT box should reflect what the sequence left behind
        if self.app.spectro.spec and self.app.spectro.spec.integration_ms:
            self.app.spectro.it_var.set(str(self.app.spectro.spec.integration_ms))
        self.app.refresh_status()

    def _poll(self):
        try:
            while True:
                try:
                    kind, payload = self.events.get_nowait()
                except queue.Empty:
                    break
                try:
                    if kind == "log":
                        self.app._log_line("SEQ " + payload)
                    elif kind == "progress":
                        i, n, text = payload
                        self.bar["value"] = i
                        self._mark_progress(i)
                        self._derive_state(i)
                        # the Runner's final progress(n, n) travels through self.events while _done/_failed
                        # arrive through the spectrometer worker queue: whichever is polled first wins, so
                        # a late progress event must not overwrite the finished/failed text
                        if self.running:
                            self.prog_var.set("%d/%d  %s" % (i, n, text))
                            self._set_band_tone("running")
                    elif kind == "spectrum":
                        self._show_spectrum(*payload)
                    elif kind == "call":
                        payload()
                    elif kind == "failed":
                        self._failed(*payload)
                except Exception as e:
                    # one bad event must never kill the pump (it drives progress, previews and _finish)
                    try:
                        self.app._log_line("SEQ poll handler error (%s): %s" % (kind, e))
                    except Exception:
                        pass
        finally:
            self.after(100, self._poll)
