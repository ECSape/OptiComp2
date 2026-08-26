# -*- coding: utf-8 -*-
"""Unattended stability monitor: park the stages, open the shutter, read spectra for N minutes.

Run on the lab PC (the manual GUI must have released COM4 and the spectrometer):

    py C:\\OptiComp2\\tools\\monitor.py --minutes 30 --it 997 --pol S --sample 185 --tag white

Writes data/monitor/<tag>_<timestamp>.csv (one row per frame: peak, baseline, dark-subtracted band
means and their change relative to the first frame), the same stem .npz with every spectrum, and
logs/monitor_<timestamp>.log. The shutter is closed again on exit, including on errors and Ctrl-C.

Safety (2026-08-26): before anything moves, the bus is compared with the last recorded stage state;
a module that moved on its own (bus power cycle -> auto-home) or a failed home on the fibre arm
aborts the run unless --force. A spectrometer read failure triggers close/reopen and, if needed, a
Windows PnP restart of the spectrometer's USB device (never the hub), then the run continues.
"""
import argparse
import os
import signal
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
from hw import bwtek, config as cfg, stagestate   # noqa: E402
import sequence as sq                             # noqa: E402

BANDS = ((450, 550), (600, 700), (800, 900))
POL_DEG = {"S": cfg.POL_DEG["S"], "P": cfg.POL_DEG["P"]}
MAX_RECOVERIES = 3            # spectrometer recoveries per run before giving up


class Log(object):
    """File logger; echoes to stdout only when asked (a detached process with a redirected stdout
    and no reader blocks forever on print once the pipe fills - seen 2026-08-26)."""

    def __init__(self, path, echo=True):
        self.fh = open(path, "a", encoding="utf-8")
        self.echo = echo

    def __call__(self, text):
        line = "%s %s" % (time.strftime("%H:%M:%S"), text)
        self.fh.write(line + "\n")
        self.fh.flush()
        if self.echo:
            try:
                print(line)
                sys.stdout.flush()
            except Exception:
                self.echo = False

    def bus(self, direction, text):          # ElliptecBus log signature
        self("%s %s" % (direction, text))


# ---- dry-run fakes (no hardware) ---------------------------------------------------
class _FakeInfo(object):
    pulses, travel = 143360, 360


class _FakeBus(object):
    def __init__(self):
        self.pos = {"0": 0, "1": 93982, "2": 17522, "3": 73671}
        self.protected_home = set()

    def info(self, addr):
        return _FakeInfo()

    def position(self, addr):
        return self.pos[str(addr)]

    def status(self, addr):
        return 0

    def velocity(self, addr):
        return 64

    def set_velocity(self, addr, pct):
        return None

    def move_abs(self, addr, pulses):
        self.pos[str(addr)] = pulses
        return pulses

    def home(self, addr, direction=0, force=False):
        if str(addr) in self.protected_home and not force:
            raise bwtek.BWTekError("home on module %s is blocked [dry]" % addr)   # mirrors ElliptecError
        self.pos[str(addr)] = 0
        return 0

    def forward(self, addr):
        self.pos[str(addr)] = 31
        return 31

    def backward(self, addr):
        self.pos[str(addr)] = 0
        return 0

    def close(self):
        pass


class _FakeSpec(object):
    integration_ms = 100

    def __init__(self, fail_at=0, bus=None):
        self.fail_at = fail_at            # raise on this frame number (0 = never), once
        self.reads = 0
        self.recoveries = 0
        self.opened = False
        self.bus = bus                    # when linked, light only with the fake shutter open (slider at 31)

    def open(self):
        self.opened = True
        return 1

    def set_integration_time(self, ms):
        self.integration_ms = ms
        return ms

    def read(self, avg=1, st=0, sv=0):
        self.reads += 1
        if self.fail_at and self.reads == self.fail_at:
            raise bwtek.BWTekError("bwtekReadResultUSB failed (-99) [dry]")
        time.sleep(0.02)
        c = np.full(2048, 900, np.uint16)
        lit = self.bus is None or self.bus.pos.get("0") == 31
        c[254:2031] = (20000 if lit else 950) + np.random.randint(-50, 50, 1777)
        return c

    def recover(self, usb_restart=True):
        self.recoveries += 1
        return "reopened [dry]"

    def close(self):
        self.opened = False


def band_means(counts, dark, wl):
    a = counts[bwtek.ACTIVE_FIRST:bwtek.ACTIVE_LAST + 1].astype(float) - dark[bwtek.ACTIVE_FIRST:bwtek.ACTIVE_LAST + 1].astype(float)
    w = wl[bwtek.ACTIVE_FIRST:bwtek.ACTIVE_LAST + 1]
    return [float(np.mean(a[(w >= lo) & (w < hi)])) for lo, hi in BANDS]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--minutes", type=float, default=30.0)
    ap.add_argument("--it", type=int, default=997, help="integration time ms")
    ap.add_argument("--avg", type=int, default=1)
    ap.add_argument("--interval", type=float, default=0.25, help="seconds between frames (the DLL hung after 590 back-to-back reads)")
    ap.add_argument("--pol", default="S", choices=["S", "P", "none"], help="polariser position (none = leave as is)")
    ap.add_argument("--sample", type=float, default=185.0, help="sample stage absolute degrees (theta + 105)")
    ap.add_argument("--system", type=float, default=cfg.SYSTEM_ZERO, help="detector arm absolute degrees")
    ap.add_argument("--no-move", action="store_true", help="do not move any stage (shutter only)")
    ap.add_argument("--force", action="store_true", help="run even if the stage-state check reports anomalies")
    ap.add_argument("--no-recover", action="store_true", help="stop at the first spectrometer error instead of recovering")
    ap.add_argument("--port", default="COM4")
    ap.add_argument("--tag", default="monitor")
    ap.add_argument("--root", default=os.path.join(HERE, ".."), help=argparse.SUPPRESS)   # tests
    ap.add_argument("--dry", action="store_true", help="fake hardware, for testing the script")
    ap.add_argument("--dry-fail-at", type=int, default=0, help=argparse.SUPPRESS)          # tests
    ap.add_argument("--quiet", action="store_true", help="log to file only (use when started detached)")
    ap.add_argument("--stop-file", default=None, help="finish cleanly when this file appears (default logs/STOP); taskkill would skip the shutter close")
    args = ap.parse_args(argv)

    ts = time.strftime("%Y%m%d_%H%M%S")
    root = args.root
    os.makedirs(os.path.join(root, "logs"), exist_ok=True)
    os.makedirs(os.path.join(root, "data", "monitor"), exist_ok=True)
    log = Log(os.path.join(root, "logs", "monitor_%s.log" % ts), echo=not args.quiet)
    stem = os.path.join(root, "data", "monitor", "%s_%s" % (args.tag, ts))
    state_path = os.path.join(root, "data", "stage_state.json") if args.dry else cfg.STATE_FILE
    stop_file = args.stop_file or os.path.join(root, "logs", "STOP")
    if os.path.exists(stop_file):
        os.remove(stop_file)
    log("monitor start: %s" % vars(args))
    log("create %s to stop cleanly (never taskkill: the shutter would stay open)" % stop_file)

    wl = bwtek.wavelengths()
    bus = spec = None
    runner = None
    shutter_open = False
    frames, times, dark2 = [], [], None
    recoveries = 0
    rc = 0
    try:
        if args.dry:
            bus = _FakeBus()
            spec = _FakeSpec(args.dry_fail_at, bus=bus)
        else:
            from hw import elliptec as ell
            bus = ell.ElliptecBus(args.port, log=log.bus)
            spec = bwtek.BWTek(log=log)
        n = spec.open()
        log("spectrometer open (%s), IT -> %d ms" % (n, args.it))
        spec.set_integration_time(args.it)

        # ---- bus health before anything moves
        stagestate.protect(bus)
        ppd = {}
        for a in (cfg.SYSTEM, cfg.SAMPLE, cfg.POLARISER):
            info = bus.info(a)
            ppd[a] = float(info.pulses) / info.travel
        problems, live = stagestate.check(bus, state_path, ppd=ppd, log=log)
        log("stage state: %s" % ", ".join("%s=%s%s" % (a, live[a].get("deg", live[a]["position"]), "" if live[a]["status"] == 0 else "/GS%02X" % live[a]["status"]) for a in sorted(live)))
        if problems and not args.force:
            log("!! stage state anomalies (%d) - aborting before any motion; inspect the setup, restore the arm zero, then use --force" % len(problems))
            return 2
        if stagestate.arm_reference_lost(live) and not args.no_move:
            log("!! detector arm reports a failed home (GS02): its zero is lost - refusing to move it even with --force")
            return 2
        stagestate.apply_velocities(bus, log=log)

        runner = sq.Runner(bus, spec, os.path.join(root, "data", "monitor"), log=log, ppd=ppd, state_path=state_path)

        steps = [sq.shutter(False)]
        if not args.no_move:
            steps += [sq.stage(cfg.SYSTEM, args.system, "detector arm"),
                      sq.stage(cfg.SAMPLE, args.sample, "sample stage")]
            if args.pol != "none":
                steps.append(sq.polariser(args.pol))
        for st in steps:                      # run one by one so a failure is attributable
            runner.run_step(st)

        dark = spec.read(3, 0, 0)
        log("dark (avg 3): peak %d, median %d" % (int(dark.max()), int(np.median(dark))))

        shutter_open = True                  # set on intent: a failed verify may still have moved the slider
        runner.run_step(sq.shutter(True))

        fh = open(stem + ".csv", "w")
        fh.write("time,elapsed_s,peak,baseline," + ",".join("mean_%d_%d" % b for b in BANDS)
                 + "," + ",".join("rel_%d_%d_pct" % b for b in BANDS) + "\n")
        t0 = time.time()
        first = None
        k = 0
        while time.time() - t0 < args.minutes * 60.0:
            if os.path.exists(stop_file):
                log("stop file found - finishing after %d frames" % k)
                break
            try:
                counts = spec.read(args.avg, 0, 0)
            except bwtek.BWTekError as e:
                log("!! spectrometer read failed at frame %d: %s" % (k + 1, e))
                if args.no_recover or recoveries >= MAX_RECOVERIES:
                    raise
                recoveries += 1
                how = spec.recover()          # raises BWTekError when nothing works -> outer handler
                log("!! recovered (%s), %d/%d recoveries used; %.0f s gap in the series" % (how, recoveries, MAX_RECOVERIES, time.time() - t0 - (times[-1] if times else 0)))
                continue
            el = time.time() - t0
            st = bwtek.spectrum_stats(counts)
            base = float(np.median(counts[:bwtek.ACTIVE_FIRST]))
            means = band_means(counts, dark, wl)
            if first is None:
                first = means
            rel = [100.0 * (m / f - 1.0) if abs(f) > 50.0 else float("nan") for m, f in zip(means, first)]
            fh.write("%s,%.1f,%d,%.0f,%s,%s\n" % (time.strftime("%H:%M:%S"), el, st["max"], base,
                                                  ",".join("%.1f" % m for m in means), ",".join("%.2f" % r for r in rel)))
            fh.flush()
            frames.append(counts.copy())
            times.append(el)
            k += 1
            if k == 1 or k % 30 == 0:
                log("frame %d t=%.0fs peak=%d sat=%d base=%.0f rel=%s" % (k, el, st["max"], st["saturated_active"], base,
                                                                         " ".join("%+.2f%%" % r for r in rel)))
            if args.interval > 0:
                time.sleep(args.interval)
        fh.close()
        log("done: %d frames in %.0f s (%d recoveries)" % (k, time.time() - t0, recoveries))
    except KeyboardInterrupt:
        log("interrupted by user")
    except Exception as e:
        log("!! %s: %s" % (type(e).__name__, e))
        rc = 1
    finally:
        # Cleanup must survive a second Ctrl-C: ignore SIGINT while the shutter is being closed.
        try:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        except Exception:
            pass
        clean = True
        if runner is not None:
            # Always try to close the shutter, whatever the flags say: a verified 'fw' that raised may
            # still have moved the slider. Two attempts, then shout.
            for attempt in (1, 2):
                try:
                    runner.run_step(sq.shutter(False))
                    shutter_open = False
                    break
                except Exception as e:
                    log("!! closing shutter failed (attempt %d): %s" % (attempt, e))
                    time.sleep(1.0)
            if shutter_open:
                clean = False
                log("!!!! SHUTTER MAY STILL BE OPEN - close it manually (0bw) !!!!")
            elif spec is not None and frames:
                try:
                    dark2 = spec.read(3, 0, 0)
                    log("dark after (avg 3): median %d" % int(np.median(dark2)))
                except Exception as e:
                    log("!! dark-after read failed: %s" % e)
        if frames:
            try:
                np.savez_compressed(stem + ".npz", wavelength=wl, elapsed_s=np.array(times), spectra=np.array(frames, dtype=np.uint16),
                                    dark=dark, dark_after=dark2 if dark2 is not None else np.zeros(0, np.uint16),
                                    integration_ms=args.it, average=args.avg)
                log("saved %s.csv / .npz" % stem)
            except Exception as e:
                log("!! saving npz failed: %s" % e)
        if bus is not None and runner is not None:
            try:
                stagestate.record(bus, state_path, note="monitor end")
            except Exception as e:
                log("!! recording stage state failed: %s" % e)
        for obj, name in ((spec, "spectrometer"), (bus, "bus")):
            if obj is not None:
                try:
                    obj.close()
                except Exception as e:
                    log("!! closing %s failed: %s" % (name, e))
        log("monitor end (%s)" % ("clean" if clean else "SHUTTER STATE UNKNOWN"))
        if not clean:
            return 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
