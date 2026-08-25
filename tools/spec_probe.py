# -*- coding: utf-8 -*-
"""Spectrometer probe: open DLL, set integration time, read one spectrum, print stats.

Usage: py tools/spec_probe.py [--it 100] [--avg 1] [--save out.csv]
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import numpy as np
from hw import bwtek


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--it", type=int, default=100, help="integration time in ms")
    ap.add_argument("--avg", type=int, default=1)
    ap.add_argument("--raw", action="store_true", help="no smoothing")
    ap.add_argument("--save", default=None)
    args = ap.parse_args()

    log = lambda t: print("  %s %s" % (time.strftime("%H:%M:%S"), t))
    spec = bwtek.BWTek(log=log)
    try:
        print("devices:", spec.open())
        spec.set_integration_time(args.it)
        sm = (0, 0) if args.raw else (3, 5)
        counts = spec.read(args.avg, *sm)
        st = bwtek.spectrum_stats(counts)
        wl = bwtek.wavelengths()
        print("max %d at pixel %d (%.1f nm); saturated %d px (active %d); mean(active) %.1f"
              % (st["max"], st["argmax"], wl[st["argmax"]], st["saturated"], st["saturated_active"], st["mean_active"]))
        print("first/active/last pixels:", counts[:3], counts[254:257], counts[-3:])
        if args.save:
            np.savetxt(args.save, np.column_stack([wl, counts]), fmt="%.3f,%d", header="wavelength_nm,counts", comments="")
            print("saved", args.save)
    finally:
        spec.close()
        print("closed")


if __name__ == "__main__":
    main()
