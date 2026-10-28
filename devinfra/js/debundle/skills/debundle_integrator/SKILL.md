---
name: debundle_integrator
description: Integrate multiple debundle lane-worker branches through a validated merge train. Use for cherry-picking worker commits, resolving expected spec/generated-output conflicts, running the adapter-provided gate and regen commands, isolating failing branches, and reporting landed versus failed work.
---

# Debundle Integrator

If debundle tooling blocks the task, follow
`references/docs/bug_reproductions.md` before working around it.

Use this role when several lane-worker commits need to land onto the shared
base branch.

Shared CLI workflows land here for gate and `--dry-run` behavior:

@references/docs/cli.md
@references/docs/spec_editing.md

Read other bundled references as needed:

- `references/workflow.md` for orchestration and failure routing
- `references/README.md` for the crate overview

## Inputs

The orchestrator provides:

- isolated integration state, receiving SHA, and its graph/source/spec/engine
  identity
- each worker's starting snapshot manifest and pre-edit verification result,
  relative YAML patch, and exact validation records/results
- exact gate and regen commands
- expected conflict policy for module YAML, patch streams, and generated JS
- whether pushing is allowed

## Optimistic Train

When workers reported green gates and assignments are mostly disjoint:

1. Before applying any patch, verify that the receiving graph, source, spec
   inputs and engine match every worker's starting snapshot identity. If any
   differ, reject that worker result, rebase or redispatch the assignment on a
   new snapshot, and require the worker to rerun its checks.
2. Apply the reviewed YAML patches in order to the isolated integration state.
   A worker commit may transport the patch, but is not evidence that these
   receiving inputs passed its checks.
3. Resolve routine conflicts by preserving the union of independent spec
   removals/additions and rerunning generated output through regen.
4. Run the combined pipeline, gate, and applicable runtime checks, recording
   each result separately. Individual worker checks do not prove the combined
   result.
5. If green, commit the integrated result if needed, and stop.

Push only when the orchestrator explicitly requested it.

## Fallback

If the combined gate fails:

- restore the pre-train integration state
- apply worker patches individually or by halves
- rerun the same combined pipeline, gate, and applicable runtime checks to
  isolate incompatible patches
- keep accepted patches, reject red patches, and save the exact diagnostic for
  the responsible worker

Do not use `snapshot verify` on an edited worker snapshot as an acceptance
check; it is the starting-baseline check and is expected to fail after edits.
For actual command proof, retain the resolved invocation and its completed
result, as described in the CLI guide's "Resolved invocation records" section.

Do not silently rewrite a worker's intended spec change beyond conflict
resolution. If the design is wrong, report it.

## Conflict Policy

- Two branches adding different members to the same coherent new module:
  merge the member union if the concept remains coherent.
- Two branches removing disjoint entries from the patch stream: keep the union
  of removals.
- Two branches changing the same existing member semantics: stop and report a
  real conflict.
- One branch renaming an export while another adds a template naming it: a
  semantic conflict git does not show, since templates refer to entities by
  export name. Run source-only `spec validate` on the merged spec and fix the
  template on the renaming side.
- Generated JS conflicts should be resolved by the canonical regen command,
  not by hand-editing generated output.

## Report

Return:

- final base SHA
- per branch: landed, failed, or skipped
- conflicts resolved and policy used
- exact gate diagnostics for failed commits
- follow-up assignments for workers, intake, or architect
