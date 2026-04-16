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

Tool/API skills are the highest-value target. They have real, deterministic outputs that
can be diffed against golden files. Workflow skills use the eval harness pattern (below).

## Process: new skill

1. **Enumerate atomic operations** the skill needs to teach. What are the primitives?
   What compositions matter?
2. **Write one recipe per operation** — real, runnable code demonstrating exactly one
   thing. Put recipes in `examples/`.
3. **Write a shared helper library** (`examples/helpers.py` or similar) for setup/teardown
   patterns that repeat across recipes. Recipes import helpers; they don't copy them.
4. **Write a test per recipe** that actually executes it and asserts on the outputs.
5. **Commit golden outputs** (images, JSON, text files) alongside the tests so diffs are
   machine-checked.
6. **Reference recipes in the skill doc** — every non-obvious pattern should say
   "see `examples/foo.py`" and that file should be in the tarball the agent receives.
7. **Wire into Bazel** — `skill_package` target in the skill's `BUILD.bazel`, entry in
   `skills/BUILD.bazel`'s `all_skills_tar`.

## Process: upgrading an existing skill

1. **Audit the claims** — list every pattern, API call, or command the skill describes.
2. **Check what's tested** — which claims have runnable recipes? Which are prose-only?
3. **Identify the highest-value gaps** — untested claims most likely to mislead if stale.
4. **Add recipes and tests for the gaps**, following the new skill process above.
5. **Update the skill doc** to reference the new recipe files.

## Recipe quality

- **Atomic**: one recipe, one concept. Two things → split.
- **Real**: runs against the actual tool/API/environment. No mocks.
- **Crisp**: minimal setup, direct demonstration. Skip boilerplate the reader can infer.
- **Self-contained**: imports from helpers, not from other recipes.
- **Referenced**: the skill doc says "see `examples/foo.py`" and that file exists in
  the deployed tarball.

Recipes can be Python scripts, shell scripts, or any executable that runs in a container.
The form follows the skill's domain.

## Test quality

- **Runs the actual recipe** — invokes it against the real tool.
- **Checks actual output** — assert on output content, not just exit code. The right
  assertion style depends on the recipe: normal `assert` statements, golden file diffs
  (syrupy snapshots or committed files), or inspecting side effects in a container.
- **Fails loudly** — no swallowed exceptions. A broken recipe → a red test.

## Bazel wiring

```python
# examples/BUILD.bazel — Python recipe
py_library(name = "my_recipe", srcs = ["my_recipe.py"], deps = [...])

py_test(
    name = "test_my_recipe",
    srcs = ["test_my_recipe.py"],
    data = ["//skills/myskill/conda:tool_binary"],
    deps = [":my_recipe", "//skills/myskill:conftest", ...],
)

# examples/BUILD.bazel — shell recipe tested in a Docker container
sh_test(
    name = "test_my_script",
    srcs = ["test_my_script.sh"],
    data = ["my_script.sh"],
    # requires_docker = True if needed
)

# myskill/BUILD.bazel
load("//skills:defs.bzl", "skill_package")

skill_package(
    name = "myskill",
    srcs = [
        "SKILL.md",
        "//skills/myskill/examples:my_recipe.py",
    ],
)
```

Add the `myskill_tar` to `skills/BUILD.bazel`'s `all_skills_tar` deps.

## Eval harness (workflow/reasoning skills)

For skills where outputs aren't deterministic, use the eval harness pattern:

```
skills/myskill/eval/
  README.md          # how to run the eval
  scenario_name/
    TASK.md          # prompt given to the agent
    CHECKLIST.md     # scored rubric the judge fills out (0/1/2 per criterion)
  run_eval.py        # launches agent with skill + MCP tools, records transcript
  BUILD.bazel
```

The eval is a `py_binary` target (`run_eval`) that agents or humans run manually.
CI does not gate on it (non-deterministic), but it provides a repeatable benchmark
for skill improvements.

## Reference

See `//skills/freecad` for the canonical example of a fully grounded tool/API skill:
recipes in `examples/`, tests with golden outputs, `conftest.py` for container fixtures,
and an eval harness in `eval/`.
