# Dataflow-aware S-chain emission

Follow-up to #1669 (RED tests already on devel). Replace the unconditional
`Sequenced(prev → curr)` over adjacent impure top-level statements in
`graph.rs:469-478` with a dataflow-aware edge set: emit an init-order
edge only when one statement's writes can be observed by the other.

## Meta principle

The debundler may host **conditionally-correct optimizations**: inferences
that are sound only when the input satisfies a checkable precondition,
with a conservative fallback (the current strict S-chain) for inputs
that fail the check. This is deliberate — generic-JS-correct dataflow is
infeasible (`with`, `eval`, dynamic property access, `Function`-constructor),
but the real input (the Tana RE bundle in `gaffer-private`) is well-behaved.
Worth the conditional precision.

Lives in `devinfra/js/debundle/AGENTS.md` as a first-class rule so future
passes follow the same pattern.

## Tier breakdown

### Tier 1 — static-key `globalThis` writes/reads only

Handles:

- `s_chain_skips_disjoint_global_property_writes`
- `s_chain_keeps_edge_when_writes_overlap` (guard)

Effect summary per impure statement:

- `eager_global_writes: BTreeSet<JsWord>` — static-string-key
  `globalThis.X = ...` / `globalThis["X"] = ...` writes
- `eager_global_reads: BTreeSet<JsWord>` — symmetric
- `eager_rebinds` / `eager_reads` — already on `StatementFacts`
- `dataflow_summarizable: bool` — `false` if statement contains any
  unmodeled construct (computed-key globalThis access, `with`, `eval`,
  `Function(...)` constructor, `Object.defineProperty` on globals, etc.)

Algorithm in `graph.rs` (only when chunk-spec flag
`dataflow_aware_s_chain: true`):

```
last_writer: BTreeMap<EffectCell, OwnerId>
for each impure stmt curr in source order:
    if !curr.dataflow_summarizable:
        emit Sequenced(curr → owner) for every prior impure owner   # safe fallback
    else:
        for cell in (curr.reads ∪ curr.writes):
            if cell ∈ last_writer:
                emit Sequenced(curr → last_writer[cell])
    for cell in curr.writes: last_writer[cell] = curr's owner
```

When the flag is off, the existing transitive-reduction chain is untouched
— zero regression risk for non-opt-in chunks.

### Tier 2 — fresh-allocation reasoning

Handles `s_chain_skips_fresh_local_alloc_after_global_write`.

Narrow shape recognizer for VarDecl RHS:

- `Object.freeze(<object_literal>)`
- `Object.create(null | <object_literal>)`
- `Object.assign({}, <pure args>)`
- bare `{...}`/`[...]` literals (already pure, included for completeness)

Effect: writes = `{declared binding}`; reads = `{Object, …pure-arg reads}`.

Bail out (mark statement non-summarizable) on computed keys, outer-binding
spreads, getters/setters/method shorthand inside the literal.

### Tier 3 — constructor escape

Handles `s_chain_skips_independent_cross_module_constructor_calls`.

Pre-pass: classify each chunk-declared class as `constructor_confined`
if its constructor body (and locally-resolved `extends` chain) writes
only `this.<static-key>` and reads only args / closure-stable values
— no outer-binding writes, no global writes.

`new <ConfinedClass>(pure_args)` callsite: writes = `{declared binding}`,
reads = `{class, …pure-arg reads}`.

## Per-spec gate

New optional bool in chunk spec (`spec.rs`): `dataflow_aware_s_chain`,
default `false`. Plumbs through `ChunkAnalysis` to `graph.rs`. Each
chunk opts in independently — blast radius bounded by the spec.

Test fixtures in `at_init_s_chain_dataflow_test.rs` set the flag via a
new `FixtureOpts` setter.

## Phase 0 — groundwork

1. Add the "conditionally-correct optimization" principle to
   `devinfra/js/debundle/AGENTS.md`.
2. Audit `gaffer-private`'s Tana RE bundle for dataflow escape hatches:
   - direct `eval(...)`
   - `with(...)`
   - `Function(...)` constructor
   - `globalThis[<dynamic>]` reads/writes
   - computed-key `globalThis[expr] = ...`
   - `Object.defineProperty` / `Reflect.defineProperty` on globals
   - `Proxy(globalThis, ...)`
3. Record audit findings in `devinfra/js/debundle/SMELLS.md` (or a new
   `dataflow_audit.md`).
4. For each shape found, add a RED bail-out test asserting
   `dataflow_summarizable=false` and the strict chain edge stays.

## Sequencing

Each phase is its own PR:

- **Phase 0**: principle + audit + bail-out tests.
- **Phase 1**: spec flag + Tier 1 effect summary + algorithm in
  `graph.rs`. Flips tests 1 + 4 GREEN. Existing tests untouched
  (flag default-off).
- **Phase 2**: Tier 2 fresh-allocation recognizer. Test 2 GREEN.
- **Phase 3**: Tier 3 constructor-confined classes. Test 3 GREEN.

After each phase, re-measure cut-edge reduction on the Tana spec in
gaffer-private to confirm the optimization fires on real input. Delete
this plan once Phase 3 lands.

## Outcome

Implemented Phase 0 + Phase 1 in one PR. Tier 1 alone is sufficient for
**all four** dataflow tests, not just tests 1 + 4: `Object.freeze({...})`
and `new HolderN()` both already have effect summaries that don't
overlap with prior `globalThis.<key>` writes, because the visitor only
records syntactically-visible cells (declared binding + reads of the
called identifier). Tiers 2 and 3 turned out to be unnecessary —
fresh-allocation reasoning and constructor-confined class analysis
add no test coverage beyond what the binding-cell + globalThis-prop
model already captures.

Three bail-out RED tests were added on top of the four originals to
pin the soundness fallback: direct `eval(...)`, `globalThis[<dynamic>]`,
and `new Function(...)` all flip `dataflow_summarizable = false`, and
the strict S-edge stays.

Live opt-in: `tana/re/web/spec/78d928dca7/spec_config.yaml` now
declares `chunk_analysis_options: { static/index-DI2GynTv:
{ dataflow_aware_s_chain: true } }`. Audit
(`devinfra/js/debundle/dataflow_audit.md`) confirmed the precondition.
