# -*- coding: utf-8 -*-
"""Movement-cycle test: does the signal at a fixed geometry change after the stages have moved?

    py C:\\OptiComp2\\tools\\cycle_test.py --cycles 3 --frames 20 --moves both --tag si

Baseline segment: shutter open at (arm --system, sample --sample, polariser --pol), `frames` spectra.
Then, `cycles` times: shutter closed, perform a set of movements, come back, open, `frames` spectra.
Movement sets (all end back at the baseline geometry):
  sample   : sample stage to theta 0 (105 deg) and back            - sample-stage backlash / wafer settling
  arm      : detector arm to the DB position (124 deg) and back    - arm repeatability
  scan     : polariser P then S, sample sweep theta 8..80 step 8   - what an angle scan does
  exchange : the double-beam choreography (sample 120, arm 150, arm 124, sample 93, back) without the pauses
  both     : scan + exchange
Per segment the log shows the band means and their change relative to the baseline segment, so the
movement that produces the 15-25 % drops seen on 2026-08-25 can be identified. Everything is saved to
data/cycle/<tag>_<timestamp>.csv (per frame, with the segment index) and .npz.
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
import monitor                                    # noqa: E402  (Log, fakes, band_means, BANDS)
import sequence as sq                             # noqa: E402


class _StopRequested(Exception):
    pass


def move_set(kind, sample_deg, system_deg, pol):
    back = [sq.stage(cfg.SYSTEM, system_deg, "arm back"), sq.stage(cfg.SAMPLE, sample_deg, "sample back")]
    if kind == "sample":
        return [sq.sample_theta(0)] + back
    if kind == "arm":
        return [sq.stage(cfg.SYSTEM, cfg.SYSTEM_DB, "arm DB")] + back
    if kind == "scan":
        other = "P" if pol == "S" else "S"
        steps = [sq.polariser(other)] + [sq.sample_theta(t) for t in range(8, 81, 8)] + [sq.polariser(pol)]
        return steps + back
    if kind == "exchange":
        return [sq.stage(cfg.SAMPLE, cfg.SAMPLE_EXCHANGE, "sample exchange"), sq.stage(cfg.SYSTEM, cfg.SYSTEM_EXCHANGE, "arm exchange"),
                sq.stage(cfg.SYSTEM, cfg.SYSTEM_DB, "arm DB"), sq.stage(cfg.SAMPLE, cfg.SAMPLE_DB, "sample DB"),
                sq.stage(cfg.SAMPLE, cfg.SAMPLE_EXCHANGE, "sample exchange"), sq.stage(cfg.SYSTEM, cfg.SYSTEM_EXCHANGE, "arm exchange")] + back
    if kind == "both":
        return move_set("scan", sample_deg, system_deg, pol) + move_set("exchange", sample_deg, system_deg, pol)
    raise ValueError(kind)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cycles", type=int, default=3)
    ap.add_argument("--frames", type=int, default=20, help="spectra per segment")
    ap.add_argument("--moves", default="both", choices=["sample", "arm", "scan", "exchange", "both"])
    ap.add_argument("--it", type=int, default=997)
    ap.add_argument("--avg", type=int, default=1)
    ap.add_argument("--interval", type=float, default=0.25)
    ap.add_argument("--pol", default="S", choices=["S", "P"])
    ap.add_argument("--sample", type=float, default=185.0, help="baseline sample stage degrees (theta + 105)")
    ap.add_argument("--system", type=float, default=cfg.SYSTEM_ZERO)
    ap.add_argument("--force", action="store_true", help="run even if the stage-state check reports anomalies")
    ap.add_argument("--port", default="COM4")
    ap.add_argument("--tag", default="cycle")
    ap.add_argument("--root", default=os.path.join(HERE, ".."), help=argparse.SUPPRESS)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--dry-fail-at", type=int, default=0, help=argparse.SUPPRESS)
    ap.add_argument("--no-recover", action="store_true", help="abort on a spectrometer read failure instead of recovering")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--stop-file", default=None, help="finish cleanly when this file appears (default logs/STOP)")
    args = ap.parse_args(argv)

    ts = time.strftime("%Y%m%d_%H%M%S")
    root = args.root
    os.makedirs(os.path.join(root, "logs"), exist_ok=True)
    os.makedirs(os.path.join(root, "data", "cycle"), exist_ok=True)
    log = monitor.Log(os.path.join(root, "logs", "cycle_%s.log" % ts), echo=not args.quiet)
    stem = os.path.join(root, "data", "cycle", "%s_%s" % (args.tag, ts))
    state_path = os.path.join(root, "data", "stage_state.json") if args.dry else cfg.STATE_FILE
    stop_file = args.stop_file or os.path.join(root, "logs", "STOP")
    if os.path.exists(stop_file):
        os.remove(stop_file)
    log("cycle test start: %s" % vars(args))
    log("create %s to stop cleanly (never taskkill: the shutter would stay open)" % stop_file)

    wl = bwtek.wavelengths()
    bus = spec = runner = None
    shutter_open = False
    rows, frames, seg_of, dark = [], [], [], None
    darks = []                                     # one dark per segment (shutter closed after the movements)
    segments = []                                  # (label, [band means per frame])
    rc = 0
    try:
        if args.dry:
            bus = monitor._FakeBus()
            spec = monitor._FakeSpec(fail_at=args.dry_fail_at, bus=bus)
        else:
            from hw import elliptec as ell
            bus = ell.ElliptecBus(args.port, log=log.bus)
            spec = bwtek.BWTek(log=log)
        n = spec.open()
        spec.set_integration_time(args.it)
        log("spectrometer open (%s), IT %d ms" % (n, args.it))

        stagestate.protect(bus)
        ppd = {}
        for a in (cfg.SYSTEM, cfg.SAMPLE, cfg.POLARISER):
            info = bus.info(a)
            ppd[a] = float(info.pulses) / info.travel
        problems, live = stagestate.check(bus, state_path, ppd=ppd, log=log)
        log("stage state: %s" % ", ".join("%s=%s%s" % (a, live[a].get("deg", live[a]["position"]), "" if live[a]["status"] == 0 else "/GS%02X" % live[a]["status"]) for a in sorted(live)))
        if problems and not args.force:
            log("!! stage state anomalies - aborting before any motion (restore the stages, then --force)")
            return 2
        if stagestate.arm_reference_lost(live):
            log("!! detector arm reports a failed home: refusing to run")
            return 2
        stagestate.apply_velocities(bus, log=log)
        runner = sq.Runner(bus, spec, os.path.join(root, "data", "cycle"), log=log, ppd=ppd, state_path=state_path)

        for st in [sq.shutter(False), sq.stage(cfg.SYSTEM, args.system, "arm"), sq.stage(cfg.SAMPLE, args.sample, "sample"), sq.polariser(args.pol)]:
            runner.run_step(st)
        def take_dark(label):
            d = spec.read(3, 0, 0)
            darks.append(d)
            log("dark (%s, avg 3): median %d, max %d" % (label, int(np.median(d)), int(d.max())))
            return d

        dark = take_dark("baseline")

        recoveries = [0]

        def read_frame():
            """One spectrum; a DLL/USB failure is recovered like in monitor.py (up to MAX_RECOVERIES)."""
            while True:
                if os.path.exists(stop_file):
                    raise _StopRequested()
                try:
                    return spec.read(args.avg, 0, 0)
                except bwtek.BWTekError as e:
                    log("!! spectrometer read failed: %s" % e)
                    if args.no_recover or recoveries[0] >= monitor.MAX_RECOVERIES:
                        raise
                    recoveries[0] += 1
                    how = spec.recover()
                    spec.set_integration_time(args.it)
                    log("recovered (%s), %d/%d" % (how, recoveries[0], monitor.MAX_RECOVERIES))

        def measure(label):
            runner.run_step(sq.shutter(True))
            means = []
            try:
                for k in range(args.frames):
                    c = read_frame()
                    m = monitor.band_means(c, dark, wl)
                    means.append(m)
                    st = bwtek.spectrum_stats(c)
                    if st["saturated_active"]:
                        log("WARNING %s frame %d: %d saturated pixels" % (label, k + 1, st["saturated_active"]))
                    rows.append((len(segments), label, time.strftime("%H:%M:%S"), st["max"], float(np.median(c[:bwtek.ACTIVE_FIRST])), m))
                    frames.append(c.copy())
                    seg_of.append(len(segments))
                    if args.interval > 0:
                        time.sleep(args.interval)
            finally:
                runner.run_step(sq.shutter(False))
            arr = np.array(means)
            segments.append((label, arr))
            base = segments[0][1].mean(axis=0)
            rel = 100.0 * (arr.mean(axis=0) / base - 1.0)
            noise = 100.0 * arr.std(axis=0) / arr.mean(axis=0)
            log("segment %d %-22s bands %s | vs baseline %s | frame noise %s" % (
                len(segments) - 1, label, " ".join("%.0f" % v for v in arr.mean(axis=0)),
                " ".join("%+.2f%%" % v for v in rel), " ".join("%.2f%%" % v for v in noise)))

        shutter_open = True
        measure("baseline")
        for c in range(1, args.cycles + 1):
            log("--- cycle %d: movements '%s' (shutter closed)" % (c, args.moves))
            for st in move_set(args.moves, args.sample, args.system, args.pol):
                runner.run_step(st)
            dark = take_dark("cycle %d" % c)          # thermal drift of the baseline over a long test
            measure("after %s #%d" % (args.moves, c))
        shutter_open = False
        log("summary (band means relative to baseline, %s):" % " / ".join("%d-%d" % b for b in monitor.BANDS))
        base = segments[0][1].mean(axis=0)
        for i, (label, arr) in enumerate(segments):
            log("  %d %-22s %s" % (i, label, " ".join("%+.2f%%" % v for v in 100.0 * (arr.mean(axis=0) / base - 1.0))))
    except _StopRequested:
        log("stop file found - finishing early")
    except KeyboardInterrupt:
        log("interrupted by user")
        rc = 1
    except Exception as e:
        log("!! %s: %s" % (type(e).__name__, e))
        rc = 1
    finally:
        try:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        except Exception:
            pass
        clean = True
        if runner is not None:
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
        if rows:
            try:
                with open(stem + ".csv", "w") as fh:
                    fh.write("segment,label,time,peak,baseline," + ",".join("mean_%d_%d" % b for b in monitor.BANDS) + "\n")
                    for seg, label, t, peak, base_, m in rows:
                        fh.write("%d,%s,%s,%d,%.0f,%s\n" % (seg, label, t, peak, base_, ",".join("%.1f" % v for v in m)))
                np.savez_compressed(stem + ".npz", wavelength=wl, spectra=np.array(frames, dtype=np.uint16), segment=np.array(seg_of),
                                    dark=dark, darks=np.array(darks, dtype=np.uint16), integration_ms=args.it, average=args.avg,
                                    labels=np.array([s[0] for s in segments]))
                log("saved %s.csv / .npz" % stem)
            except Exception as e:
                log("!! saving failed: %s" % e)
        if bus is not None and runner is not None:
            try:
                stagestate.record(bus, state_path, note="cycle_test end", ppd=ppd)
            except Exception as e:
                log("!! recording stage state failed: %s" % e)
        for obj in (spec, bus):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
        log("cycle test end (%s)" % ("clean" if clean else "SHUTTER STATE UNKNOWN"))
        if not clean:
            return 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
