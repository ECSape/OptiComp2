# -*- coding: utf-8 -*-
"""Instrument geometry constants (degrees), copied from the original OptiComp calibration.

Re-verify these with an alignment check before trusting absolute angles; the original
code carried two different sample offsets (103 in stageframework.py, 105 in
hardwaremanager.py – the VAR scan used 105).
"""
import os

SHUTTER = "0"
POLARISER = "1"
SYSTEM = "2"          # lower stage / detector arm – fibre torsion caution
SAMPLE = "3"          # upper stage

# Polariser: physical verification (Brewster minimum in saved P data) -> 146 deg = P, 236 deg = S
POL_DEG = {"P": 146.0, "S": 236.0}

SYSTEM_ZERO = 44.0
DB_IT_MS = 1000               # double-beam spectra always at this integration time (thesis: 1000 ms)

# HW-F02 lamp-stability gate (runs before the reference calibration only): hold until the active-band
# mean stays within STABILISE_PCT of its recent mean for STABILISE_READS consecutive reads; after
# STABILISE_MAX_READS reads it warns and proceeds (never a hard block). Ported from the original
# rampupsource/SRTI supervisor so lamp drift between the reference and sample sessions cannot bias R.
STABILISE_PCT = 0.5
STABILISE_READS = 5
STABILISE_MAX_READS = 60
SYSTEM_DB = 124.0
SYSTEM_EXCHANGE = 150.0

SAMPLE_ZERO = 103.0           # stageframework.SAMPLEOFFSET (setzero / setDB reference)
SAMPLE_VAR_OFFSET = 105.0     # hardwaremanager.SAMPLEOFFSET: stage angle = theta + 105
SAMPLE_DB = 93.0
SAMPLE_EXCHANGE = 120.0

# Gatekeeper (thesis 4.2.3.2): 0 <= start < stop <= 80, step >= 1
THETA_MIN = 0
THETA_MAX = 80
STEP_MIN = 1

SOFT_LIMITS = {SYSTEM: (0.0, 200.0), SAMPLE: (0.0, 200.0)}

# ---- bus health (2026-08-26 incident: the ELLB is USB-powered; any USB replug power-cycles the
# modules and ELL14/ELL18 auto-home at power-up, which the fibre-carrying arm cannot survive)
VELOCITY = {SYSTEM: 50, SAMPLE: 50}         # percent, applied on every connect (arm from the original 2sv32 = 50 %;
                                            # the loaded sample ELL18 is also run at 50 % to cut mechanical time-outs)
PROTECTED_HOME = (SYSTEM,)                  # home() refused unless force=True
DATA_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
STATE_FILE = os.path.join(DATA_ROOT, "stage_state.json")   # last known position/status per module
STATE_TOLERANCE_DEG = 0.5                   # a larger jump between sessions = moved without us
SPEC_USB_VID = "VID_16A3"                   # B&W Tek spectrometer (Cypress CYUSB), for the PnP restart
