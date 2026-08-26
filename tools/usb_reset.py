# -*- coding: utf-8 -*-
"""Restart the spectrometer's USB device from software (the equivalent of a cable replug).

    py C:\\OptiComp2\\tools\\usb_reset.py            # restart + probe
    py C:\\OptiComp2\\tools\\usb_reset.py --probe    # probe only (InitDevices / GetDeviceCount / TestUSB)

Needs an administrator account (pnputil /restart-device). Only the B&W Tek device (VID_16A3) is
restarted - never the USB hub, so the Elliptec bus is not power-cycled and the stages do not
auto-home. Close the GUI / any monitor first: the DLL session must not be open in another process.
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from hw import bwtek   # noqa: E402


def probe(log=print):
    s = bwtek.BWTek(log=log)
    try:
        n = s.open()
        s.set_integration_time(100)
        c = s.read(1, 0, 0)
        log("probe OK: %d device(s), frame max %d" % (n, int(c.max())))
        return True
    except bwtek.BWTekError as e:
        log("probe FAILED: %s" % e)
        return False
    finally:
        s.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--probe", action="store_true", help="only probe, do not restart")
    ap.add_argument("--vid", default=bwtek.SPEC_USB_VID)
    args = ap.parse_args(argv)
    if args.probe:
        return 0 if probe() else 1
    ids = bwtek.usb_instance_ids(args.vid)
    print("connected USB devices matching %s: %s" % (args.vid, ids or "none"))
    if not ids:
        return 1
    for iid in ids:
        print("pnputil /restart-device %s" % iid)
        try:
            out = bwtek.pnp_restart(iid)
            print(out.strip())
        except bwtek.BWTekError as e:
            print("FAILED: %s" % e)
            return 1
    print("waiting for the device to re-enumerate...")
    if not bwtek.wait_for_device(args.vid, timeout=20.0):
        print("device did not come back within 20 s")
        return 1
    time.sleep(3.0)
    return 0 if probe() else 1


if __name__ == "__main__":
    sys.exit(main())
