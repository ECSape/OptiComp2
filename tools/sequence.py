# -*- coding: utf-8 -*-
"""Measurement sequences: step builders + a runner (GUI-independent, testable with fakes).

A sequence is a list of Step objects. Builders expand high-level actions (reference
calibration, dark, single angle, angle scan, double-beam) into primitive steps:
  stage(addr, deg) | shutter(open) | set_it(ms) | auto_it() | acquire(tag, avg, meta) | pause(msg)
The runner executes them on the calling thread (the spectrometer worker), moves stages
through the Elliptec bus, and writes every spectrum + a JSON manifest as it goes.
"""
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from hw import bwtek
from hw import config as cfg
from hw import stagestate


class Step(object):
    def __init__(self, kind, text, **params):
        self.kind = kind
        self.text = text
        self.params = params

    def __repr__(self):
        return "Step(%s: %s)" % (self.kind, self.text)


class SequenceAbort(Exception):
    pass


# ---- builders ----------------------------------------------------------------
def stage(addr, deg, what):
    return Step("stage", "%s -> %.2f°" % (what, deg), addr=addr, deg=float(deg))


def polariser(pol):
    return stage(cfg.POLARISER, cfg.POL_DEG[pol], "偏振片 %s" % pol)


def sample_theta(theta):
    return Step("stage", "样品台 θ=%g° (台 %.2f°)" % (theta, theta + cfg.SAMPLE_VAR_OFFSET),
                addr=cfg.SAMPLE, deg=float(theta) + cfg.SAMPLE_VAR_OFFSET, theta=float(theta))


def shutter(open_):
    return Step("shutter", "快门 %s" % ("开" if open_ else "关"), open=bool(open_))


def set_it(ms, save=False):
    """Set the integration time; with save=True the previous value is remembered for restore_it()."""
    return Step("set_it", "积分时间 %d ms" % ms, ms=int(ms), save=bool(save))


def restore_it():
    return Step("restore_it", "恢复积分时间")


def auto_it():
    return Step("auto_it", "自动定标积分时间 (峰值→85%)")


def acquire(tag, avg, **meta):
    return Step("acquire", "采集 %s (平均 %d)" % (tag, avg), tag=tag, avg=int(avg), meta=meta)


def pause(msg):
    return Step("pause", "暂停: %s" % msg, msg=msg)


def check_theta_range(start, stop, step):
    """Thesis gatekeeper: 0 <= start < stop <= 80, step >= 1. Returns the angle list."""
    if not (cfg.THETA_MIN <= start < stop <= cfg.THETA_MAX):
        raise ValueError("需要 %d ≤ start < stop ≤ %d (得到 %g, %g)" % (cfg.THETA_MIN, cfg.THETA_MAX, start, stop))
    if step < cfg.STEP_MIN:
        raise ValueError("步长需 ≥ %d" % cfg.STEP_MIN)
    return list(np.arange(start, stop + 1e-9, step))


def apply_min_it():
    return Step("apply_min_it", "取 S/P 定标结果中较小的积分时间")


def build_reference_calibration():
    """Thesis 4.2.3.3 sets the integration time on the reference at 80° / S (its brightest
    point for specular dielectrics). Measured 2026-08-25 on a diffuse reference the P
    channel was 12 % brighter, so both polarisations are calibrated and the smaller
    integration time is kept."""
    return [stage(cfg.SYSTEM, cfg.SYSTEM_ZERO, "探测臂零位"), sample_theta(cfg.THETA_MAX), shutter(True),
            polariser("S"), auto_it(), polariser("P"), auto_it(), apply_min_it(), shutter(False)]


def build_dark(avg, tag="dark"):
    return [shutter(False), acquire(tag, avg, kind="dark")]


def build_single_angle(theta, pols, avg, prefix):
    steps = [stage(cfg.SYSTEM, cfg.SYSTEM_ZERO, "探测臂零位")]
    for pol in pols:
        steps += [polariser(pol), sample_theta(theta), shutter(True),
                  acquire("%s_%s_%g" % (prefix, pol, theta), avg, kind="var", pol=pol, theta=float(theta))]
    steps.append(shutter(False))
    return steps


def build_scan(start, stop, step, pols, avg, prefix):
    angles = check_theta_range(start, stop, step)
    steps = [stage(cfg.SYSTEM, cfg.SYSTEM_ZERO, "探测臂零位")]
    for pol in pols:                        # polariser moves once per polarisation, sample sweeps
        steps.append(polariser(pol))
        for th in angles:
            steps += [sample_theta(th), shutter(True),
                      acquire("%s_%s_%g" % (prefix, pol, th), avg, kind="var", pol=pol, theta=float(th))]
    steps.append(shutter(False))
    return steps


def build_double_beam(pols, avg, prefix):
    """Exchange position -> user swaps port cover -> DB geometry -> acquire per polarisation."""
    steps = [shutter(False), set_it(cfg.DB_IT_MS, save=True),
             stage(cfg.SAMPLE, cfg.SAMPLE_EXCHANGE, "样品台交换位"), stage(cfg.SYSTEM, cfg.SYSTEM_EXCHANGE, "探测臂交换位"),
             pause("请把积分球端口盖换到 DB（直射）位置，然后点确定"),
             stage(cfg.SYSTEM, cfg.SYSTEM_DB, "探测臂 DB 位"), stage(cfg.SAMPLE, cfg.SAMPLE_DB, "样品台 DB 位")]
    for pol in pols:
        steps += [polariser(pol), shutter(True), acquire("%s_DB_%s" % (prefix, pol), avg, kind="db", pol=pol)]
    steps += [shutter(False), acquire("dark_db", avg, kind="dark"),        # dark at the DB integration time
              restore_it()]                                                # back to the session integration time
    steps += [
              stage(cfg.SAMPLE, cfg.SAMPLE_EXCHANGE, "样品台交换位"), stage(cfg.SYSTEM, cfg.SYSTEM_EXCHANGE, "探测臂交换位"),
              pause("请把端口盖换回正常测量位置，然后点确定"),
              stage(cfg.SYSTEM, cfg.SYSTEM_ZERO, "探测臂零位"), stage(cfg.SAMPLE, cfg.SAMPLE_ZERO, "样品台零位")]
    return steps


# ---- runner -------------------------------------------------------------------
def check_soft_limits(addr, deg):
    """Raise ValueError when `deg` lies outside the configured soft limits of module `addr`."""
    lim = cfg.SOFT_LIMITS.get(str(addr))
    if lim and not (lim[0] <= deg <= lim[1]):
        raise ValueError("stage %s target %.2f° outside soft limits %s" % (addr, deg, lim))
    return deg


class Runner(object):
    """Executes steps. `ask_user(msg)` must block until the operator confirms (False = abort)."""

    def __init__(self, bus, spec, outdir, log=None, ask_user=None, abort=None, progress=None, ppd=None, on_spectrum=None,
                 state_path=None):
        self.bus = bus
        self.state_path = cfg.STATE_FILE if state_path is None else state_path   # "" / False disables
        self.spec = spec
        self.outdir = outdir
        self.log = log or (lambda t: None)
        self.ask_user = ask_user or self._no_operator     # unattended: a pause aborts, it is never auto-confirmed
        self.abort = abort                        # threading.Event or None
        self.progress = progress or (lambda i, n, step: None)
        self._ppd = ppd or {}                     # addr -> pulses per degree (read from IN if missing)
        self.positions = {}                       # addr -> last known degrees
        self.shutter_open = None
        self.manifest = []
        self.on_spectrum = on_spectrum or (lambda rec, counts: None)   # live preview hook
        self.it_candidates = []
        self._saved_it = None
        self.wl = bwtek.wavelengths()

    def _check_abort(self):
        if self.abort is not None and self.abort.is_set():
            raise SequenceAbort("aborted by user")

    def _ppd_for(self, addr):
        if addr not in self._ppd:
            info = self.bus.info(addr)
            self._ppd[addr] = float(info.pulses) / info.travel
        return self._ppd[addr]

    def _move(self, addr, deg):
        check_soft_limits(addr, deg)
        ppd = self._ppd_for(addr)
        self._check_unexpected_motion(addr, ppd)
        pulses = self.bus.move_abs(addr, int(round((deg % 360.0) * ppd)))
        actual = (pulses / ppd) % 360.0
        self.positions[addr] = actual
        self._record_state(addr, pulses)
        err = abs(((actual - deg + 180) % 360) - 180)
        if err > 0.3:
            # the bus layer already retried; a wrong angle must abort, never be measured
            raise RuntimeError("stage %s reached %.3f° instead of %.3f° – sequence aborted" % (addr, actual, deg))
        if err > 0.05:
            self.log("WARNING stage %s settled at %.3f° (target %.3f°, off by %.3f°); actual angle recorded" % (addr, actual, deg, actual - deg))
        return actual

    def _check_unexpected_motion(self, addr, ppd):
        """Before moving: has the module moved since we last commanded it (power cycle -> auto-home,
        hand rotation)? Warn for any module; abort for the fibre arm, whose zero would be lost."""
        ref = self.positions.get(addr)
        if ref is None and self.state_path:           # first move of this run: compare with the on-disk record
            rec = (stagestate.load(self.state_path) or {}).get("modules", {}).get(str(addr))
            if rec and rec.get("position") is not None:
                ref = (rec["position"] / ppd) % 360.0
        if ref is None:
            return
        try:
            now = (self.bus.position(addr) / ppd) % 360.0
        except Exception as e:
            self.log("WARNING could not read position of stage %s before moving: %s" % (addr, e))
            return
        diff = abs(((now - ref + 180) % 360) - 180)
        if diff <= cfg.STATE_TOLERANCE_DEG:
            return
        msg = "stage %s is at %.2f° but was left at %.2f° - moved without us (bus power cycle / auto-home / hand rotation?)" % (
            addr, now, ref)
        if addr == cfg.SYSTEM:
            raise RuntimeError(msg + " - detector arm zero may be lost; sequence aborted")
        self.log("WARNING " + msg)

    def _record_state(self, addr, pulses, status=0):
        """Update the on-disk record of where the modules are (no serial traffic)."""
        if not self.state_path:
            return
        try:
            doc = stagestate.load(self.state_path) or {}
            mods = dict(doc.get("modules", {}))
            rec = dict(mods.get(str(addr), {}))
            rec.update({"position": int(pulses), "status": int(status)})
            mods[str(addr)] = rec
            stagestate.save(mods, self.state_path, note="runner move %s" % addr)
        except Exception as e:                        # bookkeeping must never abort a measurement
            self.log("WARNING could not record stage state: %s" % e)

    @staticmethod
    def load_manifest(outdir):
        """Records already saved in this session directory (empty list if none)."""
        path = os.path.join(outdir, "manifest.json")
        if not os.path.isfile(path):
            return []
        try:
            with open(path, encoding="utf-8") as f:
                return list(json.load(f).get("spectra", []))
        except ValueError:
            # unreadable manifest: keep it for forensics instead of silently overwriting it
            aside = path + time.strftime(".corrupt_%Y%m%d_%H%M%S")
            try:
                os.replace(path, aside)
            except OSError:
                pass
            return []

    def run(self, steps):
        os.makedirs(self.outdir, exist_ok=True)
        self.manifest = self.load_manifest(self.outdir)     # append to earlier runs, never overwrite
        n = len(steps)
        try:
            for i, st in enumerate(steps):
                self._check_abort()
                self.progress(i, n, st)
                self.log("[%d/%d] %s" % (i + 1, n, st.text))
                self.run_step(st)
            self.progress(n, n, None)
        except BaseException:
            # abort / error with the shutter open would leave the lamp on the sample and the
            # detector: close it before the exception reaches the GUI; a temporary (double-beam)
            # integration time must not survive the failed run either
            self.close_shutter_safely()
            self.restore_it_safely()
            raise
        finally:
            self._write_manifest()
        return self.manifest

    @staticmethod
    def _no_operator(msg):
        raise SequenceAbort("pause '%s' needs an operator (no ask_user given); use ask_user=lambda m: True to auto-confirm" % msg)

    def restore_it_safely(self):
        """Undo a set_it(save=True) that was not followed by restore_it; never raises."""
        if not self._saved_it:
            return True
        try:
            self.spec.set_integration_time(self._saved_it)
            self.log("integration time restored to %d ms (safety)" % self._saved_it)
            self._saved_it = None
            return True
        except Exception as e:
            self.log("WARNING could not restore the integration time to %s ms: %s" % (self._saved_it, e))
            return False

    def close_shutter_safely(self):
        """Close the shutter unless it is known to be closed; never raises."""
        if self.shutter_open is False:
            return True
        for attempt in (1, 2):
            try:
                pos = self.bus.backward(cfg.SHUTTER)
                self.shutter_open = False
                self._record_state(cfg.SHUTTER, pos if pos is not None else 0)
                self.log("shutter closed (safety)")
                return True
            except Exception as e:
                self.log("WARNING closing the shutter failed (attempt %d): %s" % (attempt, e))
                time.sleep(1.0)
        self.log("!!!! SHUTTER MAY STILL BE OPEN - close it manually (0bw) !!!!")
        return False

    def run_step(self, st):
        """Execute one step (used by run() and by tools that need single verified actions)."""
        p = st.params
        if st.kind == "stage":
            self._move(p["addr"], p["deg"])
        elif st.kind == "shutter":
            if p["open"]:
                pos = self.bus.forward(cfg.SHUTTER)
            else:
                pos = self.bus.backward(cfg.SHUTTER)
            self.shutter_open = p["open"]
            self._record_state(cfg.SHUTTER, pos if pos is not None else (31 if p["open"] else 0))
        elif st.kind == "set_it":
            if p.get("save"):
                self._saved_it = self.spec.integration_ms
            self.spec.set_integration_time(p["ms"])
        elif st.kind == "restore_it":
            if self._saved_it:
                self.spec.set_integration_time(self._saved_it)
                self.log("integration time restored to %d ms" % self._saved_it)
                self._saved_it = None
        elif st.kind == "auto_it":
            self.it_candidates.append(self._auto_it())
        elif st.kind == "apply_min_it":
            if self.it_candidates:
                it = min(self.it_candidates)
                self.spec.set_integration_time(it)
                self.log("integration time set to %d ms (candidates %s)" % (it, self.it_candidates))
                self.it_candidates = []
        elif st.kind == "acquire":
            self._acquire(p["tag"], p["avg"], p.get("meta", {}))
        elif st.kind == "pause":
            if not self.ask_user(p["msg"]):
                raise SequenceAbort("aborted at pause")
        else:
            raise ValueError("unknown step kind %s" % st.kind)

    def _auto_it(self):
        it = self.spec.integration_ms or 100
        for k in range(8):
            self._check_abort()
            counts = self.spec.read(1, 0, 0)
            peak = int(counts[bwtek.ACTIVE_FIRST:bwtek.ACTIVE_LAST + 1].max())
            base = int(counts.min())
            self.log("auto-IT %d: %d ms -> peak %d (%.0f%%)" % (k, it, peak, 100.0 * peak / bwtek.ADC_MAX))
            self.on_spectrum({"tag": "auto-IT %d ms" % it, "integration_ms": it, "peak": peak, "preview_only": True}, counts)
            if bwtek.peak_in_band(peak):
                return it
            if peak - base < 50 and it >= bwtek.AUTO_IT_DARK_MS:
                raise RuntimeError("auto-IT: no light at %d ms (shutter closed? lamp off? fibre?)" % it)
            it = bwtek.next_integration_time(it, peak, base)
            self.spec.set_integration_time(it)
        raise RuntimeError("auto-IT did not converge in 8 steps (last %d ms, peak %d)" % (it, peak))

    def _acquire(self, tag, avg, meta):
        t0 = time.time()
        counts = self.spec.read(avg, 0, 0)
        st = bwtek.spectrum_stats(counts)
        it = self.spec.integration_ms
        # a dark belongs to one integration time: darks at different times must not overwrite each other
        fname = "%s_%dms.csv" % (tag, it) if meta.get("kind") == "dark" else tag + ".csv"
        rec = {"tag": tag, "time": time.strftime("%Y-%m-%d %H:%M:%S"), "integration_ms": it,
               "average": avg, "shutter_open": self.shutter_open,
               "polariser_deg": self.positions.get(cfg.POLARISER), "system_deg": self.positions.get(cfg.SYSTEM),
               "sample_deg": self.positions.get(cfg.SAMPLE), "peak": st["max"], "saturated_active": st["saturated_active"],
               "file": fname, "read_seconds": round(time.time() - t0, 2)}
        rec.update(meta)
        path = os.path.join(self.outdir, rec["file"])
        np.savetxt(path, np.column_stack([self.wl, counts]), fmt="%.3f,%d", header="wavelength_nm,counts", comments="")
        stale = [r for r in self.manifest if r.get("file") == fname]
        if stale:                                   # the CSV was just overwritten: drop records that described it
            self.manifest = [r for r in self.manifest if r.get("file") != fname]
            self.log("replacing %d earlier record(s) of %s" % (len(stale), fname))
        self.manifest.append(rec)
        self._write_manifest()
        self.on_spectrum(rec, counts)
        if st["saturated_active"]:
            self.log("WARNING %s: %d saturated pixels in the active region" % (tag, st["saturated_active"]))
        return counts

    def _write_manifest(self):
        path = os.path.join(self.outdir, "manifest.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:              # write-then-rename: never a half-written manifest
            json.dump({"created": time.strftime("%Y-%m-%d %H:%M:%S"), "config": {
                "POL_DEG": cfg.POL_DEG, "SAMPLE_VAR_OFFSET": cfg.SAMPLE_VAR_OFFSET, "SYSTEM_ZERO": cfg.SYSTEM_ZERO,
                "SYSTEM_DB": cfg.SYSTEM_DB, "SAMPLE_DB": cfg.SAMPLE_DB},
                "spectra": self.manifest}, f, indent=1, ensure_ascii=False)
        os.replace(tmp, path)
