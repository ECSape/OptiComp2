# -*- coding: utf-8 -*-
"""Unattended stability monitor: park the stages, open the shutter, read spectra for N minutes.

Run on the lab PC (the manual GUI must have released COM4 and the spectrometer):

    py C:\\OptiComp2\\tools\\monitor.py --minutes 30 --it 997 --pol S --sample 185 --tag white

Writes data/monitor/<tag>_<timestamp>.csv (one row per frame: peak, baseline, dark-subtracted band
means and their change relative to the first frame), the same stem .npz with every spectrum, and
logs/monitor_<timestamp>.log. The shutter is closed again on exit, including on errors and Ctrl-C.
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
from hw import bwtek, config as cfg          # noqa: E402
import sequence as sq                        # noqa: E402

BANDS = ((450, 550), (600, 700), (800, 900))
POL_DEG = {"S": cfg.POL_DEG["S"], "P": cfg.POL_DEG["P"]}


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
    def info(self, addr):
        return _FakeInfo()

    def move_abs(self, addr, pulses):
        return pulses

    def forward(self, addr):
        return 31

    def backward(self, addr):
        return 0

    def close(self):
        pass


class _FakeSpec(object):
    integration_ms = 100

    def open(self):
        return 1

    def set_integration_time(self, ms):
        self.integration_ms = ms
        return ms

    def read(self, avg=1, st=0, sv=0):
        time.sleep(0.05)
        c = np.full(2048, 900, np.uint16)
        c[254:2031] = 20000 + np.random.randint(-50, 50, 1777)
        return c

    def close(self):
        pass


def band_means(counts, dark, wl):
    a = counts[bwtek.ACTIVE_FIRST:bwtek.ACTIVE_LAST + 1].astype(float) - dark[bwtek.ACTIVE_FIRST:bwtek.ACTIVE_LAST + 1].astype(float)
    w = wl[bwtek.ACTIVE_FIRST:bwtek.ACTIVE_LAST + 1]
    return [float(np.mean(a[(w >= lo) & (w < hi)])) for lo, hi in BANDS]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--minutes", type=float, default=30.0)
    ap.add_argument("--it", type=int, default=997, help="integration time ms")
    ap.add_argument("--avg", type=int, default=1)
    ap.add_argument("--interval", type=float, default=0.0, help="extra seconds between frames")
    ap.add_argument("--pol", default="S", choices=["S", "P", "none"], help="polariser position (none = leave as is)")
    ap.add_argument("--sample", type=float, default=185.0, help="sample stage absolute degrees (theta + 105)")
    ap.add_argument("--system", type=float, default=cfg.SYSTEM_ZERO, help="detector arm absolute degrees")
    ap.add_argument("--no-move", action="store_true", help="do not move any stage (shutter only)")
    ap.add_argument("--port", default="COM4")
    ap.add_argument("--tag", default="monitor")
    ap.add_argument("--dry", action="store_true", help="fake hardware, for testing the script")
    ap.add_argument("--quiet", action="store_true", help="log to file only (use when started detached)")
    args = ap.parse_args(argv)

    ts = time.strftime("%Y%m%d_%H%M%S")
    root = os.path.join(HERE, "..")
    os.makedirs(os.path.join(root, "logs"), exist_ok=True)
    os.makedirs(os.path.join(root, "data", "monitor"), exist_ok=True)
    log = Log(os.path.join(root, "logs", "monitor_%s.log" % ts), echo=not args.quiet)
    stem = os.path.join(root, "data", "monitor", "%s_%s" % (args.tag, ts))
    log("monitor start: %s" % vars(args))

    wl = bwtek.wavelengths()
    bus = spec = None
    runner = None
    shutter_open = False
    frames, times, dark2 = [], [], None
    try:
        if args.dry:
            bus, spec = _FakeBus(), _FakeSpec()
        else:
            from hw import elliptec as ell
            bus = ell.ElliptecBus(args.port, log=log.bus)
            spec = bwtek.BWTek(log=log)
        n = spec.open()
        log("spectrometer open (%s), IT -> %d ms" % (n, args.it))
        spec.set_integration_time(args.it)
        runner = sq.Runner(bus, spec, os.path.join(root, "data", "monitor"), log=log)

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
            counts = spec.read(args.avg, 0, 0)
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
        log("done: %d frames in %.0f s" % (k, time.time() - t0))
    except KeyboardInterrupt:
        log("interrupted by user")
    except Exception as e:
        log("!! %s: %s" % (type(e).__name__, e))
        raise
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
        for obj, name in ((spec, "spectrometer"), (bus, "bus")):
            if obj is not None:
                try:
                    obj.close()
                except Exception as e:
                    log("!! closing %s failed: %s" % (name, e))
        log("monitor end (%s)" % ("clean" if clean else "SHUTTER STATE UNKNOWN"))
        if not clean:
            return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
