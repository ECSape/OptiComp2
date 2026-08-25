# -*- coding: utf-8 -*-
"""Tk tab: compute and plot variable-angle reflectance from two session directories."""
import os
import sys
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from analysis import standards as sd
from analysis import var

DATA_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")


class AnalysisPanel(ttk.Frame):
    def __init__(self, master, app):
        ttk.Frame.__init__(self, master, padding=6)
        self.app = app
        self.results = {}
        self._si = None
        self._build()
        self.refresh_sessions()

    def _build(self):
        f = ttk.LabelFrame(self, text="输入", padding=6)
        f.pack(fill="x")
        ttk.Label(f, text="样品会话").grid(row=0, column=0, sticky="e")
        self.sample_cb = ttk.Combobox(f, width=24, state="readonly")
        self.sample_cb.grid(row=0, column=1, sticky="w")
        ttk.Label(f, text="参考会话").grid(row=0, column=2, sticky="e", padx=(12, 0))
        self.ref_cb = ttk.Combobox(f, width=24, state="readonly")
        self.ref_cb.grid(row=0, column=3, sticky="w")
        ttk.Button(f, text="刷新", command=self.refresh_sessions).grid(row=0, column=4, padx=6)

        ttk.Label(f, text="参考标准件").grid(row=1, column=0, sticky="e")
        self.std_cb = ttk.Combobox(f, width=24, state="readonly", values=["硅片 (Fresnel 表)", "常数 (右侧输入)"])
        self.std_cb.current(0)
        self.std_cb.grid(row=1, column=1, sticky="w")
        self.const_var = tk.StringVar(value="0.99")
        ttk.Entry(f, textvariable=self.const_var, width=6).grid(row=1, column=2, sticky="w", padx=(12, 0))
        self.db_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="双光束 (DB) 替代修正", variable=self.db_var).grid(row=1, column=3, sticky="w")
        self.back_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="扣除玻璃背面反射 (BK7, 非相干平板)", variable=self.back_var).grid(row=2, column=3, sticky="w")
        self.theory_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="叠加硅理论曲线 (样品为硅片时)", variable=self.theory_var).grid(row=2, column=1, sticky="w")
        ttk.Label(f, text="R(θ) 取波长 nm").grid(row=2, column=2, sticky="e", padx=(12, 0))
        self.lam_var = tk.StringVar(value="600")
        ttk.Entry(f, textvariable=self.lam_var, width=6).grid(row=2, column=4, sticky="w")
        ttk.Button(f, text="计算 S 与 P", command=self.compute).grid(row=3, column=0, columnspan=2, sticky="ew", pady=4)
        ttk.Button(f, text="导出 CSV…", command=self.export).grid(row=3, column=2, sticky="ew", pady=4)
        ttk.Button(f, text="保存图…", command=self.save_fig).grid(row=3, column=3, sticky="w", pady=4)
        self.note_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.note_var, foreground="#a50", wraplength=900, justify="left").pack(anchor="w")

        try:
            import matplotlib
            matplotlib.use("TkAgg")
            from matplotlib.figure import Figure
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        except Exception as e:
            ttk.Label(self, text="matplotlib 不可用: %s" % e).pack()
            self.canvas = None
            return
        self.fig = Figure(figsize=(9, 4.2), dpi=90)
        self.ax1 = self.fig.add_subplot(121)
        self.ax2 = self.fig.add_subplot(122)
        self.fig.tight_layout()
        self.canvas = FigureCanvasTkAgg(self.fig, master=self)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

    def refresh_sessions(self):
        names = []
        if os.path.isdir(DATA_ROOT):
            names = sorted(d for d in os.listdir(DATA_ROOT) if os.path.isfile(os.path.join(DATA_ROOT, d, "manifest.json")))
        for cb in (self.sample_cb, self.ref_cb):
            cur = cb.get()
            cb["values"] = names
            if cur in names:
                cb.set(cur)
            elif names:
                cb.current(len(names) - 1)

    def _standard(self):
        if self.std_cb.current() == 0:
            if self._si is None:
                self._si = sd.SiliconStandard()
            return self._si
        return sd.ConstantStandard(float(self.const_var.get()))

    def compute(self):
        if not self.sample_cb.get() or not self.ref_cb.get():
            messagebox.showwarning("缺输入", "请选择样品会话与参考会话")
            return
        try:
            sample = var.Session(os.path.join(DATA_ROOT, self.sample_cb.get()))
            reference = var.Session(os.path.join(DATA_ROOT, self.ref_cb.get()))
            standard = self._standard()
            lam = float(self.lam_var.get())
        except Exception as e:
            messagebox.showerror("读取失败", str(e))
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
        self.note_var.set("\n".join(notes) if notes else "无警告")
        if not self.results:
            messagebox.showerror("无结果", "\n".join(notes))
            return
        self._plot(lam)

    def _plot(self, lam):
        if not self.canvas:
            return
        import matplotlib.cm as cm
        self.ax1.clear()
        self.ax2.clear()
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
            self.ax2.plot(res.thetas, res.at_wavelength(lam), "o" + ls, label="%s 实测 @%g nm" % (pol, lam))
            if si_theory is not None:
                th_grid = np.linspace(min(res.thetas), max(res.thetas), 81)
                self.ax2.plot(th_grid, [si_theory.reflectance([lam], t, pol)[0] for t in th_grid], ls, color="k", lw=0.8,
                              label="Si 理论 %s" % pol)
        self.ax1.set_xlabel("wavelength (nm)")
        self.ax1.set_ylabel("R")
        self.ax1.set_ylim(0, 1.1)
        self.ax1.legend(fontsize=7)
        self.ax2.set_xlabel("θ (deg)")
        self.ax2.set_ylabel("R @ %g nm" % lam)
        self.ax2.set_ylim(0, 1.1)
        self.ax2.legend(fontsize=7)
        self.ax1.set_title("%s / %s" % (self.sample_cb.get(), self.ref_cb.get()), fontsize=9)
        self.fig.tight_layout()
        self.canvas.draw_idle()

    def export(self):
        if not self.results:
            messagebox.showwarning("无结果", "先点『计算』")
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
