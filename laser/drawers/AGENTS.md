# laser/drawers — agent notes

Material cut-test experiments for the drawer build, run on the Noisebridge laser. Machine calibration, focus procedure,
and the Z-sign gotcha live in <calibration_log.md> — read it before designing a cut.

## Material test configs

- The material-test generator (<../material_test/README.md>) **auto-generates the grid's title block and axis labels**,
  which already show the swept parameters. Do **not** restate the swept axes in the TOML `title` (no
  `"passes × z/pass"`-style headers). Keep `title` to the material plus a short identifier only, e.g.
  `"8.4mm plywood exp5"`.
- Each experiment gets its own `8mm_plywood_N/` folder holding the toml, the generated lbrn2 and a short README of
  setup + results; result photos live in the Google Photos album (linked from the README), not committed.
