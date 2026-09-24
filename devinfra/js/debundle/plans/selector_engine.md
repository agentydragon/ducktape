# Plan: one selector engine

Every command resolves selectors with one resolve (`selector_resolve.rs`), one
program over every chunk. Templates cannot name other entities.

## Goal

One resolve function that every command calls, with one declared semantics:

- **Matching** is the shape matcher's job, in our code: tree matching with holes,
  index-pruned, returning every place a selector matches. Nothing else decides a
  match. Native lowering of `source_match` into solver constraints is deleted.
- **Assignment** is a CSP over opaque ids, solved by CP-SAT. Each entity is a
  variable whose domain is its matcher candidates (`(chunk, owner)` ids). Table
  constraints link a candidate to the binding at each position where its
  template names another entity, and relational selectors contribute tables of
  the same shape. `all_different` over claimed places is part of the semantics.
  No syntax tree is sent to the solver.
- **The answer is the unique solution.** Each entity comes out as exactly one
  of: `resolved` (with `resolved_by`), `no_match`, `ambiguous` (at most N
  candidates listed), `conflict` (with the other selectors involved), or
  `too_broad`.

A free identifier in a template means exactly one of: a hole keyword; a
reference to the spec entity of that name (same module first, else a unique
name across the spec; a name exported by several modules is an authoring
error); the unshadowed global of that name; or, otherwise, an alpha-renamed
wildcard.

## Steps

Each step lands with the
command-level e2e tests that pin its behaviour. An internal test that encodes a
rule gets a command-level equivalent before the code it tests is deleted.
Consumer specs migrate in lockstep, gated by their generated-output diff
tests. Each step moves the part of the goal it achieves into <../SPEC.md>.

1. **Template references.** Entity names in templates become constraints.
   `validate` lists each template's free identifiers by kind.
2. **Pinning by use site.** An entity with no distinctive shape (a helper copy)
   is pinned through a template that mentions it.
3. **Bump tooling.** Failing selectors reported next to unclaimed code;
   evidence from the previous version's spec directory; a hint when a selector
   no longer matches in its chunk but matches in another; a cross-chunk
   `same_as` relation for mirrored module trees.

## Cleanup

Each item is deleted in the PR that lands the step making it removable, not in
a later sweep.

- **As fixes land:** the matching `SELECTOR_BUGS.md` entries.
- **This plan**, when its last step lands.
