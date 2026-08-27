@README.md

## Skill authoring

Follow the `verify-docs` skill when writing or reviewing documentation and skills. Invoke `/verify-docs` to audit.

### `allowed-tools` requires explicit user approval

**Do NOT add the `allowed-tools` frontmatter key without explicit approval
from the user.** The default is to **omit it entirely** — skills work fine
without it, tools just go through normal permission prompts.

`allowed-tools` auto-grants the listed tools for the whole of the agent's
response — every call until the user types again runs unprompted
(`allowed-tools: Bash` = unprompted arbitrary Bash). This is a meaningful
security surface.

See <skills/docs/allowed-tools-internals.md> for how this works under the hood.

## Example scripts and testing

Skills that include example scripts (referenced from `SKILL.md`) should package them into the skill's `.skill` archive via `skill_package(srcs=...)`. Tests that verify these examples actually work live alongside but outside the skill package as `testonly` targets.

Pattern:

- `SKILL.md` references example scripts (e.g., `examples/bracket/parametric_sketch.py`)
- `skill_package(srcs=[...])` includes `SKILL.md` + all example scripts
- `py_test` targets run the examples and compare outputs against committed golden files
- Golden output files live alongside each example (the committed `drawing.dxf`, `drawing.svg`, render PNGs in the example's own directory)
- Test helpers (comparators, fixtures) are `testonly = True` and not part of the skill package

Example (`skills/freecad/`): each example is a directory under `examples/<name>/` holding
its build script, committed outputs, and adjacent test:

- `examples/bracket/`: `parametric_sketch.py` (build), `drawing.dxf` / `drawing.svg` / `drawing.pdf` (committed outputs), `test_parametric_sketch.py` (runs the build in a FreeCAD Docker container and diffs against the committed `drawing.dxf`)
- `examples/bearing_block/`, `examples/compound/`, `examples/cube_with_hole/`: same pattern, each with its own `build*.py` and `test_*.py`
