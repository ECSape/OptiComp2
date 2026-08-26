# -*- coding: utf-8 -*-
"""Close the shutter and verify it - the one thing to run after any script died hard.

    py C:\\OptiComp2\\tools\\shutter_close.py [--port COM4]

Exit code 0 when the slider reports position 0 afterwards, 1 otherwise. Nothing else moves.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from hw import config as cfg, elliptec as ell   # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="COM4")
    args = ap.parse_args(argv)
    bus = ell.ElliptecBus(args.port, log=lambda d, t: print("%s %s" % (d, t)))
    try:
        for attempt in (1, 2):
            try:
                pos = bus.backward(cfg.SHUTTER)
                print("shutter closed, position %s" % pos)
                return 0 if pos == 0 else 1
            except ell.ElliptecError as e:
                print("attempt %d failed: %s" % (attempt, e))
        print("!!!! SHUTTER STATE UNKNOWN")
        return 1
    finally:
        bus.close()


if __name__ == "__main__":
    sys.exit(main())
