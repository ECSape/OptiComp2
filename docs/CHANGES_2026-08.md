# OptiComp -> OptiComp2 change summary (2026-08-25 to 2026-08-26)

This document collects all of the work done over two days on the control software of the angle-resolved spectrophotometer (the ARS/VAR system, Chapter 4 of the Tyson thesis):
the review conclusions on the original OptiComp v1.3.3, the design of each layer of the OptiComp2 rewrite, two hardware incidents and their countermeasures, the tools and tests,
and the deployment status. The original `OptiComp` repository was **not modified in any way**; all code lives in a separate repository, `OptiComp2` (deployed on the lab PC at
`C:\OptiComp2`).

---

## 1. Background and decision

| Item | Content |
|---|---|
| Original software | OptiComp v1.3.3, Python 3.9 + Tkinter, 11 modules, about 1,800 lines |
| Review | 2026-08-25 full function-level review, 35 findings (all adversarially verified) |
| Decision | Do not patch the original code; do a clean rewrite in the order "protocol layer -> spectrometer -> sequence -> analysis -> GUI", validating each step at the instrument |
| New software | OptiComp2, 5 directories / 38 version-controlled files (excluding data/), about 9,800 lines (including 9 test files, 129 unit tests) |

### 1.1 The most important findings from the review of the original software

1. **Motion commands are not verified**: about 4 % of `ma` commands to the ELL18 sample stage are silently swallowed (no reply, no motion, then GS00); the original software never checks the final position, so angle-wrong spectra may exist in the old data.
2. **Saturation is not detected**: 33 of 57 archived datasets contain VAR spectra clipped at 65535 (fixed `HARD_IT`, no saturation check).
3. **Dark integration-time mismatch**: `Dark.csv` is acquired at a fixed 1000 ms while the VAR spectra use `HARD_IT`, so the dark subtraction is not rigorous.
4. **Multi-threaded DLL calls**: the GUI thread and the scan thread call BWTEKUSB.dll concurrently (no declared thread safety).
5. **Off-by-one active-pixel slice**: the thesis specifies 254-2030 (inclusive); the code slices 1776 pixels, dropping 2030.
6. **Gatekeeper rule not enforced**: the thesis requires step >= 1, the code does not check.
7. **Reference auto-integration-time calibration not enabled**: the 80°/S calibration described in thesis 4.2.3.3 survives only as commented-out code (`spectrometerframework.py:99-122`) and a never-called `stageframework.setintegrationtime()`; a fixed `HARD_IT` is used in practice.
8. **The S/P file labels are actually correct** (TEM modes): in the 14 glass/ARC datasets `*_P_*` shows a Brewster minimum at 56° and `*_S_*` rises monotonically; only the single-polarisation-mode UI label is reversed relative to the files.
9. **Inconsistent sample-stage offset constant**: `stageframework.py` uses 103°, `hardwaremanager.py` uses 105° (the VAR scan actually used 105°).
10. The detector-arm speed: the original program sends `2sv32` = 0x32 = 50 % (the module default is 64 %).

---

## 2. OptiComp2 architecture

```
hw/          elliptec.py   Elliptec ELLx protocol layer (parse every reply, resend, verify position, protected home)
             bwtek.py      BWTEKUSB.dll wrapper (return-code checks, single thread, auto IT, reopen/recover)
             stagestate.py motor state record/compare (stage_state.json)
             config.py     geometry constants, soft limits, speeds, protected list
tools/       sequence.py   step builder + Runner (verify moves, manifest, shutter safety)
             manual_gui.py / *_panel.py / ui_theme.py   Tk GUI (v1.0, sidebar layout)
             monitor.py, cycle_test.py, restore_stages.py, usb_reset.py, shutter_close.py,
             ell_probe.py, spec_probe.py, demo_hw.py, ui_render.py
analysis/    var.py (eq. 4.10 reflectance), standards.py (Si Fresnel standard, BK7, slab inversion)
tests/       9 files, 129 tests (all using a fake serial port / fake DLL, no hardware needed)
```

**Threading model**: the DLL is called only on the `SpectroWorker` thread; manual commands go through `HardwareWorker`, and the sequence runs on `SpectroWorker` (the Runner drives the motors from that thread),
manual submissions are refused while a sequence runs so the two never interleave; every bus send/receive is serialised by an additional lock. The Tk thread only submits tasks through a queue and fetches results with `after()`.

---

## 3. Key changes in each layer

### 3.1 Elliptec protocol layer (`hw/elliptec.py`)

| Problem (measured) | Countermeasure |
|---|---|
| ELL18 occasionally swallows a command (no reply, no motion) | Read the position before sending; no reply -> poll GS; if the position has not changed, **resend**, up to 3 times |
| After a long move it stops a few hundredths of a degree short of the target | Target ±20 pulses (0.05°) counts as on-target; <= 120 pulses (0.3°) issues a **corrective move**; still off -> accept with a warning, beyond 0.3° -> error |
| PO is returned only at the end of motion; an ELL18 move at 50 % speed takes over 5 s | Poll GS after a first 2 s wait, motion timeout 60 s |
| Reply lines truncated by a read timeout | Any line without `\n` or a payload of the wrong length is discarded, not decoded |
| Cross-talk / delayed replies (e.g. a PO that only arrives during a poll) | `query(expect=...)` discards unexpected types |
| GS0A reported at the end of a long move although it has arrived | Accept and log if the position is within tolerance |
| Mechanical timeout GS02 | `ma`/`fw`/`bw` retry once; `mr` is converted to a `ma` to the original target (you cannot walk another relative amount from where it stalled); **`ho` never retries** |
| A swallowed shutter causes a dark frame | `fw`/`bw` verify the endpoint position (tolerance 0) |
| Homing the fibre arm (module 2) would tangle the fibre | `protected_home`: `home("2")` requires `force=True`, and the raw command channel refuses to send `ho` to a protected address |
| Module type / pulse count hard-coded | Read the model, serial number, travel and pulses/rev from the `IN` reply |

### 3.2 Spectrometer (`hw/bwtek.py`)

- Except for `bwtekSetTimingsUSB` (only the return value is logged) and the close calls, all DLL calls set `argtypes/restype` and check the return code; `read()` logs a WARNING on an anomalous duration (< 0.5×IT or > IT+5 s).
- **Auto integration time**: target peak 85 % of full scale, accept band 78-92 %, linear extrapolation `IT' = IT·(0.85·65535 - base)/(peak - base)`, halve on saturation, at most 8 steps;
  if there is still no light at >= 4000 ms it **aborts** (it no longer "carries over" a non-converged IT).
- **Reference calibration is done once in each of the S and P polarisations, taking min(IT_S, IT_P)**: on a diffuse white board the P channel is about 12 % brighter than S at every angle (system polarisation throughput),
  so calibrating on S alone as the thesis does would saturate the P spectra.
- **Recovery ladder** `recover()`: (1) close + reopen; (2) `pnputil /restart-device` restarts only the spectrometer USB device (VID_16A3, equivalent to a replug but does not touch the hub the ELLB is on)
  -> wait for re-enumeration -> reopen. Requires administrator rights; `run_manual_gui.bat` elevates automatically.
- Saturated pixels are recorded as NaN in the analysis; darks are stored per integration time (`dark_997ms.csv`), and the analysis matches the dark with the same IT.

### 3.3 Motor state record (`hw/stagestate.py`, new)

- The sequence writes that module's position and status code after every move; on disconnect/exit it writes a full snapshot of all four modules (position, status code, speed) to `data/stage_state.json`;
- On every connect / script start it compares against the baseline: position difference > 0.5°, non-zero status code, or a changed speed -> flagged as an anomaly.
  Unattended scripts: any anomaly aborts (`--force` can bypass), but when the detector arm reports GS02 (home failed) they refuse to run even with `--force`;
  the sequence Runner re-checks once before every move: warn for a normal module, abort for the detector arm; on connect the GUI only shows a banner and a dialog and does not block manual operation;
- On connect the detector-arm speed is set back to 50 % (a module returns to the default 64 % after a power loss).

### 3.4 Measurement sequence (`tools/sequence.py`)

- Step primitives: `stage / shutter / set_it / auto_it / acquire / pause / restore_it / apply_min_it`;
  builders: reference calibration (80°, S+P, auto IT), dark, single angle, angle scan, double-beam DB (with a port-cap swap pause, fixed 1000 ms and restored).
- Runner: soft limits (0-200°); "motion not by us" detection before a move; `try/except BaseException` -> close shutter + restore IT;
  `finally` atomically writes `manifest.json` (tmp + `os.replace`), and a corrupt manifest is moved to `manifest.json.corrupt_<time>`.
- Gatekeeper: 0 <= start < stop <= 80, step >= 1; when unattended, a `pause` aborts rather than waiting forever.
- On a repeat acquisition the old manifest record is replaced; an identical step block is refused for re-queuing.

### 3.5 Analysis (`analysis/`)

- Eq. 4.10: `Rx = (Sx-Sd)/(Sy-Sd) · (Scy-Sd)/(Scx-Sd) · Ry`, darks matched by IT, and if the IT differs it is normalised by counts/ms (a linear-detector assumption, recorded in `notes`).
- Active pixels 254-2030 (inclusive) = 1777 pixels.
- Standards: constant (white board), Si Fresnel table (`standards/silicon_TE/TM.csv`, real part n only, marked invalid below 380 nm), BK7 Sellmeier, slab back-reflection inversion.

### 3.6 GUI (`tools/manual_gui.py` v1.0)

- Sidebar with 5 pages: instrument (connection + quick-start checklist + motor state + notes), motors & shutter, spectrometer, measurement, analysis;
- A persistent status bar (serial port / spectrometer / IT / shutter / detector arm / sequence) + a red "Close shutter" available from any page; log drawer Ctrl+L;
- Shortcuts Ctrl+1..5 switch pages, Ctrl+L log, Ctrl+R run, Esc abort, F5 query all info, Ctrl+Shift+S save log; close-shutter is a status-bar button with no shortcut;
- Manual operations are locked while a sequence runs; the exit flow: abort -> the worker thread closes the shutter + records state (30 s cap) -> spectrometer close -> serial port close;
- Demo mode `--demo` (`DemoBus`/`DemoSpec`, does not touch COM4/DLL), `--screenshot DIR` auto-tours and captures screenshots.

### 3.7 Tools

| Tool | Purpose |
|---|---|
| `restore_stages.py` | Read-only report; `--safe --arm` restores the reference under supervision (polariser/sample stage to zero then parked at S/185°, detector arm at 50 % speed, homed, parked at 44°), writes the baseline; `--arm` needs an interactive terminal to type `YES` (only `--yes` can skip it) |
| `usb_reset.py` | Probe or PnP-restart the spectrometer USB device |
| `shutter_close.py` | After a hard-killed script: `0bw` and verify the position is 0 |
| `monitor.py` | Unattended stability monitor (continuous reads at fixed geometry -> CSV, three bands' change relative to the first frame) |
| `cycle_test.py` | Motion cycle test (sample/arm/scan/exchange/both), a dark per cycle, automatic `recover()` |
| `ell_probe.py`, `spec_probe.py` | Read-only probes |
| Stop method | Create a `logs/STOP` file -> the script closes the shutter, saves data, records state and exits; taskkill is forbidden |

---

## 4. Two hardware incidents and countermeasures

### 4.1 2026-08-25: sample stage swallows commands
During the first full sequence (`data/test_ref`) the ELL18 ignored `ma` twice in a row. -> protocol-layer resend + position verification (§3.1).

### 4.2 2026-08-26 01:14 / 01:21: USB power incident
USB topology (verified with Get-PnpDevice): root hub -> generic hub -> {B&W Tek spectrometer VID_16A3&PID_2EC8; child hub -> FTDI ELLB bus COM4; keyboard/mouse}.
The ELLB has no external power, so **any** replug on that hub cuts power to all modules, and the ELL14/ELL18 auto-home on power-up:
polariser -> 0°, sample stage 185° -> 102.9°, the fibre-carrying detector arm stalls on its home (GS02, a meaningless 11° position read), speed reset to 64 %.
Also: after 590 consecutive 997 ms reads the spectrometer blocked for 25 s on one read and returned -99, after which every new process saw `GetDeviceCount()==0` until a physical replug re-enumerated it.

Countermeasures: state record and gate (§3.3), protected home, no GS02 retry, `recover()` soft replug (§3.2), the `RUNBOOK.md` start-up procedure,
and a recommendation to give the ELLB external 5 V and disable USB selective suspend (the `powercfg` commands in RUNBOOK section B).

---

## 5. Tests and deployment

- `tests/`: 129 tests, passing both locally (pytest) and on the lab PC (`py -m unittest discover -s tests`, no pytest);
  covering protocol encode/decode / truncated replies / resend / corrective move / protected home, DLL return codes and pnputil safety, Runner safety (abort closes shutter, no-operator pause, soft limits),
  monitor/cycle fault-injection recovery (`--dry-fail-at`), state comparison, the GUI feature reference table and layout audit (demo hardware).
- Deployment: `scp -r tools tests hw analysis ... Admin@100.68.49.11:C:/OptiComp2/`, SHA-256 verified per file; a full test run is done remotely after each deployment.
- Commit record (OptiComp2 `main`, GitHub `ECSape/OptiComp2`): `9ec747e` … `6194c65`, 31 commits in all, see `git log`.

---

## 6. Known issues / to do

- The screenshots are synthetic renders (macOS did not grant Screen Recording), so the real-machine appearance needs to be confirmed in the lab.
- The layout audit covers all pages at 1280x820 and 1100x720, but not the case where the log drawer is opened at a height < 820 px (which compresses the analysis figure).
- The detector-arm zero was lost in the 2026-08-26 incident and must be restored under supervision per RUNBOOK section A before any measurement.
- The Si motion cycle test (RUNBOOK section C) has not yet been run; the white-board vs Si comparison is used to separate a "sample-mounting" from a "detector-arm / port repeatability" problem.
- External power for the ELLB and moving the spectrometer to a native host USB port are the hardware-level root-cause recommendations.
