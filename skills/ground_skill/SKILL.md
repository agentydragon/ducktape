---
name: ground-skill
description: >
  Ground a skill in tested, runnable examples. Use when creating a new skill or
  upgrading an existing skill to have CI-verified recipes. Triggers when the user
  asks to create a skill, add examples to a skill, or verify a skill has tested
  recipes.
---

# Ground a Skill

A **grounded skill** backs its claims with runnable code that CI verifies continuously.
Prose rots; tests catch it. The agent using the skill reads the same recipe files that
the tests execute.

## Skill types and grounding approach

| Type           | Examples                  | Grounding                                      |
| -------------- | ------------------------- | ---------------------------------------------- |
| Tool/API       | freecad, buildbuddy_api   | Runnable recipe scripts + output golden tests  |
| Workflow/meta  | followups, verify_docs    | Eval harness: task scenario + scored checklist |
| Pure reasoning | superforecaster, readback | Eval harness if testable; otherwise minimal    |

Tool/API skills are the highest-value target — deterministic outputs that can be asserted
against. Workflow skills use the eval harness pattern (below).

## Recipes are scaffold-free; tests provide the scaffold

Recipes in the skill should contain only the **interesting logic** — the thing the agent
needs to learn. Boilerplate (starting a process, setting env vars, spinning up a container,
creating temp dirs) belongs in the test scaffold, not the recipe.

The skill doc explains the required scaffolding **once**, globally. Example: "All FreeCAD
scripts assume a running FreeCAD instance under Xvfb with `QT_QPA_PLATFORM=offscreen`."
Agents understand this applies to every recipe without re-reading it each time.

Tests add the scaffolding programmatically: start the tool, inject the recipe, tear down.
The recipe itself stays clean — just the meaty steps — so agents burn no context on setup
noise and still get fully tested code.

Shared boilerplate across recipes goes in a helper library (`examples/helpers.py` or
similar). Recipes import helpers; they never copy them.

## Workflow: writing a tested example

Follow this sequence when adding a recipe for something new:

1. **Decide what the example demonstrates** — one atomic operation, one composition.
2. **Research and figure out how to do it** — read docs, look at existing code, search.
3. **Try it yourself with fast iteration** — install prerequisites, use the actual tool
   interactively, the same way the recipe will use it. Don't write the recipe yet.
4. **When things don't work, document the trap** — wrong assumptions, misleading docs,
   gotchas you hit. These are exactly the pitfalls the skill should warn about. Add them
   to the skill's gotchas section as you find them.
5. **Once it works correctly** (you've verified the output is right — visually, formally,
   or informally) — write the recipe following the steps you just did.
6. **Run the recipe** as the agent would run it, including any scaffolding.
7. **If something breaks, repeat from step 3** — fix the recipe, document any new
   gotchas, iterate until it runs cleanly.
8. **Formalize into a test** — add scaffolding in the test harness, write assertions,
   commit any golden outputs.

The key insight: step 3 (hands-on iteration) is where you discover what the skill needs
to say. Skipping it and writing the recipe from docs alone produces untested assumptions.

## Recipe quality

- **Atomic**: one recipe, one concept. Two things → split.
- **Real**: runs against the actual tool/API/environment. No mocks.
- **Scaffold-free**: contains only the logic; scaffolding lives in tests and is explained
  once in the skill doc.
- **Deduplicated**: shared setup goes into a helper library, not copy-pasted.
- **Referenced**: the skill doc says "see `examples/foo.py`" and that file is in the
  deployed tarball.

Recipes can be Python scripts, shell scripts, or any executable that runs in a container.
The form follows the skill's domain.

## Test quality and assertion styles

Choose the assertion style that gives the best signal-to-noise for the recipe's output:

**Normal assertions** — for structured outputs where you know exactly what to check:

```python
result = run_recipe(input)
assert result["count"] == 42
assert "error" not in result
```

**Visual golden tests** — for 2D/3D renders, drawings, or any image output. Commit the
expected image alongside the test. GitHub diffs make pixel drift immediately visible in
code review. Use a perceptual diff (not byte equality) to tolerate minor rendering
variation.

**Loose text matching** — for outputs where exact text varies by tool version but key
content is stable. Check for the presence of known good strings and absence of known bad
ones rather than full golden equality. Makes tests robust to minor version changes:

```python
output = run_recipe(binary, test_data)
assert "foo" in output       # known bool var in test data
assert "bar" not in output   # known string var, should not appear
```

**Container side-effects** — for recipes that mutate a file, database, or service: run
the recipe in a container, then inspect the container state.

All tests must fail loudly — no swallowed exceptions. A broken recipe → a red test.

## Bazel wiring

```python
# examples/BUILD.bazel
py_library(name = "my_recipe", srcs = ["my_recipe.py"], deps = [...])

py_test(
    name = "test_my_recipe",
    srcs = ["test_my_recipe.py"],
    data = ["golden.png", "//skills/myskill/conda:tool_binary"],
    deps = [":my_recipe", "//skills/myskill:conftest", ...],
)

# myskill/BUILD.bazel
load("//skills:defs.bzl", "skill_package")

skill_package(
    name = "myskill",
    srcs = ["SKILL.md", "//skills/myskill/examples:my_recipe.py"],
)
```

Add `myskill_tar` to `skills/BUILD.bazel`'s `all_skills_tar` deps.

## Eval harness (workflow/reasoning skills)

For skills where outputs aren't deterministic, use the eval harness pattern:

```
skills/myskill/eval/
  README.md          # how to run the eval
  scenario_name/
    TASK.md          # prompt given to the agent
    CHECKLIST.md     # scored rubric (0/1/2 per criterion)
  run_eval.py        # launches agent with skill + MCP tools, records transcript
  BUILD.bazel
```

The eval is a `py_binary` run manually by agents or humans. CI does not gate on it
(non-deterministic), but it provides a repeatable benchmark for skill improvements.

## Process: upgrading an existing skill

1. **Audit the claims** — list every pattern, API call, or command the skill describes.
2. **Check what's tested** — which claims have runnable recipes? Which are prose-only?
3. **Identify the highest-value gaps** — untested claims most likely to mislead if stale.
4. **Add recipes and tests for the gaps**, following the workflow above.
5. **Update the skill doc** to reference the new recipe files.

## Reference

See `//skills/freecad` for the canonical example of a fully grounded tool/API skill:
scaffold-free recipes in `examples/`, shared helpers in `freecad_helpers.py`, tests with
visual and structural golden outputs, `conftest.py` for container fixtures, and an eval
harness in `eval/`.
