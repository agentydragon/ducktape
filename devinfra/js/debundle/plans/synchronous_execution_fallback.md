# Synchronous-execution fallback

Narrow the blast radius of an incomplete synchronous execution. Dispatch order
is <../TODO.md>; this plan burns down as steps land.

## Problem

Measured on the canonical Tana input, `devel` 4377a57bae versus the pinned
engine ecb4a734 (<../perf/pin_vs_devel_tana_2026_10_29.md>): 331 statements
went from complete to incomplete, each gaining 500–2,700 outgoing edges to
30–50% of all prior declarations. Those edges form the 1,833-module SCC and
add about 1 M edges.

The edges are the statement's synchronous read set, not the S-chain. Of the
growers' outgoing edges, 387,894 are `promoted_at_init` eager uses whose
callee is the statement itself, and 379,206 are `Sequenced`. On a
representative statement the two target sets overlap 490 of 506: each
`Sequenced` edge orders a promoted read against the read binding's nearest
preceding writer (`graph/build.rs::promote_at_init_calls`). The read set is
what the interpreter's havoc paths promote for a construction or call it cannot
model: class-member promotion of a rejected construction, the callback pool
fallback (`bounded_invocation_fallback`), and the foreign-record promotion.

Every grower carries a loss other than `callback_pool_limit`:

| Loss alongside the pool limit       | Statements | Site                                                   |
| ----------------------------------- | ---------: | ------------------------------------------------------ |
| `unsupported_construction`          |        192 | one local class, three factory wrappers (`new C(...)`) |
| `unsupported_operation/member_read` |         71 | descriptor alias chains, Map/Set registries + freeze   |
| `unknown_call_target`               |         31 | opaque calls                                           |
| array spread, membership protocol   |          3 |                                                        |

### Dropped: summarizing an overflowing pool

The earlier steps 1–2 summarized a pool above `MAX_SYNCHRONOUS_CALLBACK_POOL`
by invoking each body once with `Unknown` arguments, and narrowed unresolved
callees to their own read closure. Both premised that the statement could stay
complete. It cannot: every caller of `invoke_possible_callbacks` (opaque call,
rejected construction, own-data read, implicit coercion, callable field write,
protocol steps) records its own loss before the pool is consulted, so the pool
is reached only on an already incomplete statement. Re-summarizing the escaped
pool at each statement also replaces the fallback's incremental closure cache:
the prototype timed out `opaque_callback_scale_test`.

## Steps

### 1. Unblock the rejected construction

All 192 construction losses sit at the three wrapper sites of one local class,
and all reject at `field_may_exist_on_object_prototype` (the first field
checked; every field fails). The whole-program prototype census holds 63,067
entries with no named property (49,124 escapes, 10,987 reflective writes, 2,785
computed writes, 171 deletes; 61,877 of all 88,617 entries are in the vendor
chunk), so no field name can be proved absent from `Object.prototype`. The
pinned engine predates this check.

The engine cannot prove the premise. The audited author input that supplies
exactly it already exists: `retained_record_factory: true` on each wrapper
(SPEC § Synchronous analysis limits). This is a spec change in the private
Tana spec, gated on that audit; measure its effect before landing it.

### 2. Classify the member-read losses

Group the 71 `member_read` growers by the operation the interpreter declined
(descriptor alias, registry `Map`/`Set`, `Object.freeze` input) and decide per
group between a builtin-table row (`facts/abstract_domain/builtins.rs`), an
existing author input, or nothing.

### 3. Bound what an incomplete statement promotes

Design, soundness argument and prototype measurement: § Havoc reach below.
Verdict: land R1 (record the reach on each loss) only. R2 (promote only the
reach) removes 16% of edges on Tana but leaves the 1,833-module SCC and the
210-member conflict, which come from losses whose reach is `Global`.

### 4. Measure

Before and after each step, per <abstract_interpreter.md> § 6.1: public
controls, and canonical Tana on `devel`. Report edges, SCC sizes, atomic
conflicts, wall and RSS, and the Claude CLI class B pivots (`YC`, `Ujr`, `xe`,
gaffer-private `claude/cli/re/2.1.292/debug/2026-10-29-conflict-inventory.md`)
as the cross-check. A smaller graph is not a pass; the gate decision is
reported as observed.

## Havoc reach

Design for step 3. Measured on canonical Tana with engine `fc3ef985de` plus a
logging-only patch (its graph matches the unpatched engine exactly: 3,146,382
edges, conflicts 210/9/7/3/2).

### Today's rule

Suppose statement `s` reaches an operation the interpreter cannot model: an
unknown callee, a rejected construction, an unsupported member read or
coercion. The interpreter then records a precision loss and calls
`invoke_possible_callbacks`.

The pool is everything unknown code could hold:

- `global_escaped_callbacks`: every callback passed to opaque code anywhere
  in the source prefix, accumulated over the whole prefix;
- the exported callbacks, when export reentry is enabled;
- the callbacks in the operation's own arguments.

The pool is the prefix-wide escape set, so on Tana it exceeds
`MAX_SYNCHRONOUS_CALLBACK_POOL` and takes `bounded_invocation_fallback`. That
fallback expands the roots of every callback in the pool through
`SourceUnitReadClosure::extend_synchronous_source_units` (synchronous read
edges, stopping at a definite `await`). It then promotes every lazy read and
rebind it reaches as a synchronous effect of `s`. `CallbackFallbackSummary`
persists across statements, so the first fallback in each statement applies
the whole closure accumulated so far.

A rejected construction also adds `promote_class_members`; a foreign record
adds `promote_foreign_record`. The fallback then escapes the argument
callbacks into `global_escaped_callbacks` for every later statement.

Graph build turns those effects into edges (`graph/build.rs::promote_at_init_calls`):

| Edge from `s`                                                                                                       | Source                                   | Tana                                   | Lemma it serves                              |
| ------------------------------------------------------------------------------------------------------------------- | ---------------------------------------- | -------------------------------------- | -------------------------------------------- |
| `EagerUse` `promoted_at_init`, callee = `s`, to each read/rebound binding's declaration                             | `synchronous.reads`/`rebinds`            | 1,550,322                              | 3 (at-init read sees an initialized binding) |
| `Sequenced` `binding_timeline` reader-after-writer (and writer-after-reader from the next writer)                   | `s` joins the binding's callback readers | ≈1.5 M                                 | 3, 5 (read sees the source-prefix value)     |
| `EagerUse` `promoted_at_init` over `UnresolvedCallFallback` (`SourceUnitReadClosure` of unresolved sources/callees) | `!synchronous.complete`                  | 48,659 (3,111 pairs not already above) | 3                                            |
| `Sequenced` `cell_order` heap-frontier barrier (`IncompleteSynchronousExecution`)                                   | `emit_heap_content_order`                | live frontier only                     | 5 (array/set contents)                       |
| `Sequenced` `BarrierAfterPrior` against every prior impure unit                                                     | `dataflow_opacity`, not incompleteness   | 0 (`trusted_dataflow_summaries`)       | 5                                            |

Each kind is needed under the current theorem. Unknown code running during
`s` may invoke any callback it can reach. That callback's binding reads then
fire at `s`'s position, so they are at-init reads of `s` (Lemma 3) and must
observe the source-prefix value (Lemma 5, binding timeline). Of the 1,306
incomplete units, 998 carry more than 1,000 out-edges each, 3,055,753 edges in
total (97% of the graph). The strict S-chain fallback contributes nothing on
Tana.

### Proposed rule

Each havoc event `e` in `s` carries a reach:

- `Bounded(roots)`: the unknown part is known code (a rejected local
  construction, a known callback pool over budget, a recursion or expansion
  limit). `roots` are the owners of that code plus the callables reachable
  from the values it receives: arguments, receiver and captures, through
  stored properties and captured contexts. The promoted set is the
  synchronous read closure of `roots`. The cumulative escape pool, the export
  callbacks and the cross-statement fallback summary are not read.
- `Global`: everything today's rule promotes, kept as the conservative
  fallback.

The event collapses to `Global` when any of these holds:

1. The callee is unknown: an import from another chunk without a callable
   summary, a value read from the global object, a parameter or other
   `Unknown`, or an unknown member. Imported code may call any callback it
   was ever handed.
2. The known code contains such a callee, transitively, over the same
   synchronous adjacency.
3. An input value is tainted (`tainted_objects`, `tainted_functions`,
   `escaped_class_capabilities`), or reaches a global-object alias. Opaque
   code may have attached a retained callback to it, and the interpreter's
   heap does not show that.
4. `this` in reached method bodies can be a borrowed receiver: the class or
   method is published, escaped or tainted.
5. Reachability is incomplete (`eval`, `with`, `Function`), the value is a
   `ForeignDeferredRecord`, or computing the reach itself exceeds a budget.

Writes into the escape set are unchanged. A `Bounded` event still escapes its
argument callbacks into `global_escaped_callbacks`, taints its inputs and marks
class capabilities escaped. Only the read of the prefix-wide pool is narrowed.
The binding-timeline, heap-frontier and `dataflow_opacity` rules are
unchanged, and they consume the narrowed read set.

### Soundness

Claim: for a `Bounded` event, every binding a callback can read synchronously
during `e` lies in the read closure of `roots`. Under that claim, Lemmas 3 and
5 hold for the reads the closure contains, exactly as they do for the reads
of a complete statement.

The claim rests on three assumptions:

- **A11 (intrinsic integrity).** Intrinsics the known code calls run no
  user code beyond their builtin row.
- **A1/A5 and the dynamic-scope rule** (no `eval`/`with`, enforced by
  collapse rule 5). No reads exist beyond the static ones.
- **A complete escape model.** Every route by which unknown code obtains a
  callable goes through `global_escaped_callbacks`, taint or class-capability
  escape. Today's `Global` rule already depends on this; the narrowing adds
  the dependency that a `Bounded` input's value set is complete when the input
  is untainted.

Counterexamples:

| Case                                                                                          | Captured by the reach?                                                                                                                                                                                                                     | Fallback          |
| --------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------- |
| Unknown callee receives object `o`; later another module's at-init code reaches `o`           | `s` itself is `Global` (rule 1). The later statement either calls unknown code, which is `Global` and sees `o`'s callbacks because `s` escaped them, or reaches `o` through a binding or global read in known code, an ordinary read edge. | rule 1            |
| A known constructor stores its argument in a global; later code calls it back                 | The body is not interpreted, but the fallback still escapes the argument callbacks. A later call through the global is a callee read from the global object, so rule 1 makes it `Global` and the pool contains them.                       | escape write kept |
| Aliasing through globals: known code calls `globalThis.p()` or an alias                       | No: the value is unknown.                                                                                                                                                                                                                  | rule 1 or 3       |
| Deferred callback runs later in initialization (host queue, registry, `FinalizationRegistry`) | Its later invocation happens in some statement `t` that calls unknown code, which is `Global` and holds the callback because `s` escaped it. After initialization, Lemma 4 applies.                                                        | escape write kept |
| Heap mutation through an escaped receiver: `new Local(r)` where opaque code holds `r`         | No: opaque code may have installed accessors or callbacks on `r` that the interpreter cannot see.                                                                                                                                          | rule 3            |
| Control 1 in <native_receiver_identity.md>: `{}` assigned to a global slot, read back         | Read-back is not identity: a setter can drop the store and a getter return `Array.prototype`. A global-slot read is `Unknown`.                                                                                                             | rule 1 or 3       |
| Control 2: a method stores `this` in a global                                                 | `this` can be `Array.prototype` when the method is borrowed. A published or escaped method body has a `Top` receiver.                                                                                                                      | rule 4            |

Both native-receiver controls must still reject after R2.

### Prototype

Patch: `/var/cache/agentydragon/incomplete-narrowing/havoc_reach_prototype.patch`
(on `fc3ef985de`; binaries, scripts and per-run outputs are beside it). It
narrows only the rejected local construction, the largest bounded loss.
`DEBUNDLE_PROTO_REACH` selects the mode:

- `1`: unconditional local pool, invoking each callback;
- `3`: unconditional local reach, summarized through the fallback;
- `4`: as `3`, with a one-level, non-transitive version of collapse rules 1–2
  (a global or imported callee, or an unresolved member call rooted at a
  chunk-top or global binding, in the constructor facts).

Mode `4` is a measurement, not a sound implementation: rules 2–4 are not
transitive there.

| Run (`runs/narrow-mode*`)     |     Edges | Units > 1,000 out-edges | Module edges | Largest SCC | Atomic conflicts |                          Main-chunk analysis |
| ----------------------------- | --------: | ----------------------: | -----------: | ----------: | ---------------- | -------------------------------------------: |
| `0` (today)                   | 3,146,382 |                     998 |      347,754 |       1,833 | 210/9/7/3/2      |                                        190 s |
| `1` (invoke local pool)       |         – |                       – |            – |           – |                  | > 2,000 s (killed at ordinal 4,041 of 9,709) |
| `3` (local reach, summarized) | 2,641,200 |                     725 |      239,670 |       1,833 | 210/9/7/2/2      |                                        187 s |
| `4` (with collapse test)      | 2,678,518 |                     742 |      242,772 |       1,833 | 210/9/7/2/2      |                                        186 s |

In mode `4`, 333 construction events stayed `Bounded` and 31 collapsed to
`Global`. Wall times are from concurrent runs on a shared host; compare them
within the table only.

Findings:

- **Most of the flood is `Global` by rule 1.** Of the 998 flood units, 586
  (2,070,425 edges) reach an unknown callee. Mostly that is an import from the
  vendor chunk: builder chains such as `.describe`, `.optional`, `.min`, and
  minified imports called directly. The rest are parameters. Only 412 units
  (985,328 edges) have bounded losses alone. That 412-unit share is the most
  R2 can remove.
- **R2 for construction removes 16% of edges** and reduces module edges by
  31%, but the SCC and the 210-member conflict stay. They come from the
  `Global` units.
- **Small pools cost more than the fallback.** A local pool is small enough
  to enumerate, and invoking each callback made main-chunk analysis more than
  10× slower (mode `1`, `callback_values` and `imbl` copy-on-write hot). R2
  must summarize like mode `3`.
- **The author premise does more.** With `retained_record_factory` on the
  three construction wrappers (step 1; sync-fallback's local run), edges fall
  to 2,575,738 and the conflicts to one of 9 members. A modelled construction
  does not escape its arguments either, so later `Global` pools shrink. R2
  keeps that write by design.

### Steps (PR-sized)

- **R1. Record the reach.** Each `ExecutionPrecisionLoss` carries
  `reach: Bounded | Global { cause }` in the report; there is no analysis
  change. Tests (e2e, `synchronous_precision_report_test.rs`):
  - a rejected local construction with an inline callback argument →
    `Bounded`;
  - the same constructor calling a global function → `Global { unknown_callee }`;
  - one calling a chunk helper that calls an import → `Global` (transitive);
  - an argument previously passed to an opaque call → `Global { tainted_input }`;
  - a published class whose method is borrowed → `Global { borrowed_receiver }`;
  - `with` in the prefix → `Global { dynamic_scope }`.
- **R2. Promote only the reach for `Bounded` events.** This is the mode `3`
  logic, with collapse rules 1–5 transitive, and only after R1's fixtures pin
  the classification. Tests:
  - a constructor whose argument callback writes a binding read at init by a
    later module keeps that edge, and Node output matches;
  - an unrelated escaped callback (escaped earlier through an opaque call)
    that writes a binding produces no edge from the bounded statement but
    keeps one from the next `Global` statement;
  - negatives that must still reject:
    - the two native-receiver controls (`intrinsic_array_iteration_test.rs`);
    - a deferred callback registered with opaque code and fired by a later
      opaque call;
    - a tainted receiver passed to a local constructor;
    - a global alias stored and called back through `globalThis`;
  - `opaque_callback_scale_test` within its current bound.
- **R3. Drop graph build's `UnresolvedCallFallback` for incomplete statements
  whose losses are all recorded.** That holds only once every interpreter loss
  path carries a reach; the 3,111 uncovered pairs on Tana must first be
  explained or covered. Tests: the #5 promotion set (`at_init_promotion_*`).
- **R4. Measure** per step 4.

R1 is cheap and gives step 2 its classification. R2 is sound under the
assumptions above but yields 16% of edges and no gate change on Tana. Gate
it on step 1 and on cross-chunk callable reach: summaries for the vendor
imports would turn rule 1's largest group into `Bounded`.

## Not in scope

Changing a budget constant; the export-reentry policy (<host_behavior.md>).
