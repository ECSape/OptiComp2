# -*- coding: utf-8 -*-
"""Restore the stage references after a bus power event (modules auto-home at power-up).

    py C:\\OptiComp2\\tools\\restore_stages.py                 # report only (read-only queries)
    py C:\\OptiComp2\\tools\\restore_stages.py --safe          # home polariser + sample stage, park at S / 185 deg
    py C:\\OptiComp2\\tools\\restore_stages.py --arm           # ALSO home the fibre arm (operator watching!) and park at 44 deg

Order with --safe --arm: shutter closed, polariser home -> S, sample home -> --sample, arm speed 50 %,
arm home (ho0, as the original program did on every start) -> --system. The arm step is refused
unless the fibre has been checked: the script asks for a typed confirmation unless --yes.
Everything is logged to logs/restore_<timestamp>.log and the final state is recorded for the
consistency checks of the GUI / monitor / sequences.
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
from hw import config as cfg, elliptec as ell, stagestate   # noqa: E402
import monitor                                            # noqa: E402  (Log, _FakeBus)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--safe", action="store_true", help="home polariser and sample stage, park them")
    ap.add_argument("--arm", action="store_true", help="home the fibre arm too (needs an operator at the instrument)")
    ap.add_argument("--yes", action="store_true", help="skip the typed confirmation for --arm")
    ap.add_argument("--pol", default="S", choices=["S", "P"])
    ap.add_argument("--sample", type=float, default=185.0)
    ap.add_argument("--system", type=float, default=cfg.SYSTEM_ZERO)
    ap.add_argument("--port", default="COM4")
    ap.add_argument("--root", default=os.path.join(HERE, ".."), help=argparse.SUPPRESS)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    ts = time.strftime("%Y%m%d_%H%M%S")
    os.makedirs(os.path.join(args.root, "logs"), exist_ok=True)
    log = monitor.Log(os.path.join(args.root, "logs", "restore_%s.log" % ts), echo=not args.quiet)
    state_path = os.path.join(args.root, "data", "stage_state.json") if args.dry else cfg.STATE_FILE
    log("restore start: %s" % vars(args))

    if args.arm and not args.yes:
        ans = ""
        interactive = False
        try:
            interactive = sys.stdin is not None and sys.stdin.isatty()
        except Exception:
            interactive = False
        if not interactive:                         # detached / SSH without a terminal: nobody is watching the arm
            log("arm home needs an interactive console (or --yes with an operator present)")
        else:
            try:
                ans = input("Fibre checked and slack, operator watching the arm? type YES to home module 2: ")
            except (EOFError, OSError):             # console closed under us
                ans = ""
        if ans.strip() != "YES":
            log("arm home not confirmed - aborting")
            return 2

    bus = monitor._FakeBus() if args.dry else ell.ElliptecBus(args.port, log=log.bus)
    rc = 0
    ppd = None
    try:
        stagestate.protect(bus)
        ppd = {}
        for a in (cfg.POLARISER, cfg.SYSTEM, cfg.SAMPLE):
            info = bus.info(a)
            ppd[a] = float(info.pulses) / info.travel
        problems, live = stagestate.check(bus, state_path, ppd=ppd, log=log)
        log("stage state: %s" % ", ".join("%s=%s%s" % (a, live[a].get("deg", live[a]["position"]), "" if live[a]["status"] == 0 else "/GS%02X" % live[a]["status"]) for a in sorted(live)))
        if not (args.safe or args.arm):
            log("report only (use --safe / --arm to act)")
            return 0

        def park(addr, deg, what):
            pulses = bus.move_abs(addr, int(round((deg % 360.0) * ppd[addr])))
            got = (pulses / ppd[addr]) % 360.0
            log("%s -> %.2f deg (asked %.2f)" % (what, got, deg))
            if abs(((got - deg + 180) % 360) - 180) > 0.3:
                raise RuntimeError("%s settled at %.2f deg instead of %.2f" % (what, got, deg))

        log("shutter close")
        bus.backward(cfg.SHUTTER)
        if args.safe:
            log("polariser home")
            bus.home(cfg.POLARISER, 0)
            park(cfg.POLARISER, cfg.POL_DEG[args.pol], "polariser %s" % args.pol)
            log("sample stage home")
            bus.home(cfg.SAMPLE, 0)
            park(cfg.SAMPLE, args.sample, "sample stage")
        if args.arm:
            stagestate.apply_velocities(bus, log=log)
            log("ARM HOME (ho0) - watch the fibre")
            bus.home(cfg.SYSTEM, 0, force=True)
            park(cfg.SYSTEM, args.system, "detector arm")
        problems, live = stagestate.check(bus, "", ppd=ppd, log=log)   # status codes only
        log("final: %s" % ", ".join("%s=%s%s" % (a, live[a].get("deg", live[a]["position"]), "" if live[a]["status"] == 0 else "/GS%02X" % live[a]["status"]) for a in sorted(live)))
        if problems:
            log("!! modules still report problems: %s" % "; ".join(problems))
            rc = 1
    except Exception as e:
        log("!! %s: %s" % (type(e).__name__, e))
        rc = 1
    finally:
        if args.safe or args.arm:                  # a report-only run must not become the baseline
            try:
                stagestate.record(bus, state_path, note="restore_stages", ppd=ppd)
                log("stage state recorded")
            except Exception as e:
                log("!! recording stage state failed: %s" % e)
        try:
            bus.close()
        except Exception:
            pass
        log("restore end (rc %d)" % rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
