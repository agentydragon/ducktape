# 8.4 mm plywood cut test — least-burn power × speed sweep (2026-06-17)

Exp 5. Holds the working focus-stepping from <../8mm_plywood_4/README.md> (`z_per_pass = −0.2`, 5 passes, focus at
surface) and sweeps **power down × speed up** to find the lowest-energy recipe that still fully severs. Machine facts:
<../calibration_log.md>.

## Setup

- Fixed: `z_per_pass = −0.2`, **5 passes**, `z_offset = 0` (focus at surface).
- Grid: `power_pct` ∈ {35, 40, 45, 50} × `speed_mm_s` ∈ {20, 25, 30}.
- Files: `8mm_plywood_5.toml`, `8mm_plywood_5.lbrn2`.

## Results

`fell` = dropped during the cut · `clean` = full sever, came out with no/▏trivial effort · `push` = top severed but
**bottom veneer intact**, needed a finger push to crack · `stuck` = did not release. `push`/`clean` judged from the
[edges photo][e5-edges] (cross-sections in grid layout) and the user's handling.

| speed \ power | 35        | 40       | 45       | 50    |
| ------------- | --------- | -------- | -------- | ----- |
| 20 mm/s       | clean     | **fell** | **fell** | clean |
| 25 mm/s       | push      | push     | push     | clean |
| 30 mm/s       | **stuck** | push     | push     | push  |

- **Speed is the dominant lever for full penetration**, not power. **20 mm/s severs at every power tested (even 35%)**;
  25 mm/s only fully cuts at 50%; 30 mm/s never fully cuts (and 35/30 didn't even release).
- Within a speed, power matters only at the margin (e.g. 25 mm/s needs 50% to finish).
- `push` cells left the **bottom veneer** uncut — the kerf didn't penetrate the last layer. Consistent with the cut
  front running marginal at the bottom on the faster rows.

Photos: [grid front][e5-grid] (all seated), [spontaneous drops][e5-drop] (holes = 40/20 & 45/20 that fell during the
cut), [cut-outs][e5-cut] (tops, grid layout), [edges][e5-edges] (cross-sections, grid layout — `push` cells show lighter
raw veneer at the bottom).

## Conclusion

To fully sever 8.4 mm ply, **keep speed at 20 mm/s**; there, power can drop all the way to **35%** and still cut
through. Faster (25/30 mm/s) leaves the bottom veneer except at high power.

35% / 20 is the lowest-power full cut, but the edge close-ups (below) show it doesn't burn any less than higher powers
at 20 mm/s. Final recipe pick is in the close-up section.

## Edge close-ups (char comparison)

Two angles each of the four full-cut candidates (Google Photos): 35/20 ([a][c35a], [b][c35b]), 40/20 ([a][c40a],
[b][c40b]), 45/20 ([a][c45a], [b][c45b]), 50/25 ([a][c50a], [b][c50b]).

**Verdict: char is broadly similar across all four** — every cut has a heavy blackened kerf wall (inherent to CO₂ on
glue-layered ply). The only visible trend: the **slower 20 mm/s cells char slightly more** (thicker, crumblier black
zone from longer dwell), most noticeably 35/20; **50/25 looks marginally cleaner**. Lower power did _not_ buy noticeably
less burn at 20 mm/s because the extra dwell offsets it.

So the "least burn" and "least power" goals point different directions, and the gap is small:

- **Least char / fastest:** 50/25 — marginally cleanest edge, 25 % faster, but highest power.
- **Most reliable release:** 40/20 or 45/20 — fell out spontaneously, moderate power.
- **Least power:** 35/20 — but chars as much as the rest (no burn win) and releases less freely.

**Production pick: 40 % / 20 mm/s** (5 passes, `z_per_pass = −0.2`, focus at surface) — reliable spontaneous release at
modest power, char no worse than the alternatives. Use **50 / 25** instead if edge cleanliness or throughput matters
more than tube wear.

## Next (optional)

Edge char is near its floor for this material; further gains would need air-assist tuning or masking, not parameter
changes. If throughput matters, exp 6 could check whether **6–7 passes lets 25–30 mm/s fully sever** (faster per-cut,
similar or less char). Otherwise the recipe is settled.

[e5-grid]:
  https://photos.google.com/share/AF1QipM1oj5Y8VAvqJsvA8Z6zUneTlEMUgcKar-bqDRTKQDyphouFGdHAoWeB8cUFRnJYA/photo/AF1QipMmSEffRFmAxgv2sC5kKk9ZiuOeghuwBcZysxtg?key=eTV5a3d5cjF0bVhuX3QtcHZWR1g4X3o1eGplTXZ3
[e5-drop]:
  https://photos.google.com/share/AF1QipM1oj5Y8VAvqJsvA8Z6zUneTlEMUgcKar-bqDRTKQDyphouFGdHAoWeB8cUFRnJYA/photo/AF1QipNtfaHiBprimkZzTJRvo5s0-Q-6v5tyrCXDxgjV?key=eTV5a3d5cjF0bVhuX3QtcHZWR1g4X3o1eGplTXZ3
[e5-cut]:
  https://photos.google.com/share/AF1QipM1oj5Y8VAvqJsvA8Z6zUneTlEMUgcKar-bqDRTKQDyphouFGdHAoWeB8cUFRnJYA/photo/AF1QipOTcn17y-vWEJLc9E6NbGN1MbBmTFYw94v0mSZf?key=eTV5a3d5cjF0bVhuX3QtcHZWR1g4X3o1eGplTXZ3
[e5-edges]:
  https://photos.google.com/share/AF1QipM1oj5Y8VAvqJsvA8Z6zUneTlEMUgcKar-bqDRTKQDyphouFGdHAoWeB8cUFRnJYA/photo/AF1QipOv2hweVlFINUd8EaxOXJylqToevl2KcXCQPD7d?key=eTV5a3d5cjF0bVhuX3QtcHZWR1g4X3o1eGplTXZ3
[c35a]:
  https://photos.google.com/share/AF1QipM1oj5Y8VAvqJsvA8Z6zUneTlEMUgcKar-bqDRTKQDyphouFGdHAoWeB8cUFRnJYA/photo/AF1QipPaUKQ6ewY09P-gVssUQniv0wFJ3CUnLIsvELmn?key=eTV5a3d5cjF0bVhuX3QtcHZWR1g4X3o1eGplTXZ3
[c35b]:
  https://photos.google.com/share/AF1QipM1oj5Y8VAvqJsvA8Z6zUneTlEMUgcKar-bqDRTKQDyphouFGdHAoWeB8cUFRnJYA/photo/AF1QipPL9CxEmZv7yT9W3L1Ef-BmNGwDf7KnzOuGQjsY?key=eTV5a3d5cjF0bVhuX3QtcHZWR1g4X3o1eGplTXZ3
[c40a]:
  https://photos.google.com/share/AF1QipM1oj5Y8VAvqJsvA8Z6zUneTlEMUgcKar-bqDRTKQDyphouFGdHAoWeB8cUFRnJYA/photo/AF1QipMAtuLfLQeqQwyRq2WVcW7DlNtesBlcieVLYjfF?key=eTV5a3d5cjF0bVhuX3QtcHZWR1g4X3o1eGplTXZ3
[c40b]:
  https://photos.google.com/share/AF1QipM1oj5Y8VAvqJsvA8Z6zUneTlEMUgcKar-bqDRTKQDyphouFGdHAoWeB8cUFRnJYA/photo/AF1QipMLZL6gHEpxXq5rV9PpErpst2aCOEHOBjjnVOVZ?key=eTV5a3d5cjF0bVhuX3QtcHZWR1g4X3o1eGplTXZ3
[c45a]:
  https://photos.google.com/share/AF1QipM1oj5Y8VAvqJsvA8Z6zUneTlEMUgcKar-bqDRTKQDyphouFGdHAoWeB8cUFRnJYA/photo/AF1QipMPVxYGaLU4q6MWebVfxC1e7OlQAxKtJGJnHmal?key=eTV5a3d5cjF0bVhuX3QtcHZWR1g4X3o1eGplTXZ3
[c45b]:
  https://photos.google.com/share/AF1QipM1oj5Y8VAvqJsvA8Z6zUneTlEMUgcKar-bqDRTKQDyphouFGdHAoWeB8cUFRnJYA/photo/AF1QipO2Q2z9k53-gjZZLqkbT9JfHDp3Lk5L2SaBg1Ix?key=eTV5a3d5cjF0bVhuX3QtcHZWR1g4X3o1eGplTXZ3
[c50a]:
  https://photos.google.com/share/AF1QipM1oj5Y8VAvqJsvA8Z6zUneTlEMUgcKar-bqDRTKQDyphouFGdHAoWeB8cUFRnJYA/photo/AF1QipPbWID4bNkznbvRaGGGqg9e1zXqRRVdDfidQobM?key=eTV5a3d5cjF0bVhuX3QtcHZWR1g4X3o1eGplTXZ3
[c50b]:
  https://photos.google.com/share/AF1QipM1oj5Y8VAvqJsvA8Z6zUneTlEMUgcKar-bqDRTKQDyphouFGdHAoWeB8cUFRnJYA/photo/AF1QipOK6vyuyLXzCyNn7PcYWpm4oT1z92kEczA11UWh?key=eTV5a3d5cjF0bVhuX3QtcHZWR1g4X3o1eGplTXZ3
