# Cross-process Stage B: why we backed out

A previous design proposed splitting the per-chunk pipeline into two
Bazel actions: **Stage A** (parse + facts + owner-graph + atomic-
units) emits a cacheable JSON artifact; **Stage B** (the materializer:
assemble partition → realizability → lower) reads it as input. Goal:
spec edits hit only Stage B; Stage A's expensive parse + analysis
gets cached.

The plan landed partially — the structural composer
(`stage_one/mod.rs::compute_stage_one_analysis`) and the on-disk
sidecars (`reports/tree/<chunk_id>/chunk_analysis/{facts,
atomic_units,manifest}.json`) shipped. **The cross-process consumer
never did, and the design is abandoned.** This note records why, so
we don't sleepwalk back into it.

## What broke it: SWC hygiene is `Globals`-local

SWC carries binding identity as `Id = (Atom, SyntaxContext)`. The
`SyntaxContext` is a `u32` index into a `Globals`-local table of
`Mark` chains. **A `SyntaxContext` value is meaningful only within
the SWC `Globals` instance that minted it.**

If Stage B runs in a separate process (or even the same process with
a fresh `Globals`), the resolver issues fresh `Mark`s. The `u32`s
that Stage A wrote into `facts.json` map to different `Mark` chains
in Stage B's `Globals` — the deserialized `Id`s don't compare equal
to anything Stage B's resolver produces.

Concretely: Stage A writes `("counter", SyntaxContext(7))` for a
top-level binding. Stage B's resolver assigns a fresh top-level
`Mark` that produces `SyntaxContext(3)` for the same conceptual
binding. The `BTreeSet<Id>` lookup `binding_owner.get(deserialized_id)`
misses.

`facts/wire.rs::IdReport`'s same-process round-trip test passes
because both ends share `Globals`. The cross-`Globals` test — parse
→ serialize → drop `Globals` → re-parse fresh → deserialize → assert
`top_level_id` matches — does not exist and (per the analysis below)
would fail.

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
graph build at `graph.rs::owner_graph` looks each one up in
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
the owner graph, then it doesn't need to deserialize Stage A's
`Id`s at all — it produces its own. But that means Stage A's
cached output is value-less to Stage B: Stage B redoes the parse
and the fact analysis. At that point Stage A's cache only proves
"did Stage A succeed", which is not why we'd build the artifact.

### Honest path: SWC hygiene-snapshot replay

The structurally correct cross-process design would serialize the
sequence of `Mark::fresh(parent)` and `apply_mark(prev, mark)` ops
Stage A performed during resolution, then replay them in Stage B's
fresh `Globals` before deserializing any `Id`. The required SWC
internals are all public (`Mark::parent()`, `SyntaxContext::outer()`,
`SyntaxContext::remove_mark()`) — no fork required — but it's a
substantial implementation, and the cache value it delivers
(~5–10s saved on gaffer-scale spec edits, dominated by parse)
doesn't justify it.

## What we kept

The composer pattern survived: `compute_stage_one_analysis` is a
clean function that runs parse → facts → owner-graph → atomic-units
behind one named call. Its value is **structural readability** —
the materializer reads more cleanly when Stage A is a single
function call rather than four inline stages — not cross-process
caching.

## Pointers

- `WIRE_FORMAT.md` — the live `Atom`-only convention.
- `stage_one/mod.rs` — the composer that survived.
- Git history: the abandoned design lived in `PIPELINE_SPLIT.md`
  pre-deletion (`git log -- devinfra/js/debundle/PIPELINE_SPLIT.md`).
