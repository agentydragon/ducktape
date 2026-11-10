# Translation-validation gate

Replace the realizability gate's "no cycle among materialized constraining
edges" (Pass 1) plus separate ESM simulation (Pass 2) with translation
validation of the candidate emit:

1. compute the order the emitted modules actually evaluate statements in;
2. find the statement pairs whose relative order differs from source order;
3. assess whether every such pair commutes, judged from the interpreter's
   effect facts, with TDZ as an ordinary effect conflict; and
4. check the independent structural placement obligations of the same candidate.

Neither pass survives as a verdict. Cycle detection becomes at most a hint to
the order search. The ESM loader model becomes a plain, exact component: it
evaluates the emitted import lists as ECMA-262 does, cyclic or not, and it
always runs on the real emitted order, never on a fallback order.

The architecture this serves is <target_architecture.md>. This plan contains
the remaining migration. The report-only candidate assessment pairs effect
conflicts with typed placement evidence for the same finalized assignment; the
shared projection also supplies emission's checked placement maps. Plan-building
refusals before a final assignment remain outside that assessment. The legacy
gate remains authoritative on `devel`. Draft #14552 is a reference
implementation with the flip already applied; the landing order that keeps the
flip as the late, small PR this plan's § Migration requires is in
<convergence_landing.md>.

## Problem

The gate materializes ordering edges for pairs the analysis cannot prove
independent, then rejects module-level cycles among them. Many of those pairs
keep their source order in the emit. Opaque heap frontiers can induce cycles
through the graph encoding even when a candidate order could preserve all
actual conflicts. Translation validation checks the reordered pairs directly;
it still needs conservative effect coverage and an acceptable candidate order.

Historical investigations live in <../debug/2026_10_31_tana_scc_pessimism.md>
and <../debug/2026_10_31_pure_call_heap_frontier.md>. Their counts describe the
recorded inputs and engines, not current canonical outcomes.

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
- all structural placement checks succeed, including cross-module rebinding,
  declarator grouping and destination projection;
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
sharpness on the swapped pairs alone.

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

## Effect facts the pair check needs

- **Per-statement effect sets** as abstract-interpreter step 7 defines them
  (`Binding`, `GlobalProp`, `Contents`, `Alloc`, `Havoc`), plus the
  evaluation-time initialization writes (excluding instantiated functions,
  imports and bare var redeclarations). Today these live, in
  partial form, in `StatementFacts` (`reads`, `rebinds`, `heap_contents`,
  `global_reads`/`global_writes`, `synchronous_calls`).
- **Complete domain coverage.** An `OperationReceipt` can seal typed lexical
  reads and rebinds for one ordered root initializer; other owners can still
  carry `LegacyExecution { complete }` or uncovered authority. This binding
  evidence does not close property, heap, global or host domains. Unmodeled
  effects remain conservative `Havoc`; neither execution completeness nor a
  binding receipt proves arbitrary effect coverage.
- **Synchronous callee effects folded into the caller**, which the interpreter
  already does for binding and collection accesses. Bounded fallback reach
  is currently report-only, without a retained effect-domain closure;
  incomplete or unmodelled domains remain conservative `Havoc` until that
  closure exists (<synchronous_execution_fallback.md>). Neither purity
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
  placement edit elsewhere would flag a valid premise as unused.

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
  - The purity migration preserves one per-chunk context. TV reads the
    settled per-statement result and adds no constraint on it.
- **Staged pipeline** (<../docs/bazel_integration.md>):
  - TV runs in S4 on the S3 facts artifact, which must carry the effect sets.
    It already carries `StatementFacts`.
  - Commuting resolution already sits in S4.
  - The graph build shrinks: it emits only the hard edges the search seeds
    from, and the full edge set becomes a tooling view built on demand.
  - Assignment edits, the agent loop, still never touch S3.
- **Tooling** (atomic groups, `peel`, `modules propose`, `gate describe`,
  `per_atomic_group` / `coarse_atomic_groups`): these read `dependency_graph.json.gz` edges
  and atomic groups. They keep that data until each is ported to query
  swapped-pair conflicts. Atomic groups remain a planning heuristic, not the
  acceptance criterion.

## Migration

Keep today's gate authoritative until the differences between edge-TV and
effect-TV have been explained and the replacement has complete effect
coverage. Bounded report examples are not proof inputs: comparisons must use
the complete conflict enumeration.

1. **Effect coverage and discrepancy audit.** Complete the missing effect
   domains in abstract-interpreter step 7 and audit the existing effect-set
   report's differences against the edge-based verdict per category. Use the
   full conflict export for this audit: bounded examples and aggregate counts
   do not establish effect coverage. Aggregate every conflicting cell for a
   pair before applying its premise; covering one pair must not hide another.
   Classify each discrepancy from its source
   and effect witnesses before changing authority. A new acceptance may expose
   conservative legacy edges or missing or understated effects; it requires proof
   that no conflicting cell was omitted. A new rejection may expose a conflict
   missed by legacy rules, conservative effect coverage, or an incorrect new
   effect. Verdict disagreement alone does not identify which checker is wrong.
   - Discharge swapped forward reads (§ Relation to today's gate).
     The gate accepts some orders only by swapping a forward edge, which
     the edge-TV report lists as `forward_edges_swapped` outside its verdict.
     For each such case, prove the read cannot run at initialization or retain
     the conflict. Existing pair premises and legacy acceptance do not prove
     that a callback is deferred.
2. **Search.** Seed from hard edges and add the refine loop. Preserve the
   verdict and swapped-pair comparisons for each candidate order.
3. **Flip.** TV decides; today's gate becomes a report section for one
   release, then goes, Pass 1's cycle verdict and Pass 2's degraded-order
   fallback with it. `commuting_statements` diagnostics move to the
   effect-set definition of "unused".
   - Final-candidate edit assessments and emission already use the same
     `AssignmentAnalysis` import order. Graph-only decomposition validation
     in `peel/decompose.rs` still uses the legacy gate without that order; retain
     that boundary until an appropriate search/validation contract is proved.
   - Audit destructuring-sibling and nested-`var` routing refusals before the
     finalized assignment. Keep those hard errors until their request-building
     obligations can be represented. An assessment's empty blocker list does
     not cover requests that never reached it. Preserve the shared projection's
     distinction between structural blockers and semantic-order SCC vetoes.
4. **Slim.** The graph build materializes hard edges only, and the full edge
   set moves behind the tooling queries that still need it.

The comparison runs in e2e as differential tests
(`e2e/translation_validation_differential_test.rs`, plus the oracle in every
e2e test), and on the canonical inputs as a report diff.

## Open questions

- **Search granularity.** Do phantom imports between whole modules give the
  search enough freedom, or do some conflicts need a statement moved to
  another module (a spec edit, out of scope)? The search report must show how
  often the refine loop stops because a hard edge would close a cycle.
- **Host-ordered effects.** Microtask and timer enqueue and host output need
  an explicit cell before TV can accept swaps of statements that only enqueue.
  Until then those stay `Havoc`, which is safe.
- **Fallback reach.** Global reach makes swapped opaque statements conflict.
  Narrowing it requires the domain-closure proof in
  <synchronous_execution_fallback.md>; better diagnostics alone do not improve
  acceptance.
- **Swap-count stability.** An edit in one module can reorder others and
  surface new swapped pairs. Diagnostics should say the conflict is new to
  this order, and the search should prefer orders close to the previous
  accepted one.
- **Tooling that reads atomic groups** (`unassigned_mode: per_atomic_group` or
  `coarse_atomic_groups`, `peel`): keep atomic groups as planning output indefinitely, or redefine
  them as SCCs of hard edges?
