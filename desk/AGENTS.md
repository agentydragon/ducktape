@README.md

## Keep documentation, schematic source, and rendered schematic in sync with reality

Everything in this directory describes a single physical thing — the desk
wiring — and must stay consistent with what is actually plugged in. When you
learn something new or the wiring changes, update **every** artifact that the
change touches, in the same commit:

- `README.md` — current-state prose. Sections that most often need updates:
  Devices on hand, Cables on hand, Target topology (mermaid), Cable plan,
  Cable routing, Placement & mounting, Open questions.
- `wiring_schematic.dot` — source of truth for the physical-layout
  schematic. Update device rows, edges, colours, and grommet routing to
  match the new state.
- `wiring_schematic.svg` — regenerate whenever the `.dot` changes:
  ```bash
  dot -Tsvg desk/wiring_schematic.dot -o desk/wiring_schematic.svg
  ```
  Commit both files together; the SVG is the version other files (and PR
  reviewers) can read without a Graphviz install.
- `debug/build_log.md` — for empirical / experimental / build events
  (something was plugged in, a test was run, a hypothesis was updated).
  Add a dated `## YYYY-MM-DD — <headline>` section, keep chronological
  order. Do not use this file for spec / target-state changes; those
  belong in `README.md`.

A change that affects the topology should almost always touch **at least**
`README.md` and either `wiring_schematic.dot` (topology change) or
`build_log.md` (experiment / observation). Missing one is drift; catch it
during review.

Cable colours in the schematic are the key to the eventual coloured
physical cable markers — if a new cable is added, pick a new distinct
colour and keep the map 1:1.
