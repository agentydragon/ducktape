# Cross-process analysis reuse: SWC hygiene

A first design split the per-chunk pipeline into two Bazel actions: **Stage A**
(parse, facts, dependency graph, atomic units) wrote a JSON artifact that
**Stage B** (assignment, realizability, lowering) read. It was abandoned over
the problem below. Cross-process reuse now exists: `debundle analyze` writes an
analysis artifact that `debundle run --analysis` reads (docs/design.md §
Pipeline split), encoded by `analysis_codec`. This note records why the
obvious encodings are wrong, so the codec is not "simplified" back into one.

## What broke it: SWC hygiene is `Globals`-local

SWC carries binding identity as `Id = (Atom, SyntaxContext)`. The
`SyntaxContext` is a `u32` index into a `Globals`-local table of
`Mark` chains. **A `SyntaxContext` value is meaningful only within
the SWC `Globals` instance that minted it.**

If Stage B runs in a separate process (or even the same process with
a fresh `Globals`), the resolver issues fresh `Mark`s. The `u32`s
that Stage A would write into a facts sidecar map to different
`Mark` chains in Stage B's `Globals` — the deserialized `Id`s don't
compare equal to anything Stage B's resolver produces.

Concretely: Stage A writes `("counter", SyntaxContext(7))` for a
top-level binding. Stage B's resolver assigns a fresh top-level
`Mark` that produces `SyntaxContext(3)` for the same conceptual
binding. The `BTreeSet<Id>` lookup `binding_owner.get(deserialized_id)`
misses.

A same-process `Id` round trip would pass because both ends share
`Globals`. The cross-`Globals` round trip — parse → serialize → drop
`Globals` → re-parse fresh → deserialize → assert `top_level_id`
matches — fails with raw `u32` contexts. `analysis_codec.rs` now writes
contexts as first-occurrence ordinals, which that round trip
(`lowering/analysis_artifact.rs` tests) passes; the staged pipeline plan
revisits the rest of this note.

## Rejected alternatives

We considered each of these. None worked.

### Drop `ctxt` from the wire, reconstruct via `top_level_id(name, fresh_mark)`

**Unsound under shadowing.** `StatementFacts` is pre-filter raw
analyzer output and includes inner-scope reads from nested function
bodies (the `StatementFactsCollector` visitor descends through
`lazy_visit_function` and friends). Closure-local declarations carry
inner-scope marks, not the chunk's `top_level_mark`.

```js
const counter = 0; // top-level binding (top_level_ctxt)
function increment() {
  let counter = 1; // inner binding (inner_ctxt)
  return counter; // lazy_read records (counter, inner_ctxt)
}
```

`StatementFactsCollector` records `lazy_reads = {("counter",
inner_ctxt)}` for the `increment` statement. The downstream owner-
graph build (`graph/build.rs::build_dependency_graph_with`) looks each one up in
`binding_owner`. `("counter", inner_ctxt)` misses (only
`("counter", top_level_ctxt)` is keyed), and no edge is emitted —
the closure's shadow doesn't pollute the realizability graph. This
is correct: closure-local `counter` and top-level `counter` are
unrelated.

If the wire format dropped the ctxt: deserialize `name="counter"`,
reconstruct as `top_level_id("counter", fresh_mark)`, hit the
top-level binding → spurious `LazyUse` edge. The realizability gate
sees at-init / lazy edges that aren't actually there. The bundler
rejects or mis-orders chunks.

We cannot statically prove "no closure ever shadows a top-level
name", so the simple approach is **structurally unsound**.

### Pre-filter `facts.json` (drop inner-scope `Id`s before serialization)

Keeps the Atom-only wire convention pure (every other JSON file in
the debundle report set carries `Atom`s, not `Id`s) but loses the
closure-local read records humans want when inspecting the artifact
during debugging. Not worth the loss when no separate-process
consumer needs the change.

### Rely on SWC resolver determinism: Stage B re-parses

If Stage B re-parses the chunk, runs `analyze_chunk`, and rebuilds
the dependency graph, then it doesn't need to deserialize Stage A's
`Id`s at all — it produces its own. But that means Stage A's
cached output is value-less to Stage B: Stage B redoes the parse
and the fact analysis. At that point Stage A's cache only proves
"did Stage A succeed", which is not why we'd build the artifact.

### What works: re-parse, then map context ordinals

Replaying the `Mark` operations Stage A performed is unnecessary. The
resolver's scope structure is deterministic for given source text, so the
reader re-parses the same modules (which it does anyway to lower them) and
numbers every context by its first occurrence in a pre-order walk of those
modules. The writer writes each `SyntaxContext` as that ordinal; the reader
maps ordinals back to its own contexts. Inner scopes keep their own ordinal, so
the shadowing hazard above does not arise. Facts are not recomputed, unlike the
re-parse alternative. `analysis_codec` implements this; its
`decodes_into_a_fresh_globals` test is the cross-`Globals` round trip.

## Pointers

- `../wire_format.md` — the `Atom`-only convention of JSON reports.
- `analysis_codec.rs`, `lowering/analysis_artifact.rs` — the artifact encoding.
- Git history: the abandoned design lived in `PIPELINE_SPLIT.md`
  pre-deletion (`git log -- devinfra/js/debundle/PIPELINE_SPLIT.md`).
