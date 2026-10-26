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

### 5. Validate and audit completion

Public implementation PRs #12125, #12127, #12129, #12130, and #12131 have
landed with their required CI green. Combined public suite, optimized build, and
JavaScript-equivalence evidence live in `perf/large_web_corpus_baseline.md`.

- Finish Gaffer regeneration and the established browser/runtime controls against
  the final merged engine pin, publish the paired pin update, and track its merge.
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
