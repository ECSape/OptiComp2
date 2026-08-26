# -*- coding: utf-8 -*-
"""Tk page that builds and runs measurement sequences (uses sequence.py).

Layout: page header (运行 / 中止) + progress band, a session card and an add-step card on the left,
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
SUBTITLE = "组合步骤、运行序列，并查看本会话已完成的采集。"
GEOMETRY = "几何: S=%g° P=%g° · 样品台=θ+%g° · 探测臂零位 %g° · DB %g°/%g°" % (
    cfg.POL_DEG["S"], cfg.POL_DEG["P"], cfg.SAMPLE_VAR_OFFSET, cfg.SYSTEM_ZERO, cfg.SYSTEM_DB, cfg.SAMPLE_DB)

TIPS = {
    "run": "运行整个队列（Ctrl+R / ⌘R）。运行前会依次检查：串口与光谱仪、积分时间、已有数据、重复标签，最后确认。",
    "abort": "请求中止（Esc）：当前步骤结束后停止，Runner 会关闭快门并恢复临时积分时间。",
    "load": "读取 data/<会话名>/manifest.json，把已完成的采集列在下方表格里；运行前也会自动读取。",
    "ref": "探测臂→44°，样品台→θ=80°，快门开，S 与 P 各自动定标，取较小的积分时间，快门关。参考件应为白板/标准件。",
    "dark": "快门关后采集一张暗底，文件按积分时间命名（dark_<IT>ms.csv）。",
    "single": "在给定 θ 采集所选偏振各一张：探测臂零位 → 偏振片 → 样品台 θ+105° → 快门开 → 采集 → 快门关。",
    "scan": "对 起…止 每隔 步 度重复单角度测量；偏振片每个偏振只转一次。要求 0 ≤ 起 < 止 ≤ 80，步 ≥ 1。",
    "db": "双光束替代修正：先到交换位并暂停等你换积分球端口盖，再到 DB 位（探测臂 124° / 样品台 93°）以 1000 ms 采集 S/P 与暗底，最后换回并复位。",
    "set_it": "在队列中插入「积分时间 N ms」（永久设置，会被记为已选定）。",
    "pause": "插入一个暂停：运行到这里时弹出提示，点确定继续、取消则中止。",
    "shutter": "插入单独的快门步骤（通常不需要：测量步骤自带开关快门）。",
    "geometry": "来自 hw/config.py：POL_DEG、SAMPLE_VAR_OFFSET、SYSTEM_ZERO、SYSTEM_DB、SAMPLE_DB。",
    "remove": "删除队列中选中的步骤（运行中不可用）。",
    "clear": "清空整个队列（运行中不可用）。",
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
        self.header = ui_theme.PageHeader(self, "测量", SUBTITLE,
                                          actions=[("▶ 运行序列", self.run, "Primary.TButton"), ("■ 中止", self.abort_run, "Destructive.TButton")])
        self.header.grid(row=0, column=0, sticky="ew", pady=(0, SPACE["md"]))
        self.btn_run = self.header.buttons["▶ 运行序列"]
        self.btn_abort = self.header.buttons["■ 中止"]
        self.btn_abort.configure(state="disabled")
        tooltip(self.btn_run, TIPS["run"])
        tooltip(self.btn_abort, TIPS["abort"])

        # ---- progress band
        band = ttk.Frame(self, style="Page.TFrame")
        band.grid(row=1, column=0, sticky="ew", pady=(0, SPACE["md"]))
        band.columnconfigure(1, weight=1)
        self.prog_var = tk.StringVar(value="空闲")
        self.prog_label = ttk.Label(band, textvariable=self.prog_var, style="TLabel", anchor="w")
        self.prog_label.grid(row=0, column=0, sticky="w", padx=(0, SPACE["md"]))
        self.bar = ttk.Progressbar(band, mode="determinate")
        self.bar.grid(row=0, column=1, sticky="ew")
        # reliability toggles (2026-08-27): every run reconnects the spectrometer and zeros every stage,
        # so the operator no longer power-cycles the USB or reconnects between samples (kept on the band
        # row to stay within the 720 px height budget). The fibre arm is moved to its zero, never homed.
        self.autoreconnect_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(band, text="开始前重连光谱仪", variable=self.autoreconnect_var,
                        style="TCheckbutton").grid(row=0, column=2, sticky="e", padx=(SPACE["md"], 0))
        self.autoreset_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(band, text="开始前位置归零", variable=self.autoreset_var,
                        style="TCheckbutton").grid(row=0, column=3, sticky="e", padx=(SPACE["sm"], 0))
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
        card = Card(parent)                              # one labelled row; a title would only repeat 会话
        card.grid(row=2, column=0, sticky="ew", pady=(0, SPACE["md"]))
        self.session_card = card
        row = ttk.Frame(card.body, style="CardBody.TFrame")
        row.grid(row=0, column=0, sticky="w")
        ttk.Label(row, text="会话/样品名", style="FormLabel.TLabel").pack(side="left", padx=(0, SPACE["sm"]))
        self.session_var = tk.StringVar(value=time.strftime("session_%Y%m%d_%H%M"))
        e = ttk.Entry(row, textvariable=self.session_var, width=18)
        e.pack(side="left")
        bind_enter(e, self.load_history)
        self.btn_load = ttk.Button(row, text="载入已有记录", command=self.load_history, style="Ghost.TButton")
        self.btn_load.pack(side="left", padx=(SPACE["xs"], 0))
        tooltip(self.btn_load, TIPS["load"])
        ttk.Label(row, text="文件标签前缀", style="FormLabel.TLabel").pack(side="left", padx=(SPACE["xl"], SPACE["sm"]))
        self.prefix_var = tk.StringVar(value="sample")
        ttk.Entry(row, textvariable=self.prefix_var, width=8).pack(side="left")
        ttk.Label(row, text="平均次数", style="FormLabel.TLabel").pack(side="left", padx=(SPACE["lg"], SPACE["sm"]))
        self.avg_var = tk.StringVar(value="3")
        ttk.Spinbox(row, from_=1, to=50, textvariable=self.avg_var, width=3, justify="right").pack(side="left")
        ttk.Label(row, text="次", style="Card.Caption.TLabel").pack(side="left", padx=(SPACE["xs"], 0))
        ttk.Label(row, text="偏振", style="FormLabel.TLabel").pack(side="left", padx=(SPACE["lg"], SPACE["sm"]))
        self.pol_var = tk.StringVar(value="S+P")
        ttk.Combobox(row, textvariable=self.pol_var, values=["S", "P", "S+P"], state="readonly", width=4).pack(side="left")

    def _build_steps_card(self, parent):
        card = Card(parent, title="添加步骤", subtitle=GEOMETRY)
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
        button(r0, "① 参考定标 · 80° S/P 自动积分时间", self.add_reference, "ref").pack(side="left")
        button(r0, "② 暗底 · 快门关，当前积分时间", self.add_dark, "dark").pack(side="left", padx=(SPACE["sm"], 0))

        self.theta_var = tk.StringVar(value="45")
        g_th = ttk.Frame(f, style="CardBody.TFrame")
        e_th = ttk.Entry(g_th, textvariable=self.theta_var, width=6, justify="right")
        e_th.pack(side="left")
        bind_enter(e_th, self.add_single)
        unit_label(g_th, "°")
        form_row(f, 1, "θ", g_th, button(f, "③ 单角度测量", self.add_single, "single"), label_width=8, pady=pady)

        sc = ttk.Frame(f, style="CardBody.TFrame")
        self.start_var, self.stop_var, self.step_var = tk.StringVar(value="8"), tk.StringVar(value="80"), tk.StringVar(value="4")
        for i, (lab, v) in enumerate((("起", self.start_var), ("止", self.stop_var), ("步", self.step_var))):
            ttk.Label(sc, text=lab, style="FormLabel.TLabel").pack(side="left", padx=((0 if i == 0 else SPACE["sm"]), SPACE["xs"]))
            e = ttk.Entry(sc, textvariable=v, width=4, justify="right")
            bind_enter(e, self.add_scan)
            e.pack(side="left")
        unit_label(sc, "°")
        form_row(f, 2, "扫描范围", sc, button(f, "④ 角度扫描", self.add_scan, "scan"), label_width=8, pady=pady)

        r4 = ttk.Frame(f, style="CardBody.TFrame")
        r4.grid(row=3, column=0, columnspan=3, sticky="w", pady=pady)
        button(r4, "⑤ 双光束 DB · 含换端口盖暂停", self.add_db, "db").pack(side="left")
        self.btn_shutter_close = button(r4, "添加：快门关", lambda: self._add([sq.shutter(False)], dedupe=False), "shutter")
        self.btn_shutter_close.pack(side="left", padx=(SPACE["md"], 0))
        self.btn_shutter_open = button(r4, "添加：快门开", lambda: self._add([sq.shutter(True)], dedupe=False), "shutter")
        self.btn_shutter_open.pack(side="left", padx=(SPACE["sm"], 0))

        self.it_var = tk.StringVar(value="1000")
        g_it = ttk.Frame(f, style="CardBody.TFrame")
        e_it = ttk.Entry(g_it, textvariable=self.it_var, width=8, justify="right")
        e_it.pack(side="left")
        bind_enter(e_it, self.add_set_it)
        unit_label(g_it, "ms")
        form_row(f, 4, "积分时间", g_it, button(f, "添加：设定积分时间", self.add_set_it, "set_it"), label_width=8, pady=pady)

        self.pause_var = tk.StringVar(value="请更换样品，然后点确定")
        e_pause = ttk.Entry(f, textvariable=self.pause_var, width=24)
        bind_enter(e_pause, self.add_pause)
        form_row(f, 5, "暂停提示", e_pause, button(f, "添加：暂停", self.add_pause, "pause"), label_width=8, pady=pady)
        # form_row gives the last used column the weight; one action column (2) for every row, the
        # fields keep their natural width and the wide button rows push their slack into column 2
        for c in range(2):
            f.columnconfigure(c, weight=0)
        f.columnconfigure(2, weight=1)

    def _build_queue_card(self, parent):
        card = Card(parent, title="步骤队列 · 0 步 · 0 次采集")
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
        self.queue_empty = empty_state(body, "队列为空。", "用左侧「添加步骤」加入 ①–⑤。\n典型顺序：① → ② → ④（白板）→ 换样品 → ④ → ⑤。")
        self.queue_empty.place(in_=self.listbox, relx=0.5, rely=0.42, anchor="center")
        b = ttk.Frame(body, style="CardBody.TFrame")
        b.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(SPACE["sm"], 0))
        self.btn_remove = ttk.Button(b, text="删除选中", command=self.remove_selected)
        self.btn_remove.pack(side="left")
        self.btn_clear = ttk.Button(b, text="清空", command=self.clear)
        self.btn_clear.pack(side="left", padx=(SPACE["sm"], 0))
        tooltip(self.btn_remove, TIPS["remove"])
        tooltip(self.btn_clear, TIPS["clear"])
        self._edit_buttons += [self.btn_remove, self.btn_clear]

    def _build_done_card(self, parent, px):
        card = Card(parent, title="已完成的采集 · 0 张")
        card.grid(row=0, column=0, sticky="nsew", padx=(0, SPACE["md"]))
        self.done_card = card
        self.done_frame = card                       # legacy name
        body = card.body
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        cols = ("time", "tag", "theta", "pol", "it", "peak", "sat")
        self.done_tree = ttk.Treeview(body, columns=cols, show="headings", height=3, takefocus=0)   # grows with the window
        for c, w, t, a in zip(cols, (70, 130, 45, 40, 55, 55, 40), ("时间", "标签", "θ°", "偏振", "IT ms", "峰值%", "饱和"),
                              ("w", "w", "e", "center", "e", "e", "e")):
            self.done_tree.heading(c, text=t, anchor=a)
            self.done_tree.column(c, width=px(w), minwidth=px(w), anchor=a, stretch=(c == "tag"))
        self.done_tree.tag_configure("odd", background=COLORS["row_alt"])
        sb = ttk.Scrollbar(body, orient="vertical", command=self.done_tree.yview)
        self.done_tree.configure(yscrollcommand=sb.set)
        self.done_tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.done_empty = empty_state(body, "本会话还没有采集。", "运行后每张光谱会出现在这里。")
        self.done_empty.place(in_=self.done_tree, relx=0.5, rely=0.6, anchor="center")

    def _build_preview_card(self, parent, px):
        card = Card(parent, title="实时预览", subtitle="(尚无数据)")
        card.grid(row=0, column=1, sticky="nsew")
        self.preview_card = card
        self.preview_var = tk.StringVar(value="(尚无数据)")
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
            ttk.Label(body, text="matplotlib 不可用: %s" % e, style="Card.TLabel").grid(row=0, column=0, sticky="w")
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
        self._empty_text = ui_theme.mpl_empty(self.ax, "运行序列后显示最近一张光谱")
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
        for prefix, tone in (("完成", "done"), ("已中止", "aborted"), ("失败", "failed"), ("中止中", "aborted"), ("等待操作", "paused"), ("空闲", "idle")):
            if text.startswith(prefix):
                return tone
        return "running"

    def set_locked(self, locked):
        """Called by the App whenever sequence_running changes; the messagebox guards stay in place."""
        st = "disabled" if locked else "normal"
        for b in self._edit_buttons:
            b.configure(state=st)
        self.header.set_subtitle(SUBTITLE.rstrip("。") + " · 序列运行中" if locked else SUBTITLE)

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
        self.queue_card.set_title("步骤队列 · %d 步 · %d 次采集" % (len(self.steps), n_acq))
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
            messagebox.showwarning("未就绪", "请先在「仪器」页连接串口并初始化光谱仪")
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
            hint = ("最近的会话 %s 用的是 %d ms。是否在队列开头插入『积分时间 %d ms』？\n（选否则按 %s ms 采集，取消则不运行）"
                    % (last[0], last[1], last[1], self.app.spectro.spec.integration_ms)) if last else "请先在光谱仪页设置/定标积分时间，或在队列前加①。"
            if last:
                ans = messagebox.askyesnocancel("积分时间未设置", "GUI 启动后尚未设置积分时间（当前 %s ms，默认值）。\n\n%s"
                                                % (self.app.spectro.spec.integration_ms, hint))
                if ans is None:
                    return
                if ans:
                    self.steps.insert(0, sq.set_it(last[1]))
                    self._refresh()
            else:
                messagebox.showwarning("积分时间未设置", "GUI 启动后尚未设置积分时间（当前 %s ms，默认值）。\n%s" % (self.app.spectro.spec.integration_ms, hint))
                return
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
                prev = self.prog_var.get()
                self.prog_var.set("等待操作：%s" % msg)
                self._set_band_tone("paused")
                self.app.refresh_status()
                try:
                    box["ok"] = messagebox.askokcancel("序列暂停", msg)
                finally:
                    self.prog_var.set(prev)
                    self._set_band_tone("running")
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
                self.events.put(("failed", ("已中止", str(e), runner.shutter_open)))
            except Exception as e:
                self.events.put(("failed", ("失败", "%s: %s" % (type(e).__name__, e), runner.shutter_open)))
            return None

        self.app.spectro.worker.submit("sequence", job, self._done)

    def abort_run(self):
        self.abort.set()
        self.prog_var.set("中止中…(等待当前步骤结束)")
        self._set_band_tone("aborted")
        self.app.refresh_status()

    def _done(self, manifest):
        if manifest is None:               # reported through the "failed" event
            return
        self._finish("完成: %d 个光谱已保存" % len(manifest))
        self.clear()                       # a finished queue must not silently run again

    def _failed(self, title, msg, shutter_open):
        self._finish("%s: %s" % (title, msg))
        # the Runner closes the shutter on its way out; mirror what it reports (None = unknown)
        self.app.set_shutter({True: "open", False: "closed"}.get(shutter_open, "unknown"), "sequence %s" % title)
        extra = "" if shutter_open is False else "\n\n注意：快门状态未知，请看状态栏或在「电机与快门」页确认已关闭。"
        if title == "已中止":
            messagebox.showwarning("序列已中止", msg + extra)
        else:
            messagebox.showerror("序列失败", msg + "\n\n队列保留，可修正后重新运行。" + extra)

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
        self.done_card.set_title("已完成的采集 · %s · %d 张" % (os.path.basename(outdir), len(recs)))
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
            self.done_card.set_title("已完成的采集 · %s · %d 张" % (os.path.basename(self.session_var.get().strip()), n))
        peak = int(rec.get("peak") or 0)
        self.preview_var.set("%s   IT %s ms   峰值 %d (%.0f%%)" % (rec.get("tag"), rec.get("integration_ms"), peak, 100.0 * peak / bwtek.ADC_MAX))
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
                kind, payload = self.events.get_nowait()
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
        except queue.Empty:
            pass
        self.after(100, self._poll)
