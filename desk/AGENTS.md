@README.md

## Keep every artifact in sync with the physical wiring

When the wiring changes or you learn something new, update every affected artifact in the same
commit (missing one is drift):

- `README.md`: current/target state (devices, cables, topology, cable plan, routing, placement,
  open questions).
- `wiring_schematic.dot`: source of truth for the physical schematic (device rows, edges,
  colours, grommet routing). Regenerate and commit the SVG with it:
  `dot -Tsvg desk/wiring_schematic.dot -o desk/wiring_schematic.svg`.
- `debug/build_log.md`: experiments and observations only, as dated
  `## YYYY-MM-DD — <headline>` sections in chronological order; never spec/target-state changes.

A topology change touches `README.md` plus the `.dot`; an experiment touches `README.md` plus the
build log. Schematic cable colours map 1:1 to the future physical cable markers: a new cable gets
a new distinct colour.
