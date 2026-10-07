# 8.4 mm plywood cut test — passes × negative Z-per-pass (2026-06-17)

Exp 4. Follows <../8mm_plywood_3/README.md>, which found the Z-step sign was backwards (positive lowered the bed). Here
`z_per_pass` is **negative** so the bed rises and focus tracks the cut deeper. Machine/calibration facts:
<../calibration_log.md>.

## Setup

- Power **50%**, speed **20 mm/s**, focus at surface (`z_offset = 0`, 7.4 mm spacer).
- Grid: `num_passes` ∈ {3, 4, 5, 6} × `z_per_pass` ∈ {0.0, −0.1, −0.2} commanded (0, −0.37, −0.74 mm/pass physical,
  deeper).
- Files: `8mm_plywood_4.toml`, `8mm_plywood_4.lbrn2`.

**The negative flip worked** — focus now tracks downward (bed rises), confirmed by the clear improvement with deeper
steps below.

## Results

`fell` = dropped out unaided when lifted · `nudge` = released with a tiny finger nudge · `stuck` = would not release
with a nudge. Cross-checked against the photos.

| z/pass \ passes | 3     | 4        | 5        | 6        |
| --------------- | ----- | -------- | -------- | -------- |
| 0.0             | stuck | stuck    | nudge    | nudge    |
| −0.1            | stuck | stuck    | nudge    | **fell** |
| −0.2            | nudge | **fell** | **fell** | **fell** |

- Strong gradient: **more passes and/or deeper step → cleaner release.** That's the signature of focus correctly
  tracking the kerf down.
- **−0.2 step dominates:** even **4 passes at −0.2 fell out spontaneously**, and 3/−0.2 was a trivial nudge. Fixed focus
  (0.0) never dropped unaided and needed 5+ passes for even a nudge.
- Only the low-energy corner stayed stuck: {3, 4} passes × {0.0, −0.1}.

Photos: [grid front][e4-grid] (all cells seated), [spontaneous drops][e4-drop] (holes = the 4 that fell unaided: 6/−0.1,
4/−0.2, 5/−0.2, 6/−0.2), [cut-outs][e4-cut] (the 8 released squares).

## Conclusion

With correct (negative) focus stepping, **8.4 mm ply severs at far fewer passes than fixed-focus**: `z_per_pass = −0.2`,
**4 passes** gives a clean spontaneous drop at 50% / 20 mm/s — vs the 6–7 fixed-focus passes from exp 3.

**Updated production recipe:** 50% power, 20 mm/s, focus at surface, **`z_per_pass = −0.2`, 4 passes** (5 for margin).
Crash: 4 passes × −0.2 rises 0.6 cmd = 2.2 mm — well clear.

## Next (exp 5)

Optimize for less burn / power at fixed 4–5 passes / −0.2: sweep **power down** (e.g. 35/40/45/50%) and/or **speed up**
(20/25/30 mm/s) to find the minimum energy that still severs cleanly.

[e4-grid]:
  https://photos.google.com/share/AF1QipM1oj5Y8VAvqJsvA8Z6zUneTlEMUgcKar-bqDRTKQDyphouFGdHAoWeB8cUFRnJYA/photo/AF1QipMUrYgReQkWI2zSmWt04DU7hu2jKvwrkf-zz8Mv?key=eTV5a3d5cjF0bVhuX3QtcHZWR1g4X3o1eGplTXZ3
[e4-drop]:
  https://photos.google.com/share/AF1QipM1oj5Y8VAvqJsvA8Z6zUneTlEMUgcKar-bqDRTKQDyphouFGdHAoWeB8cUFRnJYA/photo/AF1QipMFabwQ-bYrWyYP6bNSUnLofIfX3gK9b9r5PTsu?key=eTV5a3d5cjF0bVhuX3QtcHZWR1g4X3o1eGplTXZ3
[e4-cut]:
  https://photos.google.com/share/AF1QipM1oj5Y8VAvqJsvA8Z6zUneTlEMUgcKar-bqDRTKQDyphouFGdHAoWeB8cUFRnJYA/photo/AF1QipN0mzCgUjYRKIbCSUKogWcqlOUvr9WUOxj1788_?key=eTV5a3d5cjF0bVhuX3QtcHZWR1g4X3o1eGplTXZ3
