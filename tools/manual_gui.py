# -*- coding: utf-8 -*-
"""Manual hardware-control GUI for the OptiComp instrument (stages + shutter).

Step 1 of the OptiComp2 rewrite: nothing is automated here. Every button sends one
command, waits for the module to finish, and shows the decoded reply. All serial traffic
is logged (and can be saved). Original OptiComp is untouched; do not run both at once
because COM4 is exclusive.

Python 3.9 / Tk 8.6 / pyserial 3.5 compatible.
"""
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, scrolledtext

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from hw import elliptec as ell

DEFAULT_PORT = "COM4"

# Device roles on the OptiComp bus (from stageframework.py + thesis chapter 4).
DEVICES = [
    {"addr": "0", "name": "0  快门 Shutter (ELL6 滑块)", "kind": "slider"},
    {"addr": "1", "name": "1  偏振片 Polariser (ELL14)", "kind": "rotation",
     "presets": [("P 偏振 = 146° (POLOFFSET)", 146.0), ("S 偏振 = 236° (POLOFFSET+90)", 236.0), ("0°", 0.0)],
     "limits": None},
    {"addr": "2", "name": "2  探测臂/下台 System stage (ELL14) ⚠ 光纤", "kind": "rotation",
     "presets": [("零位 44° (SYSTEMOFFSET)", 44.0), ("DB 位 124°", 124.0), ("交换位 150°", 150.0)],
     "limits": (0.0, 200.0)},
    {"addr": "3", "name": "3  样品台/上台 Sample stage (ELL14)", "kind": "rotation",
     "presets": [("零位 103° (stageframework)", 103.0), ("VAR 0° = 105° (hardwaremanager)", 105.0),
                 ("DB 位 93°", 93.0), ("交换位 120°", 120.0), ("VAR 80° = 185°", 185.0)],
     "limits": (0.0, 200.0)},
]


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


class DevicePanel(ttk.LabelFrame):
    def __init__(self, master, app, spec):
        ttk.LabelFrame.__init__(self, master, text=spec["name"], padding=6)
        self.app = app
        self.spec = spec
        self.addr = spec["addr"]
        self.info = None
        self.ppd = None                      # pulses per degree (rotation only)

        self.info_var = tk.StringVar(value="(未查询)")
        self.pos_var = tk.StringVar(value="位置: ?")
        self.stat_var = tk.StringVar(value="状态: ?")
        ttk.Label(self, textvariable=self.info_var, foreground="#555").grid(row=0, column=0, columnspan=6, sticky="w")
        ttk.Label(self, textvariable=self.pos_var, font=("TkDefaultFont", 10, "bold")).grid(row=1, column=0, columnspan=3, sticky="w")
        ttk.Label(self, textvariable=self.stat_var).grid(row=1, column=3, columnspan=3, sticky="w")

        row = 2
        ttk.Button(self, text="信息 in", command=self.do_info).grid(row=row, column=0, sticky="ew")
        ttk.Button(self, text="状态 gs", command=self.do_status).grid(row=row, column=1, sticky="ew")
        ttk.Button(self, text="位置 gp", command=self.do_position).grid(row=row, column=2, sticky="ew")
        ttk.Button(self, text="回零 ho0", command=self.do_home).grid(row=row, column=3, sticky="ew")
        row += 1

        if spec["kind"] == "slider":
            ttk.Button(self, text="打开快门 fw", command=lambda: self.motion("fw", self.app.bus_forward)).grid(row=row, column=0, columnspan=2, sticky="ew")
            ttk.Button(self, text="关闭快门 bw", command=lambda: self.motion("bw", self.app.bus_backward)).grid(row=row, column=2, columnspan=2, sticky="ew")
        else:
            ttk.Label(self, text="绝对角度 °").grid(row=row, column=0, sticky="e")
            self.abs_var = tk.StringVar(value="")
            ttk.Entry(self, textvariable=self.abs_var, width=8).grid(row=row, column=1, sticky="ew")
            ttk.Button(self, text="移动到 ma", command=self.do_move_abs).grid(row=row, column=2, sticky="ew")
            ttk.Label(self, text="相对 °").grid(row=row, column=3, sticky="e")
            self.rel_var = tk.StringVar(value="1")
            ttk.Entry(self, textvariable=self.rel_var, width=6).grid(row=row, column=4, sticky="ew")
            ttk.Button(self, text="相对移动 mr", command=self.do_move_rel).grid(row=row, column=5, sticky="ew")
            row += 1
            ttk.Label(self, text="预设").grid(row=row, column=0, sticky="e")
            self.preset_cb = ttk.Combobox(self, values=[p[0] for p in spec["presets"]], state="readonly", width=30)
            self.preset_cb.current(0)
            self.preset_cb.grid(row=row, column=1, columnspan=3, sticky="ew")
            ttk.Button(self, text="到预设", command=self.do_preset).grid(row=row, column=4, columnspan=2, sticky="ew")
            row += 1
            ttk.Label(self, text="速度 %").grid(row=row, column=0, sticky="e")
            self.vel_var = tk.StringVar(value="100")
            ttk.Spinbox(self, from_=10, to=100, increment=10, textvariable=self.vel_var, width=6).grid(row=row, column=1, sticky="ew")
            ttk.Button(self, text="设速 sv", command=self.do_set_velocity).grid(row=row, column=2, sticky="ew")
            ttk.Button(self, text="读速 gv", command=self.do_get_velocity).grid(row=row, column=3, sticky="ew")
        for c in range(6):
            self.columnconfigure(c, weight=1)

    # ---- helpers -----------------------------------------------------------
    def _show_position(self, pulses):
        if pulses is None:
            return
        if self.ppd:
            self.pos_var.set("位置: %.3f°  (%d pulses)" % ((pulses / self.ppd) % 360.0, pulses))
        elif self.info and self.info.pulses_per_unit:
            self.pos_var.set("位置: %.3f mm  (%d pulses)" % (pulses / self.info.pulses_per_unit, pulses))
        else:
            self.pos_var.set("位置: %d pulses" % pulses)

    def _on_info(self, info):
        self.info = info
        if self.spec["kind"] == "rotation" and info.travel:
            self.ppd = float(info.pulses) / info.travel
        self.info_var.set("%s  SN %s  fw %s  travel %d  pulses %d" % (info.model_name, info.serial, info.fw, info.travel, info.pulses))

    def _need_ppd(self):
        if self.ppd is None:
            messagebox.showwarning("先查询信息", "请先点『信息 in』读取模块参数（脉冲/度），再做角度运动。")
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
        self.app.submit("%s gs" % self.addr, lambda: self.app.bus.status(self.addr),
                        lambda c: self.stat_var.set("状态: %02X %s" % (c, ell.STATUS_CODES.get(c, "?"))))

    def do_position(self):
        self.app.submit("%s gp" % self.addr, lambda: self.app.bus.position(self.addr), self._show_position)

    def do_home(self):
        if not messagebox.askyesno("回零", "模块 %s 将执行回零 (ho0)，会产生运动。继续？" % self.addr):
            return
        self.motion("ho0", self.app.bus_home)

    def motion(self, label, fn):
        self.stat_var.set("状态: 运动中…")
        self._t0 = time.time()
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

    def do_move_rel(self):
        if not self._need_ppd():
            return
        try:
            ddeg = float(self.rel_var.get())
        except ValueError:
            messagebox.showerror("输入错误", "请输入角度数值")
            return
        if abs(ddeg) > 30 and not messagebox.askyesno("大角度相对移动", "相对移动 %.2f°，继续？" % ddeg):
            return
        pulses = int(round(ddeg * self.ppd))
        self.stat_var.set("状态: 运动中… %+.2f°" % ddeg)
        self._t0 = time.time()
        self.app.submit("%s mr %+.2f°" % (self.addr, ddeg), lambda: self.app.bus.move_rel(self.addr, pulses), self._after_motion)

    def do_set_velocity(self):
        try:
            pct = int(self.vel_var.get())
        except ValueError:
            return
        self.app.submit("%s sv %d%%" % (self.addr, pct), lambda: self.app.bus.set_velocity(self.addr, pct), lambda _: self.do_get_velocity())

    def do_get_velocity(self):
        self.app.submit("%s gv" % self.addr, lambda: self.app.bus.velocity(self.addr),
                        lambda v: self.stat_var.set("状态: 速度 %s%%" % v))


class App(tk.Tk):
    def __init__(self):
        tk.Tk.__init__(self)
        self.title("OptiComp2 – 手动硬件控制 (步骤 1: 电机与快门)")
        self.bus = None
        self.results = queue.Queue()
        self.worker = HardwareWorker(self.results)
        self.worker.start()
        self.log_lines = []
        logdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs")
        os.makedirs(logdir, exist_ok=True)
        self.autolog_path = os.path.join(logdir, time.strftime("manual_%Y%m%d_%H%M%S.log"))
        self.autolog = open(self.autolog_path, "a")
        self._build()
        self._log_line("--- auto log: %s ---" % os.path.abspath(self.autolog_path))
        self.after(100, self._poll_results)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- layout ------------------------------------------------------------
    def _build(self):
        top = ttk.Frame(self, padding=6)
        top.pack(fill="x")
        ttk.Label(top, text="串口").pack(side="left")
        self.port_var = tk.StringVar(value=DEFAULT_PORT)
        ttk.Entry(top, textvariable=self.port_var, width=8).pack(side="left", padx=4)
        self.btn_connect = ttk.Button(top, text="连接", command=self.connect)
        self.btn_connect.pack(side="left")
        self.btn_disconnect = ttk.Button(top, text="断开", command=self.disconnect, state="disabled")
        self.btn_disconnect.pack(side="left", padx=4)
        ttk.Button(top, text="查询全部信息", command=self.query_all).pack(side="left", padx=4)
        self.conn_var = tk.StringVar(value="未连接")
        ttk.Label(top, textvariable=self.conn_var, foreground="#a00").pack(side="left", padx=10)

        body = ttk.Frame(self, padding=6)
        body.pack(fill="both", expand=True)
        self.panels = []
        for i, spec in enumerate(DEVICES):
            p = DevicePanel(body, self, spec)
            p.grid(row=i // 2, column=i % 2, sticky="nsew", padx=4, pady=4)
            self.panels.append(p)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)

        raw = ttk.LabelFrame(self, text="原始命令 (高级)", padding=6)
        raw.pack(fill="x", padx=6)
        ttk.Label(raw, text="例如 2gs / 0in / 3gj").pack(side="left")
        self.raw_var = tk.StringVar()
        e = ttk.Entry(raw, textvariable=self.raw_var, width=20)
        e.pack(side="left", padx=4)
        e.bind("<Return>", lambda _: self.send_raw())
        ttk.Button(raw, text="发送", command=self.send_raw).pack(side="left")

        logf = ttk.LabelFrame(self, text="日志 (TX/RX)", padding=6)
        logf.pack(fill="both", expand=True, padx=6, pady=6)
        self.log = scrolledtext.ScrolledText(logf, height=12, font=("Consolas", 9), state="disabled")
        self.log.pack(fill="both", expand=True)
        bar = ttk.Frame(logf)
        bar.pack(fill="x")
        ttk.Button(bar, text="清空", command=self.clear_log).pack(side="left")
        ttk.Button(bar, text="保存日志…", command=self.save_log).pack(side="left", padx=4)

    # ---- connection --------------------------------------------------------
    def connect(self):
        port = self.port_var.get().strip()
        try:
            self.bus = ell.ElliptecBus(port, timeout=5.0, motion_timeout=60.0, log=self._log_serial)
        except Exception as e:
            messagebox.showerror("串口打开失败", "%s\n\n请确认原 OptiComp 程序已关闭（COM 口独占）。" % e)
            return
        self.conn_var.set("已连接 %s" % port)
        self.btn_connect.config(state="disabled")
        self.btn_disconnect.config(state="normal")
        self._log_line("--- opened %s ---" % port)

    def disconnect(self):
        if self.bus:
            self.bus.close()
            self.bus = None
        self.conn_var.set("未连接")
        self.btn_connect.config(state="normal")
        self.btn_disconnect.config(state="disabled")
        self._log_line("--- port closed ---")

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
        return self.bus.home(addr, 0)

    # ---- jobs --------------------------------------------------------------
    def submit(self, label, fn, callback=None):
        if self.bus is None:
            messagebox.showwarning("未连接", "请先连接串口")
            return
        self.worker.submit(label, fn, callback)

    def _poll_results(self):
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
                    messagebox.showerror("命令失败", "%s\n%s" % (label, value))
        except queue.Empty:
            pass
        self.after(100, self._poll_results)

    def send_raw(self):
        txt = self.raw_var.get().strip()
        if len(txt) < 3:
            return
        addr, cmd, data = txt[0], txt[1:3], txt[3:]
        self.submit("raw %s" % txt, lambda: self.bus.query(addr, cmd, data), lambda r: self._log_line("   decoded: %s" % r))

    # ---- logging -----------------------------------------------------------
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
        self.log.config(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def clear_log(self):
        self.log_lines = []
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")

    def save_log(self):
        path = filedialog.asksaveasfilename(defaultextension=".log", initialfile=time.strftime("manual_%Y%m%d_%H%M%S.log"))
        if path:
            with open(path, "w") as f:
                f.write("\n".join(self.log_lines) + "\n")

    def _on_close(self):
        self.disconnect()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
