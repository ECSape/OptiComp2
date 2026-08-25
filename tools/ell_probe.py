# -*- coding: utf-8 -*-
"""Read-only probe of the Elliptec bus: identifies modules 0-3 without moving anything.

Only query commands are sent (in / gs / gp / gv). Safe to run while the user is away.
Usage:  py tools/ell_probe.py [--port COM4] [--addrs 0123]
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from hw import elliptec as ell


def log(direction, text):
    print("  %s %-6s %s" % (time.strftime("%H:%M:%S"), direction, text))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM4")
    ap.add_argument("--addrs", default="0123")
    ap.add_argument("--timeout", type=float, default=3.0)
    args = ap.parse_args()

    print("Opening %s @ %d baud (timeout %.1fs)" % (args.port, ell.BAUDRATE, args.timeout))
    bus = ell.ElliptecBus(args.port, timeout=args.timeout, log=log)
    try:
        for addr in args.addrs:
            print("\n=== module %s ===" % addr)
            try:
                info = bus.info(addr)
                print("  -> " + info.describe())
            except ell.ElliptecError as e:
                print("  -> info failed: %s" % e)
                continue
            try:
                code = bus.status(addr)
                print("  -> status %02X (%s)" % (code, ell.STATUS_CODES.get(code, "?")))
            except ell.ElliptecError as e:
                print("  -> status failed: %s" % e)
            try:
                pos = bus.position(addr)
                unit = ""
                if info.pulses_per_unit:
                    unit = "  = %.3f %s" % (pos / info.pulses_per_unit, "deg" if info.travel == 360 else "mm")
                print("  -> position %d pulses%s" % (pos, unit))
            except ell.ElliptecError as e:
                print("  -> position failed: %s" % e)
            try:
                v = bus.velocity(addr)
                print("  -> velocity %s%%" % v)
            except ell.ElliptecError as e:
                print("  -> velocity failed: %s" % e)
    finally:
        bus.close()
        print("\nPort closed.")


if __name__ == "__main__":
    main()
