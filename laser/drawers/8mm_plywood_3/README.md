# 8.4 mm plywood cut test — passes × Z-per-pass (2026-06-17)

First cut scan after recalibrating the Noisebridge Kaitian CM1309. Machine model, the Z conversion factor (`f ≈ 3.69`),
the 0.1-commanded Z quantum, and the focus result (7.4 mm spacer) are in <../calibration-log.md>.

## Setup

- Power **50%** (min=max), speed **20 mm/s**, kerf 0, single focus at the surface (`z_offset = 0`, 7.4 mm spacer).
- Grid: `num_passes` ∈ {4, 5, 6, 7} (X) × `z_per_pass` ∈ {0.0, +0.1, +0.2} commanded (Y).
- Files: `8mm_plywood_3.toml`, `8mm_plywood_3.lbrn2`.

### ⚠️ Z sign was inverted vs our assumption

We set `z_per_pass` **positive** expecting the bed to rise (focus deeper). It went the other way: positive `z_per_pass`
drove the bed **down** each pass, pulling focus _away_ from the material. The sign is flipped somewhere we haven't
pinned down — LightBurn _displays_ the step as positive too, and the controller coordinate is fine (increasing commanded
Z = bed up, as calibrated); possibly a LightBurn Z-direction switch. So **negative** `z_per_pass` should raise the bed /
deepen focus (matches the generator's default) — to be applied in the next experiment. Read the +0.1/+0.2 rows here as
"focus stepped the wrong way," not "tracked down."

## Results

A cell "passes" if the 20 mm square releases with a **light finger nudge** (nothing more forceful). the [cut-outs
photo][e3-cut] shows the squares that came free that way.

| passes \ z/pass | 0.0      | +0.1      | +0.2      |
| --------------- | -------- | --------- | --------- |
| 4               | nudge    | **stuck** | **stuck** |
| 5               | nudge    | **stuck** | nudge     |
| 6               | nudge    | **stuck** | nudge     |
| 7               | **fell** | nudge     | nudge     |

- **Every fixed-focus cell (z/pass = 0) released**, even 4 passes. `7/0` was the only cell that **fell out unaided** —
  it stayed on the bed when the sheet was lifted; the rest needed a light nudge.
- The four that wouldn't release with a nudge: **4/0.1, 5/0.1, 6/0.1, 4/0.2** — all stepping cells (which defocused
  upward due to the inverted sign).

Photos: [labeled grid][e3-grid], [released squares][e3-cut].

## Conclusion

Once focus and Z were correct, **8.4 mm ply cuts cleanly at 50% / 20 mm/s with fixed focus at the surface** — the
per-pass Z stepping was never needed and (inverted) only hurt. The original "inconsistent cuts" were the Z
miscalibration, not energy or focus.

**Production recipe:** 50% power, 20 mm/s, focus at surface (7.4 mm spacer), `z_per_pass = 0`, **6 passes** for margin
(7 for certainty). No focus stepping.

If stepping is ever revisited, use **negative** `z_per_pass` (bed up = focus deeper).

[e3-grid]:
  https://photos.google.com/share/AF1QipM1oj5Y8VAvqJsvA8Z6zUneTlEMUgcKar-bqDRTKQDyphouFGdHAoWeB8cUFRnJYA/photo/AF1QipPpw4xch7I3voD3U14eoX94ujr-wLGrFP8bsuEt?key=eTV5a3d5cjF0bVhuX3QtcHZWR1g4X3o1eGplTXZ3
[e3-cut]:
  https://photos.google.com/share/AF1QipM1oj5Y8VAvqJsvA8Z6zUneTlEMUgcKar-bqDRTKQDyphouFGdHAoWeB8cUFRnJYA/photo/AF1QipPFIAwi-B5nY_EELJY0udUutbIECNyKCTF3YM28?key=eTV5a3d5cjF0bVhuX3QtcHZWR1g4X3o1eGplTXZ3
