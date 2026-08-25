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
