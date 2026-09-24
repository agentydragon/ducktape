# Plan: one selector engine

Every command resolves selectors with one resolve (`selector_resolve.rs`), one
program over every chunk, with the semantics of <../SPEC.md>. Templates cannot
yet name other entities; this plan adds that, then the tooling it enables.

## Goal

What <../SPEC.md> does not state yet:

- **Free identifiers.** A free identifier in a template means exactly one of: a
  hole keyword; a reference to the spec entity of that name (same module first,
  else a unique name across the spec; a name exported by several modules is an
  authoring error); the unshadowed global of that name; or, otherwise, an
  alpha-renamed wildcard.
- **Reference tables.** A template reference becomes a table constraint linking
  each candidate of the template's entity to the binding the referenced entity
  takes at that position, in the same joint solve as `all_different` and the
  relational selectors.
- **`resolved_by: own_references`.** An entity unique only through the places
  its template's references take is resolved by its references, distinct from
  `own_selector` and `elimination`.

## Steps

Each step lands with the command-level e2e tests that pin its behaviour. An
internal test that encodes a rule gets a command-level equivalent before the
code it tests is deleted. Consumer specs migrate in lockstep, gated by their
generated-output diff tests. Each step moves the part of the goal it achieves
into <../SPEC.md>.

1. **Template references.** Entity names in templates become reference-table
   constraints. `validate` lists each template's free identifiers by kind.
   Acceptance: the "Stable identifiers are only local" repro of
   <../SELECTOR_BUGS.md> — a `Widget` class selector plus
   `const defaultWidget = new Widget(ANYTHING);` — through `debundle run`
   resolves `defaultWidget` to the instance of `Widget` in a chunk that
   constructs two classes, and is `no_match` in a chunk that constructs only
   the other class.
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

- **With step 1:** the "Stable identifiers are only local" entry of
  <../SELECTOR_BUGS.md>, and R5 of <relational_selectors.md>.
- **This plan**, when its last step lands.
