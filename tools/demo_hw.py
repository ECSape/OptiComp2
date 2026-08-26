# -*- coding: utf-8 -*-
"""Fake hardware for `manual_gui.py --demo`: an Elliptec bus and a B&W Tek spectrometer that behave
like the real instrument (protocol log lines, motion delays, protected home, realistic spectra that
follow the shutter / polariser / sample angle / detector-arm geometry) without touching COM4 or the DLL.

Everything here reuses the dry-run fakes of tools/monitor.py and the real sequence Runner, so the GUI
code path in demo mode is identical to the lab PC - only the bus/spectrometer factories differ.
"""
import os
import sys
import threading
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
from hw import bwtek, config as cfg, elliptec as ell, stagestate   # noqa: E402
import monitor                                                     # noqa: E402
import sequence as sq                                              # noqa: E402

PPD = 143360 / 360.0                                # pulses per degree of an ELL14/ELL18
DEFAULT_POS = {"0": 0, "1": 93980, "2": 17522, "3": 73671}    # shutter closed, S (236 deg), arm 44 deg, sample 185 deg
_MODULES = {                                        # addr -> (model, serial, fw, travel, pulses)  (2026-08-25 inventory)
    "0": (0x06, "10600130", "14", 31, 31),
    "1": (0x0E, "11400293", "19", 360, 143360),
    "2": (0x12, "11800032", "18", 360, 143360),
    "3": (0x12, "11800031", "18", 360, 143360),
}


def _ang_diff(a, b):
    return abs(((a - b) + 180.0) % 360.0 - 180.0)


class DemoInfo(monitor._FakeInfo):
    """Per-address DeviceInfo look-alike: model_name/serial/fw/year/hw/travel/pulses/pulses_per_unit/describe()."""

    def __init__(self, addr):
        self.addr = str(addr)
        model, serial, fw, travel, pulses = _MODULES.get(self.addr, _MODULES["3"])
        self.model = model
        self.serial = serial
        self.year = "2019"
        self.fw = fw
        self.hw = "01"
        self.travel = travel
        self.pulses = pulses

    @property
    def model_name(self):
        return ell.MODEL_NAMES.get(self.model, "ELL%d (unknown type)" % self.model)

    @property
    def pulses_per_unit(self):
        return float(self.pulses) / self.travel if self.travel else None

    def raw(self):
        return "%sIN%02X%s%s%s%s%04X%08X" % (self.addr, self.model, self.serial, self.year, self.fw, self.hw, self.travel, self.pulses)

    def describe(self):
        return ("addr %s: %s  SN %s  year %s  fw %s  hw %s  travel %d  pulses %d (%.3f/unit)"
                % (self.addr, self.model_name, self.serial, self.year, self.fw, self.hw, self.travel, self.pulses, self.pulses_per_unit or 0.0))


class DemoBus(monitor._FakeBus):
    """log(direction, text) receives synthetic 'TX 2ma00011E00' / 'RX 2PO00011E00' lines so the log drawer
    looks real. anomaly: None | 'arm' (status('2') -> 2 = GS02, arm reference lost) | 'moved' (sample stage
    82 deg away from the recorded state) - for screenshots of the warning path."""

    def __init__(self, log=None, motion_seconds=0.6, anomaly=None):
        monitor._FakeBus.__init__(self)
        self.pos = dict(DEFAULT_POS)
        self._log = log or (lambda direction, text: None)
        self._lock = threading.RLock()
        self.motion_seconds = motion_seconds
        self.vel = {"1": 64, "2": 64, "3": 64}
        self.status_codes = {"0": 0, "1": 0, "2": 0, "3": 0}
        self.port = "DEMO"
        self.closed = False
        self.anomaly = None
        self.calls = []                     # (addr, cmd) history, for tests
        if anomaly:
            self.inject_anomaly(anomaly)

    def inject_anomaly(self, kind):
        with self._lock:
            if kind == "arm":
                self.status_codes["2"] = ell.MECHANICAL_TIMEOUT
            elif kind == "moved":
                self.pos["3"] = int((self.pos["3"] + round(82.0 * PPD)) % 143360)
            self.anomaly = kind

    # ---- protocol logging ----------------------------------------------------
    def _tx(self, addr, cmd, data=""):
        self.calls.append((str(addr), cmd))
        self._log("TX", "%s%s%s" % (addr, cmd, data))

    def _rx(self, text):
        self._log("RX", text)

    def _sleep_motion(self, addr, delta_pulses):
        if self.motion_seconds <= 0:
            return
        if str(addr) == "0":
            secs = min(self.motion_seconds, 0.3)
        else:
            secs = min(self.motion_seconds, 0.01 * abs(delta_pulses) / PPD)
        if secs > 0:
            time.sleep(secs)

    # ---- queries -----------------------------------------------------------------
    def info(self, addr):
        addr = str(addr)
        with self._lock:
            self._tx(addr, "in")
            info = DemoInfo(addr)
            self._rx(info.raw())
            return info

    def status(self, addr, timeout=None):
        addr = str(addr)
        with self._lock:
            self._tx(addr, "gs")
            code = self.status_codes.get(addr, 0)
            self._rx("%sGS%02X" % (addr, code))
            return code

    def position(self, addr):
        addr = str(addr)
        with self._lock:
            self._tx(addr, "gp")
            pos = self.pos[addr]
            self._rx("%sPO%s" % (addr, ell.hex32(pos)))
            return pos

    def velocity(self, addr):
        addr = str(addr)
        with self._lock:
            self._tx(addr, "gv")
            if addr not in self.vel:
                self._rx("%sGS03" % addr)
                raise ell.ElliptecError("device %s reported GS 03 (command error / not supported)" % addr)
            self._rx("%sGV%02X" % (addr, self.vel[addr]))
            return self.vel[addr]

    def set_velocity(self, addr, percent):
        addr = str(addr)
        pct = max(0, min(100, int(percent)))
        with self._lock:
            self._tx(addr, "sv", "%02X" % pct)
            self.vel[addr] = pct
            self._rx("%sGS00" % addr)
            return None

    # ---- motion --------------------------------------------------------------------
    def move_abs(self, addr, pulses):
        addr = str(addr)
        pulses = int(pulses)
        with self._lock:
            self._tx(addr, "ma", ell.hex32(pulses))
            self._sleep_motion(addr, pulses - self.pos[addr])
            self.pos[addr] = pulses
            self._rx("%sPO%s" % (addr, ell.hex32(pulses)))
            return pulses

    def move_rel(self, addr, pulses):
        return self.move_abs(addr, self.pos[str(addr)] + int(pulses))

    def home(self, addr, direction=0, force=False):
        addr = str(addr)
        if addr in self.protected_home and not force:
            raise ell.ElliptecError("home on module %s is blocked (fibre-carrying arm); pass force=True "
                                    "only with the fibre slack and an operator watching" % addr)
        with self._lock:
            self._tx(addr, "ho", str(direction))
            self._sleep_motion(addr, self.pos[addr])
            self.pos[addr] = 0
            self.status_codes[addr] = 0
            self._rx("%sPO00000000" % addr)
            return 0

    def forward(self, addr):
        addr = str(addr)
        with self._lock:
            self._tx(addr, "fw")
            self._sleep_motion(addr, 31)
            self.pos[addr] = 31
            self._rx("%sPO%s" % (addr, ell.hex32(31)))
            return 31

    def backward(self, addr):
        addr = str(addr)
        with self._lock:
            self._tx(addr, "bw")
            self._sleep_motion(addr, 31)
            self.pos[addr] = 0
            self._rx("%sPO00000000" % addr)
            return 0

    def close(self):
        self.closed = True

    # ---- raw command console -----------------------------------------------------
    def query(self, addr, cmd, data="", timeout=None, expect=None):
        """Same shape as elliptec.decode_reply; unknown commands answer GS 03."""
        addr, cmd = str(addr), cmd.lower()
        if cmd == "in":
            info = self.info(addr)
            return {"addr": addr, "kind": "IN", "raw": info.raw(), "info": info}
        if cmd == "gs":
            code = self.status(addr)
            return {"addr": addr, "kind": "GS", "raw": "%sGS%02X" % (addr, code), "code": code, "status": ell.STATUS_CODES.get(code, "unknown")}
        if cmd == "gv":
            try:
                v = self.velocity(addr)
            except ell.ElliptecError:
                return {"addr": addr, "kind": "GS", "raw": "%sGS03" % addr, "code": 3, "status": ell.STATUS_CODES[3]}
            return {"addr": addr, "kind": "GV", "raw": "%sGV%02X" % (addr, v), "percent": v}
        if cmd == "sv":
            self.set_velocity(addr, int(data, 16) if data else 0)
            return {"addr": addr, "kind": "GS", "raw": "%sGS00" % addr, "code": 0, "status": ell.STATUS_CODES[0]}
        try:
            if cmd == "gp":
                value = self.position(addr)
            elif cmd == "ma":
                value = self.move_abs(addr, ell.parse_hex32(data.ljust(8, "0")[:8]))
            elif cmd == "mr":
                value = self.move_rel(addr, ell.parse_hex32(data.ljust(8, "0")[:8]))
            elif cmd == "fw":
                value = self.forward(addr)
            elif cmd == "bw":
                value = self.backward(addr)
            elif cmd == "ho":
                value = self.home(addr, data or 0, force=(addr not in self.protected_home))
            else:
                with self._lock:
                    self._tx(addr, cmd, data)
                    self._rx("%sGS03" % addr)
                return {"addr": addr, "kind": "GS", "raw": "%sGS03" % addr, "code": 3, "status": ell.STATUS_CODES[3]}
        except ValueError as e:
            raise ell.ElliptecError("bad data for %s%s%s: %s" % (addr, cmd, data, e))
        return {"addr": addr, "kind": "PO", "raw": "%sPO%s" % (addr, ell.hex32(value)), "value": value}


class DemoSpec(monitor._FakeSpec):
    """Realistic frames: counts = 900 + gain * IT_ms * shape(wl) * geometry + noise, clipped to uint16.

    shape(wl): Planck 2900 K x Si-response window (rises 350->450 nm, falls after 950 nm) with a small OH dip
    at 950 nm, peak = 1 near 760 nm.  geometry: shutter (bus.pos['0'] == 31) ? 1 : 0; polariser 236 deg -> 1.00,
    146 deg -> 1.12; sample theta = deg - 105; white: 0.99 * (0.97 + 0.03 cos theta) (nearly flat plate);
    si: Fresnel n = 3.9; arm at 44 +- 1 deg -> 1.0, at 124 +- 1 deg (DB) -> 0.8 * (1 + 0.03 R(0)) (sphere wall,
    sample-independent apart from a small substitution term), elsewhere -> 0.02.
    gain: IT 1000 ms at theta = 80 deg, S, white gives a peak of 0.85 * 65535 (auto-IT converges in <= 3 steps).
    noise: sqrt(N) shot + 20 counts read noise; baseline pixels (< 254, > 2030) stay dark (a few hot pixels).
    read() sleeps min(avg * IT / 1000, 0.6) s (fast=True: 0). recover() -> 'reopened [demo]'."""

    SAMPLES = ("white", "si")
    BASELINE = 900.0
    HOT_PIXELS = ((17, 1230), (88, 640), (2040, 980), (2046, 1200))     # index, extra counts (masked region)

    def __init__(self, bus, log=None, fast=False, sample="white", fail_read_at=0, seed=None):
        monitor._FakeSpec.__init__(self, fail_at=fail_read_at, bus=bus)
        self.integration_ms = None
        self._log = log or (lambda text: None)
        self.fast = fast
        self.sample = sample if sample in self.SAMPLES else "white"
        self.wl = bwtek.wavelengths()
        self._shape = self._make_shape(self.wl)
        self._rng = np.random.RandomState(seed)
        white_80 = self._sample_R(80.0, "S", "white")
        self.gain = (0.85 * bwtek.ADC_MAX - self.BASELINE) / (1000.0 * white_80)    # counts per ms at unit geometry

    @classmethod
    def _make_shape(cls, wl):
        lam = wl * 1e-9
        planck = lam ** -5 / (np.exp(1.438776877e-2 / (lam * 2900.0)) - 1.0)
        window = 1.0 / (1.0 + np.exp(-(wl - 400.0) / 25.0)) * 1.0 / (1.0 + np.exp((wl - 900.0) / 45.0))
        dip = 1.0 - 0.08 * np.exp(-((wl - 950.0) / 12.0) ** 2)
        shape = planck * window * dip
        shape[:bwtek.ACTIVE_FIRST] = 0.0
        shape[bwtek.ACTIVE_LAST + 1:] = 0.0
        return shape / shape.max()

    @staticmethod
    def _sample_R(theta, pol, sample):
        th = abs(float(theta))
        if sample == "si":
            from analysis import standards as sd
            return float(sd.fresnel(3.9, min(th, 89.0), pol))
        return 0.99 * (0.97 + 0.03 * np.cos(np.deg2rad(th)))

    def _geometry(self):
        bus = self.bus
        if bus is None:
            return 1.0
        pos = bus.pos
        if pos.get("0", 0) != 31:
            return 0.0
        pol_deg = (pos.get("1", 0) / PPD) % 360.0
        arm_deg = (pos.get("2", 0) / PPD) % 360.0
        samp_deg = (pos.get("3", 0) / PPD) % 360.0
        pol = "S" if _ang_diff(pol_deg, cfg.POL_DEG["S"]) <= _ang_diff(pol_deg, cfg.POL_DEG["P"]) else "P"
        pol_factor = 1.0 if pol == "S" else 1.12
        theta = ((samp_deg - cfg.SAMPLE_VAR_OFFSET) + 180.0) % 360.0 - 180.0
        if _ang_diff(arm_deg, cfg.SYSTEM_ZERO) <= 1.0:
            return pol_factor * self._sample_R(theta, pol, self.sample)
        if _ang_diff(arm_deg, cfg.SYSTEM_DB) <= 1.0:
            return pol_factor * 0.8 * (1.0 + 0.03 * self._sample_R(0.0, pol, self.sample))
        return 0.02 * pol_factor

    # ---- BWTek-compatible API ----------------------------------------------------
    def open(self):
        self.opened = True
        self._log("GetDeviceCount = 1 [demo]")
        return 1

    def set_integration_time(self, ms):
        ms = int(ms)
        if ms < 1:
            raise bwtek.BWTekError("integration time must be >= 1 ms")
        self.integration_ms = ms
        self._log("bwtekSetTimeUSB(%d) -> %d [demo]" % (ms, ms))
        return ms

    def read(self, average=1, smoothing_type=0, smoothing_value=0):
        if not self.opened:
            raise bwtek.BWTekError("spectrometer not opened")
        self.reads += 1
        if self.fail_at and self.reads == self.fail_at:
            raise bwtek.BWTekError("bwtekReadResultUSB failed (-99) [demo]")
        average = max(1, int(average))
        it = self.integration_ms or 100
        t0 = time.time()
        if not self.fast:
            time.sleep(min(average * it / 1000.0, 0.6))
        signal = self.gain * it * self._shape * self._geometry()
        n = len(self.wl)
        acc = np.zeros(n)
        for _ in range(average):
            shot = self._rng.normal(0.0, 1.0, n) * np.sqrt(np.maximum(signal + self.BASELINE, 1.0))
            acc += self.BASELINE + signal + shot + self._rng.normal(0.0, 20.0, n)
        counts = acc / average
        for idx, extra in self.HOT_PIXELS:
            counts[idx] += extra
        self._log("bwtekReadResultUSB(avg=%d, sm=%d/%d) -> 0 in %.2f s [demo]" % (average, smoothing_type, smoothing_value, time.time() - t0))
        return np.clip(np.round(counts), 0, bwtek.ADC_MAX).astype(np.uint16)

    def reopen(self, settle=0.0):
        it = self.integration_ms
        self.close()
        self.open()
        if it:
            self.set_integration_time(it)
        return 1

    def recover(self, usb_restart=True, vid=None):
        self.recoveries += 1
        self.reopen()
        self._log("recover: reopen succeeded [demo]")
        return "reopened [demo]"

    def close(self):
        self.opened = False


def seed_demo_data(data_root, state_path, log=None, prefix="sample"):
    """Create data_root/demo_white and data_root/demo_si (only if missing) by running sq.Runner with
    DemoBus/DemoSpec(fast=True) and ask_user=lambda m: True: build_reference_calibration() + build_dark(3)
    + build_scan(8, 80, 8, ['S','P'], 3, prefix) + build_double_beam(['S','P'], 3, prefix); the stages are then
    parked at the DemoBus defaults and stagestate.record(bus, state_path, note='demo seed') is written so the
    GUI's first connect is anomaly-free. Runs in < 3 s. Returns the list of directories created."""
    log = log or (lambda text: None)
    made = []
    ppd = {a: PPD for a in (cfg.POLARISER, cfg.SYSTEM, cfg.SAMPLE)}
    for i, (name, sample) in enumerate((("demo_white", "white"), ("demo_si", "si"))):
        outdir = os.path.join(data_root, name)
        if os.path.isfile(os.path.join(outdir, "manifest.json")):
            continue
        bus = DemoBus(motion_seconds=0.0)
        stagestate.protect(bus)
        spec = DemoSpec(bus, fast=True, sample=sample, seed=100 + i)
        spec.open()
        spec.set_integration_time(100)
        runner = sq.Runner(bus, spec, outdir, log=log, ask_user=lambda m: True, ppd=ppd, state_path=state_path)
        steps = (sq.build_reference_calibration() + sq.build_dark(3) + sq.build_scan(8, 80, 8, ["S", "P"], 3, prefix)
                 + sq.build_double_beam(["S", "P"], 3, prefix))
        runner.run(steps)
        for st in (sq.polariser("S"), sq.stage(cfg.SYSTEM, cfg.SYSTEM_ZERO, "探测臂零位"),
                   sq.stage(cfg.SAMPLE, cfg.THETA_MAX + cfg.SAMPLE_VAR_OFFSET, "样品台"), sq.shutter(False)):
            runner.run_step(st)
        stagestate.record(bus, state_path, note="demo seed", ppd=ppd)
        made.append(outdir)
        log("demo data seeded: %s (%d spectra)" % (outdir, len(runner.manifest)))
    return made
