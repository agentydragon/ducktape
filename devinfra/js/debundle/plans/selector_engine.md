# Plan: one selector engine

Every command resolves selectors with one resolve (`selector_resolve.rs`), one
program over every chunk, with the semantics of <../SPEC.md>. Templates name
other entities (<../SPEC.md> § Matching); this plan finishes that, then builds
the tooling it enables.

## Goal

What <../SPEC.md> does not state yet:

- **References everywhere.** Anonymous-statement templates take references and
  globals like `source_match` members, and a reference to an entity pinned by a
  relational selector constrains through that entity's variables instead of
  alpha-renaming.

## Steps

Each step lands with the command-level e2e tests that pin its behaviour. An
internal test that encodes a rule gets a command-level equivalent before the
code it tests is deleted. Consumer specs migrate in lockstep, gated by their
generated-output diff tests. Each step moves the part of the goal it achieves
into <../SPEC.md>.

1. **Template references.** Anonymous statements take references and globals;
   references to relational entities constrain through their variables.
2. **Pinning by use site.** Depends on step 1. An entity with no distinctive
   shape (a helper copy) is pinned through a template that mentions it.
   Acceptance: one of several identical decorate-helper copies is pinned
   through its call site, via a binding whose `local` is a free identifier of
   the call-site template.
3. **Bump tooling.**
   - **3a.** Failing selectors are reported next to the unclaimed code they
     would have claimed.
   - **3b.** A selector that no longer matches in its chunk but matches in
     another gets a hint naming that chunk.
   - **3c.** A cross-chunk `same_as` relation for mirrored module trees.
   - **3d.** Evidence from the previous version's spec directory: resolve the
     old spec against the old chunks, keep each entity's source identity, and
     use it to search the new chunks and propose repairs, with a residual
     report for semantic drift. This is the one home for version porting.

## Cleanup

Each item is deleted in the PR that lands the step making it removable, not in
a later sweep.

- **This plan**, when its last step lands.
