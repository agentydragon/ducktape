# Drawer carcass — box spec

Generated with [boxes.py](https://boxes.hackerspace-bamberg.de/) **UniversalBox**:

| Setting             | Value |
| ------------------- | ----- |
| x                   | 393   |
| y                   | 282   |
| h                   | 220   |
| outside             | true  |
| thickness           | 8.66  |
| burn                | 0     |
| extra-finger-length | 0     |

- `burn = 0` is set at the boxes.py level — **kerf is applied in the LightBurn file instead** (kerf offset in the
  `.lbrn2`).
- Cut on the Noisebridge laser with the production recipe in <calibration_log.md> (focus at surface,
  `z_per_pass = −0.2`, 5 passes, 40–45 % / 20 mm/s).

## Kerf offset fit log

| Offset  | Result                                                    |
| ------- | --------------------------------------------------------- |
| 0.21 mm | Assembles, but joints **a little too tight**.             |
| 0.26 mm | **Good fit** (verified on a full-size side piece). Final. |
