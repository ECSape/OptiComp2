# -*- coding: utf-8 -*-
"""Tk tab that builds and runs measurement sequences (uses sequence.py)."""
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import sequence as sq
from hw import bwtek
from hw import config as cfg

DATA_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")


class SequencePanel(ttk.Frame):
    def __init__(self, master, app):
        ttk.Frame.__init__(self, master, padding=6)
        self.app = app
        self.steps = []
        self.events = queue.Queue()
        self.abort = threading.Event()
        self.running = False
        self._build()
        self.after(100, self._poll)

    # ---- layout ------------------------------------------------------------
    def _build(self):
        left = ttk.LabelFrame(self, text="步骤队列 (0 步, 0 次采集)", padding=6)
        left.grid(row=0, column=0, sticky="nsew", padx=4)
        self.queue_frame = left
        self.listbox = tk.Listbox(left, width=52, height=22, font=("Consolas", 9))
        self.listbox.pack(fill="both", expand=True)
        b = ttk.Frame(left)
        b.pack(fill="x", pady=4)
        ttk.Button(b, text="删除选中", command=self.remove_selected).pack(side="left")
        ttk.Button(b, text="清空", command=self.clear).pack(side="left", padx=4)

        right = ttk.Frame(self)
        right.grid(row=0, column=1, sticky="nsew", padx=4)
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        # ---- bottom row: completed acquisitions (left) + live preview (right)
        done = ttk.LabelFrame(self, text="已完成的采集 (本会话目录)", padding=6)
        done.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        cols = ("time", "tag", "theta", "pol", "it", "peak", "sat")
        self.done_tree = ttk.Treeview(done, columns=cols, show="headings", height=8)
        for c, w, t in zip(cols, (70, 130, 45, 35, 60, 60, 40), ("时间", "标签", "θ°", "偏振", "IT ms", "峰值%", "饱和")):
            self.done_tree.heading(c, text=t)
            self.done_tree.column(c, width=w, anchor="center")
        sb = ttk.Scrollbar(done, orient="vertical", command=self.done_tree.yview)
        self.done_tree.configure(yscrollcommand=sb.set)
        self.done_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.done_frame = done
        self.done_tags = set()

        prev = ttk.LabelFrame(self, text="实时预览", padding=4)
        prev.grid(row=1, column=1, sticky="nsew", padx=4, pady=4)
        self.preview_var = tk.StringVar(value="(尚无数据)")
        ttk.Label(prev, textvariable=self.preview_var, font=("TkDefaultFont", 9, "bold")).pack(anchor="w")
        self.canvas = None
        try:
            import matplotlib
            matplotlib.use("TkAgg")
            from matplotlib.figure import Figure
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
            self.wl = bwtek.wavelengths()
            self.fig = Figure(figsize=(5, 2.6), dpi=90)
            self.ax = self.fig.add_subplot(111)
            self.ax.set_xlabel("nm", fontsize=8)
            self.ax.tick_params(labelsize=7)
            self.ax.axvspan(self.wl[bwtek.ACTIVE_FIRST], self.wl[bwtek.ACTIVE_LAST], color="#e8f4e8", zorder=0)
            self.ax.axhline(bwtek.ADC_MAX, color="r", lw=0.8, ls="--")
            (self.line,) = self.ax.plot(self.wl, [0] * len(self.wl), lw=0.8)
            self.ax.set_xlim(self.wl[0], self.wl[-1])
            self.ax.set_ylim(0, bwtek.ADC_MAX * 1.02)
            self.fig.tight_layout()
            self.canvas = FigureCanvasTkAgg(self.fig, master=prev)
            self.canvas.get_tk_widget().pack(fill="both", expand=True)
        except Exception as e:
            ttk.Label(prev, text="matplotlib 不可用: %s" % e).pack()

        f = ttk.LabelFrame(right, text="通用参数", padding=6)
        f.pack(fill="x", pady=2)
        ttk.Label(f, text="会话/样品名").grid(row=0, column=0, sticky="e")
        self.session_var = tk.StringVar(value=time.strftime("session_%Y%m%d_%H%M"))
        ttk.Entry(f, textvariable=self.session_var, width=22).grid(row=0, column=1, columnspan=2, sticky="w")
        ttk.Button(f, text="载入已有记录", command=self.load_history).grid(row=0, column=3, sticky="w")
        ttk.Label(f, text="标签前缀").grid(row=1, column=0, sticky="e")
        self.prefix_var = tk.StringVar(value="sample")
        ttk.Entry(f, textvariable=self.prefix_var, width=12).grid(row=1, column=1, sticky="w")
        ttk.Label(f, text="平均次数").grid(row=1, column=2, sticky="e")
        self.avg_var = tk.StringVar(value="3")
        ttk.Spinbox(f, from_=1, to=50, textvariable=self.avg_var, width=4).grid(row=1, column=3, sticky="w")
        ttk.Label(f, text="偏振").grid(row=2, column=0, sticky="e")
        self.pol_var = tk.StringVar(value="S+P")
        ttk.Combobox(f, textvariable=self.pol_var, values=["S", "P", "S+P"], state="readonly", width=6).grid(row=2, column=1, sticky="w")

        f = ttk.LabelFrame(right, text="添加步骤", padding=6)
        f.pack(fill="x", pady=2)
        ttk.Button(f, text="① 参考定标: 80°/S 自动积分时间", command=self.add_reference).grid(row=0, column=0, columnspan=4, sticky="ew", pady=1)
        ttk.Button(f, text="② 暗底 (快门关, 当前积分时间)", command=self.add_dark).grid(row=1, column=0, columnspan=4, sticky="ew", pady=1)
        ttk.Label(f, text="θ °").grid(row=2, column=0, sticky="e")
        self.theta_var = tk.StringVar(value="45")
        ttk.Entry(f, textvariable=self.theta_var, width=6).grid(row=2, column=1, sticky="w")
        ttk.Button(f, text="③ 单角度测量", command=self.add_single).grid(row=2, column=2, columnspan=2, sticky="ew", pady=1)
        ttk.Label(f, text="起 / 止 / 步").grid(row=3, column=0, sticky="e")
        sc = ttk.Frame(f)
        sc.grid(row=3, column=1, sticky="w")
        self.start_var, self.stop_var, self.step_var = tk.StringVar(value="8"), tk.StringVar(value="80"), tk.StringVar(value="4")
        for v in (self.start_var, self.stop_var, self.step_var):
            ttk.Entry(sc, textvariable=v, width=4).pack(side="left")
        ttk.Button(f, text="④ 角度扫描", command=self.add_scan).grid(row=3, column=2, columnspan=2, sticky="ew", pady=1)
        ttk.Button(f, text="⑤ 双光束 DB (含换端口盖暂停)", command=self.add_db).grid(row=4, column=0, columnspan=4, sticky="ew", pady=1)
        ttk.Label(f, text="积分时间 ms").grid(row=5, column=0, sticky="e")
        self.it_var = tk.StringVar(value="1000")
        ttk.Entry(f, textvariable=self.it_var, width=8).grid(row=5, column=1, sticky="w")
        ttk.Button(f, text="设定积分时间", command=self.add_set_it).grid(row=5, column=2, columnspan=2, sticky="ew", pady=1)
        ttk.Label(f, text="暂停提示").grid(row=6, column=0, sticky="e")
        self.pause_var = tk.StringVar(value="请更换样品，然后点确定")
        ttk.Entry(f, textvariable=self.pause_var, width=24).grid(row=6, column=1, columnspan=2, sticky="w")
        ttk.Button(f, text="暂停", command=self.add_pause).grid(row=6, column=3, sticky="ew", pady=1)
        ttk.Button(f, text="快门关", command=lambda: self._add([sq.shutter(False)], dedupe=False)).grid(row=7, column=0, columnspan=2, sticky="ew", pady=1)
        ttk.Button(f, text="快门开", command=lambda: self._add([sq.shutter(True)], dedupe=False)).grid(row=7, column=2, columnspan=2, sticky="ew", pady=1)

        f = ttk.LabelFrame(right, text="运行", padding=6)
        f.pack(fill="x", pady=2)
        self.btn_run = ttk.Button(f, text="▶ 运行序列", command=self.run)
        self.btn_run.pack(side="left")
        self.btn_abort = ttk.Button(f, text="■ 中止", command=self.abort_run, state="disabled")
        self.btn_abort.pack(side="left", padx=4)
        self.prog_var = tk.StringVar(value="空闲")
        ttk.Label(f, textvariable=self.prog_var).pack(side="left", padx=8)
        self.bar = ttk.Progressbar(right, mode="determinate")
        self.bar.pack(fill="x", pady=2)
        ttk.Label(right, text="几何: S=%g° P=%g°  样品台=θ+%g°  探测臂零位 %g°  DB %g°/%g°" % (
            cfg.POL_DEG["S"], cfg.POL_DEG["P"], cfg.SAMPLE_VAR_OFFSET, cfg.SYSTEM_ZERO, cfg.SYSTEM_DB, cfg.SAMPLE_DB),
            foreground="#555").pack(anchor="w")

    # ---- step editing ------------------------------------------------------
    def _pols(self):
        return ["S", "P"] if self.pol_var.get() == "S+P" else [self.pol_var.get()]

    def _avg(self):
        return max(1, int(self.avg_var.get() or 1))

    def _update_counts(self):
        n_acq = sum(1 for s in self.steps if s.kind == "acquire")
        self.queue_frame.config(text="步骤队列 (%d 步, %d 次采集)" % (len(self.steps), n_acq))

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
            messagebox.showwarning("序列运行中", "运行期间不能修改队列")
            return
        if dedupe and self._already_queued(steps):
            messagebox.showinfo("已在队列中", "完全相同的步骤块已经在队列里，不再重复添加。\n如确需重复，请先删除原有的再加。")
            return
        for s in steps:
            self.steps.append(s)
            self.listbox.insert("end", "%3d  %s" % (len(self.steps), s.text))
        self.listbox.see("end")
        self._update_counts()

    def add_reference(self):
        self._add(sq.build_reference_calibration())

    def add_dark(self):
        self._add(sq.build_dark(self._avg()))

    def add_single(self):
        try:
            th = float(self.theta_var.get())
            if not (cfg.THETA_MIN <= th <= cfg.THETA_MAX):
                raise ValueError
        except ValueError:
            messagebox.showerror("输入错误", "θ 需在 %d–%d°" % (cfg.THETA_MIN, cfg.THETA_MAX))
            return
        self._add(sq.build_single_angle(th, self._pols(), self._avg(), self.prefix_var.get() or "sample"))

    def add_scan(self):
        try:
            a, b, c = float(self.start_var.get()), float(self.stop_var.get()), float(self.step_var.get())
            steps = sq.build_scan(a, b, c, self._pols(), self._avg(), self.prefix_var.get() or "sample")
        except ValueError as e:
            messagebox.showerror("输入错误", str(e))
            return
        self._add(steps)

    def add_db(self):
        self._add(sq.build_double_beam(self._pols(), self._avg(), self.prefix_var.get() or "sample"))

    def add_set_it(self):
        try:
            self._add([sq.set_it(int(self.it_var.get()))], dedupe=False)
        except ValueError:
            messagebox.showerror("输入错误", "积分时间需为整数毫秒")

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
            messagebox.showwarning("未就绪", "请先在前两页连接串口并初始化光谱仪")
            return
        outdir = os.path.join(DATA_ROOT, self.session_var.get().strip() or time.strftime("session_%Y%m%d_%H%M%S"))
        # integration time consistency: a restarted GUI defaults to 100 ms, but the session's
        # spectra must all share one integration time unless the queue sets it explicitly
        first_acq = next((i for i, s in enumerate(self.steps) if s.kind == "acquire" and s.params.get("meta", {}).get("kind") == "var"), None)
        sets_it = any((s.kind == "set_it" and not s.params.get("save")) or s.kind in ("auto_it", "apply_min_it")
                      for s in self.steps[:first_acq]) if first_acq is not None else True
        session_its = [r["integration_ms"] for r in sq.Runner.load_manifest(outdir) if r.get("kind") == "var"]
        if first_acq is not None and not sets_it and session_its:
            session_it = max(set(session_its), key=session_its.count)
            current = self.app.spectro.spec.integration_ms
            if current != session_it:
                if messagebox.askyesno("积分时间不一致",
                                       "光谱仪当前积分时间 %s ms，而会话 %s 已有数据用的是 %d ms。\n\n"
                                       "是否在队列开头插入『积分时间 %d ms』？（选否则按 %s ms 采集）"
                                       % (current, os.path.basename(outdir), session_it, session_it, current)):
                    self.steps.insert(0, sq.set_it(session_it))
                    self._refresh()
        tags = [s.params["tag"] for s in self.steps if s.kind == "acquire"]
        n_acq = len(tags)
        existing = sorted(set(t for t in tags if t != "dark") & set(r.get("tag") for r in sq.Runner.load_manifest(outdir)))
        if existing:
            if not messagebox.askyesno("已测过", "该会话目录里已有 %d 个同名采集（例如 %s），会被覆盖。\n仍要运行？"
                                       % (len(existing), ", ".join(existing[:4]))):
                return
        dups = sorted(set(t for t in tags if tags.count(t) > 1 and t != "dark"))
        if dups:
            if not messagebox.askyesno("重复标签", "队列里有 %d 个标签重复（例如 %s），后一次采集会覆盖前一次的文件。\n"
                                       "通常是因为多次点击了添加按钮——建议先『清空』再重新添加。\n\n仍要运行？"
                                       % (len(dups), ", ".join(dups[:4]))):
                return
        est = sum(s.params["avg"] for s in self.steps if s.kind == "acquire")
        if not messagebox.askyesno("运行序列", "共 %d 步，%d 次采集（约 %d 帧曝光），数据目录:\n%s\n\n电机将自动运动。开始？" % (len(self.steps), n_acq, est, outdir)):
            return
        self.running = True
        self.app.sequence_running = True
        self.abort.clear()
        self.btn_run.config(state="disabled")
        self.btn_abort.config(state="normal")
        self.bar["maximum"] = len(self.steps)
        steps = list(self.steps)
        bus, spec = self.app.bus, self.app.spectro.spec
        ppd = {p.addr: p.ppd for p in self.app.panels if p.ppd}

        def ask_user(msg):
            ev = threading.Event()
            box = {"ok": False}

            def show():
                box["ok"] = messagebox.askokcancel("序列暂停", msg)
                ev.set()
            self.events.put(("call", show))
            ev.wait()
            return box["ok"]

        def log(t):
            self.events.put(("log", t))

        def progress(i, n, step):
            self.events.put(("progress", (i, n, step.text if step else "完成")))

        def on_spectrum(rec, counts):
            self.events.put(("spectrum", (rec, counts)))

        self.load_history(outdir)
        self._mark_progress(0)

        def job():
            runner = sq.Runner(bus, spec, outdir, log=log, ask_user=ask_user, abort=self.abort, progress=progress,
                               ppd=ppd, on_spectrum=on_spectrum)
            return runner.run(steps)

        self.app.spectro.worker.submit("sequence", job, self._done)

    def abort_run(self):
        self.abort.set()
        self.prog_var.set("中止中…(等待当前步骤结束)")

    def _done(self, manifest):
        self._finish("完成: %d 个光谱已保存" % len(manifest))
        self.clear()                       # a finished queue must not silently run again

    # ---- progress marks / history / preview ---------------------------------
    def _mark_progress(self, current):
        """Rewrite listbox rows: ✓ done, ▶ current, blank pending."""
        for i, st in enumerate(self.steps):
            mark = "✓" if i < current else ("▶" if i == current else " ")
            self.listbox.delete(i)
            self.listbox.insert(i, "%s %3d  %s" % (mark, i + 1, st.text))
            self.listbox.itemconfig(i, foreground="#888" if i < current else ("#0a0" if i == current else "#000"))
        if 0 <= current < len(self.steps):
            self.listbox.see(current)

    def load_history(self, outdir=None):
        if outdir is None:
            outdir = os.path.join(DATA_ROOT, self.session_var.get().strip())
        recs = sq.Runner.load_manifest(outdir)
        self.done_tree.delete(*self.done_tree.get_children())
        self.done_tags = set()
        for r in recs:
            self._add_done(r)
        self.done_frame.config(text="已完成的采集: %s (%d 张)" % (os.path.basename(outdir), len(recs)))

    def _add_done(self, r):
        theta = r.get("theta")
        self.done_tree.insert("", "end", values=(
            r.get("time", "")[11:], r.get("tag", ""), "" if theta is None else "%g" % theta, r.get("pol", ""),
            r.get("integration_ms", ""), "%.0f" % (100.0 * (r.get("peak") or 0) / bwtek.ADC_MAX),
            r.get("saturated_active", 0)))
        self.done_tags.add(r.get("tag"))
        self.done_tree.yview_moveto(1.0)

    def _show_spectrum(self, rec, counts):
        if not rec.get("preview_only"):
            self._add_done(rec)
            n = len(self.done_tree.get_children())
            self.done_frame.config(text="已完成的采集: %s (%d 张)" % (os.path.basename(self.session_var.get().strip()), n))
        peak = int(rec.get("peak") or 0)
        self.preview_var.set("%s   IT %s ms   峰值 %d (%.0f%%)" % (rec.get("tag"), rec.get("integration_ms"), peak, 100.0 * peak / bwtek.ADC_MAX))
        if self.canvas:
            self.line.set_ydata(counts)
            self.ax.set_ylim(0, max(1000, counts.max() * 1.1))
            self.canvas.draw_idle()

    def _finish(self, text):
        self.running = False
        self.app.sequence_running = False
        self.btn_run.config(state="normal")
        self.btn_abort.config(state="disabled")
        self.prog_var.set(text)
        self.app._log_line("SEQ " + text)
        # the spectrometer tab's IT box should reflect what the sequence left behind
        if self.app.spectro.spec and self.app.spectro.spec.integration_ms:
            self.app.spectro.it_var.set(str(self.app.spectro.spec.integration_ms))

    def _poll(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self.app._log_line("SEQ " + payload)
                elif kind == "progress":
                    i, n, text = payload
                    self.bar["value"] = i
                    self.prog_var.set("%d/%d  %s" % (i, n, text))
                    self._mark_progress(i)
                elif kind == "spectrum":
                    self._show_spectrum(*payload)
                elif kind == "call":
                    payload()
        except queue.Empty:
            pass
        self.after(100, self._poll)
