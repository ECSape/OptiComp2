# -*- coding: utf-8 -*-
"""Tk page: compute and plot variable-angle reflectance from two session directories.

Layout: page header (Compute / Export / Save figure), an input card and an option card, a warning banner bound
to note_var, and the result card with the two figures. The computation itself is unchanged.
"""
import os
import sys
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
from analysis import standards as sd
from analysis import var
import ui_theme
from ui_theme import SPACE, COLORS, Card, Banner, form_row, bind_enter, tooltip

DATA_ROOT = os.path.join(HERE, "..", "data")      # default; the App may point elsewhere (--demo)
PAGE_PAD = (SPACE["xl"], SPACE["md"], SPACE["xl"], SPACE["md"])
SUBTITLE = "Reflectance R(λ, θ) from a sample session, a reference, and a standard."
EMPTY_SESSIONS = "No sessions yet: run a sequence on the Measurement page and session directories under data/ appear here."

TIPS = {
    "compute": "Read both session manifests and compute R(λ, θ) = sample/reference × standard reflectance for S and P (optional DB and back-reflection correction).",
    "export": "Export two CSVs by polarisation (<name>_S.csv / <name>_P.csv): first column wavelength, the rest R at each θ.",
    "save_fig": "Save the current figure as PNG (150 dpi).",
    "refresh": "Rescan data/ for session directories that contain a manifest.json.",
    "std": "The known reflectance of the reference: a silicon wafer uses the Fresnel table (with index dispersion), or enter a constant (white board ~ 0.99).",
    "db": "Use the double-beam (DB) ratio to correct the systematic difference from swapping the integrating-sphere port; both sessions must contain DB steps.",
    "back": "For a glass substrate, subtract the back reflection with a BK7 incoherent-slab model to get the single-surface reflectance.",
    "theory": "Overlay the silicon Fresnel theory curve on the R(θ) plot to check the instrument with a silicon wafer.",
    "lam": "The R(θ) subplot takes the reflectance at this wavelength (nm).",
}


class AnalysisPanel(ttk.Frame):
    def __init__(self, master, app):
        ttk.Frame.__init__(self, master, style="Page.TFrame", padding=PAGE_PAD)
        self.app = app
        self.results = {}
        self._si = None
        self._empty_text = None
        self._build()
        self.refresh_sessions()

    def _data_root(self):
        return getattr(self.app, "data_root", None) or DATA_ROOT

    def _build(self):
        px = self.app.theme.px
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)
        self.header = ui_theme.PageHeader(self, "Analysis", SUBTITLE,
                                          actions=[("Compute S and P", self.compute, "Primary.TButton"), ("Export CSV…", self.export), ("Save figure…", self.save_fig)])
        self.header.grid(row=0, column=0, sticky="ew", pady=(0, SPACE["lg"]))
        self.btn_compute = self.header.buttons["Compute S and P"]
        self.btn_export = self.header.buttons["Export CSV…"]
        self.btn_save_fig = self.header.buttons["Save figure…"]
        tooltip(self.btn_compute, TIPS["compute"])
        tooltip(self.btn_export, TIPS["export"])
        tooltip(self.btn_save_fig, TIPS["save_fig"])

        top = ttk.Frame(self, style="Page.TFrame")
        top.grid(row=1, column=0, sticky="ew")
        top.columnconfigure(0, weight=3)
        top.columnconfigure(1, weight=2)
        # ---- input card
        inp = Card(top, title="Input")
        inp.grid(row=0, column=0, sticky="nsew", padx=(0, SPACE["md"]))
        self.input_card = inp
        f = inp.body
        self.sample_cb = ttk.Combobox(f, width=14, state="readonly")
        b_refresh = ttk.Button(f, text="Refresh", command=self.refresh_sessions)
        tooltip(b_refresh, TIPS["refresh"])
        form_row(f, 0, "Sample session", self.sample_cb, b_refresh, label_width=10)
        self.ref_cb = ttk.Combobox(f, width=14, state="readonly")
        form_row(f, 1, "Reference session", self.ref_cb, label_width=10)
        for cb in (self.sample_cb, self.ref_cb):
            cb.grid_configure(sticky="ew")               # same right edge as the standard row below
        std_row = ttk.Frame(f, style="CardBody.TFrame")
        self.std_cb = ttk.Combobox(std_row, width=18, state="readonly", values=["Silicon (Fresnel table)", "Constant (enter at right)"])
        self.std_cb.current(0)
        tooltip(self.std_cb, TIPS["std"])
        self.std_cb.pack(side="left")
        ttk.Label(std_row, text="Constant", style="FormLabel.TLabel").pack(side="left", padx=(SPACE["md"], SPACE["sm"]))
        self.const_var = tk.StringVar(value="0.99")
        e_const = ttk.Entry(std_row, textvariable=self.const_var, width=6, justify="right")
        bind_enter(e_const, self.compute)
        e_const.pack(side="left")
        form_row(f, 2, "Reference standard", std_row, label_width=10)
        self.sessions_hint = ttk.Label(f, text=EMPTY_SESSIONS, style="Card.Caption.TLabel", wraplength=px(380), justify="left")
        self.sessions_hint.grid(row=3, column=0, columnspan=4, sticky="w", pady=(SPACE["sm"], 0))
        f.columnconfigure(1, weight=0)                 # the Refresh column (2) takes the slack, fields keep their width
        f.columnconfigure(2, weight=1)
        # ---- options card
        opt = Card(top, title="Options")
        opt.grid(row=0, column=1, sticky="nsew")
        self.options_card = opt
        o = opt.body
        o.columnconfigure(0, weight=1)
        self.db_var = tk.BooleanVar(value=True)
        c1 = ttk.Checkbutton(o, text="Double-beam (DB) correction", variable=self.db_var, style="Card.TCheckbutton")
        c1.grid(row=0, column=0, columnspan=3, sticky="w", pady=2)
        self.back_var = tk.BooleanVar(value=False)
        c2 = ttk.Checkbutton(o, text="Subtract glass back reflection (BK7)", variable=self.back_var, style="Card.TCheckbutton")
        c2.grid(row=1, column=0, columnspan=3, sticky="w", pady=2)
        self.theory_var = tk.BooleanVar(value=True)
        c3 = ttk.Checkbutton(o, text="Overlay silicon theory (wafers)", variable=self.theory_var, style="Card.TCheckbutton")
        c3.grid(row=2, column=0, columnspan=3, sticky="w", pady=2)
        tooltip(c1, TIPS["db"])
        tooltip(c2, TIPS["back"])
        tooltip(c3, TIPS["theory"])
        lam_row = ttk.Frame(o, style="CardBody.TFrame")
        lam_row.grid(row=3, column=0, sticky="w", pady=(SPACE["sm"], 0))
        ttk.Label(lam_row, text="R(θ) wavelength", style="FormLabel.TLabel").pack(side="left", padx=(0, SPACE["sm"]))
        self.lam_var = tk.StringVar(value="600")
        e_lam = ttk.Entry(lam_row, textvariable=self.lam_var, width=6, justify="right")
        bind_enter(e_lam, self.compute)
        e_lam.pack(side="left")
        tooltip(e_lam, TIPS["lam"])
        ttk.Label(lam_row, text="nm", style="Card.Caption.TLabel").pack(side="left", padx=(SPACE["xs"], 0))
        # ---- notes banner (note_var is the data source; the banner mirrors it)
        self.note_var = tk.StringVar(value="")
        self.note_banner = Banner(self, tone="warning", closable=True)
        self.note_banner.grid(row=2, column=0, sticky="ew", pady=(SPACE["md"], 0))
        self.note_banner.hide()
        self.note_var.trace_add("write", lambda *_a: self._sync_note())
        # ---- result card
        res = Card(self, title="Results")
        res.grid(row=3, column=0, sticky="nsew", pady=(SPACE["md"], 0))
        self.result_card = res
        body = res.body
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        try:
            import matplotlib
            matplotlib.use("TkAgg")
            from matplotlib.figure import Figure
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        except Exception as e:
            ttk.Label(body, text="matplotlib unavailable: %s" % e, style="Card.TLabel").grid(row=0, column=0, sticky="w")
            self.canvas = None
            return
        self.fig = Figure(figsize=(9, 3.6), dpi=100)
        self.fig.patch.set_edgecolor(COLORS["card"])
        self.ax1 = self.fig.add_subplot(121)
        self.ax2 = self.fig.add_subplot(122)
        for ax in (self.ax1, self.ax2):
            ui_theme.mpl_style_axes(ax)
            ax.set_visible(False)
        self._empty_text = self.fig.text(0.5, 0.5, "Select a sample and reference session, then click Compute S and P.", ha="center", va="center",
                                         color=COLORS["text3"], fontsize=10)
        self.fig.tight_layout(pad=1.2)
        self.canvas = FigureCanvasTkAgg(self.fig, master=body)
        w = self.canvas.get_tk_widget()
        w.configure(width=px(640), height=px(220), highlightthickness=0)
        w.grid(row=0, column=0, sticky="nsew")
        ui_theme.mpl_bind_resize(self.canvas, self.fig)

    # ---- presentation helpers ------------------------------------------------
    def _sync_note(self):
        text = self.note_var.get().strip()
        if text and text != "No warnings":
            self.note_banner.show(text, "warning")
        else:
            self.note_banner.hide()

    # ---- data ----------------------------------------------------------------
    def refresh_sessions(self):
        names = []
        root = self._data_root()
        if os.path.isdir(root):
            names = sorted(d for d in os.listdir(root) if os.path.isfile(os.path.join(root, d, "manifest.json")))
        for cb in (self.sample_cb, self.ref_cb):
            cur = cb.get()
            cb["values"] = names
            if cur in names:
                cb.set(cur)
            elif names:
                cb.current(len(names) - 1)
            else:
                cb.set("")
        if names:
            self.sessions_hint.grid_remove()
        else:
            self.sessions_hint.grid()

    def _standard(self):
        if self.std_cb.current() == 0:
            if self._si is None:
                self._si = sd.SiliconStandard()
            return self._si
        return sd.ConstantStandard(float(self.const_var.get()))

    def compute(self):
        if not self.sample_cb.get() or not self.ref_cb.get():
            messagebox.showwarning("Missing input", "Select a sample session and a reference session")
            return
        try:
            sample = var.Session(os.path.join(self._data_root(), self.sample_cb.get()))
            reference = var.Session(os.path.join(self._data_root(), self.ref_cb.get()))
            standard = self._standard()
            lam = float(self.lam_var.get())
        except Exception as e:
            messagebox.showerror("Read failed", str(e))
            return
        self.results = {}
        notes = []
        for pol in ("S", "P"):
            try:
                res = var.compute_reflectance(sample, reference, standard, pol, use_db=self.db_var.get())
            except var.AnalysisError as e:
                notes.append("%s: %s" % (pol, e))
                continue
            if self.back_var.get():
                n = sd.bk7_index(res.wl)
                for i, th in enumerate(res.thetas):
                    res.R[i] = sd.slab_to_single_surface(res.R[i], sd.fresnel(n, th, pol))
                res.notes.append("back-surface (BK7) correction applied")
            self.results[pol] = res
            notes += ["%s: %s" % (pol, n) for n in res.notes]
        self.note_var.set("\n".join(notes) if notes else "No warnings")
        if not self.results:
            messagebox.showerror("No results", "\n".join(notes))
            return
        self._plot(lam)
        self.app._log_line("ANALYSIS %s / %s: %s" % (self.sample_cb.get(), self.ref_cb.get(), ", ".join(sorted(self.results))))

    def _plot(self, lam):
        if not self.canvas:
            return
        import matplotlib.cm as cm
        if self._empty_text is not None:
            try:
                self._empty_text.remove()
            except Exception:
                pass
            self._empty_text = None
        self.ax1.clear()
        self.ax2.clear()
        for ax in (self.ax1, self.ax2):
            ax.set_visible(True)
            ui_theme.mpl_style_axes(ax)
        si_theory = self._si if (self.theory_var.get() and self._si is not None) else (sd.SiliconStandard() if self.theory_var.get() else None)
        for pol, ls in (("S", "-"), ("P", "--")):
            res = self.results.get(pol)
            if res is None:
                continue
            ok = res.valid
            colors = cm.viridis(np.linspace(0, 1, len(res.thetas)))
            for i, th in enumerate(res.thetas):
                self.ax1.plot(res.wl[ok], res.R[i][ok], ls, color=colors[i], lw=0.8,
                              label="%s %g°" % (pol, th) if i in (0, len(res.thetas) - 1) else None)
            self.ax2.plot(res.thetas, res.at_wavelength(lam), "o" + ls, ms=4, label="%s measured @%g nm" % (pol, lam))
            if si_theory is not None:
                th_grid = np.linspace(min(res.thetas), max(res.thetas), 81)
                self.ax2.plot(th_grid, [si_theory.reflectance([lam], t, pol)[0] for t in th_grid], ls, color=COLORS["text"], lw=0.8,
                              label="Si theory %s" % pol)
        self.ax1.set_xlabel("wavelength (nm)")
        self.ax1.set_ylabel("R")
        self.ax1.set_ylim(0, 1.1)
        self.ax1.legend(fontsize=7)
        self.ax2.set_xlabel("θ (deg)")
        self.ax2.set_ylabel("R @ %g nm" % lam)
        self.ax2.set_ylim(0, 1.1)
        self.ax2.legend(fontsize=7)
        self.ax1.set_title("%s / %s" % (self.sample_cb.get(), self.ref_cb.get()), fontsize=9)
        self.fig.tight_layout(pad=1.2)
        self.canvas.draw_idle()

    def export(self):
        if not self.results:
            messagebox.showwarning("No results", "Click Compute first")
            return
        base = filedialog.asksaveasfilename(defaultextension=".csv", initialfile="R_%s.csv" % self.sample_cb.get())
        if not base:
            return
        root, ext = os.path.splitext(base)
        for pol, res in self.results.items():
            res.save_csv("%s_%s%s" % (root, pol, ext))
        self.app._log_line("ANALYSIS exported %s_{S,P}%s" % (root, ext))

    def save_fig(self):
        if not self.canvas:
            return
        path = filedialog.asksaveasfilename(defaultextension=".png", initialfile="R_%s.png" % self.sample_cb.get())
        if path:
            self.fig.savefig(path, dpi=150)
            self.app._log_line("ANALYSIS figure saved %s" % path)
