---
name: debundle_explain
description: >-
  Answer questions about how software works when its repository has a full or
  partial debundle spec, extending the spec and emitted modules to cover the
  code needed to explain the behavior. Use by default for such questions even
  when the user does not say "debundle"; always use it when they do.
---

# Debundle Explain

If debundle tooling blocks the task, follow
`references/docs/bug_reproductions.md` before working around it.

The [task chooser](references/README.md#choose-a-task) links to the relevant
inspection and editing recipes. Read the linked reference for the operation.

Use this skill to answer a software-behavior question from the debundled code
and leave the spec covering the code needed to answer it. Search for a matching
spec when the target is unclear. If no spec or source snapshot exists, say so
and answer from the available source without claiming the debundle tree covers
it.

## Workflow

1. **Find the target and its adapter.** Read the target's `AGENTS.md`, README,
   and spec/build definitions. Identify the source snapshot or version, spec
   modules, emitted module tree, pipeline/gate command, and any tests that run
   against the emitted code. Never edit the upstream bundle.
2. **Trace the behavior.** Read the existing emitted modules and follow the
   path from the relevant entry point through branches, helpers, data, and
   call sites. Use `debundle graph-summary`, `coverage`, `atoms`, `describe`,
   and `inspect-source` as needed; coverage counts alone do not establish the
   behavior. Include only the surrounding code needed to explain the question
   and preserve correct extraction boundaries.
3. **Close spec gaps.** If any code needed for the answer is missing or hard
   to read in the emitted tree, extend the spec to claim the complete relevant
   atomic groups in coherent, idiomatic modules. Give modules and bindings
   descriptive names. Add factual module or binding `comment:` annotations
   where they clarify behavior, ownership, or a relationship a reader would
   otherwise have to rediscover. These comments emit into the generated code;
   edit them with `debundle modules comment` / `debundle bindings comment`, not
   YAML `#` comments.
4. **Keep selectors durable.** Follow `debundle_stabilize` and
   `references/docs/selectors.md`: anchor selectors on purpose-bearing,
   likely-stable code features; do not pin minified binding names. Prefer a
   concise identifying feature with volatile bodies holed out. A selector
   resolving uniquely today does not prove it will survive a future rebuild.
   If no honest stable anchor exists, leave documented selector debt instead
   of copying a long generated body into the spec.
5. **Prove the emitted result.** Run the target's `debundle run` or equivalent
   gate after edits, then run tests that exercise the debundled code when the
   target provides them. Regenerate through the adapter; do not hand-edit
   generated output. Fix failures before relying on the tree for the answer,
   or clearly report what could not be verified.
6. **Answer from the result.** Explain the observed control/data flow and name
   the generated module paths and functions that show it. Report the spec
   additions and exact gate/test results, and distinguish verified behavior
   from any remaining uncertainty.

For read-only coverage planning, use `debundle_plan_work`; for substantial
source discovery, use `debundle_intake`; use `debundle_mint_names` for naming
passes and `debundle_architect` when module boundaries are unclear. A focused
question should not turn into unrelated spec cleanup. Use
`debundle_lane_worker` or `debundle_orchestrator` only when the needed peel
grows into a substantial or multi-lane task.
