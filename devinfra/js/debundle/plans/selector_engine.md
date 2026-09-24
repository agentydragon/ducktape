# Plan: one selector engine

Every command resolves selectors with one resolve (`selector_resolve.rs`), one
program over every chunk, with the semantics of <../SPEC.md>. Templates name
other entities (<../SPEC.md> § Matching); this plan builds the tooling that
enables.

## Steps

Each step lands with the command-level e2e tests that pin its behaviour. An
internal test that encodes a rule gets a command-level equivalent before the
code it tests is deleted. Consumer specs migrate in lockstep, gated by their
generated-output diff tests. Each step states in <../SPEC.md> the behaviour it
adds.

1. **Pinning by use site.** An entity with no distinctive
   shape (a helper copy) is pinned through a template that mentions it.
   Acceptance: one of several identical decorate-helper copies is pinned
   through its call site, via a binding whose `local` is a free identifier of
   the call-site template.
2. **Bump tooling.**
   - **2a.** Failing selectors are reported next to the unclaimed code they
     would have claimed.
   - **2b.** A selector that no longer matches in its chunk but matches in
     another gets a hint naming that chunk.
   - **2c.** A cross-chunk `same_as` relation for mirrored module trees.
   - **2d.** Evidence from the previous version's spec directory: resolve the
     old spec against the old chunks, keep each entity's source identity, and
     use it to search the new chunks and propose repairs, with a residual
     report for semantic drift. This is the one home for version porting.

## Cleanup

Each item is deleted in the PR that lands the step making it removable, not in
a later sweep.

- **This plan**, when its last step lands.
