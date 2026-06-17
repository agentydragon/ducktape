# Debundle User Guide

Step-by-step workflows for the `debundle` CLI, split by task. The command surface
itself is in `cli.md`; these documents are the operational companion.

- `cli_basics.md` — env vars, output formats, running the pipeline, inspecting a
  binding, evidence files, the gate. The base every workflow builds on.
- `selectors.md` — authoring portable `source_match` / `binding_groups` selectors
  that survive rebuilds.
- `spec_editing.md` — proposing, moving, merging, and renaming modules and
  bindings; peel heuristics; `comment:` fields.

## See also

- `cli.md` — current command surface.
- `README.md` — crate pitch, Bazel integration, `comment:` schema.
- `design.md` — the realizability theorem the gate enforces.
