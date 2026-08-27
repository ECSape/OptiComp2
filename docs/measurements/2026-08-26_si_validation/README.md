# 2026-08-26 system validation: polished crystalline silicon vs white board (first full VAR measurement)

The first reference + sample session completed with the OptiComp2 GUI v1.1, kept as the baseline record of the system state.
The raw data (`white_new/`, `si_new/`, 42 spectra each + `manifest.json`) is byte-for-byte identical to the copy in `C:\OptiComp2\data\` on the lab PC.

## Measurement conditions

| Item | Value |
|---|---|
| Time | white_new 21:50-21:54, si_new 22:04-22:08 (lamp warmed up >= 30 min) |
| Geometry | detector arm 44° (re-homed that night), sample stage θ+105°, polariser S=236° / P=146°, DB positions 124°/93° |
| Angles | θ = 8:4:80 (19 angles) × S, P, 3 averages per spectrum |
| Integration time | 1424 ms (auto-calibrated on the white board at 80°, using min(IT_S, IT_P)); DB spectra fixed at 1000 ms |
| Darks | `dark_1424ms.csv`, `dark_db_1000ms.csv`, one set per session |
| Saturation | 0 pixels; no warnings in the log; no resends / corrective moves |
| Sample | 20x20 mm polished Si, mounted in a 30x30 mm holder; reference is the white board (reflectance taken as the constant 0.99) |
| Peaks | white board S ≈ 49 k, P ≈ 56 k (P is 13 % brighter than S); Si S rises monotonically 19 k -> 37 k, P is lowest 10.7 k at 64-68° |
| DB factor | (Scy-Sd)/(Scx-Sd) = 1.111 ± 0.054 (S), 1.108 ± 0.052 (P), 420-1000 nm |

## Results (`compare_si.py` -> `si_vs_fresnel.png`, `R_si_S.csv`, `R_si_P.csv`)

Measured / Fresnel-table ratio (420-1000 nm mean, DB-corrected):

| θ | S | P |
|---|---|---|
| 8° | 1.15 | 1.15 |
| 20° | 1.09 | 1.11 |
| 32° | 1.03 | 1.12 |
| 44° | 1.02 | 1.24 |
| 56° | 1.06 | 1.62 |
| 68° | 1.01 | 5.5 (measured 0.19 vs theory 0.049) |
| 76° | 0.98 | — (measured 0.30 vs theory ≈ 0) |
| 80° | 0.96 | 18 (measured 0.46 vs theory 0.025) |

- **S polarisation**: agrees with Fresnel within ±5 % over 32-72°; high by +9…+15 % at 8-28°; low by -2…-4 % at 76-80°. The spectral shape is correct.
- **P polarisation**: below 40° it is high by the same amount as S (the polariser angle is correct — the S/P ratio is identical at 8°); the Brewster minimum (≈76°) is filled in, the measured minimum is 0.18-0.19, recovering to 0.46 at 80°.
- **Explanation**: solving the two S and P equations under a "beam overspills the sample and hits the holder" model gives the overspill fraction f and holder reflectance R_h:
  68° f≈0.23 / R_h≈0.66; 76° f≈0.43 / 0.71; 80° f≈0.60 / 0.75. R_h is consistent across the three, f grows as 1/cosθ, corresponding to a **≈8-10 mm spot overspilling the 20 mm silicon wafer above 60°**. The same model also explains the S low bias at 80°.
- **Low-angle +10-15 %** (S and P together): the white-board reflectance is not exactly the constant 0.99 and/or the DB correction magnitude is uncertain; without DB the 8° value is 1.04.

## Conclusions / conventions (from this day on)

1. The system (motor geometry, polarisation, integration time, darks, DB procedure) works correctly, and `white_new` is a valid reference for the day.
2. Results for 20 mm-scale samples are trustworthy within **θ ≤ 60°** (S ±5 %, absolute level ±10 % pending the white-board ground truth); above 60° they are affected by beam overspill, P polarisation most of all. Samples ≥ 50 mm are usable to about 78°.
3. To do (does not affect the current measurement): measure the spot diameter; add a black mask around the sample; obtain a white-board calibration curve to replace the constant 0.99.
