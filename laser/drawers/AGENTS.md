# laser/drawers — agent notes

Drawer-build material cut tests on the Noisebridge laser. Read <calibration_log.md> (machine
calibration, focus procedure, Z-sign gotcha) before designing a cut.

- The material-test generator (<../material_test/README.md>) already labels the swept axes in the
  title block; keep the TOML `title` to material plus short ID (e.g. `"8.4mm plywood exp5"`).
- One `8mm_plywood_N/` folder per experiment: toml, generated lbrn2, short README of setup and
  results. Result photos go in the Google Photos album linked from the README, not the repo.
