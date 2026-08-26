# 2026-08-26 measurement campaign: ASA 3D-printed specimens

First sample campaign after the [Si system validation](../2026-08-26_si_validation/).
Six additively manufactured (ASA) specimens measured against the white-board
reference `white_new` (archived under `../2026-08-26_si_validation/`).

> **Specimen identities to be filled in by the author.** The six sessions are
> stored under their acquisition labels only. Their optical behaviour is
> characterised here; the material / print / surface-finish description of each
> (`01`–`04`, `pei`, `std`) must be supplied for the thesis text
> (`docs/thesis/chapter_measurements.tex`, marked `[specimen details: ...]`).

## Sessions (each = 43 files: 19 angles × S/P + 2 darks + 2 double-beam + manifest)

| session | time (2026-08-26) | notes |
|---|---|---|
| `asa_3d_01` | 22:28–22:33 | |
| `asa_3d_02` | 22:37–22:41 | 1 dead pixel at 350 nm in DB (masked, outside band) |
| `asa_3d_03` | 22:46–22:51 | |
| `asa_3d_04` | 23:04–23:08 | first attempt (23:00) aborted on arm GS02; **fully re-run**, no leftover files |
| `asa_3d_pei` | 23:10–23:14 | brighter specimen |
| `asa_3d_std` | 23:16–23:20 | brightest specimen; 1 dead pixel at 374 nm in DB (masked) |

Parameters identical to the Si validation: IT 1424 ms, θ = 8:4:80, avg 3, S+P,
DB at 1000 ms, per-IT darks. **0 saturated pixels**, all encoder read-backs match
the calibrated geometry within 0.05°, no resend/corrective-move events.

## Integrity check (all passed)

- File completeness: 43/43 per session, no missing spectra.
- Geometry: polariser / arm / sample angles match config for every spectrum.
- `asa_3d_04`: the 23:00 run failed at step ~37 (detector-arm mechanical
  time-out, GS02); the sequencer's replace-in-place logic overwrote every file
  in the 23:04 re-run, so the archived session contains only the clean run
  (all timestamps 23:04:27–23:08:30).
- Two isolated NaNs (asa_3d_02 @ 350 nm, asa_3d_std @ 374 nm) are single dead
  DB pixels below the 450 nm usable edge — no effect on results.

## Analysis

`python3 analyze_asa.py` regenerates, from the archived sessions:
- `R_<specimen>_<pol>.csv` — full reflectance spectra per angle;
- thesis figures (Nature style, vector PDF) into `../../thesis/figures/`:
  `meas_si_validation`, `meas_asa_angle`, `meas_asa_spectra`;
- `../../thesis/figures/tab_asa_reflectance.tex` — the reflectance table.

### Findings (white = 0.99, double-beam corrected, 450–900 nm mean)

- All six specimens are **dark and spectrally neutral**, near-normal reflectance
  0.04–0.05; `std` brightest, `pei` next, `01`–`04` mutually indistinguishable.
- **Reproducibility** of the repeated `01`/`02`/`04` group: per-angle CV
  **1.9 %** for θ ≤ 60° (both polarisations) — the campaign's tightest
  run-to-run figure.
- p-polarisation shows a shallow pseudo-Brewster minimum at ≈40–52°
  (R_p ≈ 0.02–0.03).
- **θ > 60° is overfill-contaminated** (beam > 20 mm specimen, bright holder);
  for these dark surfaces the grazing-angle values are upper bounds.
- Absolute scale carries ±10 % until the white board is calibrated (common to
  all specimens; cancels in specimen-to-specimen comparison).

Full write-up: thesis chapter `docs/thesis/chapter_measurements.tex` (Chapter 6).
