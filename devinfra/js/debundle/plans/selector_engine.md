# Plan: one selector engine

Selector resolution today runs through two matching engines, and commands mix
them. `run` asks the shape matcher (`ChunkResolver`) for candidates and falls
back to native AST lowering when the matcher finds none. `spec match-selector`
and the `synthesize-selectors` uniqueness proof use native lowering only.
`spec validate --source-file` uses the matcher per selector, with no joint
solve. The same selector and chunk can therefore get different answers from
different commands (<../SELECTOR_BUGS.md> lists the reproduced cases).

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
  No syntax tree is sent to the solver. An entity with one candidate is a
  constant; only groups of entities that interact become solver requests.
- **The answer is the unique solution.** Each entity comes out as exactly one
  of: `resolved` (with `resolved_by`), `no_match`, `ambiguous` (at most N
  candidates listed), `conflict` (with the other selectors involved), or
  `too_broad`.
- **One program covers all chunks.** A module tree keeps its chunk scope as a
  domain restriction, and several trees may scope to one chunk. Chunk analysis,
  the realizability gate and lowering stay per chunk.

A free identifier in a template means exactly one of: a hole keyword; a
reference to the spec entity of that name (same module first, else a unique
name across the spec; a name exported by several modules is an authoring
error); the unshadowed global of that name; or, otherwise, an alpha-renamed
wildcard.

## Steps

Fixes to what exists come first; features follow. Each step lands with the
command-level e2e tests that pin its behaviour. An internal test that encodes a
rule gets a command-level equivalent before the code it tests is deleted.
Consumer specs migrate in lockstep, gated by their generated-output diff
tests.

1. **Semantics doc.** Write the goal above as `SPEC.md` for selector
   resolution.
2. **Every command agrees.** A command-level test runs one fixture through each
   command that resolves selectors and requires the same answer. Teach the
   matcher block, `switch` and named-function-expression identifier scopes, which
   today only native lowering has.
3. **`const ANYTHING = <expr>` holes only the name.** Today the hole swallows the
   whole declarator, initializer included.
4. **Pin current behaviour worth keeping.** Every `alpha_all` scoping rule that
   only native lowering's internal tests pin gets a command-level agreement case.
   Injectivity forcing and chained relational selectors are already covered end
   to end (`e2e/global_selector_assignment_test.rs`,
   `e2e/intrinsic_alias_lowering_test.rs`).
5. **One engine.** Delete native `source_match` lowering and `FactDomains`.
   `anonymous_resolution.rs` (edit gate, `peel`) and `validate --source-file`
   call the shared resolve.
6. **Truthful outcomes.**
   - Localize contradictions with one assumption literal per selector, so an
     unsatisfiable program names the conflicting selectors instead of marking
     every target `NoMatch`.
   - Cap candidates per entity (after already-fixed references narrow them);
     over the cap is `too_broad`, rejected without a solve.
   - `resolved_by: elimination` warning: after the solve, filter the entity's
     candidates by its own reference and relation tables with every other
     entity held at its solved value; more than one survivor means it is unique
     only because others claimed its alternatives. Report the claimers.
   - Fast-fail stops at the first failed entity; keep-going reports every
     outcome and continues with failed entities unclaimed.
7. **One program across chunks**, with per-tree chunk scope and several trees
   per chunk.

Features, after the steps above:

8. **Template references.** Entity names in templates become constraints.
   `validate` lists each template's free identifiers by kind.
9. **Pinning by use site.** An entity with no distinctive shape (a helper copy)
   is pinned through a template that mentions it.
10. **Bump tooling.** Failing selectors reported next to unclaimed code;
    evidence from the previous version's spec directory; a hint when a selector
    no longer matches in its chunk but matches in another; a cross-chunk
    `same_as` relation for mirrored module trees.

## Cleanup

Each item is deleted in the PR that lands the step making it removable, not in
a later sweep.

- **Step 5 (one engine):**
  - the AST selector atoms (`selector_ir.rs`) and their encoding in
    `selector_constraint_model_builder.rs`, keeping only what candidate,
    reference and relation tables need;
  - the internal tests of deleted code, once their rules have command-level
    equivalents;
  - `docs/selector_resolution.md`, rewritten for the new engine. Its measured
    rejection of encoding tree matching as solver constraints stays as a short
    decision record citing
    `debug/perf/2026_09_17_matcher_vs_native_lowering.md`.
- **Step 6 (truthful outcomes):**
  - the decoder mapping an unsatisfiable or unknown solve to an outcome for
    every target;
  - the deprecated no-op `--keep-going` flag.
- **Step 7 (one program across chunks):**
  - per-chunk CP-SAT request and summary files, the `selector_problem` output
    group in `pipeline.bzl`, and the
    `DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_{REQUEST_PROTO,SUMMARY_JSON,DUMP_ONLY}`
    wiring, replaced by one request per interacting group;
  - the README's Bazel section describing them;
  - downstream: chunks aliased twice so two module trees can share them.
- **As fixes land:** the matching `SELECTOR_BUGS.md` entries.
- **The `selector_solve` question:** if it is not the relation evaluator, the
  prototype, its tests, and step 1 of "Landing a new relation" in
  `relational_selectors.md` go with step 5.
- **This plan**, when its last step lands.

## Open questions

- Whether the `selector_solve` Datalog prototype becomes the relation evaluator
  or is deleted with the rest of the second engine.
- The default candidate cap and ambiguity listing size N.
