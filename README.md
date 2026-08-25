# OptiComp2

Clean-room, step-by-step rewrite of the OptiComp VAR instrument software.
The original `OptiComp` repository is left untouched; this project lives in its own
folder and repository.

## Step 1 – manual hardware control (stages + shutter)

* `hw/elliptec.py` – Thorlabs Elliptec protocol (parsed replies, busy polling, no fixed sleeps)
* `tools/ell_probe.py` – read-only bus probe (`in/gs/gp/gv` only, never moves anything)
* `tools/manual_gui.py` – Tk GUI: connect COM4, query modules, home, absolute/relative
  moves in degrees, presets from the original calibration, shutter fw/bw, raw command
  console, full TX/RX log
* `tests/test_elliptec.py` – unit tests with a fake serial port

Run on the instrument PC (Python 3.9):

    py tools\ell_probe.py --port COM4
    py tools\manual_gui.py

Close the original OptiComp application first – COM4 is exclusive.
