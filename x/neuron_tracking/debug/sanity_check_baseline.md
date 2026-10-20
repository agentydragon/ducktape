# Frame-52 sanity check baseline

The user accepted the primary assistant's 15-candidate annotation as close enough
for a coarse check, then selected four positive examples. `sample_checks.json`
records the resulting constraints on frame 52, source timestamp 25.9 s:

| Check                     | Half-open source-pixel box | Required body evidence      |
| ------------------------- | -------------------------- | --------------------------- |
| Side/belly count          | `[245, 560, 635, 805]`     | 12–20 distinct body centers |
| Blue, second from left    | `[332, 714, 348, 731]`     | At least one body center    |
| Dark blue                 | `[487, 643, 519, 669]`     | At least one body pixel     |
| Dark teal below dark blue | `[493, 676, 518, 693]`     | At least one body pixel     |
| Leftmost red/pink         | `[254, 758, 290, 786]`     | At least one body pixel     |

The last three boxes cover the union of body, branch, and uncertainty pixels of
the reviewed candidates, excluding label decorations. Only predicted **body**
pixels can satisfy them. The four examples must all be present and have separate
IDs: any predicted ID matching more than one example fails the distinctness check.

Centers are area-weighted centroids of nonempty body masks. Colors and arbitrary
identity names are not matching criteria. Source hash, dimensions, frame index,
and timestamp must match before checks run. These are coarse constraints, not
pixel-accurate reference masks or evidence of tracking across frames.

| Saved annotation                     | Centers in crop | Count | Blue center | Dark blue | Teal | Red/pink | Distinct examples | All  |
| ------------------------------------ | --------------- | ----- | ----------- | --------- | ---- | -------- | ----------------- | ---- |
| Trial 1                              | 0               | Fail  | Fail        | Fail      | Fail | Fail     | Fail              | Fail |
| Trial 2                              | 1               | Fail  | Fail        | Fail      | Fail | Fail     | Fail              | Fail |
| Trial 3                              | 0               | Fail  | Fail        | Fail      | Fail | Fail     | Fail              | Fail |
| Trial 4                              | 11              | Fail  | Pass        | Pass      | Pass | Pass     | Pass              | Fail |
| Trial 5                              | 0               | Fail  | Fail        | Fail      | Fail | Fail     | Fail              | Fail |
| Primary assistant's frame annotation | 15              | Pass  | Pass        | Pass      | Pass | Pass     | Pass              | Pass |

Trial 4 matches the blue, dark blue, teal, and red/pink examples with `N026`,
`N023`, `N079`, and `N084`, respectively. The manual annotation uses `N002`,
`N010`, `N011`, and `N001`. Trial 2's only match in the larger crop is `N007`.

Thus 0/5 trials pass all checks. Trial 4 passes every example check and their
distinctness check but is one body below the count check's lower bound. These
counts do not establish that its other predictions are correct. The broader
qualitative findings remain in [the trial review](first_trials.md).

The shared Python CLI evaluated all six saved annotations. Reports are saved as
`sample_check_results.json` in each run directory under
`~/drive/neuron-tracking/runs/`, with their combined results in
`~/drive/neuron-tracking/runs/manual_frame_review/first_five_check_summary.json`.
Raw media, masks, manual annotations, and reports stay outside Git; the checker
and criteria are committed independently.
