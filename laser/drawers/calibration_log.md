# Noisebridge laser — calibration log

Machine: Noisebridge **Kaitian CM1309**, ~150 W CO2, Ruida controller (bed/table moves in Z). LightBurn-driven. Cut
params: power %, speed mm/s.

## Machine basics (mechanics)

- **The bed/table moves in Z** (not the head's Z drive). **Increasing commanded Z = bed moves UP, toward the tool head**
  → the head↔material gap shrinks and focus goes deeper into the material. Decreasing Z = bed down, gap grows, focus
  rises above the surface.
- **Bed hard limit (top of travel):** max commanded **Z 2999.9** — the bed physically cannot go higher. Any rise must
  fit entirely below this.
- **Crash limit:** the bed rising until the material hits the head — i.e. when the head↔material gap (= the focus-spacer
  thickness, currently 7.4 mm) closes to zero.
- **The tool head height is independently adjustable** over a range, via a **wingnut**. Focus is set mechanically:
  loosen the wingnut, sandwich `[head | spacer | material top]`, tighten, remove the spacer. So **focus is decoupled
  from bed commanded Z** — you can make the focus sandwich with the bed parked anywhere in a range, then pick a bed Z
  that leaves room to rise under the hard limit.
- **`z_per_pass` sign gotcha:** despite the controller's "increasing Z = bed up", and even though LightBurn _displays_
  the step as positive, **a positive `z_per_pass` lowers the bed each pass** (focus away from material). The sign is
  flipped somewhere between the cut setting and the bed — root cause not found (maybe a LightBurn Z-direction switch or
  a controller axis-dir config). Pragmatic rule: **use negative `z_per_pass` in the TOML to raise the bed / deepen
  focus** (matches the generator's default). Positive-lowers-bed observed in 8mm_plywood_3; **negative-raises-bed /
  deepens-focus confirmed in 8mm_plywood_4** (deeper steps released the cut at far fewer passes).
- Power capped at **55%** (Noisebridge tube-life rule). 8.4 mm ply is over their stated ~6 mm reliable cut depth for the
  150 W.

## Z-axis conversion factor

**The Z axis is badly miscalibrated — the table moves ~3.69× the commanded distance.** Commanded Z (controller readout)
does NOT equal physical travel. All `z_offset_mm` / `z_per_pass_mm` values sent to this machine must be scaled by `1/f`
to get real mm.

### Measurement — 2026-06-16

| Quantity                     | Value                          |
| ---------------------------- | ------------------------------ |
| Controller Z readout (lower) | 2962.4 mm                      |
| Controller Z readout (top)   | 2999.9–3000.0 mm               |
| Commanded span               | 37.6 mm (37.5 if top = 2999.9) |
| Actual span (caliper)        | 138.7 mm                       |
| **Conversion factor `f`**    | **138.7 / 37.6 ≈ 3.69**        |

`f = actual / commanded`. Direction: moving up. Single measurement so far.

### How to use it

To achieve a **true physical** Z move of `d` mm, command `d / f`:

- software scale = `1/f ≈ 0.271`
- real −0.7 mm step → command **−0.190 mm**
- real −0.1 mm offset → command **−0.027 mm**

So in the material-test TOML, set `z_offset_mm = real_offset × 0.271` and `z_per_pass_mm = real_step × 0.271`.

Equivalent: previously commanding `z_per_pass_mm = -0.7` actually moved the focus **~2.58 mm per pass** (−0.7 × 3.69) —
overshooting focus every layer. Likely the main cause of the inconsistent 8.4 mm cuts.

### TODO / confirm

- [ ] Repeat the measurement (2nd pair, ideally a longer span and the other direction) to confirm `f` and check
      linearity + backlash.
- [ ] If trained/authorized: the real fix is the Ruida vendor Z **step length**
      (`new_step = old_step × actual / commanded`). NB manual says don't change machine settings unless trained —
      otherwise compensate in software (above).
- [x] Run a focus test and record the focus offset (see below).

## Focus test — 2026-06-17

Photo: [focus test ladder][focus-photo] (LightBurn Focus Test ladder; lines go from wide/charred at the top to a fine
kerf near focus).

### Setup

- Material: flat offcut, surface of interest on top.
- Focus reference: **7.4 mm spacer** on the material at commanded **Z 2999.0**, head dropped onto it (so 7.4 mm ≈ this
  lens's focal/gauge distance).
- Lowered to commanded **Z 2997.5** before the test (−1.5 commanded = −5.5 mm physical below the reference).
- LightBurn Focus Test: start Z **0.00**, end Z **2.5 mm** (commanded), ~24 lines.
- Cut params: **25 mm/s**, power **min 12% / max 15%**, single pass.

### Result

**Sharpest (narrowest, deepest) line at offset Z = 1.5 mm commanded** (LightBurn mark).

- Focus offset 1.5 commanded × `f` (3.69) ≈ **5.5 mm physical** above the 2997.5 start.
- That lands at absolute commanded **Z 2999.0** — i.e. exactly the 7.4 mm-spacer reference. The spacer method sets focus
  correctly.

### How to focus this machine

**Drop the head onto a 7.4 mm spacer on the material surface at commanded Z 2999.0.** (7.4 mm ≈ the focal/gauge distance
for the current lens.)

## Z command granularity

LightBurn/Ruida Z fields accept a **minimum increment of 0.1 commanded mm**. With `f ≈ 3.69` the physical Z quantum is
therefore:

| commanded | physical |
| --------- | -------- |
| 0.1       | 0.37 mm  |
| 0.2       | 0.74 mm  |
| 0.3       | 1.11 mm  |

All `z_offset_mm` / `z_per_pass_mm` values in cut configs must be exact multiples of 0.1 commanded, or LightBurn rounds
them unpredictably. Finer Z would require fixing the Ruida vendor step-length (trained-maintainer only) — not worth it.

## Experiment — 8.4 mm plywood cut scan (8mm_plywood_3) — DONE 2026-06-17

Full writeup (config, lbrn2, photos, results table): <8mm_plywood_3/README.md>.

**Result:** with focus + Z calibrated, **8.4 mm ply severs cleanly at 50% / 20 mm/s with fixed focus at the surface** —
every `z_per_pass = 0` cell released with a light finger nudge at all pass counts (4–7), and `7/0` fell out unaided.
Per-pass Z stepping was never needed.

**Discovered:** **positive `z_per_pass` drives the bed _down_** (focus away from material), so the stepping cells
defocused and cut worse. LightBurn displays the step as positive too, and the controller coordinate is fine (increasing
commanded Z = bed up) — so the sign is flipped somewhere unidentified. Pragmatic fix for the next experiment: **use
negative `z_per_pass` in the TOML** to raise the bed / deepen focus.

**Production recipe (fixed-focus):** 50% power, 20 mm/s, focus at surface (7.4 mm spacer), `z_per_pass = 0`, **6
passes** (7 for certainty). _Superseded by exp 4 below._

## Experiment — 8.4 mm plywood cut scan (8mm_plywood_4) — DONE 2026-06-17

Full writeup: <8mm_plywood_4/README.md>. Repeats exp 3's grid with **negative** `z_per_pass` (passes 3–6 × z/pass
0/−0.1/−0.2) so focus tracks the cut deeper.

**Result:** the negative flip works — deeper steps released the cut at far fewer passes. **`z_per_pass = −0.2`, 4 passes
fell out spontaneously** at 50% / 20 mm/s; fixed focus never dropped unaided and needed 5+ passes for even a nudge.

**Recipe after exp 4:** 50% power, 20 mm/s, focus at surface, `z_per_pass = −0.2`, 4 passes. _Refined by exp 5 below._

## Experiment — 8.4 mm plywood power × speed sweep (8mm_plywood_5) — DONE 2026-06-17

Full writeup: <8mm_plywood_5/README.md>. Held `z_per_pass = −0.2`, 5 passes, focus at surface; swept power
{35,40,45,50}% × speed {20,25,30} mm/s.

**Result: speed is the dominant lever for full penetration, not power.** 20 mm/s severs at every power (even 35%); 25
mm/s only at 50%; 30 mm/s never fully cuts (bottom veneer left). 40/20 and 45/20 fell out spontaneously.

Edge close-ups (the [Google Photos album][gphotos-album]) show char is similar across the 20 mm/s cells — slower dwell
means low power doesn't burn less; 50/25 is marginally cleanest.

**Production recipe:** focus at surface (7.4 mm spacer), `z_per_pass = −0.2`, **5 passes, 40% power, 20 mm/s** —
reliable spontaneous release at modest power. Keep speed at 20 (faster leaves the bottom veneer except at 50%). Alt: 50%
/ 25 mm/s if throughput/edge matters. Use **45%** for full-size parts (margin for warp / bed-position variation).

### Hold-down

The honeycomb bed is **ferromagnetic** (steel) — strong neodymium magnets clamp through 8.6 mm ply to it. Hold-downs
only need to **flatten warp** (focus consistency); the sheet stays one piece until the last pass, so parts don't shift.
Ring the perimeter (outside the job bounding box = always clear of the head); add an interior magnet only for a center
bow, at a spot LightBurn's traversal preview shows the head avoids. Keep magnets low — head clearance drops to ~4 mm as
the bed rises during passes; a strike mid-cut ruins the part. (Printed comb-nub hold-downs are the fallback if a
non-magnetic bed is ever used.)

[focus-photo]:
  https://photos.google.com/share/AF1QipM1oj5Y8VAvqJsvA8Z6zUneTlEMUgcKar-bqDRTKQDyphouFGdHAoWeB8cUFRnJYA/photo/AF1QipPNwQbE3jtlAgbU3gnAqTB4VeKin-McRwmW8_Rq?key=eTV5a3d5cjF0bVhuX3QtcHZWR1g4X3o1eGplTXZ3
[gphotos-album]: https://photos.app.goo.gl/WPgWyv2fPheY8eDG7
