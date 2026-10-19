@README.md

- Write and review skills per the `verify-docs` skill (`/verify-docs` to audit).
- **Never add `allowed-tools` frontmatter without explicit user approval** — omit it
  by default. It auto-grants the listed tools for the rest of the agent's response
  (`allowed-tools: Bash` = unprompted arbitrary Bash). Internals:
  <docs/allowed-tools-internals.md>.
- **Example scripts** referenced from `SKILL.md` ship in the archive via
  `skill_package(srcs=...)`. `py_test`s run them and diff against committed golden
  outputs beside each example; test helpers are `testonly = True` and stay out of the
  package. Reference layout: `freecad/examples/<name>/` (e.g. `bracket/`: build
  script `parametric_sketch.py`, committed `drawing.{dxf,svg,pdf}`,
  `test_parametric_sketch.py` running the build in a FreeCAD Docker container).
