# -*- coding: utf-8 -*-
"""Remember where the Elliptec modules were and notice when they moved without us.

The ELLB bus board is USB-powered: a replug (or a hub power glitch) restarts every module,
and ELL14/ELL18 modules auto-home at power-up. On 2026-08-26 this silently moved the
polariser to 0, the sample stage by 82 deg and left the fibre-carrying arm with a failed
home (GS02) and a meaningless position register. Every tool therefore:

  * records the modules' positions/status after motion and on disconnect (`record`), and
  * compares the live bus with the record on connect (`check`), reporting anomalies.

An anomaly is a warning for most modules; for the fibre arm it means the zero reference is
lost and nothing should move until the operator has restored it.
"""
import json
import os
import time

from hw import config as cfg
from hw import elliptec as ell

MODULES = (cfg.SHUTTER, cfg.POLARISER, cfg.SYSTEM, cfg.SAMPLE)


def snapshot(bus, addrs=MODULES, ppd=None):
    """Live positions/status/velocity of the modules: {addr: {...}}. Read-only queries."""
    out = {}
    for a in addrs:
        a = str(a)
        rec = {"position": bus.position(a), "status": bus.status(a)}
        try:
            rec["velocity"] = bus.velocity(a)
        except ell.ElliptecError:
            rec["velocity"] = None
        if ppd and a in ppd and ppd[a]:
            rec["deg"] = round((rec["position"] / float(ppd[a])) % 360.0, 3)
        out[a] = rec
    return out


def load(path=None):
    """None -> the configured record; "" / False -> no record at all."""
    if path is None:
        path = cfg.STATE_FILE
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return None


def save(state, path=None, note=""):
    if path is None:
        path = cfg.STATE_FILE
    if not path:                                   # bookkeeping disabled
        return None
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    doc = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "note": note, "modules": state}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1)
    os.replace(tmp, path)
    return path


def record(bus, path=None, note="", ppd=None):
    """Snapshot the bus and save it. Returns the snapshot."""
    st = snapshot(bus, ppd=ppd)
    save(st, path, note)
    return st


def compare(saved, live, ppd=None, tolerance_deg=None):
    """List of anomaly strings between a saved record and a live snapshot (empty = consistent)."""
    tol = cfg.STATE_TOLERANCE_DEG if tolerance_deg is None else tolerance_deg
    problems = []
    mods = (saved or {}).get("modules", {}) if saved and "modules" in saved else (saved or {})
    for a, now in live.items():
        code = now.get("status", 0)
        if code not in (0, None):
            problems.append("module %s status GS%02X (%s)%s" % (
                a, code, ell.STATUS_CODES.get(code, "?"),
                " - the fibre arm's home attempt failed; its zero is LOST" if a == cfg.SYSTEM and code == ell.MECHANICAL_TIMEOUT else ""))
        old = mods.get(a)
        if not old:
            continue
        dp = now["position"] - old["position"]
        if ppd and a in ppd and ppd[a]:
            ddeg = abs(((dp / float(ppd[a])) + 180.0) % 360.0 - 180.0)
            if ddeg > tol:
                problems.append("module %s moved %.2f deg since %s (%d -> %d pulses)" % (
                    a, ddeg, (saved or {}).get("time", "last record"), old["position"], now["position"]))
        elif dp != 0 and a != cfg.SHUTTER:
            problems.append("module %s position changed since %s (%d -> %d pulses)" % (
                a, (saved or {}).get("time", "last record"), old["position"], now["position"]))
        elif dp != 0:
            problems.append("shutter position changed since %s (%d -> %d)" % ((saved or {}).get("time", "last record"), old["position"], now["position"]))
        if old.get("velocity") is not None and now.get("velocity") is not None and old["velocity"] != now["velocity"]:
            problems.append("module %s velocity %d%% -> %d%% (module reset?)" % (a, old["velocity"], now["velocity"]))
    return problems


def check(bus, path=None, ppd=None, log=None):
    """Compare the live bus with the saved record. Returns (anomalies, live_snapshot).

    No record yet -> no anomalies except non-zero status codes."""
    live = snapshot(bus, ppd=ppd)
    saved = load(path)
    problems = compare(saved, live, ppd=ppd)
    if log:
        for p in problems:
            log("STAGE STATE: " + p)
    return problems, live


def apply_velocities(bus, velocities=None, log=None):
    """Set the configured module speeds (the modules forget them on power-up)."""
    velocities = cfg.VELOCITY if velocities is None else velocities
    for a, pct in velocities.items():
        try:
            cur = bus.velocity(a)
        except ell.ElliptecError:
            cur = None
        if cur != pct:
            bus.set_velocity(a, pct)
            if log:
                log("module %s velocity %s%% -> %d%%" % (a, cur, pct))


def protect(bus):
    """Mark the fibre arm so bus.home() refuses it unless forced."""
    bus.protected_home = set(str(a) for a in cfg.PROTECTED_HOME)
    return bus.protected_home


def arm_reference_lost(live):
    """True when the fibre arm reports a failed home (its position register is meaningless)."""
    m = live.get(cfg.SYSTEM)
    return bool(m) and m.get("status") == ell.MECHANICAL_TIMEOUT
