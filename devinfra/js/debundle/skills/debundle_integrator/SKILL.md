---
name: debundle_integrator
description: Integrate multiple debundle lane-worker branches through a validated merge train. Use for cherry-picking worker commits, resolving expected spec/generated-output conflicts, running the adapter-provided gate and regen commands, isolating failing branches, and reporting landed versus failed work.
---

# Debundle Integrator

Use this role when several lane-worker commits need to land onto the shared
base branch.

Shared CLI workflows land here for gate and `--dry-run` behavior:

@references/cli.md
@references/spec_editing.md

Read other bundled references as needed:

- `references/workflow.md` for orchestration and failure routing
- `references/README.md` for the crate pitch + Comments

## Inputs

The orchestrator provides:

- base branch and starting SHA
- ordered worker commits or branches
- exact gate and regen commands
- expected conflict policy for module YAML, patch streams, and generated JS
- whether pushing is allowed

## Optimistic Train

When workers reported green gates and assignments are mostly disjoint:

1. Apply all worker commits in order onto the base branch.
2. Resolve routine conflicts by preserving the union of independent spec
   removals/additions and rerunning generated output through regen.
3. Run the gate once on the combined result.
4. If green, run regen, commit the integrated result if needed, and stop.

Push only when the orchestrator explicitly requested it.

## Fallback

If the combined gate fails:

- restore the pre-train base
- apply worker commits individually or by halves
- run the gate to isolate failing commits or incompatible pairs
- keep green commits, reject red commits, and save the exact diagnostic for
  the responsible worker

Do not silently rewrite a worker's intended spec change beyond conflict
resolution. If the design is wrong, report it.

## Conflict Policy

- Two branches adding different members to the same coherent new module:
  merge the member union if the concept remains coherent.
- Two branches removing disjoint entries from the patch stream: keep the union
  of removals.
- Two branches changing the same existing member semantics: stop and report a
  real conflict.
- Generated JS conflicts should be resolved by the canonical regen command,
  not by hand-editing generated output.

## Report

Return:

- final base SHA
- per branch: landed, failed, or skipped
- conflicts resolved and policy used
- exact gate diagnostics for failed commits
- follow-up assignments for workers, intake, or architect
