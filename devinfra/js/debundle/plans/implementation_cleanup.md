# Debundle implementation cleanup

Burn down the remaining naming and responsibility cleanup after the phase-boundary
refactor. Remove completed entries as their PRs merge; delete this ledger only
after the final audit meets the completion criteria. Durable contracts belong in
the spec, design docs, or code. Documents outside this plan must not link here.

## Constraints

- Preserve the exact one-program/one-spec contract, source order, SWC hygiene,
  analysis results, acceptance, and generated JavaScript.
- Use isolated worktrees and independently reviewable PRs. Keep open PRs rebased
  on `devel`, mergeable, and green in CI. The user owns merges.
- Update every caller of changed interfaces, including Gaffer readers and
  packaged skills. No compatibility aliases or transitional shims.
- Split by responsibility without adding frameworks, duplicate analysis,
  indexes, or a new intermediate representation. Keep shared facts shared.
- Keep semantic changes separate. Compare downstream controls before stricter
  acceptance; allow at most two hours of focused repair, stop sooner for a deep
  redesign, and defer with a reproduction and impact when necessary. Preserve
  the working downstream pin until the candidate validates.

## Remaining work

### 3. Finish terminology adoption

PR #12129 carries the remaining report/namespace cutover and current-schema skill
recipes.

- Distinguish the authored residual namespace, the unassigned destination policy,
  and the physical entry destination. Trace readers before deciding the wire
  representation; preserve semantics through any paired public/private cutover.
- Audit old graph/source-unit vocabulary in live code, docs, BUILD interfaces,
  and skills. Preserve legitimate distinct concepts rather than replacing words
  mechanically.

### 4. Separate responsibility clusters

PRs #12131 and #12130 carry the execution-analysis and artifact splits.

- `facts/sync_effects.rs`: separate abstract-value representation, execution
  indexing/collection, and interpretation. Keep their algorithms and analysis
  ownership intact; expose narrow internal interfaces rather than blanket
  visibility or sibling glob imports.
- `artifacts/artifact.rs`: move metrics and report construction out of the
  program containers. Update live imports, BUILD declarations, docs, and callers
  in the same change; do not retain convenience re-exports.

### 5. Validate and audit completion

- Let each PR run its affected CI; use focused Bazel builds/tests for iteration
  and uncertainty. Check named Rust libraries so lint aspects run too.
- Run the public Debundle suite and packaged skill checks on the combined head.
- Compare generated JavaScript against the working downstream control, run normal
  regeneration, and retain the established browser/runtime acceptance controls.
- Record rejected and unverified registered corpus controls separately. Preserve
  existing structural metric findings, rules, exemptions, and baselines.
- Audit the final tree for redundant wrappers, terminology drift, misplaced
  responsibilities, stale docs, and transitional interfaces. Resolve findings
  within this scope before declaring completion.

## Completion criteria

Every item above has landed or has an explicit bounded deferral with evidence.
Current phase names agree across actual callers and downstream readers. Small
helpers carry useful behavior or invariants. Execution analysis and artifact
metrics have clear owners. Combined validation preserves acceptance and generated
JavaScript, with known failures and unverified cases stated accurately. Every
remaining open PR is mergeable, and hosted CI limitations are reported separately
from direct Bazel results.
