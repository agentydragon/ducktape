# Translation-validation gate

Replace the realizability gate's "no cycle among materialized constraining
edges" (Pass 1) plus separate ESM simulation (Pass 2) with translation
validation of the candidate emit:

1. compute the order the emitted modules actually evaluate statements in;
2. find the statement pairs whose relative order differs from source order;
3. accept iff every such pair commutes, judged from the interpreter's effect
   facts, with TDZ as an ordinary effect conflict.

Neither pass survives as a verdict. Cycle detection becomes at most a hint to
the order search. The ESM loader model becomes a plain, exact component: it
evaluates the emitted import lists as ECMA-262 does, cyclic or not, and it
always runs on the real emitted order, never on a fallback order. Today Pass 2
runs on a `ModuleId` order whenever Pass 1 is cyclic, which manufactured
Tana's 1,800-module SCC (`debug/2026_10_31_tana_scc_pessimism.md`).

The architecture this serves is <target_architecture.md>. This plan burns
down as its steps land.

## Problem

The gate materializes an ordering edge for every statement pair the analysis
cannot prove independent, then rejects any module-level cycle among them. On
Tana (gaffer-private pin `e1f145dd`, spec including gaffer #686) that is
2,118,225 dependency edges. Pass 1 fails on constraining SCCs of 963 and 2
modules. Dropping the 1,054 cross-module `cell_order` edges out of an opaque
heap frontier makes Pass 1 acyclic. The reported 1,800-module cycle is an
all-edge SCC that Pass 2 judged on the degraded order Pass 1 left behind.

Almost none of those edges concern a pair the emit reorders
([§ Measurements](#measurements)): under a realizable candidate order, 2,640
statement pairs that carry an edge swap, against 2.1 million edges. The
precision budget is spent on pairs that never move, and the conservative
encoding of opaque heap effects (frontier barriers chained through the
statement list) creates cycles that no single reordered pair would.

## Semantics

**Programs compared.** The source chunk evaluates its top-level statements in
source order. The emitted bundle evaluates the same statements, each in
exactly one module, in the order ESM evaluation reaches them: module bodies in
DFS post-order of the import graph from the entry, each body in source order
(design.md § Lemma 2). Import and export declarations only link bindings.
Function declarations are initialized at instantiation in both programs.

**Swapped pair.** Statements `u`, `v` with `u` before `v` in source order and
`v` evaluated before `u` in the emitted order. Statements of one module never
swap.

**Commutation.** Each statement has an effect set over cells: `Binding(b)`
(including the declaration's initialization), `GlobalProp`, heap
`Contents(site)` and `Alloc(site)`, host-ordered queues (microtask enqueue,
host output), and `Havoc`. `u` and `v` commute when no cell is written by one
and read or written by the other, and neither has `Havoc` unless the other is
effect-free. A read of `b` that would run before `b`'s initialization is a
read of `Binding(b)` against the declaration's write, so TDZ and
read-before-`var`-initializer are the same rule, not a separate check.

**Gate.** Accept iff, under the emitted order:

- every swapped pair commutes or is covered by a `commuting_statements`
  premise;
- no rebinding write crosses a module boundary (unchanged clause 2);
- the existing entry guard holds: nothing at-init reads from entry, which
  evaluates last.

**Soundness.** The argument is by induction on emitted position. Suppose every
statement evaluated before `u` saw, on its read cells, the values it saw in
source order. For each cell `u` reads, consider its writers:

- a writer earlier in source that ran after `u` would be a swapped
  conflicting pair;
- a writer later in source that ran before `u` would be one too.

So `u`'s last writer per read cell is the same statement as in source order,
and by the hypothesis it wrote the same value. `u` therefore reads the same
values, takes the same path, and has the same effects. At the end every
cell's last writer is the same statement in both programs, so the final states
agree. Lazy code runs after evaluation over that equal state.

This needs exactly one property of the effect sets: each is a sound
over-approximation of what the statement reads and writes when it runs on the
source-order state. It needs no order-independent summary. The interpreter
computes facts per source prefix (`Prefix(n)`), and the induction shows each
statement runs on a state equal to that prefix on the cells it reads, so those
facts stay valid in the reordered program.

Top-level `await` and abrupt completion stay excluded, as today (assumptions
A2 and A6).

**Relation to today's gate.** When Pass 1 and Pass 2 accept, the emitted
order violates no constraining edge. A constraining edge is the materialized
form of "this pair may conflict", so TV, run with conflicts read off the
existing edges, accepts every spec today's gate accepts on the same order.
Conversely, a constraining module cycle forces some violated edge in every
order, so TV over edges rejects exactly what today rejects. The gain comes
only when the pair check is sharper than the edge set. TV needs that
sharpness on the swapped pairs alone, which the measurement shows are few.

That equivalence holds for edges whose dependency precedes the dependent in
the source. A **forward** ordering edge, an at-init read of a later
declaration (`EagerUse`, often promoted through a call), asks the emit to
swap the pair: the gate counts it violated when the emit keeps source order,
and satisfied when the emit swaps it. Under § Semantics the opposite holds:
kept order commutes, and a swap is a read of `Binding(b)` against its
initialization. The edge-based TV (`translation_validation.json`, checked
against the gate in every e2e test with `DEBUNDLE_TV_ORACLE`) therefore
judges forward edges as the gate does and reports both directions beside the
sweep. A forward read usually comes with a `binding_timeline` edge back from
the declaration, so the pair stays atomic and neither direction is accepted;
a forward edge without one (a premise-cleared callback read, as in
`call_registration_contract_test`) is accepted only by swapping it, which
the effect-set check must judge.

## Checking without retaining all pairs

Tana swaps 7.7 million statement pairs under a realizable order. The sweep
retains, per cell, already-run readers and writers ordered by source ordinal.
For each statement `u`:

- a write to `c` conflicts if an already-run reader or writer of `c` has a
  larger ordinal;
- a read of `c` conflicts if an already-run writer of `c` has a larger
  ordinal;
- `Havoc` against any effectful already-run statement with a larger ordinal,
  and the reverse, also conflict.

Enumerate every matching partner with a larger source ordinal. A single
maximum is insufficient with pair-specific premises: if source 1 writes c,
2 and 3 read c, and the emit runs 3,2,1, a premise for (1,3) must not hide
(1,2). Collect all cells for a candidate pair before classifying its premise;
never remove participants because another pair was covered.

Traversal is output-sensitive. Ordered suffix queries cost O(A log N), and
merging them by partner costs O(K log(A + 1)) before pair/cell aggregation,
where A is access count and K is conflict incidence. K can be quadratic.
Default reports count all pairs/categories but retain deterministic bounded
examples, with explicit truncation. Opt-in `--effect-conflicts-jsonl` streams
the full rows from the same visitor; effect-domain coverage and clean
canonical comparisons remain necessary for the full TV2 migration.

## Effect facts the pair check needs

- **Per-statement effect sets** as abstract-interpreter step 7 defines them
  (`Binding`, `GlobalProp`, `Contents`, `Alloc`, `Havoc`), plus the
  evaluation-time initialization writes (excluding instantiated functions,
  imports and bare var redeclarations). Today these live, in
  partial form, in `StatementFacts` (`reads`, `rebinds`, `heap_contents`,
  `global_reads`/`global_writes`, `synchronous_calls`).
- **Synchronous callee effects folded into the caller**, which the interpreter
  already does for binding and collection accesses. Bounded fallback reach
  is currently report-only, without a retained effect-domain closure;
  incomplete or unmodelled domains remain conservative `Havoc` until that
  closure exists (`plans/synchronous_execution_fallback.md`). Neither purity
  nor synchronous completeness certifies arbitrary property/global/host
  effect coverage.
- **Host-ordered effects**: microtask and timer enqueue, and host output.
  Swapping two `Promise.resolve().then(...)` statements reorders callbacks, so
  enqueues write one ordered queue cell. Today they are covered only by
  purity's `Havoc`. Their absence must stay conservative until they are
  modelled.
- **Nothing pairwise.** The binding-timeline and cell-order edge rules become
  projections of these sets, used only by tooling.

## Choosing the candidate order

The interpreter judges an order; it never searches. The emitted order is a
function of the import graph, which is binding imports plus the phantom
side-effect imports the emitter adds for ordering. So the search chooses
phantom imports, outside the interpreter:

1. **Seed.** Take today's Lemma 2 order, computed over the constraining edges
   (with any cycle among them condensed) minus the soft categories: heap-frontier `cell_order`, and
   `binding_timeline` edges with a `fallback` side. Hard edges are binding
   reads, rebinds and definite cell conflicts.
2. **Validate.** Run the sweep on the seed's emitted order.
3. **Refine** each conflicting swapped pair `(u, v)`:
   - if a premise or a sharper effect fact (computed for that pair only)
     clears it, accept the pair;
   - otherwise add `u → v` as a hard ordering edge (a phantom import) and go
     back to step 1.
4. **Stop** when the order validates, or when an added hard edge would close a
   module cycle. The order then cannot be steered further; the loader model
   still evaluates the order the imports give, and validation rejects with the
   remaining conflicting pairs as evidence, never with the cycle. Each
   iteration only adds edges, so the loop terminates.

Spec authors keep control of placement. The search only orders modules the
spec already defines.

## `commuting_statements`

A premise certifies that its two source units commute. Under TV it is
consulted only when that pair swaps and the sweep finds a conflict.

- The **contradiction check** stays: reject a premise whose pair has a
  definite conflict (two `observed` effects on one cell).
- **"Unused"** (`commuting_statements_diagnostics`) means today "inerts no
  edge". Under TV, judge it against the effect sets: a premise whose pair has
  no conflict is unused. It must not be judged against the order, or a
  placement edit elsewhere would turn a valid premise into an error.

## Diagnostics

A rejection names the pair, the order change and the cell:

```text
static/index-DI2GynTv: `xS` (unit 3307, module app/bootstrap/initbundle/part-203)
  now runs before unit 3290 (`Se(...)`, module features/x), which precedes it in
  the source; they conflict on Contents(alloc 812): 3290 writes, 3307 reads.
  Evaluation order: features/x is imported by entry before part-203 (import 14 of 2,790).
  Fix: co-locate the two, keep features/x after part-203, or certify the pair
  in chunk_analysis_options.static/index-DI2GynTv.commuting_statements.
```

`gate list` and `gate describe` gain a `swapped_conflict` evidence kind,
which replaces cycles and atomic-group conflicts once the flip lands. The report keeps the full set of
swapped conflicting pairs, not only the first.

## Interaction with other work

- **Abstract interpreter** (<abstract_interpreter.md>):
  - Step 7 supplies the effect sets.
  - Step 9 ("`EffectsDisjoint` and gate consumption") is this gate's pair
    check. TV needs it applied to swapped pairs only, not to every
    `Sequenced` edge.
  - Step 4c keeps purity and the interpreter in one per-chunk context. TV
    reads the settled per-statement result and adds no constraint on it.
- **Staged pipeline** (<staged_pipeline.md>):
  - TV runs in S4 on the S3 facts artifact, which must carry the effect sets.
    It already carries `StatementFacts`.
  - Commuting resolution already sits in S4.
  - The graph build shrinks: it emits only the hard edges the search seeds
    from, and the full edge set becomes a tooling view built on demand.
  - Assignment edits, the agent loop, still never touch S3.
- **Tooling** (atomic groups, `peel`, `modules propose`, `gate describe`,
  per-atomic-group `unassigned_mode`): these read `dependency_graph.json.gz` edges
  and atomic groups. They keep that data until each is ported to query
  swapped-pair conflicts. Atomic groups remain a planning heuristic, not the
  acceptance criterion.

## Migration

Each step keeps today's gate authoritative until the comparison is clean.
Step 1 has landed: TV over today's edges runs beside the gate, writes
`translation_validation.json`, and agrees with the gate on every e2e fixture
and on both canonical inputs.

2. **Effect-set conflicts.** Switch the sweep to effect sets (needs
   abstract-interpreter step 7) and report the differences against the
   edge-based verdict per category. A pair TV accepts that the gate rejects
   must have no conflicting cell. A pair TV rejects that the gate accepts is
   a soundness bug in the edge rules or a missing effect.
   - Forward edges move into the sweep here (§ Relation to today's gate).
     The gate accepts some orders only by swapping a forward edge, which
     step 1 reports as `forward_edges_swapped` outside its verdict: on
     Claude, the accepted chunks `mcp` (30) and `remoteWorkerWire` (6).
     Each needs an effect fact showing the read does not run at init, as a
     `no_reentry_call` callback's does not; otherwise TV rejects them.
3. **Search.** Seed from hard edges and add the refine loop. Report
   side by side: today's verdict, TV's verdict, and swapped pairs per
   category.
4. **Flip.** TV decides; today's gate becomes a report section for one
   release, then goes, Pass 1's cycle verdict and Pass 2's degraded-order
   fallback with it. `commuting_statements` diagnostics move to the
   effect-set definition of "unused".
   - TV needs the emitted import order, which only `AssignmentAnalysis`
     (`debundle run`) builds. Peel's `gate::validate_assignment` callers
     (`peel/propose.rs` decomposition validation) have none, so they get
     no TV verdict. Before the flip they need that order, or keep today's
     gate as their verdict.
5. **Slim.** The graph build materializes hard edges only, and the full edge
   set moves behind the tooling queries that still need it.

The comparison runs in e2e as differential tests
(`e2e/translation_validation_differential_test.rs`, plus the oracle in every
e2e test), and on the canonical inputs as a report diff.

## Measurements

### Initial report-only effect projection

`effect_translation_validation.json` is a sibling of the unchanged edge-TV
report. It projects guarded initialization writes, eager and synchronous
binding reads/rebinds, collection accesses and allocation sites from staged
facts. Unknown global/property/host coverage is separate from legacy timing
opacity and stays global Havoc. Composed heap provenance is explicitly
unclassified where the producer has no per-access evidence. Counts cover
all raw conflicting swapped pairs, existing premise dispositions and
observed contradictions; this report never rejects or drives the old oracle.

Defaults retain twenty deterministic pair examples with up to twenty cells
each, and exact pair/cell counts. The sweep merges per-cell ordered suffix
iterators and aggregates one pair's cells at a time. `examples_truncated`
and each pair's `cells_truncated` state whether details were omitted;
`--effect-conflicts-jsonl` now provides full rows through the same enumeration.
The report makes
no full semantic precision claim and does not complete step 2. In particular
the active Claude mcp/remoteWorkerWire forward reads remain reportable with
their existing pair-premise dispositions; oldgate acceptance is not proof
that their callbacks cannot execute at init.

### Original prototype measurements

The prototype is an offline script over existing `dependency_graph.json.gz`
outputs; it changes no engine code and is not committed.

It rebuilds the emitted module order as the gate's simulator does:

- module import graph from cross-module dependency edges;
- dependency-first Kahn toposort of the constraining subgraph, `ModuleId`
  tie-break;
- Lemma 2 entry-import order;
- DFS post-order, with entry last.

It then counts swapped statement pairs and classifies every edge as:

- intra-module (never swaps);
- cross-module, order kept;
- cross-module, swapped;
- violated (a constraining edge whose target evaluates after its source).

Check of the rebuild: with every edge kept it reproduces the gate's
constraining SCCs of 963 and 2 modules exactly.

**Candidate order.** Pass 1 fails on Tana, so the candidate order is
heuristic. The toposort uses the constraining subgraph minus the 1,054
cross-module `cell_order` edges out of a heap frontier, which leaves it
acyclic. Those dropped edges are then judged like any other.

**Tana** (pin `e1f145dd`, 9,709 source units, 2,781 modules):

| Quantity                                          | Count             |
| ------------------------------------------------- | ----------------- |
| Statement pairs                                   | 47,127,486        |
| Swapped pairs                                     | 7,742,456 (16.4%) |
| Swapped pairs, both impure                        | 15,543 of 410,871 |
| Swapped pairs, both with incomplete execution     | 6,079 of 339,900  |
| Dependency edges                                  | 2,118,225         |
| Intra-module                                      | 10,867            |
| Cross-module, order kept                          | 2,102,938 (99.3%) |
| Cross-module, swapped                             | 4,420 (0.21%)     |
| Distinct swapped statement / module pairs on them | 2,640 / 1,187     |
| Constraining edges violated by this order         | 3,841             |

Swapped edges by category; the second figure is how many of them are
violated constraining edges:

| Category                                         | Swapped | Violated |
| ------------------------------------------------ | ------: | -------: |
| `eager_use`                                      |   1,757 |    1,748 |
| `sequenced` `binding_timeline` fallback→observed |   1,683 |    1,683 |
| `lazy_use` (non-constraining)                    |     562 |        0 |
| `sequenced` `cell_order` `after_barrier`         |     342 |      341 |
| `sequenced` `cell_order` other frontier barriers |      35 |       35 |
| `sequenced` `binding_timeline` observed→observed |      28 |       28 |
| `sequenced` `binding_timeline` other             |      13 |        6 |
| `cell_order` definite (`read_after_write`, …)    |       0 |        0 |

What the numbers show:

- **Edge relevance.** 99.5% of the edge machinery concerns pairs this order
  never reorders.
- **Pass 2 failures.** The violated `eager_use` and fallback→observed
  `binding_timeline` edges are the Pass 2 failures on this order. They sit in
  lazy-import I-cycles, where the DFS enters a cycle at the wrong member, so
  this particular order is not acceptable either.
- **Where precision matters.** For Tana's spec, about 3,800 pairs need it.
  The heap-barrier pairs (376) and the fallback pairs (1,689) are where the
  interpreter's refinement matters. The 6,079 swapped pairs of
  incomplete-execution statements are the honest upper bound on opaque
  conflicts today's barrier chain stands for.

**Claude CLI** (gaffer-private `831c8502` spec, 76 chunks, 43,949 units, same
heuristic order):

- 120,630,553 statement pairs, of which 2,023,777 swap.
- 3,602,083 edges: 3,565,952 intra-module (most code still unassigned),
  19,425 cross-module with order kept, 16,706 cross-module swapped.
- 7,258 violated constraining edges, all in the five chunks the gate rejects:
  `binding_timeline` observed→fallback 6,955, observed→observed 300, heap
  barrier 3.
- Four of those chunks keep a small constraining SCC even without heap
  barriers: `api` 4 modules, `cli` 7, `featureFlags` 8, `toolCgroup` 2.

**Edge-based TV on the real emitted order** (the engine that landed it,
2026-10-31). Unlike the prototype, this is the order the emitter writes:
when the constraining subgraph is cyclic the emitter has no linker order and
sorts module imports by `ModuleId`, the degraded order the plan replaces.

| Input (gaffer-private #693, `988eeeab`)     | Chunks rejected by both | Swapped pairs | Conflicting edges (statement pairs, module pairs) | Forward edges kept / swapped |
| ------------------------------------------- | ----------------------: | ------------: | ------------------------------------------------- | ---------------------------: |
| Tana                                        |                  1 of 1 |    15,109,993 | 267,217 (134,302, 25,523)                         |                       6 / 15 |
| Claude CLI (75 of 76 chunks reach the gate) |                 5 of 75 |     2,024,711 | 3,968 (3,968, 18)                                 |                    0 / 4,025 |

TV and the gate agree on every chunk.

- Tana's conflicts: `eager_use` 136,262, `binding_timeline` 130,764,
  `cell_order` 191, on the `ModuleId` order left by the gate's constraining
  SCCs of 963 and 2 modules.
- Claude's conflicts, in the five rejected chunks (`api` 706, `cli` 2,102,
  `featureFlags` 1,078, `toolCgroup` 80, `sessionSDK` 2), are
  `binding_timeline` edges but for three heap-frontier `cell_order` edges.
  Each `binding_timeline` conflict sits opposite a swapped forward
  `eager_use` edge: an at-init read of a later declaration split from it. `mcp` (30)
  and `remoteWorkerWire` (6) are accepted with swapped forward edges.
- TV adds no measurable time: the runs took 1:39 (Tana) and 0:52 (Claude)
  against 1:56 and 1:01 for the same inputs on `cb9eebe9`.

## Open questions

- **Search granularity.** Do phantom imports between whole modules give the
  search enough freedom, or do some conflicts need a statement moved to
  another module (a spec edit, out of scope)? Step 3's report will show how
  often the refine loop stops because a hard edge would close a cycle.
- **Host-ordered effects.** Microtask and timer enqueue and host output need
  an explicit cell before TV can accept swaps of statements that only enqueue.
  Until then those stay `Havoc`, which is safe.
- **Fallback reach.** The 6,079 opaque-opaque swapped pairs on Tana decide
  whether TV accepts in practice. If fallback reach stays global, TV rejects
  the same specs, with better diagnostics.
- **Swap-count stability.** An edit in one module can reorder others and
  surface new swapped pairs. Diagnostics should say the conflict is new to
  this order, and the search should prefer orders close to the previous
  accepted one.
- **Tooling that reads atomic groups** (`unassigned_mode: per_atomic_group`,
  `peel`): keep atomic groups as planning output indefinitely, or redefine
  them as SCCs of hard edges?
