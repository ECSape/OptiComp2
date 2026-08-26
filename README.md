# OptiComp2

Clean-room, step-by-step rewrite of the OptiComp VAR instrument software.
The original `OptiComp` repository is left untouched; this project lives in its own
folder and repository.

## Layout

* `hw/elliptec.py` – Thorlabs Elliptec protocol (parsed replies, busy polling, stray-reply
  discard, protected home for the fibre arm)
* `hw/bwtek.py` – B&W Tek spectrometer via BWTEKUSB.dll, with `reopen()` / `recover()`
  (close + reopen, then a Windows PnP restart of the USB device – the software replug)
* `hw/stagestate.py` – records the modules' positions/status and detects modules that moved
  without us (the USB-powered ELLB restarts and the modules auto-home on any replug)
* `hw/config.py` – geometry constants from the original calibration, speeds, protections
* `tools/manual_gui.py` – Tk GUI: stages, shutter, spectrometer, sequences, analysis
* `tools/sequence.py` – step builders + Runner (verified moves, manifest, state bookkeeping)
* `tools/monitor.py` – unattended stability monitor (spectra vs time at a fixed geometry)
* `tools/cycle_test.py` – movement-cycle test: signal before/after stage movements
* `tools/restore_stages.py` – report / restore the stage references after a bus power event
* `tools/usb_reset.py` – probe or PnP-restart the spectrometer's USB device (admin)
* `tools/ell_probe.py`, `tools/spec_probe.py` – read-only probes
* `analysis/var.py` – VAR reflectance from a session (double-beam substitution correction)
* `tests/` – unit tests with fakes (`py -m unittest discover -s tests`)

Run on the instrument PC (Python 3.9):

    py tools\manual_gui.py
    py tools\restore_stages.py            # read-only report of the bus
    py tools\monitor.py --minutes 30 --it 997 --pol S --sample 185 --tag si
    py tools\cycle_test.py --cycles 3 --frames 20 --moves both --tag si

Close the original OptiComp application first – COM4 and the spectrometer are exclusive.

## Hardware cautions (learned the hard way)

* Module 2 (detector arm) carries the fibre: never home it unless the fibre is slack and
  someone is watching. `bus.home("2")` is refused without `force=True`.
* The ELLB bus board is USB-powered and shares a hub with the spectrometer: do not replug
  any USB cable on that hub while the arm is away from zero – every power cycle makes all
  modules auto-home. Prefer `tools/usb_reset.py` (PnP restart of the spectrometer only).
* The stage-state record (`data/stage_state.json`) is compared on every connect / script
  start; anomalies block motion until the operator has checked the instrument.
* A blocked home (GS02) is never retried automatically; a stalled relative move is retried
  as an absolute move to the original target; a reply line cut by the read timeout is
  discarded, never decoded.

## Data conventions

* `manifest.json` per session directory, written atomically; an unreadable manifest is
  moved aside as `manifest.json.corrupt_<time>` instead of being overwritten.
* Darks are stored per integration time (`dark_997ms.csv`, `dark_db_100ms.csv`); the
  analysis picks the dark whose integration time matches the spectrum.
* The Runner closes the shutter whenever a sequence fails or is aborted, and the GUI closes
  it on exit; auto-IT aborts instead of "using" a non-converged integration time.

See `RUNBOOK.md` for the start-of-day procedure after a bus power event.
