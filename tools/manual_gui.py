# -*- coding: utf-8 -*-
"""Manual hardware-control GUI for the OptiComp instrument (stages + shutter + spectrometer + sequences).

Every button sends one command, waits for the module to finish, and shows the decoded reply. All
serial traffic is logged (and can be saved). Original OptiComp is untouched; do not run both at once
because COM4 is exclusive.

    py tools\\manual_gui.py                      # lab PC, real hardware (unchanged behaviour)
    py tools\\manual_gui.py --demo               # fake hardware, never opens COM4 or the DLL
    py tools\\manual_gui.py --demo --screenshot DIR [--no-capture]   # drives the demo and saves PNGs

Threading rules (unchanged): serial calls run only on HardwareWorker (app.submit), DLL calls only on
SpectroWorker (spectro.worker.submit); results come back through queues polled with after(100).

Python 3.9 / Tk 8.6 / pyserial 3.5 compatible.
"""
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
from hw import elliptec as ell
from hw import config as cfg
from hw import stagestate
import sequence as sq
import ui_theme
from ui_theme import SPACE, COLORS, Card, StatusPill, Disclosure, Banner, form_row, unit_label, bind_enter, bind_shortcut, tooltip, empty_state

DEFAULT_PORT = "COM4"
__version__ = "1.0"         # shown in the title bar and log header so the running build is unambiguous
PAGE_PAD = (SPACE["xl"], SPACE["md"], SPACE["xl"], SPACE["md"])

# Device roles on the OptiComp bus (from stageframework.py + thesis chapter 4). Angles are the
# instrument calibration and must not change; the labels are what the operator sees.
DEVICES = [
    {"addr": "0", "name": "快门 · 模块 0", "short": "快门", "kind": "slider", "hint": "ELL6 滑块：前进 = 打开，后退 = 关闭"},
    {"addr": "1", "name": "偏振片 · 模块 1", "short": "偏振片", "kind": "rotation",
     "presets": [("P 偏振 · 146°", 146.0), ("S 偏振 · 236°", 236.0), ("0°", 0.0)],
     "limits": None, "hint": "ELL14 旋转台：S = 236°，P = 146°"},
    {"addr": "2", "name": "探测臂（下台）· 模块 2 · ⚠ 光纤", "short": "探测臂 / 下台 ⚠ 光纤", "kind": "rotation",
     "presets": [("零位 · 44°", 44.0), ("DB 位 · 124°", 124.0), ("交换位 · 150°", 150.0)],
     "limits": (0.0, 200.0), "hint": "带光纤的探测臂：不要回零，软限位 0–200°"},
    {"addr": "3", "name": "样品台（上台）· 模块 3", "short": "样品台 / 上台", "kind": "rotation",
     "presets": [("零位 · 103°", 103.0), ("θ = 0° · 105°", 105.0), ("DB 位 · 93°", 93.0), ("交换位 · 120°", 120.0), ("θ = 80° · 185°", 185.0)],
     "limits": (0.0, 200.0), "hint": "样品台：台角 = θ + 105°，软限位 0–200°"},
]

STATUS_ZH = {0: "正常", 1: "通信超时", 2: "机械超时", 3: "命令错误", 4: "数值超范围", 5: "模块隔离", 6: "退出隔离",
             7: "初始化错误", 8: "热错误", 9: "忙", 10: "传感器错误", 11: "电机错误", 12: "超出范围", 13: "过流"}

TIPS = {
    "info": "查询模块信息（协议命令 in）：型号、序列号、固件、行程与脉冲数。角度运动前必须先读一次，用于换算脉冲/度。",
    "status": "读取状态码（gs）：00 正常，02 机械超时（回零失败），09 忙。",
    "position": "读取当前位置（gp），按脉冲返回并换算为角度或毫米。",
    "home": "回零（ho0，顺时针）：模块运动到机械零点。探测臂（模块 2）带光纤，回零可能整圈旋转——通常不需要。",
    "open": "滑块前进（fw）= 打开快门，光将照到样品与探测器。",
    "close": "滑块后退（bw）= 关闭快门。",
    "move": "绝对移动（ma）到目标角度；超出软限位 0–200° 会先询问。",
    "rel": "相对移动（mr）。为核对软限位并避免卡滞后重复叠加，实际以绝对移动到「当前 + Δ」执行；超过 30° 会先询问。",
    "preset": "常用位置；数值来自 hw/config.py 的仪器标定（论文第 4 章）。",
    "set_vel": "设置速度百分比（sv，10–100 %）。探测臂在每次连接时自动设为 50 %（原程序 2sv32）。",
    "get_vel": "读取当前速度百分比（gv）。",
    "raw": "直接发送协议命令：首字符地址，随后两字符命令，其余数据，例如 2gs、0in、3ma00011E00。回复在日志中解码显示。",
    "query_all": "读取四个模块的信息、状态与位置（F5）。",
    "connect": "打开串口并做健康检查：保护探测臂、与上次记录的位置比对、设置速度。",
    "disconnect": "记录电机状态后关闭串口。序列运行中不能断开。",
    "close_shutter": "任何时候都可以点：关闭快门（0bw）。序列运行中会先询问是否中止。",
}


class HardwareWorker(threading.Thread):
    """Runs serial jobs one at a time off the Tk thread; results go back via a queue."""

    def __init__(self, results):
        threading.Thread.__init__(self, daemon=True)
        self.jobs = queue.Queue()
        self.results = results

    def run(self):
        while True:
            label, fn, callback = self.jobs.get()
            try:
                value = fn()
                self.results.put(("ok", label, value, callback))
            except Exception as e:                       # report, never die
                self.results.put(("err", label, e, callback))

    def submit(self, label, fn, callback=None):
        self.jobs.put((label, fn, callback))


def _display_path(path):
    """Path for captions: relative to the repo when inside it, otherwise absolute with the home directory as ~."""
    root = os.path.abspath(os.path.join(HERE, ".."))
    p = os.path.abspath(path)
    try:
        rel = os.path.relpath(p, root)
    except ValueError:                                   # Windows: different drives
        rel = ".."
    if not rel.startswith(".."):
        return rel.replace("\\", "/")
    home = os.path.expanduser("~")
    if p.startswith(home):
        p = "~" + p[len(home):]
    return p.replace("\\", "/")


class DevicePanel(Card):
    """One card per Elliptec module: readout (position + status pill), the everyday actions, and a
    collapsed '高级' area with the protocol-level buttons (info / status / position / home / speed)."""

    def __init__(self, master, app, spec):
        self.app = app
        self.spec = spec
        self.addr = spec["addr"]
        self.info = None
        self.ppd = None                      # pulses per degree (rotation only)
        self.info_var = tk.StringVar(value="(未查询)")
        self.pos_var = tk.StringVar(value="—")
        self.pos_detail_var = tk.StringVar(value="")
        self.stat_var = tk.StringVar(value="状态: ?")
        self._buttons = []
        Card.__init__(self, master, title=spec["name"], subtitle="(未查询)")
        self.subtitle_label.configure(textvariable=self.info_var)
        tooltip(self.title_label, spec.get("hint", ""))
        body = self.body
        body.columnconfigure(0, weight=1)

        # ---- readout: big value + status pill
        ro = ttk.Frame(body, style="CardBody.TFrame")
        ro.grid(row=0, column=0, sticky="ew", pady=(0, SPACE["md"]))
        ro.columnconfigure(0, weight=1)
        left = ttk.Frame(ro, style="CardBody.TFrame")
        left.grid(row=0, column=0, sticky="w")
        ttk.Label(left, textvariable=self.pos_var, style="Value.TLabel").pack(anchor="w")
        ttk.Label(left, textvariable=self.pos_detail_var, style="Card.Caption.TLabel").pack(anchor="w")
        self.stat_pill = StatusPill(ro, text="?", tone="neutral", on_card=True)
        self.stat_pill.grid(row=0, column=1, sticky="ne")
        self.stat_var.trace_add("write", lambda *_a: self._sync_stat_pill())

        row = 1
        if spec["kind"] == "slider":
            br = ttk.Frame(body, style="CardBody.TFrame")
            br.grid(row=row, column=0, sticky="w", pady=(0, SPACE["sm"]))
            b = ttk.Button(br, text="打开快门", style="Primary.TButton", command=lambda: self.motion("fw", self.app.bus_forward))
            b.pack(side="left", padx=(0, SPACE["sm"]))
            self._buttons.append(tooltip(b, TIPS["open"]))
            b = ttk.Button(br, text="关闭快门", style="Destructive.TButton", command=lambda: self.motion("bw", self.app.bus_backward))
            b.pack(side="left")
            self._buttons.append(tooltip(b, TIPS["close"]))
            row += 1
        else:
            form = ttk.Frame(body, style="CardBody.TFrame")
            form.grid(row=row, column=0, sticky="ew", pady=(0, SPACE["sm"]))
            row += 1
            # column 0 label, column 1 field group (entry + hugging unit), column 2 the action button
            self.abs_var = tk.StringVar(value="")
            g_abs = ttk.Frame(form, style="CardBody.TFrame")
            e_abs = ttk.Entry(g_abs, textvariable=self.abs_var, width=8, justify="right")
            e_abs.pack(side="left")
            bind_enter(e_abs, self.do_move_abs)
            unit_label(g_abs, "°")
            self.btn_move = ttk.Button(form, text="移动", style="Primary.TButton", command=self.do_move_abs)
            self._buttons.append(tooltip(self.btn_move, TIPS["move"]))
            form_row(form, 0, "目标角度", g_abs, self.btn_move, label_width=8, pady=2)

            rel = ttk.Frame(form, style="CardBody.TFrame")
            self.rel_var = tk.StringVar(value="1")
            b_minus = ttk.Button(rel, text="−", style="Icon.TButton", command=lambda: self.do_move_rel(-1))
            b_minus.pack(side="left")
            e_rel = ttk.Entry(rel, textvariable=self.rel_var, width=6, justify="center")
            e_rel.pack(side="left", padx=SPACE["xs"])
            bind_enter(e_rel, lambda: self.do_move_rel(1))
            ttk.Label(rel, text="°", style="Card.Caption.TLabel").pack(side="left", padx=(0, SPACE["xs"]))
            b_plus = ttk.Button(rel, text="+", style="Icon.TButton", command=lambda: self.do_move_rel(1))
            b_plus.pack(side="left")
            self._buttons += [tooltip(b_minus, TIPS["rel"]), tooltip(b_plus, TIPS["rel"])]
            form_row(form, 1, "相对移动", rel, label_width=8, pady=2)

            self.preset_cb = ttk.Combobox(form, values=[p[0] for p in spec["presets"]], state="readonly", width=16)
            self.preset_cb.current(0)
            b_preset = ttk.Button(form, text="到预设", command=self.do_preset)
            self._buttons.append(tooltip(b_preset, TIPS["preset"]))
            form_row(form, 2, "预设", self.preset_cb, b_preset, label_width=8, pady=2)
            self.preset_cb.bind("<Return>", lambda _e: self.do_preset())
            # one action column for all rows; every form_row call gave "its" last column weight 1, so
            # reset: the fields keep their natural width, the action column absorbs the slack
            for c in range(2):
                form.columnconfigure(c, weight=0)
            form.columnconfigure(2, weight=1)

        # ---- advanced (protocol-level) actions
        self.advanced = Disclosure(body, title="高级", opened=False)
        self.advanced.grid(row=row, column=0, sticky="ew")
        adv = self.advanced.body
        br = ttk.Frame(adv, style="CardBody.TFrame")
        br.grid(row=0, column=0, sticky="w", pady=(0, SPACE["xs"]))
        for text, cmd, key, st in (("信息", self.do_info, "info", "TButton"), ("状态", self.do_status, "status", "TButton"),
                                   ("位置", self.do_position, "position", "TButton"), ("回零", self.do_home, "home", "Destructive.TButton")):
            b = ttk.Button(br, text=text, command=cmd, style=st)
            b.pack(side="left", padx=(0, SPACE["sm"]))
            self._buttons.append(tooltip(b, TIPS[key]))
        if spec["kind"] == "rotation":
            vf = ttk.Frame(adv, style="CardBody.TFrame")
            vf.grid(row=1, column=0, sticky="w", pady=(SPACE["xs"], 0))
            self.vel_var = tk.StringVar(value="100")
            sp = ttk.Spinbox(vf, from_=10, to=100, increment=10, textvariable=self.vel_var, width=5, justify="right")
            bind_enter(sp, self.do_set_velocity)
            b_set = ttk.Button(vf, text="设置速度", command=self.do_set_velocity)
            b_get = ttk.Button(vf, text="读取速度", command=self.do_get_velocity)
            self._buttons += [tooltip(b_set, TIPS["set_vel"]), tooltip(b_get, TIPS["get_vel"])]
            ttk.Label(vf, text="速度", style="FormLabel.TLabel", width=8, anchor="e").pack(side="left", padx=(0, SPACE["sm"]))
            sp.pack(side="left")
            ttk.Label(vf, text="%", style="Card.Caption.TLabel").pack(side="left", padx=(SPACE["xs"], SPACE["sm"]))
            b_set.pack(side="left", padx=(0, SPACE["sm"]))
            b_get.pack(side="left")

    def sync_velocity(self, pct):
        """Mirror a velocity read from the module into the 高级 spinbox (rotation stages only)."""
        if hasattr(self, "vel_var") and pct is not None:
            try:
                self.vel_var.set(str(int(pct)))
            except (TypeError, ValueError):
                pass

    def set_locked(self, locked):
        for b in self._buttons:
            b.configure(state="disabled" if locked else "normal")

    # ---- helpers -----------------------------------------------------------
    def _sync_stat_pill(self):
        txt = self.stat_var.get()
        shown = txt[len("状态: "):] if txt.startswith("状态: ") else txt
        tone = "neutral"
        if "运动中" in shown:
            tone = "warning"
        elif shown.startswith("完成"):
            tone = "success"
        elif shown.startswith("失败"):
            tone = "danger"
        elif shown[:2].isalnum() and len(shown) >= 2 and shown[:2] != "速度" and shown[:2].upper() == shown[:2]:
            try:
                tone = "success" if int(shown[:2], 16) == 0 else "danger"
            except ValueError:
                tone = "neutral"
        self.stat_pill.set(shown, tone)

    def _show_position(self, pulses):
        if pulses is None:
            if self.addr == cfg.SHUTTER:
                self.app.set_shutter("unknown", "%s: no position" % self.addr)
            return
        if self.ppd:
            self.pos_var.set("%.3f°" % ((pulses / self.ppd) % 360.0))
            self.pos_detail_var.set("%d pulses" % pulses)
        elif self.spec["kind"] == "slider":
            self.pos_var.set("已关闭" if int(pulses) == 0 else "已打开")
            self.pos_detail_var.set("%d mm · %d pulses" % (pulses, pulses))
        elif self.info and self.info.pulses_per_unit:
            self.pos_var.set("%.3f mm" % (pulses / self.info.pulses_per_unit))
            self.pos_detail_var.set("%d pulses" % pulses)
        else:
            self.pos_var.set("%d pulses" % pulses)
            self.pos_detail_var.set("")
        self.app.update_module(self.addr, pulses=pulses, source="%s gp" % self.addr)

    def _on_info(self, info):
        self.info = info
        if self.spec["kind"] == "rotation" and info.travel:
            self.ppd = float(info.pulses) / info.travel
        unit = "%d 脉冲/圈" % info.pulses if self.spec["kind"] == "rotation" else "行程 %d mm" % info.travel
        self.info_var.set("%s · SN %s · fw %s · %s" % (info.model_name, info.serial, info.fw, unit))

    def _need_ppd(self):
        if self.ppd is None:
            messagebox.showwarning("先查询信息", "请先点「高级 › 信息」读取模块参数（脉冲/度），再做角度运动。\n连接后「查询全部信息」也会读取。")
            return False
        return True

    def _confirm_angle(self, deg):
        lim = self.spec.get("limits")
        if lim and not (lim[0] <= deg <= lim[1]):
            return messagebox.askyesno("超出软限位",
                                       "模块 %s 目标 %.2f° 超出软限位 %.0f–%.0f°。\n"
                                       "（论文提示：下台过度旋转可能绞缠光纤）\n\n仍要移动吗？" % (self.addr, deg, lim[0], lim[1]))
        return True

    # ---- actions -----------------------------------------------------------
    def do_info(self):
        self.app.submit("%s in" % self.addr, lambda: self.app.bus.info(self.addr), self._on_info)

    def do_status(self):
        def done(c):
            self.stat_var.set("状态: %02X %s" % (c, ell.STATUS_CODES.get(c, "?")))
            self.app.update_module(self.addr, status=c)
        self.app.submit("%s gs" % self.addr, lambda: self.app.bus.status(self.addr), done)

    def do_position(self):
        self.app.submit("%s gp" % self.addr, lambda: self.app.bus.position(self.addr), self._show_position)

    def do_home(self):
        msg = "模块 %s 将执行回零 (ho0)，会产生运动。继续？" % self.addr
        if str(self.addr) == str(cfg.SYSTEM):
            # thesis 4.x: the fibre enters the sphere from below and winds around the lower support
            # at excessive lower-stage rotation; homing may sweep a full turn -> mechanical time-out
            msg = ("模块 %s 是带光纤的探测臂/下台。回零可能整圈旋转，光纤会缠绕在下支撑上并报 GS02。\n"
                   "通常不需要回零：位置由传感器保持，直接用预设 44°/124°/150° 即可。\n\n仍要回零？" % self.addr)
        if not messagebox.askyesno("回零", msg, default="no" if str(self.addr) == str(cfg.SYSTEM) else "yes"):
            return
        self.motion("ho0", self.app.bus_home)

    def motion(self, label, fn):
        if self.app.bus is None or self.app.sequence_running:
            self.app.submit("%s %s" % (self.addr, label), lambda: fn(self.addr), self._after_motion)   # guard dialogs only
            return
        self.stat_var.set("状态: 运动中…")
        self._t0 = time.time()
        if self.addr == cfg.SHUTTER:
            self.app.set_shutter("moving", "%s%s" % (self.addr, label))
        self.app.submit("%s %s" % (self.addr, label), lambda: fn(self.addr), self._after_motion)

    def _after_motion(self, pulses):
        self.stat_var.set("状态: 完成 (%.1f s)" % (time.time() - getattr(self, "_t0", time.time())))
        self._show_position(pulses)

    def do_move_abs(self):
        if not self._need_ppd():
            return
        try:
            deg = float(self.abs_var.get())
        except ValueError:
            messagebox.showerror("输入错误", "请输入角度数值")
            return
        self._go_deg(deg)

    def do_preset(self):
        if not self._need_ppd():
            return
        deg = self.spec["presets"][self.preset_cb.current()][1]
        self.abs_var.set("%g" % deg)
        self._go_deg(deg)

    def _go_deg(self, deg):
        if not self._confirm_angle(deg):
            return
        pulses = int(round((deg % 360.0) * self.ppd))
        self.stat_var.set("状态: 运动中… → %.2f°" % deg)
        self._t0 = time.time()
        self.app.submit("%s ma %.2f°" % (self.addr, deg), lambda: self.app.bus.move_abs(self.addr, pulses), self._after_motion)

    def do_move_rel(self, sign=1):
        if not self._need_ppd():
            return
        try:
            ddeg = float(self.rel_var.get()) * (1 if sign >= 0 else -1)
        except ValueError:
            messagebox.showerror("输入错误", "请输入角度数值")
            return
        if abs(ddeg) > 30 and not messagebox.askyesno("大角度相对移动", "相对移动 %.2f°，继续？" % ddeg):
            return
        addr, ppd, bus = self.addr, self.ppd, self.app.bus

        def job():
            # executed as an absolute move to (current + delta): the target is checked against the
            # soft limits and a stalled move is never re-applied from where it stopped
            start = bus.position(addr) / ppd
            target = start + ddeg
            sq.check_soft_limits(addr, target)          # ValueError -> shown by the generic error path
            return bus.move_abs(addr, int(round((target % 360.0) * ppd)))
        self.stat_var.set("状态: 运动中… %+.2f°" % ddeg)
        self._t0 = time.time()
        self.app.submit("%s mr %+.2f° (as ma)" % (self.addr, ddeg), job, self._after_motion)

    def do_set_velocity(self):
        try:
            pct = int(self.vel_var.get())
        except ValueError:
            return
        self.app.submit("%s sv %d%%" % (self.addr, pct), lambda: self.app.bus.set_velocity(self.addr, pct), lambda _: self.do_get_velocity())

    def do_get_velocity(self):
        def done(v):
            self.stat_var.set("状态: 速度 %s%%" % v)
            self.app.update_module(self.addr, velocity=v)
        self.app.submit("%s gv" % self.addr, lambda: self.app.bus.velocity(self.addr), done)


class App(tk.Tk):
    PAGES = [("instrument", "◎  仪器", "连接、状态总览、开机步骤"),
             ("motors", "◆  电机与快门", "手动运动与快门"),
             ("spectro", "◐  光谱仪", "初始化、积分时间、读谱"),
             ("measure", "▶  测量", "序列队列与运行"),
             ("analysis", "◈  分析", "反射率计算与导出")]

    def __init__(self, demo=False, data_root=None, state_path=None, screenshot_dir=None):
        tk.Tk.__init__(self)
        self.theme = ui_theme.apply_theme(self)          # before any ttk widget exists
        self.demo = bool(demo)
        self.screenshot_dir = screenshot_dir
        repo = os.path.abspath(os.path.join(HERE, ".."))
        default_root = os.path.join(repo, "data", "demo") if self.demo else os.path.join(repo, "data")
        self.data_root = os.path.abspath(data_root) if data_root else default_root
        if state_path is not None:
            self.state_path = state_path
        else:
            self.state_path = os.path.join(self.data_root, "stage_state.json") if self.demo else cfg.STATE_FILE
        self.title("OptiComp2 v%s%s" % (__version__, " · 演示模式" if self.demo else ""))
        self.bus = None
        self.bus_port = None
        self.ppd = None
        self.sequence_running = False
        # GUI-side state model (Tk thread only)
        self.shutter_state = "unknown"
        self.stage_deg = {}
        self.stage_status = {}
        self.stage_vel = {}
        self.stage_updated = None
        self.health_problems = []
        self.health_done_count = 0
        self.log_unread_errors = 0
        self._locked = False
        self._log_visible = False
        self._log_height = 200
        self._seq_done_once = False
        self.current_page = None
        # demo hooks
        self._demo_bus = None
        self._demo_spec = None
        self.demo_anomaly = None
        self.demo_motion_seconds = 0.6
        self.demo_fast = False
        if self.demo:
            import demo_hw
            self._demo_bus = demo_hw.DemoBus(log=self._log_serial, motion_seconds=self.demo_motion_seconds)
        self.results = queue.Queue()
        self.worker = HardwareWorker(self.results)
        self.worker.start()
        self.log_lines = []
        logdir = os.path.join(repo, "logs")
        os.makedirs(logdir, exist_ok=True)
        self.autolog_path = os.path.join(logdir, time.strftime(("demo_" if self.demo else "manual_") + "%Y%m%d_%H%M%S.log"))
        self.autolog = open(self.autolog_path, "a", encoding="utf-8")
        self._build()
        self._log_line("--- OptiComp2 GUI v%s%s, auto log: %s ---" % (__version__, " (demo)" if self.demo else "", os.path.abspath(self.autolog_path)))
        if self.demo:
            self._seed_demo()
        self.after(100, self._poll_results)
        self.after(500, self._tick_status)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._bind_keys()
        self.refresh_status()

    # ---- layout ------------------------------------------------------------
    def _build(self):
        self.minsize(1100, 720)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry("1280x820")
        if self.theme.platform == "win32" and (sw < 1366 or sh < 864):
            try:
                self.state("zoomed")
            except tk.TclError:
                pass
        self.statusbar = ui_theme.StatusBar(self, [("port", "串口 未连接"), ("spec", "光谱仪 未初始化"), ("it", "积分时间 —"),
                                                   ("shutter", "快门 未知"), ("arm", "探测臂 —"), ("seq", "序列 空闲")],
                                            action=("关闭快门", self.close_shutter))
        self.statusbar.pack(side="bottom", fill="x")
        tooltip(self.statusbar.action_button, TIPS["close_shutter"])
        outer = ttk.Frame(self, style="TFrame")
        outer.pack(fill="both", expand=True)
        self.sidebar = ui_theme.Sidebar(outer, [(k, label, "%s · Ctrl+%d / ⌘%d" % (hint, i + 1, i + 1)) for i, (k, label, hint) in enumerate(self.PAGES)],
                                        self.show_page, brand="OptiComp2", brand_caption="v%s%s" % (__version__, " · 演示模式" if self.demo else ""))
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.add_footer_item("log", "▤  日志", lambda: self.toggle_log(), hint="显示/收起日志抽屉 · Ctrl+L / ⌘L")
        content = ttk.Frame(outer, style="TFrame")
        content.pack(side="left", fill="both", expand=True)
        content.columnconfigure(0, weight=1)
        content.rowconfigure(1, weight=1)
        if self.demo:
            self._build_demo_banner(content)
        self.paned = ttk.PanedWindow(content, orient="vertical")
        self.paned.grid(row=1, column=0, sticky="nsew")
        self.pages_frame = ttk.Frame(self.paned, style="TFrame")
        self.paned.add(self.pages_frame, weight=1)
        self.pages_frame.columnconfigure(0, weight=1)
        self.pages_frame.rowconfigure(0, weight=1)
        self.log_drawer = ui_theme.LogDrawer(self.paned, on_clear=self.clear_log, on_save=self.save_log,
                                             on_hide=lambda: self.toggle_log(False), theme=self.theme)
        self.log = self.log_drawer.text

        self.pages = {}
        self.pages["motors"] = self._build_motors_page(self.pages_frame)
        self.pages["instrument"] = self._build_instrument_page(self.pages_frame)
        from spectro_panel import SpectrometerPanel
        self.spectro = SpectrometerPanel(self.pages_frame, self)
        self.pages["spectro"] = self.spectro
        if self.demo:
            self.spectro.spec_factory = self._demo_spec_factory
        from sequence_panel import SequencePanel
        self.sequence = SequencePanel(self.pages_frame, self)
        self.pages["measure"] = self.sequence
        from analysis_panel import AnalysisPanel
        self.analysis = AnalysisPanel(self.pages_frame, self)
        self.pages["analysis"] = self.analysis
        for p in self.pages.values():
            p.grid(row=0, column=0, sticky="nsew")
        self.show_page("instrument")

    def _build_demo_banner(self, parent):
        import demo_hw
        self.demo_banner = Banner(parent, tone="accent", closable=False)
        self.demo_banner.grid(row=0, column=0, sticky="ew", padx=(SPACE["xl"], SPACE["xl"]), pady=(SPACE["md"], 0))
        self.demo_banner.show("演示模式 · 所有硬件均为模拟，不会与仪器通信 · 数据写入 %s/" % _display_path(self.data_root))
        acts = self.demo_banner.actions
        ttk.Label(acts, text="模拟样品", style="Banner.Accent.TLabel").pack(side="left", padx=(0, SPACE["xs"]))
        self.demo_sample_var = tk.StringVar(value="白板")
        cb = ttk.Combobox(acts, textvariable=self.demo_sample_var, values=["白板", "硅片"], state="readonly", width=5)
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda _e: self._demo_sample_changed())
        ttk.Button(acts, text="关闭横幅", command=self.demo_banner.hide, style="Banner.Accent.TButton").pack(side="left", padx=(SPACE["sm"], 0))
        self._demo_samples = {"白板": "white", "硅片": "si"}
        self._demo_sample_key = "white"

    def _demo_sample_changed(self):
        key = self._demo_samples.get(self.demo_sample_var.get(), "white")
        self._demo_sample_key = key
        if self._demo_spec is not None:
            self._demo_spec.sample = key
        self._log_line("DEMO sample -> %s" % key)

    def _demo_spec_factory(self, log):
        # runs on the SpectroWorker thread: no Tk calls here (the sample key is mirrored by _demo_sample_changed)
        import demo_hw
        self._demo_spec = demo_hw.DemoSpec(self._demo_bus, log=log, fast=self.demo_fast, sample=self._demo_sample_key)
        return self._demo_spec

    def _seed_demo(self):
        try:
            import demo_hw
            made = demo_hw.seed_demo_data(self.data_root, self.state_path, log=lambda t: None)
            if made:
                self._log_line("DEMO seeded %s" % ", ".join(os.path.basename(m) for m in made))
            # every demo launch starts from a clean recorded state (the fresh DemoBus is the reference), so the
            # stage-state warning on connect only appears when an anomaly is injected (screenshot station 11)
            bus = getattr(self, "_demo_bus", None)
            if bus is not None:
                stagestate.record(bus, self.state_path, note="demo start", ppd={a: demo_hw.PPD for a in (cfg.POLARISER, cfg.SYSTEM, cfg.SAMPLE)})
            if hasattr(self, "analysis"):
                self.analysis.refresh_sessions()
        except Exception as e:
            self._log_line("!! demo seed failed: %s" % e)

    def _build_instrument_page(self, parent):
        page = ttk.Frame(parent, style="Page.TFrame", padding=PAGE_PAD)
        page.columnconfigure(0, weight=1)
        page.rowconfigure(4, weight=1)                     # cards keep their natural height; slack goes below
        self.instrument_header = ui_theme.PageHeader(page, "仪器", "连接串口与光谱仪，检查电机状态；每天从这里开始。",
                                                     actions=[("查询全部信息", self.query_all)])
        self.instrument_header.grid(row=0, column=0, sticky="ew", pady=(0, SPACE["lg"]))
        tooltip(self.instrument_header.buttons["查询全部信息"], TIPS["query_all"])

        top = ttk.Frame(page, style="Page.TFrame")
        top.grid(row=1, column=0, sticky="ew")
        top.columnconfigure(0, weight=3)
        top.columnconfigure(1, weight=2)
        # ---- connection card
        conn = Card(top, title="连接", subtitle="串口（Elliptec 总线）与 B&W Tek 光谱仪")
        conn.grid(row=0, column=0, sticky="nsew", padx=(0, SPACE["md"]))
        cb = conn.body
        self.port_var = tk.StringVar(value="DEMO" if self.demo else DEFAULT_PORT)
        e_port = ttk.Entry(cb, textvariable=self.port_var, width=8)
        bind_enter(e_port, self.connect)
        # the pair is narrower than the 6-char default so that the row (entry, two buttons, status
        # pill) and the quick-start card still fit side by side at the 1100 px minimum width
        self.btn_connect = ttk.Button(cb, text="连接", command=self.connect, style="Primary.TButton", width=4)
        self.btn_disconnect = ttk.Button(cb, text="断开", command=self.disconnect, state="disabled", width=4)
        tooltip(self.btn_connect, TIPS["connect"])
        tooltip(self.btn_disconnect, TIPS["disconnect"])
        self.conn_var = tk.StringVar(value="未连接")
        self.conn_pill = StatusPill(cb, text="未连接", tone="neutral", on_card=True)
        form_row(cb, 0, "串口", e_port, self.btn_connect, self.btn_disconnect, self.conn_pill, label_width=6)
        self.spec_pill = StatusPill(cb, text="未初始化", tone="neutral", on_card=True)
        self.btn_init_mirror = ttk.Button(cb, text="初始化光谱仪", command=lambda: self.spectro.open_dev())
        b_goto = ttk.Button(cb, text="光谱仪页 ›", style="Ghost.TButton", command=lambda: self.show_page("spectro"))
        form_row(cb, 1, "光谱仪", self.spec_pill, self.btn_init_mirror, b_goto, label_width=6)
        self.it_label = ttk.Label(cb, text="—", style="Card.TLabel")
        form_row(cb, 2, "积分时间", self.it_label, label_width=6)
        ttk.Label(cb, text="连接时会与上次记录的电机位置比对（%s）。" % _display_path(self.state_path),
                  style="Card.Caption.TLabel", wraplength=420, justify="left").grid(row=3, column=0, columnspan=6, sticky="w", pady=(SPACE["sm"], 0))
        # ---- quick start card
        quick = Card(top, title="快速开始", subtitle="典型的一天：① → ② → ③ → ④（白板）→ 换样品 → ④ → ⑤")
        quick.grid(row=0, column=1, sticky="nsew")
        qb = quick.body
        qb.columnconfigure(0, weight=1)
        self.quick_pills = {}
        steps = [("bus", "① 连接串口", ("连接", self.connect)),
                 ("spec", "② 初始化光谱仪", ("初始化", lambda: self.spectro.open_dev())),
                 ("it", "③ 设置或自动定标积分时间", ("光谱仪页 ›", lambda: self.show_page("spectro"))),
                 ("seq", "④ 排队并运行序列", ("测量页 ›", lambda: self.show_page("measure"))),
                 ("ana", "⑤ 分析结果", ("分析页 ›", lambda: self.show_page("analysis")))]
        self.quick_buttons = {}
        for i, (key, text, (btxt, bcmd)) in enumerate(steps):
            ttk.Label(qb, text=text, style="Card.TLabel").grid(row=i, column=0, sticky="w", pady=3)
            p = StatusPill(qb, text="未完成", tone="neutral", dot=False, on_card=True)
            p.grid(row=i, column=1, sticky="w", padx=SPACE["md"])
            self.quick_pills[key] = p
            st = "Ghost.TButton" if btxt.endswith("›") else "TButton"
            b = ttk.Button(qb, text=btxt, command=bcmd, style=st)
            b.grid(row=i, column=2, sticky="e")
            self.quick_buttons[key] = b
        # ---- health banner (mirrors the connect-time warning)
        self.health_banner = Banner(page, tone="danger", closable=True)
        self.health_banner.grid(row=2, column=0, sticky="ew", pady=(SPACE["md"], 0))
        self.health_banner.hide()
        # ---- bottom row: motor status table (left) beside the notes (right). Side by side, the page's
        # natural height stays under the 720 px minimum even with the health banner shown, so the
        # table is never squeezed (a hidden fourth row is exactly what the anomaly warning is about).
        bottom = ttk.Frame(page, style="Page.TFrame")
        bottom.grid(row=3, column=0, sticky="ew", pady=(SPACE["md"], 0))
        bottom.columnconfigure(0, weight=3)
        bottom.columnconfigure(1, weight=2)
        bottom.rowconfigure(0, weight=1)
        motors = Card(bottom, title="电机状态", subtitle="尚未连接")
        motors.grid(row=0, column=0, sticky="nsew", padx=(0, SPACE["md"]))
        self.motors_card = motors
        mb = motors.body
        mb.columnconfigure(0, weight=1)
        cols = ("addr", "name", "pos", "status", "vel")
        self.stage_tree = ttk.Treeview(mb, columns=cols, show="headings", height=4, selectmode="none", takefocus=0)
        for c, w, t, a in zip(cols, (48, 170, 110, 110, 56), ("模块", "名称", "位置", "状态", "速度"), ("center", "w", "w", "w", "w")):
            self.stage_tree.heading(c, text=t, anchor=a)
            self.stage_tree.column(c, width=self.theme.px(w), anchor=a, stretch=(c == "name"))
        self.stage_tree.grid(row=0, column=0, sticky="ew")
        self.stage_tree.tag_configure("odd", background=COLORS["row_alt"])
        self.stage_tree.tag_configure("danger", foreground=COLORS["danger_pressed"])
        for i, d in enumerate(DEVICES):
            self.stage_tree.insert("", "end", iid=d["addr"], values=(d["addr"], d["short"], "—", "—", "—"), tags=("odd",) if i % 2 else ("even",))
        # shown under the (dashed) rows until the first readings arrive; refresh_status() grid_remove()s it
        self.stage_empty = empty_state(mb, "尚未连接。", "连接后自动读取；也可点右上「查询全部信息」。")
        self.stage_empty.grid(row=1, column=0)
        # ---- notes card (right of the table)
        notes = Card(bottom, title="注意事项",
                     actions=[("打开数据目录", lambda: self._open_folder(self.data_root), "Ghost.TButton"),
                              ("打开日志目录", lambda: self._open_folder(os.path.dirname(self.autolog_path)), "Ghost.TButton")])
        notes.grid(row=0, column=1, sticky="nsew")
        self.notes_card = notes
        nb = notes.body
        nb.columnconfigure(0, weight=1)
        wrapped = []
        for i, t in enumerate(("• 探测臂（模块 2）带光纤，不要回零；位置由传感器保持，直接用预设即可。",
                               "• 探测臂离开零位时不要插拔集线器上的任何线：总线由 USB 供电，模块上电会自动回零。",
                               "• 光谱仪读数 −99 或设备数 0 时，用「光谱仪 › 恢复连接」重启其 USB 设备。")):
            l = ttk.Label(nb, text=t, style="Card.TLabel", wraplength=900, justify="left")
            l.grid(row=i, column=0, sticky="w", pady=1)
            wrapped.append(l)
        caption = ttk.Label(nb, text="数据目录 %s/ · 本次日志 %s" % (_display_path(self.data_root), _display_path(self.autolog_path)),
                            style="Card.Caption.TLabel", justify="left", wraplength=900)
        caption.grid(row=3, column=0, sticky="w", pady=(SPACE["sm"], 0))
        wrapped.append(caption)
        # long lines wrap to the card width instead of being clipped in a narrow window
        nb.bind("<Configure>", lambda e: [l.configure(wraplength=max(self.theme.px(200), e.width - self.theme.px(4))) for l in wrapped])
        return page

    def _build_motors_page(self, parent):
        page = ttk.Frame(parent, style="Page.TFrame", padding=PAGE_PAD)
        page.columnconfigure(0, weight=1)
        self.motors_header = ui_theme.PageHeader(page, "电机与快门", "手动控制四个 Elliptec 模块。序列运行期间这里的操作会被拒绝。",
                                                 actions=[("查询全部信息", self.query_all)])
        self.motors_header.grid(row=0, column=0, sticky="ew", pady=(0, SPACE["lg"]))
        tooltip(self.motors_header.buttons["查询全部信息"], TIPS["query_all"])
        body = ttk.Frame(page, style="Page.TFrame")
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        self.panels = []
        for i, spec in enumerate(DEVICES):
            p = DevicePanel(body, self, spec)
            p.grid(row=i // 2, column=i % 2, sticky="nsew", padx=(0, SPACE["md"]) if i % 2 == 0 else 0, pady=(0, SPACE["md"]))
            self.panels.append(p)
        self.raw_disclosure = Disclosure(page, title="高级：原始命令", opened=False, on_card=False)
        self.raw_disclosure.grid(row=2, column=0, sticky="ew")
        raw = Card(self.raw_disclosure.body, padding=SPACE["md"])
        raw.grid(row=0, column=0, sticky="ew")
        rb = raw.body
        self.raw_var = tk.StringVar()
        e = ttk.Entry(rb, textvariable=self.raw_var, width=22, font=self.theme.font("mono"))
        bind_enter(e, self.send_raw)
        self.btn_raw = ttk.Button(rb, text="发送", command=self.send_raw)
        tooltip(self.btn_raw, TIPS["raw"])
        form_row(rb, 0, "命令", e, self.btn_raw, label_width=6)
        ttk.Label(rb, text="首字符地址、随后两字符命令、其余数据（例如 2gs、0in、3ma00011E00）；回复在日志中解码显示。",
                  style="Card.Caption.TLabel").grid(row=0, column=4, sticky="w", padx=(SPACE["md"], 0))
        rb.columnconfigure(4, weight=1)
        return page

    # ---- navigation / drawer -------------------------------------------------
    def show_page(self, key):
        page = self.pages.get(key)
        if page is None:
            return
        page.tkraise()
        self.current_page = key
        self.sidebar.select(key, notify=False)

    def toggle_log(self, show=None):
        if show is None:
            show = not self._log_visible
        if show and not self._log_visible:
            self.paned.add(self.log_drawer, weight=0)
            self._log_visible = True
            self.log_unread_errors = 0
            self.sidebar.set_badge("log", None)
            self.sidebar.set_footer_active("log", True)
            self.after_idle(self._restore_log_height)
        elif not show and self._log_visible:
            try:
                self._log_height = max(120, self.log_drawer.winfo_height())
            except tk.TclError:
                pass
            self.paned.forget(self.log_drawer)
            self._log_visible = False
            self.sidebar.set_footer_active("log", False)

    def _restore_log_height(self):
        try:
            self.update_idletasks()
            total = self.paned.winfo_height()
            if total > 0:
                self.paned.sashpos(0, max(120, total - self._log_height))
        except tk.TclError:
            pass

    def _bind_keys(self):
        for i, (key, _label, _hint) in enumerate(self.PAGES):
            bind_shortcut(self, str(i + 1), lambda k=key: self.show_page(k))
        bind_shortcut(self, "l", lambda: self.toggle_log())
        bind_shortcut(self, "r", self._shortcut_run)
        bind_shortcut(self, "s", self.save_log, shift=True)
        bind_shortcut(self, "Escape", self._shortcut_abort, ctrl=False)
        bind_shortcut(self, "F5", self._shortcut_query, ctrl=False)

    def _shortcut_run(self):
        self.show_page("measure")
        self.sequence.run()

    def _shortcut_abort(self):
        if self.sequence.running and ui_theme.confirm_abort(self):
            self.sequence.abort_run()

    def _shortcut_query(self):
        if self.current_page in ("instrument", "motors"):
            self.query_all()

    def _open_folder(self, path):
        try:
            os.makedirs(path, exist_ok=True)
            if sys.platform.startswith("win"):
                os.startfile(path)                       # noqa: attribute exists on Windows only
            elif sys.platform == "darwin":
                import subprocess
                subprocess.Popen(["open", path])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            self._log_line("WARNING could not open %s: %s" % (path, e))

    # ---- connection --------------------------------------------------------
    def _open_bus(self, port):
        """Bus factory: the real Elliptec bus, or the demo bus (never COM4) in --demo mode."""
        if self.demo:
            if self.demo_anomaly:
                self._demo_bus.inject_anomaly(self.demo_anomaly)
            self._demo_bus.closed = False
            return self._demo_bus
        return ell.ElliptecBus(port, timeout=5.0, motion_timeout=60.0, log=self._log_serial)

    def connect(self):
        port = self.port_var.get().strip()
        try:
            self.bus = self._open_bus(port)
        except Exception as e:
            messagebox.showerror("串口打开失败", "%s\n\n请确认原 OptiComp 程序已关闭（COM 口独占）。" % e)
            return
        self.bus_port = port
        self.conn_var.set("已连接 %s" % port)
        self.btn_connect.config(state="disabled")
        self.btn_disconnect.config(state="normal")
        self._log_line("--- opened %s ---" % port)
        self._bus_health()
        self.refresh_status()

    def _bus_health(self):
        """Right after connecting: protect the fibre arm from home(), compare the modules with the
        last recorded state (a USB replug power-cycles the ELLB and the modules auto-home), set the
        configured speeds. Runs on the worker thread; the verdict is shown on the Tk thread."""
        bus = self.bus
        wlog = lambda t: self._log_serial("--", t)        # worker thread -> marshal to Tk

        def job():
            stagestate.protect(bus)
            ppd = {}
            for a in (cfg.POLARISER, cfg.SYSTEM, cfg.SAMPLE):
                info = bus.info(a)
                ppd[a] = float(info.pulses) / info.travel
            problems, live = stagestate.check(bus, self.state_path, ppd=ppd, log=wlog)
            stagestate.apply_velocities(bus, log=wlog)       # raises on failure -> job fails, nothing mirrored
            for a, pct in cfg.VELOCITY.items():
                if a in live and live[a].get("velocity") is not None:
                    live[a]["velocity"] = pct
            self.ppd = ppd
            return problems, live

        def done(res):
            problems, live = res
            self._log_line("stage state: " + ", ".join(
                "%s=%s%s" % (a, live[a].get("deg", live[a]["position"]), "" if live[a]["status"] == 0 else "/GS%02X" % live[a]["status"])
                for a in sorted(live)))
            for a in sorted(live):
                self.update_module(a, pulses=live[a]["position"], status=live[a]["status"], velocity=live[a].get("velocity"),
                                   deg=live[a].get("deg"), source="bus health")
            self.health_problems = list(problems)
            self.health_done_count += 1
            if problems:
                lost = stagestate.arm_reference_lost(live)
                advice = ("探测臂(2)回零失败：零位已丢失，理顺光纤后在旁边盯着执行回零，再移到 44°。" if lost else
                          "可能原因：USB 插拔/供电抖动使总线掉电，模块上电自动回零；或有人手动转动。请核对后再运动。")
                self.health_banner.show("连接后发现模块与上次记录不一致：\n%s\n%s" % ("\n".join("• " + p for p in problems), advice), "danger")
                self.refresh_status()
                messagebox.showwarning("电机状态异常", "连接后发现模块与上次记录不一致：\n\n%s\n\n%s" % (
                    "\n".join("• " + p for p in problems), advice))
            else:
                self.health_banner.hide()
                self.refresh_status()
        self.worker.submit("bus health", job, done)

    def disconnect(self):
        if self.sequence_running:
            messagebox.showwarning("序列运行中", "请先中止序列")
            return
        if self.bus:
            try:
                stagestate.record(self.bus, self.state_path, note="gui disconnect", ppd=getattr(self, "ppd", None))
                self._log_line("stage state recorded")
            except Exception as e:
                self._log_line("WARNING could not record stage state: %s" % e)
            self.bus.close()
            self.bus = None
        self.bus_port = None
        self.conn_var.set("未连接")
        self.btn_connect.config(state="normal")
        self.btn_disconnect.config(state="disabled")
        self.stage_status = {}
        self.stage_vel = {}
        self._log_line("--- port closed ---")
        self.refresh_status()

    def query_all(self):
        for p in self.panels:
            p.do_info()
            p.do_status()
            p.do_position()

    # motion wrappers so panels can pass a bound bus method lazily
    def bus_forward(self, addr):
        return self.bus.forward(addr)

    def bus_backward(self, addr):
        return self.bus.backward(addr)

    def bus_home(self, addr):
        return self.bus.home(addr, 0, force=True)      # the panel has already asked the operator

    # ---- GUI-side state model (Tk thread only) --------------------------------
    def _ppd_of(self, addr):
        addr = str(addr)
        try:
            p = self.panels[int(addr)]
            if p.addr == addr and p.ppd:
                return p.ppd
        except (ValueError, IndexError, AttributeError):
            pass
        return (self.ppd or {}).get(addr)

    def update_module(self, addr, pulses=None, status=None, velocity=None, deg=None, source=""):
        addr = str(addr)
        if pulses is not None:
            if addr == cfg.SHUTTER:
                self.stage_deg[addr] = float(pulses)                 # ELL6: mm
            else:
                ppd = self._ppd_of(addr)
                if ppd:
                    self.stage_deg[addr] = (pulses / ppd) % 360.0
        if deg is not None:
            self.stage_deg[addr] = float(deg)
        if status is not None:
            self.stage_status[addr] = int(status)
        if velocity is not None:
            self.stage_vel[addr] = velocity
            panels = getattr(self, "panels", ())
            idx = int(addr) if addr.isdigit() else -1        # hex addresses (raw commands) have no card
            if 0 <= idx < len(panels):
                panels[idx].sync_velocity(velocity)
        if addr == cfg.SHUTTER and pulses is not None:
            self.set_shutter("closed" if int(pulses) == 0 else "open", source or "position")
        self.stage_updated = time.time()
        self.refresh_status()

    def set_shutter(self, state, source=""):
        if state not in ("unknown", "open", "closed", "moving"):
            state = "unknown"
        if state != self.shutter_state:
            self.shutter_state = state
            self._log_line("SHUTTER %s%s" % (state, " (%s)" % source if source else ""))
            self.refresh_status()

    def close_shutter(self):
        """Global 'close shutter' (status bar). During a sequence the Runner owns the shutter: offer to abort."""
        if self.sequence_running:
            if messagebox.askyesno("序列运行中", "序列运行期间不能单独操作快门。\n中止序列后，Runner 会自动关闭快门。\n\n现在中止序列？"):
                self.sequence.abort_run()
            return
        self.panels[0].motion("bw", self.bus_backward)   # reuses the shutter card's action (submit() guards)

    def _tick_status(self):
        try:
            self.refresh_status()
        except Exception as e:                             # the ticker must survive anything
            self._log_line("!! status refresh error: %s" % e)
        self.after(500, self._tick_status)

    def refresh_status(self):
        sb = getattr(self, "statusbar", None)
        if sb is None or any(getattr(self, k, None) is None for k in ("spectro", "sequence", "analysis", "stage_tree")):
            return
        if self.bus is None:
            sb.set("port", "串口 未连接", "neutral")
        else:
            sb.set("port", "串口 %s" % self.bus_port, "success")
        sp = self.spectro
        state = sp.state_var.get()
        if state.startswith("已连接") or state.startswith("已恢复"):
            spec_text, spec_tone = state.split(",")[0].split(" (")[0], "success"
        elif state.endswith("中…"):
            spec_text, spec_tone = state, "warning"
        elif state.startswith("初始化失败"):
            spec_text, spec_tone = state, "danger"
        else:
            spec_text, spec_tone = state, "neutral"
        sb.set("spec", "光谱仪 %s" % spec_text, spec_tone)
        spec = sp.spec
        if spec is None or not getattr(spec, "integration_ms", None):
            sb.set("it", "积分时间 —", "neutral")
        elif sp.it_chosen:
            sb.set("it", "积分时间 %d ms" % spec.integration_ms, "success")
        else:
            sb.set("it", "积分时间 %d ms（未确认）" % spec.integration_ms, "warning")
        text, tone = {"unknown": ("快门 未知", "neutral"), "open": ("快门 已打开", "danger"),
                      "closed": ("快门 已关闭", "success"), "moving": ("快门 运动中", "warning")}[self.shutter_state]
        sb.set("shutter", text, tone)
        deg, code = self.stage_deg.get(cfg.SYSTEM), self.stage_status.get(cfg.SYSTEM)
        if deg is None:
            sb.set("arm", "探测臂 —" if not code else "探测臂 GS%02X" % code, "danger" if code else "neutral")
        else:
            sb.set("arm", "探测臂 %.2f°%s" % (deg, " · GS%02X" % code if code else ""), "danger" if code else "neutral")
        seq = self.sequence
        prog = seq.prog_var.get()
        if seq.running:
            if prog.startswith("中止中"):
                sb.set("seq", "序列 中止中…", "warning")
            elif prog.startswith("等待操作"):
                sb.set("seq", "序列 等待操作…", "warning")
            else:
                try:
                    i, n = int(seq.bar["value"]), int(seq.bar["maximum"])
                except (tk.TclError, ValueError):
                    i, n = 0, 0
                sb.set("seq", "序列 运行中 %d/%d" % (i, n), "accent")
        elif prog.startswith("完成"):
            self._seq_done_once = True
            sb.set("seq", "序列 " + prog.split(":")[0], "success")
        elif prog.startswith("已中止"):
            sb.set("seq", "序列 已中止", "warning")
        elif prog.startswith("失败"):
            sb.set("seq", "序列 失败", "danger")
        else:
            sb.set("seq", "序列 空闲", "neutral")
        self._refresh_instrument(spec_text, spec_tone)
        if self.sequence_running != self._locked:
            self._locked = self.sequence_running
            self._apply_lock(self._locked)

    def _refresh_instrument(self, spec_text, spec_tone):
        conn = self.conn_var.get()
        self.conn_pill.set(conn, "success" if conn.startswith("已连接") else "neutral")
        self.spec_pill.set(spec_text, spec_tone)
        sp = self.spectro
        spec = sp.spec
        if spec is None or not getattr(spec, "integration_ms", None):
            self.it_label.configure(text="—")
        else:
            self.it_label.configure(text="%d ms%s" % (spec.integration_ms, "" if sp.it_chosen else "（默认值，未确认）"))
        try:
            st = str(sp.btn_open["state"])
        except tk.TclError:
            st = "normal"
        self.btn_init_mirror.configure(state=st)
        self.quick_buttons["spec"].configure(state=st)
        self.quick_buttons["bus"].configure(state=str(self.btn_connect["state"]))
        done = {"bus": self.bus is not None, "spec": spec is not None, "it": spec is not None and sp.it_chosen,
                "seq": self._seq_done_once, "ana": bool(getattr(self.analysis, "results", None))}
        for key, p in self.quick_pills.items():
            if key == "seq" and self.sequence.running:
                p.set("运行中", "accent")
            else:
                p.set("已完成" if done[key] else "未完成", "success" if done[key] else "neutral")
        # motor table
        for d in DEVICES:
            a = d["addr"]
            deg, code, vel = self.stage_deg.get(a), self.stage_status.get(a), self.stage_vel.get(a)
            if deg is None:
                pos = "—"
            elif a == cfg.SHUTTER:
                pos = "%s (%d mm)" % ("已关闭" if deg == 0 else "已打开", deg)
            else:
                pos = "%.3f°" % deg
            status = "—" if code is None else "%02X %s" % (code, STATUS_ZH.get(code, ell.STATUS_CODES.get(code, "?")))
            velocity = "—" if vel is None else "%d %%" % vel
            tags = ["odd" if int(a) % 2 else "even"]
            if code:
                tags.append("danger")
            self.stage_tree.item(a, values=(a, d["short"], pos, status, velocity), tags=tuple(tags))
        if self.bus is None and not self.stage_deg:
            self.motors_card.set_subtitle("尚未连接")
            self.stage_empty.grid()
        else:
            self.stage_empty.grid_remove()
            if self.stage_updated:
                self.motors_card.set_subtitle("更新于 %s%s" % (time.strftime("%H:%M:%S", time.localtime(self.stage_updated)),
                                                              "" if self.bus is not None else " · 已断开（显示最后读数）"))

    def _apply_lock(self, locked):
        for p in self.panels:
            p.set_locked(locked)
        self.btn_raw.configure(state="disabled" if locked else "normal")
        for h in (self.instrument_header, self.motors_header):
            h.buttons["查询全部信息"].configure(state="disabled" if locked else "normal")
        self.motors_header.set_subtitle("序列运行中，手动操作已锁定。" if locked else "手动控制四个 Elliptec 模块。序列运行期间这里的操作会被拒绝。")
        self.instrument_header.set_subtitle("连接串口与光谱仪，检查电机状态；每天从这里开始" + (" · 序列运行中" if locked else "。"))
        for panel in (self.spectro, self.sequence):
            if hasattr(panel, "set_locked"):
                panel.set_locked(locked)

    # ---- jobs --------------------------------------------------------------
    def submit(self, label, fn, callback=None):
        if self.bus is None:
            messagebox.showwarning("未连接", "请先连接串口")
            return
        if self.sequence_running:
            messagebox.showwarning("序列运行中", "序列执行期间禁止手动操作电机，请先中止序列")
            return
        self.worker.submit(label, fn, callback)

    def _drain_results(self):
        try:
            while True:
                status, label, value, callback = self.results.get_nowait()
                if status == "ok":
                    if callback:
                        try:
                            callback(value)
                        except Exception as e:
                            self._log_line("!! callback error for %s: %s" % (label, e))
                else:
                    self._log_line("!! %s failed: %s" % (label, value))
                    self._on_job_failed(label)
                    messagebox.showerror("命令失败", "%s\n%s" % (label, value))
        except queue.Empty:
            pass

    def _poll_results(self):
        self._drain_results()
        self.after(100, self._poll_results)

    def _on_job_failed(self, label):
        """Keep the GUI-side state honest after a failed job (a stuck '运动中…' or a shutter that may have moved)."""
        parts = label.split(" ", 2)
        if len(parts) < 2:
            return
        addr, cmd = parts[0], parts[1]
        if addr == "raw" and len(cmd) >= 3:
            addr, cmd = cmd[0], cmd[1:3]
        for p in self.panels:
            if p.addr == addr and p.stat_var.get().startswith("状态: 运动中"):
                p.stat_var.set("状态: 失败")
        if addr == cfg.SHUTTER and cmd in ("fw", "bw", "ho0", "ho", "ma", "mr"):
            self.set_shutter("unknown", "%s %s failed" % (addr, cmd))

    def send_raw(self):
        txt = self.raw_var.get().strip()
        if len(txt) < 3:
            return
        addr, cmd, data = txt[0], txt[1:3], txt[3:]
        if not self._log_visible:
            self.toggle_log(True)                          # the reply is only visible in the log

        def done(r):
            self._log_line("   decoded: %s" % r)
            self._after_raw(addr, cmd.lower(), r)
        self.submit("raw %s" % txt, lambda: self.bus.query(addr, cmd, data), done)

    def _after_raw(self, addr, cmd, r):
        kind = r.get("kind") if isinstance(r, dict) else None
        if kind == "PO" and r.get("value") is not None:
            self.update_module(addr, pulses=r["value"], source="raw %s%s" % (addr, cmd))
        elif kind == "GS" and cmd == "gs":
            self.update_module(addr, status=r.get("code"))
        elif kind == "GV":
            self.update_module(addr, velocity=r.get("percent"))
        elif addr == cfg.SHUTTER and cmd not in ("in", "gs", "gv", "gj", "i1", "i2"):
            self.set_shutter("unknown", "raw %s%s" % (addr, cmd))

    # ---- logging -----------------------------------------------------------
    @staticmethod
    def _log_tag(text):
        if text.startswith("!!"):
            return "error"
        if "WARNING" in text or "STAGE STATE" in text or "AUTOPILOT" in text:
            return "warning"
        if text.startswith("TX "):
            return "tx"
        if text.startswith("RX "):
            return "rx"
        if text.startswith(("SEQ ", "SPEC ", "ANALYSIS ", "SHUTTER ", "DEMO ", "---")):
            return "event"
        return None

    def _log_serial(self, direction, text):
        # called from the worker thread -> marshal to Tk thread
        self.results.put(("ok", "log", None, lambda _v, d=direction, t=text: self._log_line("%s %s" % (d, t))))

    def _log_line(self, text):
        line = "%s %s" % (time.strftime("%H:%M:%S"), text)
        self.log_lines.append(line)
        try:
            self.autolog.write(line + "\n")
            self.autolog.flush()
        except Exception:
            pass
        tag = self._log_tag(text)
        try:
            self.log_drawer.append(line, tag)
        except tk.TclError:
            return
        if tag == "error" and not self._log_visible:
            self.log_unread_errors += 1
            self.sidebar.set_badge("log", self.log_unread_errors)

    def clear_log(self):
        self.log_lines = []
        self.log_drawer.clear()

    def save_log(self):
        path = filedialog.asksaveasfilename(defaultextension=".log", initialfile=time.strftime("manual_%Y%m%d_%H%M%S.log"))
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(self.log_lines) + "\n")

    def _on_close(self):
        """Orderly exit: abort a running sequence, close the shutter and record the stage state
        on the hardware worker (bounded wait), close the spectrometer on its own worker, then
        close the port. The DLL and the serial port are never touched from the Tk thread while a
        worker may be inside them."""
        if self.sequence_running:
            if not messagebox.askyesno("序列运行中", "序列仍在运行。仍要退出？（将请求中止并关闭快门）"):
                return
            self.sequence.abort.set()
        self.spectro.worker.live.clear()
        self.spectro._stop_monitor()
        if self.bus is not None:
            bus, done = self.bus, threading.Event()

            def job():
                try:
                    bus.backward(cfg.SHUTTER)
                    self._log_serial("--", "shutter closed on exit")     # queue, never Tk from the worker
                    stagestate.record(bus, self.state_path, note="gui exit", ppd=getattr(self, "ppd", None))
                finally:
                    done.set()
            self.worker.submit("exit: shutter close + state record", job)
            if not done.wait(30.0):
                self._log_line("!! exit: hardware worker busy for 30 s; shutter state unknown - check 0bw")
            else:
                self.set_shutter("closed", "exit")
            self._drain_results()
        self.sequence_running = False
        self.spectro.shutdown(timeout=5.0)
        if self.bus is not None:
            try:
                self.bus.close()
            except Exception:
                pass
            self.bus = None
        try:
            self.autolog.close()
        except Exception:
            pass
        self.destroy()


# ---- screenshot / demo tour -------------------------------------------------------
def install_autopilot(app):
    """Replace the tkinter.messagebox dialogs with auto-answers (yes/ok) that are recorded in the log,
    so the guard chain runs unattended in --screenshot mode."""
    import tkinter.messagebox as mb

    def make(name, ret):
        def fn(title=None, message=None, **kw):
            app._log_line("AUTOPILOT %s [%s]: %s" % (name, title, (message or "").replace("\n", " ")))
            return ret
        return fn
    for name, ret in (("showinfo", "ok"), ("showwarning", "ok"), ("showerror", "ok"), ("askyesno", True), ("askokcancel", True),
                      ("askyesnocancel", True), ("askretrycancel", True), ("askquestion", "yes")):
        setattr(mb, name, make(name, ret))
    return mb


def _mac_can_record_screen():
    try:
        import ctypes
        cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        return bool(cg.CGPreflightScreenCaptureAccess())
    except Exception:
        return True


def capture(root, path, log=None):
    """Save a PNG of the window. macOS: screencapture (needs the Screen Recording permission; without it
    the PNG would only contain the wallpaper, so a geometry-faithful synthetic render is written instead).
    Windows/Linux: PIL.ImageGrab. Returns 'screen' | 'render' | None."""
    log = log or (lambda t: None)
    root.update_idletasks()
    root.update()
    time.sleep(0.4)
    root.update()
    x, y, w, h = root.winfo_rootx(), root.winfo_rooty(), root.winfo_width(), root.winfo_height()
    if sys.platform == "darwin":
        if _mac_can_record_screen():
            import subprocess
            r = subprocess.run(["screencapture", "-x", "-R", "%d,%d,%d,%d" % (x, y, w, h), path])
            if r.returncode == 0 and os.path.exists(path):
                return "screen"
            try:                                               # macOS 15: -R may fail; grab the screen and crop
                from PIL import Image
                tmp = path + ".full.png"
                subprocess.run(["screencapture", "-x", tmp], check=True)
                im = Image.open(tmp)
                s = im.width / float(root.winfo_screenwidth())
                im.crop((int(x * s), int(y * s), int((x + w) * s), int((y + h) * s))).save(path)
                os.remove(tmp)
                return "screen"
            except Exception as e:
                log("WARNING screencapture failed (%s); writing a synthetic render" % e)
        else:
            log("WARNING macOS 未授予「屏幕录制」权限，PNG 为合成渲染（布局/几何真实，皮肤为近似）: %s" % os.path.basename(path))
    else:
        try:
            if sys.platform.startswith("win"):
                try:
                    import ctypes
                    ctypes.windll.user32.SetProcessDPIAware()
                except Exception:
                    pass
            from PIL import ImageGrab
            ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(path)
            return "screen"
        except Exception as e:
            log("WARNING ImageGrab failed (%s); writing a synthetic render" % e)
    try:
        import ui_render
        ui_render.render_window(root, path)
        return "render"
    except Exception as e:
        log("!! synthetic render failed: %s" % e)
        return None


class ScreenshotTour(object):
    """Drives the demo GUI through the whole workflow (connect -> spectrometer -> sequence -> analysis)
    and saves one PNG per station. Runs inside the Tk event loop with nested update() calls."""

    STATIONS = ["01_instrument_idle", "02_instrument_connected", "03_motors", "04_motors_advanced", "05_spectrometer",
                "06_measure_queue", "07_measure_running", "08_measure_done", "09_analysis", "10_log_drawer", "11_instrument_warning"]

    def __init__(self, app, outdir, capture_png=True, close=True, anomaly=None):
        self.app = app
        self.outdir = outdir
        self.capture_png = capture_png and outdir is not None
        self.close = close
        self.anomaly = anomaly or os.environ.get("OPTICOMP_DEMO_ANOMALY") or "arm"
        self.shots = []
        self.rc = 0

    def pump(self, seconds=0.0):
        t0 = time.time()
        while True:
            self.app.update()
            if time.time() - t0 >= seconds:
                return
            time.sleep(0.02)

    def wait_for(self, pred, timeout=10.0, what=""):
        t0 = time.time()
        while not pred():
            if time.time() - t0 > timeout:
                raise RuntimeError("timeout waiting for %s" % (what or pred))
            self.app.update()
            time.sleep(0.02)
        self.app.update()

    def shot(self, name):
        if not self.capture_png:
            self.pump(0.05)
            self.shots.append(name)
            return
        os.makedirs(self.outdir, exist_ok=True)
        path = os.path.join(self.outdir, name + ".png")
        how = capture(self.app, path, log=self.app._log_line)
        self.app._log_line("SHOT %s (%s)" % (path, how))
        self.shots.append(path)

    def run(self):
        app = self.app
        try:
            self._run()
        except Exception as e:
            app._log_line("!! screenshot tour failed: %s" % e)
            self.rc = 1
        finally:
            if self.close:
                try:
                    app._on_close()
                except Exception as e:
                    sys.stderr.write("tour close failed: %s\n" % e)
                    self.rc = 1
        return self.rc

    def _run(self):
        app = self.app
        sp, seq, ana = app.spectro, app.sequence, app.analysis
        app.show_page("instrument")
        self.pump(0.3)
        self.shot(self.STATIONS[0])
        n0 = app.health_done_count
        app.connect()
        self.wait_for(lambda: app.health_done_count > n0, 15, "bus health")
        sp.open_dev()
        self.wait_for(lambda: sp.spec is not None, 15, "spectrometer open")
        sp.it_var.set("997")
        sp.set_it()
        self.wait_for(lambda: sp.it_chosen, 10, "integration time")
        app.query_all()
        self.wait_for(lambda: all(p.ppd for p in app.panels if p.spec["kind"] == "rotation") and all(a in app.stage_deg for a in "0123"), 20, "query all")
        self.pump(0.2)
        self.shot(self.STATIONS[1])
        app.show_page("motors")
        self.pump(0.2)
        self.shot(self.STATIONS[2])
        app.panels[2].advanced.open()
        app.raw_disclosure.open()
        self.pump(0.2)
        self.shot(self.STATIONS[3])
        app.panels[2].advanced.close()
        app.raw_disclosure.close()
        app.show_page("spectro")
        sp.read_once()
        self.wait_for(lambda: sp.last is not None, 15, "single read")
        self.pump(0.5)
        self.shot(self.STATIONS[4])
        if app.demo and hasattr(app, "demo_sample_var"):
            app.demo_sample_var.set("硅片")
            app._demo_sample_changed()
        app.show_page("measure")
        seq.session_var.set("demo_si")
        seq.load_history()
        seq.add_reference()
        seq.add_dark()
        seq.start_var.set("8")
        seq.stop_var.set("80")
        seq.step_var.set("8")
        seq.add_scan()
        self.pump(0.2)
        self.shot(self.STATIONS[5])
        seq.run()
        self.wait_for(lambda: seq.running, 10, "sequence start")
        self.wait_for(lambda: float(seq.bar["value"]) >= 6 or not seq.running, 60, "sequence progress")
        self.pump(0.3)
        self.shot(self.STATIONS[6])
        self.wait_for(lambda: not seq.running, 300, "sequence end")
        self.pump(0.5)
        self.shot(self.STATIONS[7])
        app.show_page("analysis")
        ana.refresh_sessions()
        names = list(ana.sample_cb["values"])
        if "demo_si" in names:
            ana.sample_cb.set("demo_si")
        if "demo_white" in names:
            ana.ref_cb.set("demo_white")
        ana.std_cb.current(1)                    # the white plate is a (nearly) constant-R reference
        ana.const_var.set("0.99")
        ana.compute()
        self.pump(0.6)
        self.shot(self.STATIONS[8])
        app.toggle_log(True)
        self.pump(0.4)
        self.shot(self.STATIONS[9])
        app.toggle_log(False)
        app.demo_anomaly = self.anomaly
        app.disconnect()
        n0 = app.health_done_count
        app.connect()
        self.wait_for(lambda: app.health_done_count > n0, 15, "bus health (anomaly)")
        app.show_page("instrument")
        self.pump(0.3)
        self.shot(self.STATIONS[10])


def run_screenshot_tour(app, outdir, capture=True, close=True):
    """Spec 7.4: drive the demo through every station; returns the exit code (0 ok, 1 on timeout/failure)."""
    return ScreenshotTour(app, outdir, capture_png=capture, close=close).run()


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="OptiComp2 manual GUI")
    ap.add_argument("--demo", action="store_true", help="fake hardware (never opens COM4 or the spectrometer DLL)")
    ap.add_argument("--screenshot", metavar="DIR", default=None, help="with --demo: drive the GUI through the workflow and save PNGs to DIR")
    ap.add_argument("--no-capture", action="store_true", help="run the screenshot tour without writing PNGs")
    args = ap.parse_args(argv)
    if args.screenshot and not args.demo:
        ap.error("--screenshot requires --demo (never drives real hardware unattended)")
    app = App(demo=args.demo, screenshot_dir=args.screenshot)
    if args.screenshot:
        install_autopilot(app)
        app.geometry("1280x820+40+40")
        state = {"rc": 0}

        def start():
            state["rc"] = run_screenshot_tour(app, args.screenshot, capture=not args.no_capture)
        app.after(400, start)
        app.mainloop()
        return state["rc"]
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
