@README.md

## Skill authoring

Follow the `verify-docs` skill when writing or reviewing documentation and skills. Invoke `/verify-docs` to audit.

### `allowed-tools` requires explicit user approval

**Do NOT add the `allowed-tools` frontmatter key without explicit approval
from the user.** The default is to **omit it entirely** — skills work fine
without it, tools just go through normal permission prompts.

`allowed-tools` auto-grants tool permissions (no per-use prompt) for the
entire duration of the agent's response — every tool call Claude makes until
the user types again runs without approval. This is a meaningful security
surface: `allowed-tools: Bash` means any Bash command runs without prompting
during the skill's execution.

See <skills/docs/allowed-tools-internals.md> for how this works under the hood.

## Example scripts and testing

Skills that include example scripts (referenced from `SKILL.md`) should package them into the skill tarball via `skill_package(srcs=...)`. Tests that verify these examples actually work live alongside but outside the skill package as `testonly` targets.

Pattern:

- `SKILL.md` references example scripts (e.g., `parametric_sketch.py`)
- `skill_package(srcs=[...])` includes `SKILL.md` + all example scripts
- `py_test` / `sh_test` targets run the examples and compare outputs against committed golden files
- Golden files live in `golden/` subdirectory
- Test helpers (comparators, fixtures) are `testonly = True` and not part of the skill package

Example (`skills/freecad/`):

- Skill package: `SKILL.md`, `parametric_sketch.py`, `build_compound.py`, etc.
- Test: `test_parametric_sketch.py` runs `parametric_sketch.py` in a FreeCAD Docker container, compares DXF output against `golden/bracket.dxf`
- Test helper: `testing/compare.py` (`testonly`) normalizes non-deterministic content before diffing
