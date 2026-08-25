# -*- coding: utf-8 -*-
"""Tk panel for manual spectrometer control (used by manual_gui.py).

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

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from hw import bwtek


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
        ttk.Frame.__init__(self, master, padding=6)
        self.app = app                       # provides _log_line(text)
        self.spec = None
        self.results = queue.Queue()
        self.worker = SpectroWorker(self.results)
        self.worker.start()
        self.wl = bwtek.wavelengths()
        self.last = None
        self._build()
        self.after(100, self._poll)

    def _build(self):
        ctl = ttk.Frame(self)
        ctl.pack(fill="x")
        self.btn_open = ttk.Button(ctl, text="初始化光谱仪", command=self.open_dev)
        self.btn_open.pack(side="left")
        self.btn_close = ttk.Button(ctl, text="关闭", command=self.close_dev, state="disabled")
        self.btn_close.pack(side="left", padx=4)
        self.state_var = tk.StringVar(value="未初始化")
        ttk.Label(ctl, textvariable=self.state_var, foreground="#a00").pack(side="left", padx=8)

        ttk.Label(ctl, text="积分时间 ms").pack(side="left", padx=(16, 2))
        self.it_var = tk.StringVar(value="100")
        ttk.Entry(ctl, textvariable=self.it_var, width=8).pack(side="left")
        ttk.Button(ctl, text="设置", command=self.set_it).pack(side="left", padx=4)
        ttk.Label(ctl, text="平均").pack(side="left", padx=(12, 2))
        self.avg_var = tk.StringVar(value="1")
        ttk.Spinbox(ctl, from_=1, to=50, textvariable=self.avg_var, width=4).pack(side="left")
        self.smooth_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctl, text="平滑 (3,5) 同原程序", variable=self.smooth_var).pack(side="left", padx=8)

        ctl2 = ttk.Frame(self)
        ctl2.pack(fill="x", pady=4)
        ttk.Button(ctl2, text="单次读取", command=self.read_once).pack(side="left")
        self.btn_live = ttk.Button(ctl2, text="连续读取", command=self.toggle_live)
        self.btn_live.pack(side="left", padx=4)
        ttk.Button(ctl2, text="保存光谱 CSV…", command=self.save_csv).pack(side="left", padx=4)
        ttk.Button(ctl2, text="自动定标积分时间 (峰值→85%)", command=self.auto_it).pack(side="left", padx=4)
        self.auto_y = tk.BooleanVar(value=True)
        ttk.Checkbutton(ctl2, text="Y 自动缩放", variable=self.auto_y).pack(side="left", padx=8)
        self.stats_var = tk.StringVar(value="")
        ttk.Label(ctl2, textvariable=self.stats_var, font=("TkDefaultFont", 10, "bold")).pack(side="left", padx=12)

        try:
            import matplotlib
            matplotlib.use("TkAgg")
            from matplotlib.figure import Figure
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        except Exception as e:
            ttk.Label(self, text="matplotlib 不可用: %s" % e).pack()
            self.canvas = None
            return
        self.fig = Figure(figsize=(8, 3.6), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_xlabel("wavelength (nm)")
        self.ax.set_ylabel("counts")
        self.ax.axvspan(self.wl[bwtek.ACTIVE_FIRST], self.wl[bwtek.ACTIVE_LAST], color="#e8f4e8", zorder=0)
        self.ax.axhline(bwtek.ADC_MAX, color="r", lw=0.8, ls="--")
        (self.line,) = self.ax.plot(self.wl, np.zeros_like(self.wl), lw=0.8)
        self.ax.set_xlim(self.wl[0], self.wl[-1])
        self.ax.set_ylim(0, bwtek.ADC_MAX * 1.02)
        self.fig.tight_layout()
        self.canvas = FigureCanvasTkAgg(self.fig, master=self)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

    # ---- device ------------------------------------------------------------
    def _log(self, text):
        self.results.put(("ok", "log", None, lambda _v, t=text: self.app._log_line("SPEC " + t)))

    def open_dev(self):
        def job():
            spec = bwtek.BWTek(log=self._log)
            n = spec.open()
            spec.set_integration_time(int(self.it_var.get()))
            return spec, n
        self.state_var.set("初始化中…")
        self.worker.submit("spec open", job, self._opened)

    def _opened(self, res):
        self.spec, n = res
        self.state_var.set("已连接 (%d 台), IT %d ms" % (n, self.spec.integration_ms))
        self.btn_open.config(state="disabled")
        self.btn_close.config(state="normal")

    def close_dev(self):
        if getattr(self.app, "sequence_running", False):
            messagebox.showwarning("序列运行中", "请先中止序列")
            return
        self.worker.live.clear()
        if self.spec:
            s = self.spec
            self.spec = None
            self.worker.submit("spec close", s.close, lambda _: self.state_var.set("已关闭"))
        self.btn_open.config(state="normal")
        self.btn_close.config(state="disabled")
        self.btn_live.config(text="连续读取")

    def _need(self):
        if self.spec is None:
            messagebox.showwarning("未初始化", "请先初始化光谱仪")
            return False
        if getattr(self.app, "sequence_running", False):
            messagebox.showwarning("序列运行中", "序列执行期间禁止手动读谱")
            return False
        return True

    def set_it(self):
        if not self._need():
            return
        try:
            ms = int(self.it_var.get())
        except ValueError:
            messagebox.showerror("输入错误", "积分时间需为整数毫秒")
            return
        self.worker.submit("spec set IT", lambda: self.spec.set_integration_time(ms),
                           lambda r: self.state_var.set("已连接, IT %d ms" % r))

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
            self.btn_live.config(text="连续读取")
            return
        if not self._need():
            return
        self.worker.live.set()
        self.btn_live.config(text="停止连续")
        self._live_step()

    def _live_step(self):
        if not self.worker.live.is_set() or self.spec is None:
            return
        avg, sm = self._read_args()
        self.worker.submit("spec live", lambda: self.spec.read(avg, *sm), self._show_and_continue)

    def _show_and_continue(self, counts):
        self._show(counts)
        if self.worker.live.is_set():
            self.after(50, self._live_step)

    def _show(self, counts):
        self.last = counts
        st = bwtek.spectrum_stats(counts)
        txt = "峰值 %d @ %.1f nm   饱和像素 %d (有效区 %d)   有效区均值 %.0f" % (
            st["max"], self.wl[st["argmax"]], st["saturated"], st["saturated_active"], st["mean_active"])
        self.stats_var.set(txt)
        if self.canvas:
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
                it = bwtek.next_integration_time(it, peak, base)
                spec.set_integration_time(it)
            counts = spec.read(1, *sm)
            return it, counts

        self.state_var.set("自动定标中…")
        self.worker.submit("spec auto-IT", job, self._auto_it_done)

    def _auto_it_done(self, res):
        it, counts = res
        self.it_var.set(str(it))
        self.state_var.set("已连接, IT %d ms (自动定标)" % it)
        self._show(counts)

    def save_csv(self):
        if self.last is None:
            messagebox.showwarning("无数据", "先读取一次光谱")
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
                        self.state_var.set("初始化失败")
                    if label == "spec live":
                        self.worker.live.clear()
                        self.btn_live.config(text="连续读取")
                    if label == "sequence":
                        self.app.sequence._finish("失败/中止: %s" % value)
                        messagebox.showerror("序列失败", "%s\n\n队列已保留（已完成的步骤带 ✓），修正后可重新运行整个队列。" % value)
                    else:
                        messagebox.showerror("光谱仪错误", "%s\n%s" % (label, value))
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def shutdown(self):
        self.worker.live.clear()
        if self.spec:
            try:
                self.spec.close()
            except Exception:
                pass
            self.spec = None
