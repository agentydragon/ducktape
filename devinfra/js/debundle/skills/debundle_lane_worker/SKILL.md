---
name: debundle_lane_worker
description: Apply one scoped debundle extraction, source-unit assignment refinement, or reorganization task in a worktree. Use for reviewing source and atomic coverage, choosing module boundaries, applying validated spec edits, running adapter gates and regeneration, and committing one reviewable worker branch.
---

# Debundle Lane Worker

If debundle tooling blocks the task, follow
`references/docs/bug_reproductions.md` before working around it.

Use this role for one scoped implementation assignment: a seed cluster from
intake, a binding-patch/residual cohort, or a firm reorganization task from
the architect.

Shared CLI workflows land here for binding moves, renames, module merges, and
atom-split recovery:

@references/docs/cli.md
@references/docs/selectors.md
@references/docs/spec_editing.md

Read other bundled references as needed:

- `references/workflow.md` for role boundaries and failure routing
- `references/README.md` for the crate overview
- `references/module_shape.md` for seam and layer-ownership heuristics

## Inputs

The orchestrator or project adapter provides:

- worktree root and expected base SHA
- assignment with source-unit IDs, binding IDs, proposed destination, and notes
- `<graph>`, `<modules-dir>`, `<emitted-js-root>`, and optional source root
- project conventions/taxonomy docs
- exact gate, regen, uniqueness-check, and commit expectations

## Procedure

1. Confirm the worktree is at the expected base before editing.
2. Check the assignment against the current graph with `debundle describe`
   and `debundle inspect-source --id`; if needed, scan with `debundle atoms`,
   `coverage`, or `cluster <sym>` (see the shared CLI guide above).
3. Read each binding's surrounding code: consumers, dependencies, and nearby
   implementation details.
4. Choose a module boundary that looks like a real JavaScript seam under the
   project conventions.
5. Apply the assignment using the shared guide's CLI workflows. Use
   `modules apply` for fresh residual binding proposals, `modules refine` for
   reviewed source-unit assignments of existing non-residual claims, `bindings assign`
   for focused binding moves, and `bindings rename` / `modules merge` for their
   respective edits.
   In an app-level multi-chunk spec, pass `--tree-config` to both `modules
propose` and `modules apply` so each sees only the graph chunk's mapped
   YAML roots.

   For `modules refine`, review the `{source_unit_id, module}` batch against current
   source and graph evidence. Run `--dry-run`, then apply the same reviewed
   batch; both resolve the complete scoped spec and check the full requested
   assignment. Preserve selector text and metadata through the CLI. Residual,
   unclaimed, import, and export source units are not accepted. Use `--tree-config`
   for a multi-chunk spec and include the selected chunk root in destinations.
   See `references/docs/cli.md` § Applying reviewed source-unit assignments.

   For `modules apply`, first run `--dry-run --format json` and inspect each
   `selector_reviews` selector against the intended binding and source. Review
   retained literals and property names as rebuild anchors, using
   `references/docs/selectors.md` for stability guidance. Add a reviewed
   `selectors` override when synthesis chooses a poor anchor; omit a proposal
   from the batch when it is not ready to claim. Then apply the reviewed batch
   and run the adapter's gate. A dry-run uniqueness proof covers the current
   source snapshot, not future versions.

   For selector-stabilization assignments, follow `references/docs/selectors.md`
   (the ladder and § "Bulk conversion loop"): confirm the bucket with
   `selector-debt`, draft with `synthesize-selectors` dry-run scoped to the
   assignment, minimize before `--apply`, then run `git diff --check`, the
   adapter's gate/regen command and `selector-debt` again to report the debt
   delta. Never modify the upstream/source bundle. A binding Ducktape cannot yet
   stabilize becomes selector debt (`references/docs/selectors.md` § Selector
   debt), routed back to Ducktape tooling.

6. Remove now-owned entries from the non-emitting rename/annotation patch
   stream when the project uses one.
7. Run the adapter-provided uniqueness check, gate, and regen commands.
8. Commit one reviewable branch and report the result.

## Boundary Heuristics

Use `references/module_shape.md` for seam, tiny-module, and layer-ownership
decisions. Apply the target project's conventions to the source and graph
before choosing a destination.

You may expand the batch when the assigned peel would split an atomic group or
create a worse module shape. You may also skip assigned items when their
natural owner still belongs to a larger atomic group that should move together.

## Failure Handling

- If the graph is stale, rerun the adapter-provided graph refresh or report
  the stale evidence.
- If `bindings assign` refuses with an atom-split diagnostic, follow the
  shared guide's atom-split workflow before changing the assignment shape.
- If the gate rejects via `debundle run`, read the structured cycle/report
  output (`cycles.json`, `atomic_group_conflicts.json`) first. Use the
  cut/evidence if present. For an unknown-call side-effect cycle, follow the
  shared workflow's initialization-contract guidance before changing the assignment;
  keep the source evidence and the exact asserted contract in the review.
- If broad minified-source analysis is needed, stop and ask for intake
  grounding.
- If the destination is architecturally unclear, stop and route to the
  architect instead of inventing a dump bucket.

## Report

Keep the report short:

- branch and commit hash
- gate and regen result
- destinations changed and bindings moved
- skipped proposals or units and why
- architecture or intake follow-ups discovered
