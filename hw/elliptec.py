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
FIRST_WAIT = 2.0                # wait for the direct reply this long before polling GS
BUSY = 9
MECHANICAL_TIMEOUT = 2

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


KNOWN_KINDS = ("IN", "GS", "PO", "HO", "GJ", "SJ", "MA", "MR", "GV", "I1", "I2", "BO", "BS", "PS", "SV")


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
        self.motion_timeout = motion_timeout      # give up waiting for a move after this
        self.first_wait = FIRST_WAIT              # wait this long for the direct reply before polling
        self.poll_timeout = 2.0                   # per-GS-query timeout while polling a moving module
        self.poll_interval = 0.5
        self.attempts = 3                         # resend a swallowed motion command this many times
        self.position_tolerance = 20              # pulses (0.05 deg on ELL14/18): 'at target'
        self.accept_tolerance = 120               # pulses (0.3 deg): accept after corrective moves, with a warning
        self.mech_retry_delay = 1.0               # seconds before retrying after GS02
        self.stray_limit = 3                      # stray / fragmentary lines discarded per query
        self._travel = {}                         # addr -> travel from IN (sliders)

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
                for _ in range(self.stray_limit + 1):
                    raw = self._ser.readline()
                    text = raw.decode("ascii", "replace").strip()
                    self._log("RX", text if text else "<timeout>")
                    if not text:
                        raise ReplyTimeout("no reply from module %s to %s" % (addr, tx.decode("ascii")))
                    # A module that finished a move sends its PO on its own. When that lands while we
                    # flush the buffer and send the next poll, a head-truncated fragment ("O00004470")
                    # or a reply to the previous command is read first: discard it and read the real
                    # answer to *this* command, which the module sends next.
                    try:
                        rep = decode_reply(text)
                        ok = rep["addr"] == str(addr) and rep["kind"] in KNOWN_KINDS
                    except (ElliptecError, ValueError):
                        ok = False
                    if ok:
                        return rep
                    self._log("--", "discarding stray reply %r while talking to %s" % (text, addr))
            finally:
                self._ser.timeout = old_timeout
        raise ElliptecError("no valid reply from module %s to %s (last: %r)" % (addr, tx.decode("ascii"), text))

    # ---- queries -----------------------------------------------------------
    def info(self, addr):
        rep = self.query(addr, "in")
        if rep["kind"] != "IN":
            raise ElliptecError("expected IN, got %s" % rep["raw"])
        return rep["info"]

    def status(self, addr, timeout=None):
        rep = self.query(addr, "gs", timeout=timeout)
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
        return self._motion_query(addr, "sv", "%02X" % percent, expect_position=False, retry_if_unmoved=False)

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

    def _poll_until_idle(self, addr):
        """Poll GS with a short timeout until the module is no longer moving.

        Observed on the ELL18 (2026-08-25): while moving it answers nothing at all, and
        the deferred PO (completion) reply arrives in the read window of a later `gs`.
        So: no reply / GS09 -> still moving; PO -> move finished, position known.
        Returns (gs_code, position_or_None).
        """
        t0 = time.time()
        while True:
            try:
                rep = self.query(addr, "gs", timeout=self.poll_timeout)
            except ReplyTimeout:
                rep = None
            if rep is not None:
                if rep["kind"] == "PO":
                    return 0, rep["value"]
                if rep["kind"] == "GS" and rep["code"] != BUSY:
                    return rep["code"], None
            if time.time() - t0 > self.motion_timeout:
                raise ElliptecError("module %s still busy after %.0f s" % (addr, self.motion_timeout))
            time.sleep(self.poll_interval)

    def _motion_query(self, addr, cmd, data="", expect_position=True, target=None, retry_if_unmoved=True,
                      tolerance=None, accept=None):
        """Motion command with one automatic retry after a mechanical time-out (GS02)."""
        tol = self.position_tolerance if tolerance is None else tolerance
        acc = self.accept_tolerance if accept is None else accept
        try:
            return self._motion_query_once(addr, cmd, data, expect_position, target, retry_if_unmoved, tol, acc)
        except DeviceStatusError as e:
            if e.code == BUSY:
                raise
            # Observed 2026-08-25: ELL18 reported GS0A (sensor error) at the end of a 106 deg
            # move that had in fact reached its target. A status code with the stage at the
            # commanded position is logged, not fatal.
            if expect_position and target is not None:
                try:
                    pos = self.position(addr)
                except ElliptecError:
                    pos = None
                if pos is not None and abs(pos - target) <= tol:
                    self._log("--", "module %s reported GS %02X (%s) but is at target %d; accepted" % (addr, e.code, STATUS_CODES.get(e.code, "?"), pos))
                    return pos
            if e.code != MECHANICAL_TIMEOUT:
                raise
            self._log("--", "module %s mechanical time-out on %s%s, retrying once after %.0f s" % (addr, cmd, data, self.mech_retry_delay))
            time.sleep(self.mech_retry_delay)
            return self._motion_query_once(addr, cmd, data, expect_position, target, retry_if_unmoved, tol, acc)

    def _motion_query_once(self, addr, cmd, data, expect_position, target, retry_if_unmoved, tol, acc):
        """Issue a motion command robustly.

        Observed on the real bus (2026-08-25):
          * an ELL18 occasionally swallows a command (no reply, no motion, GS00) -> resend;
          * while moving it stays silent and sends PO only on completion -> poll GS;
          * after a long move it may stop a few hundredths of a degree short -> send the
            target again (corrective move); accept within `accept_tolerance` with a log line.
        Never silently accept a wrong position.
        """
        start = self.position(addr) if (expect_position and retry_if_unmoved) else None
        tx_cmd, tx_data = cmd, data
        for attempt in range(1, self.attempts + 1):
            try:
                rep = self.query(addr, tx_cmd, tx_data, timeout=self.first_wait)
            except ReplyTimeout:
                rep = None
            if rep is not None:
                pos = self._motion_reply(addr, rep, expect_position)
            else:
                self._log("--", "no reply from %s to %s%s within %.0f s, polling status" % (addr, tx_cmd, tx_data, self.first_wait))
                code, pos = self._poll_until_idle(addr)
                if code != 0:
                    raise DeviceStatusError(addr, code)
                if expect_position and pos is None:
                    pos = self.position(addr)
            if not expect_position or target is None:
                return pos
            err = abs(pos - target)
            if err <= tol:
                return pos
            if err <= acc:
                if attempt < self.attempts:
                    self._log("--", "module %s at %d, target %d (off by %d pulses): corrective move" % (addr, pos, target, pos - target))
                    tx_cmd, tx_data = "ma", hex32(target)          # never repeat a relative move
                    start = None
                    continue
                self._log("--", "WARNING module %s settled at %d, target %d (off by %d pulses); accepted" % (addr, pos, target, pos - target))
                return pos
            if start is not None and abs(pos - start) <= tol and retry_if_unmoved:
                self._log("--", "module %s ignored %s%s (attempt %d/%d), resending" % (addr, tx_cmd, tx_data, attempt, self.attempts))
                continue
            raise ElliptecError("module %s stopped at %d pulses, target %d (cmd %s%s)" % (addr, pos, target, tx_cmd, tx_data))
        raise ElliptecError("module %s ignored %s%s %d times" % (addr, cmd, data, self.attempts))

    def _motion_reply(self, addr, rep, expect_position=True):
        """Interpret the direct reply to a motion command; returns final position (pulses) or None."""
        if rep["kind"] == "PO":
            return rep["value"]
        if rep["kind"] == "GS":
            code, pos = rep["code"], None
            if code == BUSY:
                code, pos = self._poll_until_idle(addr)
            if code != 0:
                raise DeviceStatusError(addr, code)
            if not expect_position:
                return None
            return pos if pos is not None else self.position(addr)
        raise ElliptecError("unexpected reply %s" % rep["raw"])

    def home(self, addr, direction=0):
        return self._motion_query(addr, "ho", str(direction), target=0)

    def move_abs(self, addr, pulses):
        return self._motion_query(addr, "ma", hex32(pulses), target=int(pulses))

    def move_rel(self, addr, pulses):
        start = self.position(addr)
        return self._motion_query(addr, "mr", hex32(pulses), target=start + int(pulses))

    def _slider_travel(self, addr):
        """Forward position of a slider (ELL6 reports positions in mm; travel from IN)."""
        if addr not in self._travel:
            self._travel[addr] = self.info(addr).travel
        return self._travel[addr]

    def forward(self, addr):
        # slider: verify it really is at the forward end (a swallowed 'fw' would leave the
        # shutter closed and the following spectrum would silently be a dark frame)
        return self._motion_query(addr, "fw", target=self._slider_travel(addr), tolerance=0, accept=0)

    def backward(self, addr):
        return self._motion_query(addr, "bw", target=0, tolerance=0, accept=0)


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
