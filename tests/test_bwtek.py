# -*- coding: utf-8 -*-
import ctypes
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from hw import bwtek


class FakeDll(object):
    def __init__(self, count=1, fail_read=False):
        self.count = count
        self.fail_read = fail_read
        self.time = None
        self.calls = []

    def InitDevices(self):
        self.calls.append("InitDevices")
        return True

    def GetDeviceCount(self):
        return self.count

    def bwtekTestUSB(self, *a):
        self.calls.append(("bwtekTestUSB",) + a)
        return 0

    def bwtekSetTimingsUSB(self, *a):
        return 1

    def bwtekSetTimeUSB(self, ms, ch):
        self.time = ms
        return ms

    def bwtekReadResultUSB(self, trig, avg, st, sv, ptr, ch):
        if self.fail_read:
            return -1
        arr = (ctypes.c_ushort * bwtek.PIXELS).from_address(ptr.value)
        for i in range(bwtek.PIXELS):
            arr[i] = min(65535, i * 40)
        return bwtek.PIXELS

    def bwtekCloseUSB(self, ch):
        self.calls.append("bwtekCloseUSB")
        return 0

    def CloseDevices(self):
        self.calls.append("CloseDevices")


class Tests(unittest.TestCase):
    def test_wavelength_polynomial_matches_original(self):
        wl = bwtek.wavelengths()
        self.assertAlmostEqual(wl[254], 349.5, places=0)
        self.assertAlmostEqual(wl[2030], 1050.0, places=0)
        self.assertEqual(len(wl), 2048)

    def test_open_set_read_close(self):
        dll = FakeDll()
        s = bwtek.BWTek(dll=dll)
        self.assertEqual(s.open(), 1)
        self.assertEqual(s.set_integration_time(1000), 1000)
        counts = s.read(average=2)
        self.assertEqual(counts.dtype, np.uint16)
        self.assertEqual(counts[10], 400)
        st = bwtek.spectrum_stats(counts)
        self.assertEqual(st["max"], 65535)
        self.assertGreater(st["saturated"], 0)
        s.close()
        self.assertIn("bwtekCloseUSB", dll.calls)
        self.assertIn("CloseDevices", dll.calls)

    def test_no_device_raises(self):
        s = bwtek.BWTek(dll=FakeDll(count=0))
        with self.assertRaises(bwtek.BWTekError):
            s.open()

    def test_read_error_raises_not_zeros(self):
        s = bwtek.BWTek(dll=FakeDll(fail_read=True))
        s.open()
        with self.assertRaises(bwtek.BWTekError):
            s.read()

    def test_read_before_open_raises(self):
        with self.assertRaises(bwtek.BWTekError):
            bwtek.BWTek(dll=FakeDll()).read()




class ITCalibrationTests(unittest.TestCase):
    def test_scales_linearly_towards_target(self):
        # 1000 ms gave 53030 with baseline 900 -> expect ~ 1000*(0.85*65535-900)/(53030-900)
        it = bwtek.next_integration_time(1000, 53030, 900)
        self.assertEqual(it, round(1000 * (0.85 * 65535 - 900) / (53030 - 900)))
        self.assertTrue(bwtek.peak_in_band(0.85 * 65535))

    def test_saturated_halves(self):
        self.assertEqual(bwtek.next_integration_time(2000, 65535, 900), 1000)

    def test_dark_grows_and_clamps(self):
        self.assertEqual(bwtek.next_integration_time(100, 920, 900), 400)
        self.assertEqual(bwtek.next_integration_time(50000, 920, 900), bwtek.IT_MAX_MS)
        self.assertEqual(bwtek.next_integration_time(1, 65535, 900), bwtek.IT_MIN_MS)


if __name__ == "__main__":
    unittest.main()


class HangDll(FakeDll):
    """Models the 2026-08-26 hang: reads return -99, and after that GetDeviceCount()==0 in every
    new session until the USB device is re-enumerated (here: by the fake pnputil restart)."""

    def __init__(self):
        FakeDll.__init__(self, count=1)
        self.hung = False
        self.closed = 0

    def bwtekReadResultUSB(self, trig, avg, st, sv, ptr, ch):
        if self.hung:
            return -99
        return FakeDll.bwtekReadResultUSB(self, trig, avg, st, sv, ptr, ch)

    def GetDeviceCount(self):
        return 0 if self.hung else self.count

    def CloseDevices(self):
        self.closed += 1
        return 1

    def bwtekCloseUSB(self, ch):
        return 1


class FakeRun(object):
    """subprocess.run stand-in for pnputil."""

    def __init__(self, dll, present=True):
        self.dll = dll
        self.present = present
        self.calls = []

    def __call__(self, args, **kw):
        self.calls.append(list(args))

        class CP(object):
            returncode, stdout, stderr = 0, "", ""
        cp = CP()
        if args[1] == "/enum-devices":
            if self.present:
                cp.stdout = ("Instance ID:                USB\\VID_16A3&PID_2EC8\\6&13b694f9&0&2\n"
                             "Device Description:         B&W TEK Spectrometer\n"
                             "Instance ID:                USB\\VID_0403&PID_6015\\DT03AOM0\n")
        elif args[1] == "/restart-device":
            self.dll.hung = False                      # the restart re-enumerates the device
            cp.stdout = "Restarting device...\nDevice restarted successfully."
        return cp


class PnpSafetyTests(unittest.TestCase):
    def test_only_spectrometer_ids_may_be_restarted(self):
        calls = []
        bwtek._run = lambda *a, **k: calls.append(a)
        try:
            for bad in (r"USB\VID_0424&PID_2514\5&1", r"USB\VID_0403&PID_6015\DT03AOM0", r"USB\ROOT_HUB30\4&1"):
                with self.assertRaises(bwtek.BWTekError):
                    bwtek.pnp_restart(bad)
            self.assertEqual(calls, [])                                  # nothing executed
        finally:
            bwtek._run = None


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.dll = HangDll()
        self.spec = bwtek.BWTek(dll=self.dll)
        self.spec.open()
        self.spec.set_integration_time(997)
        self.old_run = bwtek._run

    def tearDown(self):
        bwtek._run = self.old_run

    def test_reopen_reapplies_integration_time(self):
        self.dll.time = None
        self.spec.reopen(settle=0)
        self.assertEqual(self.dll.time, 997)
        self.assertTrue(self.spec.opened)

    def test_recover_by_reopen_when_transient(self):
        bwtek._run = FakeRun(self.dll)
        self.assertEqual(self.spec.recover(), "reopened")
        self.assertEqual(bwtek._run.calls, [])               # no pnputil needed

    def test_recover_by_usb_restart_after_hang(self):
        self.dll.hung = True
        with self.assertRaises(bwtek.BWTekError):
            self.spec.read(1, 0, 0)                          # -99
        run = FakeRun(self.dll)
        bwtek._run = run
        old_sleep = bwtek.time.sleep
        bwtek.time.sleep = lambda s: None
        try:
            self.assertEqual(self.spec.recover(), "usb restart")
        finally:
            bwtek.time.sleep = old_sleep
        self.assertEqual(run.calls[0][:3], ["pnputil", "/enum-devices", "/class"])
        self.assertIn(["pnputil", "/restart-device", "USB\\VID_16A3&PID_2EC8\\6&13b694f9&0&2"], run.calls)
        self.assertTrue(self.spec.opened)
        self.assertEqual(self.dll.time, 997)
        self.assertEqual(int(self.spec.read(1, 0, 0).max()), 65535)   # reads work again

    def test_recover_gives_up_when_device_gone(self):
        self.dll.hung = True
        bwtek._run = FakeRun(self.dll, present=False)
        old_sleep = bwtek.time.sleep
        bwtek.time.sleep = lambda s: None
        try:
            with self.assertRaises(bwtek.BWTekError) as cm:
                self.spec.recover()
        finally:
            bwtek.time.sleep = old_sleep
        self.assertIn("no connected USB device", str(cm.exception))

    def test_recover_without_usb_restart(self):
        self.dll.hung = True
        bwtek._run = FakeRun(self.dll)
        with self.assertRaises(bwtek.BWTekError):
            self.spec.recover(usb_restart=False)
        self.assertEqual(bwtek._run.calls, [])

    def test_instance_id_parsing_filters_by_vid(self):
        bwtek._run = FakeRun(self.dll)
        self.assertEqual(bwtek.usb_instance_ids("VID_16A3"), ["USB\\VID_16A3&PID_2EC8\\6&13b694f9&0&2"])
        self.assertEqual(bwtek.usb_instance_ids("VID_0403"), ["USB\\VID_0403&PID_6015\\DT03AOM0"])
        self.assertEqual(bwtek.usb_instance_ids("VID_FFFF"), [])
