# Start-up / post-incident runbook (from 2026-08-27)

Prerequisite: the original OptiComp program is not running (COM4 and the spectrometer are exclusive); `C:\OptiComp2` is the latest deployment.

## A. Fibre and detector arm (do this first, works with the lamp off)

1. Look at the fibre: the run is slack and not wound around the detector arm (module 2).
2. Read-only report, do not move any motor:

       py tools\restore_stages.py

   Expected: module 2 reports GS02 (the last auto-home failed and the zero is lost), polariser about 0°, sample stage about 103°, shutter closed.
3. With someone watching the detector arm and enough slack in the fibre, restore the reference:

       py tools\restore_stages.py --safe --arm

   Type `YES` to confirm. The script will: move the polariser/sample stage to zero and park them at S (236°) / 185°; set the detector-arm speed to 50 % (the original 2sv32), home it (ho0) and park it at 44°;
   write the baseline `data\stage_state.json`. Every tool then compares against this baseline on connect and refuses motion on an anomaly.
   If the detector arm stalls again during homing (GS02), immediately cut ELLB power (unplug the ELLB USB) and check the fibre; do **not** re-home.

## B. USB soft-restart check (do it once with someone present, then no more unplugging)

`run_manual_gui.bat` now requests administrator rights automatically (click "Yes" on the UAC dialog) — the
GUI's "Recover (reopen / USB restart)" button needs elevation to restart the USB device.

In an administrator PowerShell:

    py tools\usb_reset.py

It does a PnP restart of the spectrometer (equivalent to a replug, but only for the spectrometer device), then reads a probe spectrum.
Watch: the detector arm does not move, `stage_state` shows no anomaly, the probe is OK. Later, when the DLL hangs (read -99 / device count 0):
the GUI spectrometer page's "Recover (reopen / USB restart)" button, or monitor / cycle_test, calls the same logic automatically.

Disable USB selective suspend (one-off, administrator):

    powercfg /setacvalueindex SCHEME_CURRENT 2a737441-1930-4402-8d77-b2bebba308a3 48e6b7a6-50f5-4782-a5d4-53bb8f07e226 0
    powercfg /setdcvalueindex SCHEME_CURRENT 2a737441-1930-4402-8d77-b2bebba308a3 48e6b7a6-50f5-4782-a5d4-53bb8f07e226 0
    powercfg /setactive SCHEME_CURRENT

## C. Lamp and Si motion cycle test

1. Turn on the lamp, warm up for >= 30 min (shutter closed).
2. Mount the Si (the 20x20 wafer in the 30x30 holder, confirm it sits flat and does not wobble):

       py tools\cycle_test.py --cycles 3 --frames 20 --moves both --tag si

   Log `logs\cycle_*.log` reports, for each cycle, the three bands' change relative to the baseline. If one move drops it by >2 %, run
   `--moves scan`, `--moves exchange`, `--moves sample`, `--moves arm` separately to find which motion caused it.
3. Swap in the white board and repeat: `--tag white`. If the white board is stable but Si drops -> a sample-mounting/tilt problem (a specular sample is sensitive to angle);
   if both drop -> a detector-arm / integrating-sphere port repeatability problem.
4. Once everything is stable, run the full procedure from the GUI: reference calibration (80°/S,P) -> white-board scan -> Si scan -> DB exchange -> analysis page.

## How to stop an unattended script

monitor / cycle_test checks `C:\OptiComp2\logs\STOP` every frame: to finish early, run
`New-Item C:\OptiComp2\logs\STOP` in another window and the script will close the shutter, save the data, record the state and exit.
Do **not** use taskkill / close the window — that skips the finally block and leaves the shutter open. If you already hard-killed it, immediately run

    py tools\shutter_close.py

which does exactly one thing: `0bw` and verifies the position is 0.

## D. Shutdown

The GUI "Disconnect" or exit records the motor state and closes the shutter; scripts record the same on exit. Afterwards do not plug or unplug anything on that USB hub.
