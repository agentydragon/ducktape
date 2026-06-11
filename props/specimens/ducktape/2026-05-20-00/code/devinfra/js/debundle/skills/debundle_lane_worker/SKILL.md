---
name: debundle_lane_worker
description: Apply one scoped debundle peel or reorganization assignment in a worktree. Use for confirming graph peelability, reading binding context, choosing honest module boundaries, editing debundle YAML, running the adapter-provided gate and regen commands, and committing one reviewable worker branch.
---

# Debundle Lane Worker

Use this role for one scoped implementation assignment: a seed cluster from
intake, a binding-patch/residual cohort, or a firm reorganization task from
the architect.

Read bundled references as needed:

- `references/workflow.md` for role boundaries and failure routing
- `references/debundle_user_guide.md` for graph/source/gate evidence conventions
- `references/module_shape.md` for seam and layer-ownership heuristics

## Inputs

The orchestrator or project adapter provides:

- worktree root and expected base SHA
- assignment with owner IDs, binding IDs, proposed destination, and notes
- `<graph>`, `<modules-dir>`, `<emitted-js-root>`, and optional source root
- project conventions/taxonomy docs
- exact gate, regen, uniqueness-check, and commit expectations

## Procedure

1. Confirm the worktree is at the expected base before editing.
2. Check the assignment against the current graph with `debundle_plan_work`
   commands such as `explain` and `source-slice`.
3. Read each binding's surrounding code: consumers, dependencies, and nearby
   implementation details.
4. Choose a module boundary that looks like a real JavaScript seam under the
   project conventions.
5. Edit only the debundle spec and required generated output paths. Do not
   modify the upstream/source bundle.
6. Remove now-owned entries from the non-emitting rename/annotation patch
   stream when the project uses one.
7. Run the adapter-provided uniqueness check, gate, and regen commands.
8. Commit one reviewable branch and report the result.

## Boundary Heuristics

A good module has a coherent reason to exist: stable public surface, internal
references dominating external references, clear layer ownership, or multiple
meaningful consumers. Member count alone is not the rule.

Usually avoid standalone modules for:

- primitive constants with one consumer
- one-line helpers with one consumer
- enum-like values that only parametrize a larger owner
- local style/config/data artifacts with no public contract

Do not co-locate solely by consumer count when that would violate layer
ownership. Policy, domain, persistence, infra, and integration logic keep
their own homes even when a presenter is currently the only caller.

You may expand the batch when the assigned peel would create a worse module
shape without a companion. You may also skip assigned items when their natural
owner is not peelable yet.

## Failure Handling

- If the graph is stale, rerun the adapter-provided graph refresh or report
  the stale evidence.
- If the gate rejects the batch, read the structured cycle/report output
  first. Use the cut/evidence if present; do not blindly bisect before reading
  the report.
- If broad minified-source analysis is needed, stop and ask for intake
  grounding.
- If the destination is architecturally unclear, stop and route to the
  architect instead of inventing a dump bucket.

## Report

Keep the report short:

- branch and commit hash
- gate and regen result
- destinations changed and bindings moved
- skipped candidates and why
- architecture or intake follow-ups discovered
