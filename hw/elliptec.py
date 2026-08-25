# -*- coding: utf-8 -*-
"""Thorlabs Elliptec (ELLx) serial protocol layer.

Pure-python codec plus a thread-safe bus wrapper around pyserial.
Every reply is parsed and checked; nothing is silently discarded.

Protocol summary (Thorlabs ELLx communications protocol manual):
  command  : <addr><cmd>[<data>]           ASCII, no terminator
  reply    : <addr><KIND><payload>\r\n
  IN reply : addr(1) 'IN' model(2) serial(8) year(4) fw(2) hw(2) travel(4) pulses(8) = 33 chars
  GS reply : addr 'GS' code(2 hex)
  PO reply : addr 'PO' position(8 hex, two's complement)
Motion commands (ho/ma/mr/fw/bw) answer with PO when complete or GS(busy) while moving.
"""
import threading
import time

BAUDRATE = 9600
DEFAULT_TIMEOUT = 10.0          # seconds to wait for one reply line (queries)
MOTION_TIMEOUT = 60.0           # motion commands reply only once the move has finished
BUSY = 9

STATUS_CODES = {
    0: "OK",
    1: "communication time out",
    2: "mechanical time out",
    3: "command error / not supported",
    4: "value out of range",
    5: "module isolated",
    6: "module out of isolation",
    7: "initialising error",
    8: "thermal error",
    9: "busy",
    10: "sensor error",
    11: "motor error",
    12: "out of range",
    13: "over current error",
}

MODEL_NAMES = {
    0x06: "ELL6 dual-position slider",
    0x09: "ELL9 four-position slider",
    0x0E: "ELL14 rotation stage",
    0x12: "ELL18 rotation stage",
    0x14: "ELL20 linear stage",
    0x11: "ELL17 linear stage",
}


class ElliptecError(Exception):
    pass


class ReplyTimeout(ElliptecError):
    pass


class DeviceStatusError(ElliptecError):
    def __init__(self, addr, code):
        self.addr = addr
        self.code = code
        ElliptecError.__init__(self, "device %s reported GS %02X (%s)"
                               % (addr, code, STATUS_CODES.get(code, "unknown")))


def hex32(value):
    """Signed int -> 8-char upper-case hex (two's complement)."""
    return "%08X" % (int(value) & 0xFFFFFFFF)


def parse_hex32(text):
    """8-char hex (two's complement) -> signed int."""
    v = int(text, 16)
    return v - (1 << 32) if v & 0x80000000 else v


class DeviceInfo(object):
    __slots__ = ("addr", "model", "serial", "year", "fw", "hw", "travel", "pulses")

    @classmethod
    def from_reply(cls, rx):
        if len(rx) < 33 or rx[1:3] != "IN":
            raise ElliptecError("malformed IN reply: %r" % rx)
        d = cls()
        d.addr = rx[0]
        d.model = int(rx[3:5], 16)
        d.serial = rx[5:13]          # 8 characters (original code took only 7)
        d.year = rx[13:17]
        d.fw = rx[17:19]
        d.hw = rx[19:21]
        d.travel = int(rx[21:25], 16)
        d.pulses = int(rx[25:33], 16)
        return d

    @property
    def model_name(self):
        return MODEL_NAMES.get(self.model, "ELL%d (unknown type)" % self.model)

    @property
    def pulses_per_unit(self):
        return float(self.pulses) / self.travel if self.travel else None

    def describe(self):
        return ("addr %s: %s  SN %s  year %s  fw %s  hw %s  travel %d  pulses %d (%.3f/unit)"
                % (self.addr, self.model_name, self.serial, self.year, self.fw, self.hw,
                   self.travel, self.pulses, self.pulses_per_unit or 0.0))


def decode_reply(text):
    """Decode one reply line (without CR/LF) into a dict."""
    if len(text) < 3:
        raise ElliptecError("reply too short: %r" % text)
    addr, kind, payload = text[0], text[1:3], text[3:]
    out = {"addr": addr, "kind": kind, "raw": text}
    if kind == "IN":
        out["info"] = DeviceInfo.from_reply(text)
    elif kind == "GS":
        out["code"] = int(payload[:2], 16)
        out["status"] = STATUS_CODES.get(out["code"], "unknown")
    elif kind in ("PO", "HO", "GJ", "SJ", "MA", "MR"):
        out["value"] = parse_hex32(payload[:8])
    elif kind == "GV":
        out["percent"] = int(payload[:2], 16)
    return out


class ElliptecBus(object):
    """Thread-safe wrapper around one serial port shared by several ELL modules."""

    def __init__(self, port, baudrate=BAUDRATE, timeout=DEFAULT_TIMEOUT, motion_timeout=MOTION_TIMEOUT,
                 log=None, serial_factory=None):
        if serial_factory is None:
            import serial                     # imported lazily so the codec has no hard dependency
            serial_factory = serial.Serial
        self._ser = serial_factory(port, baudrate, timeout=timeout)
        self._lock = threading.RLock()
        self._log = log or (lambda direction, text: None)
        self.port = port
        self.motion_timeout = motion_timeout

    # ---- low level ---------------------------------------------------------
    def close(self):
        with self._lock:
            try:
                self._ser.close()
            except Exception:
                pass

    def query(self, addr, cmd, data="", timeout=None):
        """Send one command and return the decoded reply (raises on timeout).

        `timeout` overrides the port read timeout for this exchange only. Motion commands
        answer only when the move has finished (an ELL18 at 50 % speed needs ~10 s for
        100 deg), so they are issued with a long timeout.
        """
        tx = ("%s%s%s" % (addr, cmd, data)).encode("ascii")
        with self._lock:
            try:
                self._ser.reset_input_buffer()    # never let a late reply desynchronise us
            except Exception:
                pass
            self._log("TX", tx.decode("ascii"))
            self._ser.write(tx)
            old_timeout = self._ser.timeout
            if timeout is not None:
                self._ser.timeout = timeout
            try:
                raw = self._ser.readline()
            finally:
                self._ser.timeout = old_timeout
        text = raw.decode("ascii", "replace").strip()
        self._log("RX", text if text else "<timeout>")
        if not text:
            raise ReplyTimeout("no reply from module %s to %s" % (addr, tx.decode("ascii")))
        rep = decode_reply(text)
        if rep["addr"] != str(addr):
            raise ElliptecError("reply from %s while talking to %s: %s" % (rep["addr"], addr, text))
        return rep

    # ---- queries -----------------------------------------------------------
    def info(self, addr):
        rep = self.query(addr, "in")
        if rep["kind"] != "IN":
            raise ElliptecError("expected IN, got %s" % rep["raw"])
        return rep["info"]

    def status(self, addr):
        rep = self.query(addr, "gs")
        if rep["kind"] != "GS":
            raise ElliptecError("expected GS, got %s" % rep["raw"])
        return rep["code"]

    def position(self, addr):
        rep = self.query(addr, "gp")
        if rep["kind"] != "PO":
            raise ElliptecError("expected PO, got %s" % rep["raw"])
        return rep["value"]

    def velocity(self, addr):
        rep = self.query(addr, "gv")
        return rep.get("percent")

    def set_velocity(self, addr, percent):
        percent = max(0, min(100, int(percent)))
        return self._motion_query(addr, "sv", "%02X" % percent, expect_position=False)

    # ---- motion ------------------------------------------------------------
    def wait_idle(self, addr, timeout=30.0, poll=0.2):
        """Poll GS until the module is no longer busy. Returns the final GS code."""
        t0 = time.time()
        while True:
            code = self.status(addr)
            if code != BUSY:
                return code
            if time.time() - t0 > timeout:
                raise ElliptecError("module %s still busy after %.0f s" % (addr, timeout))
            time.sleep(poll)

    def _motion_query(self, addr, cmd, data="", expect_position=True):
        """Issue a motion command with the long timeout and interpret its reply."""
        try:
            rep = self.query(addr, cmd, data, timeout=self.motion_timeout)
        except ReplyTimeout:
            # Reply lost (e.g. very long move): fall back to polling the status.
            self._log("--", "no reply within %.0f s, polling status" % self.motion_timeout)
            code = self.wait_idle(addr, timeout=self.motion_timeout)
            if code != 0:
                raise DeviceStatusError(addr, code)
            return self.position(addr) if expect_position else None
        return self._motion_reply(addr, rep, expect_position)

    def _motion_reply(self, addr, rep, expect_position=True):
        """Interpret the reply to a motion command; returns final position (pulses) or None."""
        if rep["kind"] == "PO":
            return rep["value"]
        if rep["kind"] == "GS":
            code = rep["code"]
            if code == BUSY:
                code = self.wait_idle(addr)
            if code != 0:
                raise DeviceStatusError(addr, code)
            return self.position(addr) if expect_position else None
        raise ElliptecError("unexpected reply %s" % rep["raw"])

    def home(self, addr, direction=0):
        return self._motion_query(addr, "ho", str(direction))

    def move_abs(self, addr, pulses):
        return self._motion_query(addr, "ma", hex32(pulses))

    def move_rel(self, addr, pulses):
        return self._motion_query(addr, "mr", hex32(pulses))

    def forward(self, addr):
        return self._motion_query(addr, "fw")

    def backward(self, addr):
        return self._motion_query(addr, "bw")


class RotationStage(object):
    """Degree-based helper on top of ElliptecBus for ELL14/ELL18 rotation modules."""

    def __init__(self, bus, addr, info=None):
        self.bus = bus
        self.addr = str(addr)
        self.info = info or bus.info(self.addr)
        if not self.info.travel:
            raise ElliptecError("module %s reports zero travel; not a rotation stage?" % addr)
        self.pulses_per_deg = float(self.info.pulses) / self.info.travel   # 143360/360 for ELL14

    def deg_to_pulses(self, deg):
        return int(round((float(deg) % 360.0) * self.pulses_per_deg))

    def pulses_to_deg(self, pulses):
        return (pulses / self.pulses_per_deg) % 360.0

    def position_deg(self):
        return self.pulses_to_deg(self.bus.position(self.addr))

    def move_deg(self, deg):
        return self.pulses_to_deg(self.bus.move_abs(self.addr, self.deg_to_pulses(deg)))

    def move_rel_deg(self, ddeg):
        return self.pulses_to_deg(self.bus.move_rel(self.addr, int(round(float(ddeg) * self.pulses_per_deg))))

    def home(self):
        return self.pulses_to_deg(self.bus.home(self.addr, 0))
