# -*- coding: utf-8 -*-
"""B&W Tek spectrometer via BWTEKUSB.dll (ctypes).

Thin wrapper with explicit argument types and return-code checks. All calls must come
from ONE thread (the DLL is not documented as thread-safe; the original app called it
concurrently from the GUI and the scan thread).

Calls used (same as the original OptiComp, which is known to work with this unit):
  InitDevices() -> bool            GetDeviceCount() -> int
  bwtekTestUSB(nUSBType, nPixelNo, nInputMode, nChannel, pParam) -> >=0 ok
  bwtekSetTimingsUSB(lTriggerExit, nMultiplier, nChannel)
  bwtekSetTimeUSB(lTime_ms, nChannel) -> lTime or <0
  bwtekReadResultUSB(nTriggerMode, nAverage, nTypeSmoothing, nValueSmoothing, pArray, nChannel) -> >=0 ok
  bwtekCloseUSB(nChannel), CloseDevices()
"""
import ctypes
import time

import numpy as np

PIXELS = 2048
ACTIVE_FIRST = 254            # thesis: pixels 254..2030 inclusive are inside the 349.5-1050 nm calibration
ACTIVE_LAST = 2030
ADC_MAX = 65535

# Wavelength calibration polynomial (nm as a function of 0-based pixel index), from the
# instrument's certificate as copied in the original code / thesis table 4.x.
WL_COEFFS = (218.821042760031, 0.528654319597965, -5.53604977490694E-5, -1.65095302917687E-9)


def wavelengths(pixels=PIXELS, coeffs=WL_COEFFS):
    i = np.arange(pixels, dtype=np.float64)
    a0, a1, a2, a3 = coeffs
    return a0 + a1 * i + a2 * i ** 2 + a3 * i ** 3


class BWTekError(Exception):
    pass


class BWTek(object):
    def __init__(self, dll=None, dll_name="BWTEKUSB.dll", pixels=PIXELS, channel=0, log=None):
        self.dll = dll if dll is not None else ctypes.cdll.LoadLibrary(dll_name)
        self.pixels = pixels
        self.channel = channel
        self.opened = False
        self.integration_ms = None
        self._log = log or (lambda text: None)
        self._buf = np.zeros(pixels, dtype=np.uint16)
        d = self.dll
        try:
            d.InitDevices.restype = ctypes.c_bool
            d.GetDeviceCount.restype = ctypes.c_int
            d.bwtekTestUSB.restype = ctypes.c_int
            d.bwtekTestUSB.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
            d.bwtekSetTimingsUSB.restype = ctypes.c_long
            d.bwtekSetTimingsUSB.argtypes = [ctypes.c_long, ctypes.c_int, ctypes.c_int]
            d.bwtekSetTimeUSB.restype = ctypes.c_long
            d.bwtekSetTimeUSB.argtypes = [ctypes.c_long, ctypes.c_int]
            d.bwtekReadResultUSB.restype = ctypes.c_int
            d.bwtekReadResultUSB.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                             ctypes.c_void_p, ctypes.c_int]
            d.bwtekCloseUSB.restype = ctypes.c_int
            d.bwtekCloseUSB.argtypes = [ctypes.c_int]
        except AttributeError:
            pass                      # fake DLL in tests

    def open(self):
        if not self.dll.InitDevices():
            raise BWTekError("InitDevices() returned false (driver/USB problem, or device held by another process)")
        n = self.dll.GetDeviceCount()
        self._log("GetDeviceCount = %d" % n)
        if n < 1:
            raise BWTekError("no B&W Tek device found")
        r = self.dll.bwtekTestUSB(1, self.pixels, 1, self.channel, None)
        self._log("bwtekTestUSB -> %d" % r)
        if r < 0:
            raise BWTekError("bwtekTestUSB failed (%d)" % r)
        r = self.dll.bwtekSetTimingsUSB(0, 1, self.channel)
        self._log("bwtekSetTimingsUSB(0,1) -> %d" % r)
        self.opened = True
        return n

    def set_integration_time(self, ms):
        ms = int(ms)
        if ms < 1:
            raise BWTekError("integration time must be >= 1 ms")
        r = self.dll.bwtekSetTimeUSB(ms, self.channel)
        self._log("bwtekSetTimeUSB(%d) -> %d" % (ms, r))
        if r < 0:
            raise BWTekError("bwtekSetTimeUSB(%d) failed (%d)" % (ms, r))
        self.integration_ms = ms
        return r

    def read(self, average=1, smoothing_type=3, smoothing_value=5):
        """Acquire one (averaged) spectrum; returns a fresh uint16 array of `pixels` counts."""
        if not self.opened:
            raise BWTekError("spectrometer not opened")
        t0 = time.time()
        r = self.dll.bwtekReadResultUSB(0, int(average), int(smoothing_type), int(smoothing_value),
                                        ctypes.c_void_p(self._buf.ctypes.data), self.channel)
        dt = time.time() - t0
        self._log("bwtekReadResultUSB(avg=%d, sm=%d/%d) -> %d in %.2f s" % (average, smoothing_type, smoothing_value, r, dt))
        if r < 0:
            raise BWTekError("bwtekReadResultUSB failed (%d)" % r)
        return self._buf.copy()

    def close(self):
        if self.opened:
            try:
                self.dll.bwtekCloseUSB(self.channel)
            except Exception:
                pass
        try:
            self.dll.CloseDevices()
        except Exception:
            pass
        self.opened = False


def spectrum_stats(counts):
    """Summary used by the GUI: peak, saturated pixel count (whole frame and active region)."""
    active = counts[ACTIVE_FIRST:ACTIVE_LAST + 1]
    return {
        "max": int(counts.max()),
        "argmax": int(counts.argmax()),
        "saturated": int((counts >= ADC_MAX).sum()),
        "saturated_active": int((active >= ADC_MAX).sum()),
        "mean_active": float(active.mean()),
    }


# ---- integration-time calibration (thesis 4.2.3.3: set on the reference at its brightest point)
IT_MIN_MS = 1
IT_MAX_MS = 60000
IT_TARGET = 0.85            # aim the peak at 85 % of full scale
IT_BAND = (0.78, 0.92)      # accept when the peak lands in this band


def next_integration_time(current_ms, peak, baseline, target=IT_TARGET):
    """Linear estimate of the integration time that puts `peak` at target*ADC_MAX.

    Counts above the ADC baseline scale ~linearly with integration time. A saturated
    frame gives no usable slope, so the time is simply halved.
    """
    if peak >= ADC_MAX:
        return max(IT_MIN_MS, current_ms // 2)
    signal = float(peak) - float(baseline)
    if signal < 50:                     # essentially dark: grow aggressively
        return min(IT_MAX_MS, current_ms * 4)
    want = target * ADC_MAX - float(baseline)
    return int(min(IT_MAX_MS, max(IT_MIN_MS, round(current_ms * want / signal))))


def peak_in_band(peak, band=IT_BAND):
    return band[0] * ADC_MAX <= peak <= band[1] * ADC_MAX
