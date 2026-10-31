# Tana's big SCCs: what holds them together (2026-10-31)

Question: why does the gate keep Tana web's largest cycles together, and what
does the engine not understand that would make splitting them valid?

Answer: the 1,800-module SCC is a side effect of the Pass-1 cycle, not an
independent cycle. The 963-module Pass-1 cycle is closed by 1,051 heap-barrier
`cell_order` edges. Without them Pass 1 is acyclic even with all 2.06 M
fallback edges. With them and no fallback edges, a 717-module Pass-1 cycle
remains. The fallback edges decide which Pass-2 SCCs survive once Pass 1 is
clean. Edges the engine labels observed leave two Pass-2 TDZ arcs, and only
one of them is a real ordering requirement.

## Inputs and method

- Engine: `-c opt` debundle at ducktape `e1f145dd8f`. Spec: gaffer-private
  main plus #686. The run is `/tmp/gaffer-repin-3/runs/tana-686-e1f145dd`.
- The verdict: rejected with 0 atomic conflicts. Unrealizable SCCs are 963
  modules (Pass 1, 229,444 edges), 2 modules (Pass 1) and 1,800 modules
  (Pass 2, 161,444 edges). The graph has 2,118,225 edges.
- The analysis reads `dependency_graph.json` offline. A Python port of
  `realizability::verdict_from_canonical` runs on filtered edge sets. It
  covers the canonical edge projection, Pass 1, the Pass-2 ESM simulator with
  petgraph's `toposort`/`DfsPostOrder` neighbour order, and the entry rule.
  - On this graph it reproduces the engine's module sets and its 161,444 Pass-2
    edges exactly.
  - It agrees with the engine's rejected module sets on all 72 Claude CLI
    graphs at the same pin that reached the gate. Nine of them exercise a Pass-2 rescue, and `sessionSDK`
    is rejected.
- Scripts are in `/var/cache/agentydragon/scc-pessimism-scripts/`; the entry
  point is `gate.py`. Scratch is in `/tmp/scc-pessimism/`. Loss sites were
  resolved to their callee with a babel AST pass (`classify.mjs`). The pass
  uses Prettier 3.8.1's parser and a scope walk.

Edge provenance follows SPEC § Realizability cut explanations. Across the
2,090,657 constraining cross-module edges (gate view, sequenced not deduped):

| Class                                                                |     Edges | Provenance                           |
| -------------------------------------------------------------------- | --------: | ------------------------------------ |
| `eager_use`, `promoted_at_init` with callee = self                   | 1,062,195 | fallback (`unresolved_at_init_call`) |
| `sequenced` `binding_timeline`, one side `fallback`                  | 1,001,180 | fallback                             |
| `sequenced` `binding_timeline`, both sides `observed`                |    23,102 | observed (but see below)             |
| `eager_use`, promoted through a resolved callee                      |     1,913 | observed                             |
| `eager_use`, direct                                                  |     1,071 | observed                             |
| `sequenced` `cell_order` with a `heap_frontier`/`opaque_barrier` end |     1,051 | assumed heap barrier                 |
| `sequenced` `cell_order` between `summarized`/allocation cells       |       145 | observed                             |

`heap_frontier` endpoints count as assumed even when their only cause is
`array_content_effects`. Their conflict is `after_barrier`, the generic heap
barrier rule: an opaque call might touch any live tracked array. No concrete
cell is involved.

## Why 1,800 is not a cycle of its own

`EsmImportOrder::build` takes `linker_order` from
`chunk_linker_order_from_pairs`. That function returns an empty vector when
the constraining pairs are cyclic, so any Pass-1 cycle empties the linker
order. With no linker positions, every import list sorts by `ModuleId`, and
the simulator's TDZ verdict on the I-graph SCC follows that arbitrary order.
The 1,800-module SCC contains the 963 and is reported because of that
fallback ordering.

Co-locating the Pass-1 SCCs (963 and 2) gives the simulator a real linker
order. Pass 2 then rejects 838 modules (95,093 edges). The SCC's size and
cut, as reported, say nothing about what a valid split needs.

## 1. Observed-only

Keeping only observed constraining edges, plus all lazy and hoisted I-graph
edges:

| Edge set                                      | Pass 1                 | Pass 2 (TDZ pairs)               |
| --------------------------------------------- | ---------------------- | -------------------------------- |
| baseline                                      | 963, 2                 | 1,800 (25,523)                   |
| observed only                                 | none                   | 26 (1), 18 (1)                   |
| observed, minus pool-labelled timeline edges  | none                   | 26 (1), 2 (1)                    |
| observed + heap-barrier `cell_order`          | 717, 13, 5, 5, 5, 2 ×5 | 1,620 (1,370), 2 (1)             |
| everything except heap-barrier `cell_order`   | none                   | 108 (161), 30 (1), 18 (7), 6 (2) |
| everything except `eager_use` fallback        | 963, 2                 | 1,800                            |
| everything except `binding_timeline` fallback | 894, 11, 2, 2, 2       | 1,710, 2                         |

With observed edges only, the cycles fall apart. Pass 1 is acyclic, and
Pass 2 keeps one TDZ pair in each of two I-graph SCCs.

**Observed is not the same as real.** Of the 23,102 observed
`binding_timeline` edges, 21,812 have a source statement that ran the escaped
callback pool to its 64-callable limit. The provenance labels effects of
interpreted pool callbacks `observed`, although nothing shows that the opaque
callee invokes them. The second arc below is one of these.

## 2. The remaining arcs

| Arc                                                                                    | Edge                                                                                                                                                                   | Verdict                                                                                                                                                                                                                                                                                                                                                                                                          |
| -------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `domains/graph/metanode/tableviewaccessor` → `domains/graph/metanode/nodeviewaccessor` | `eager_use` `Ye`, direct: `class Rn extends Ye {` (L29506) reads the superclass `class Ye extends lt {` (L14301)                                                       | **Real.** Superclass evaluation is a true init-order requirement. The TDZ is Pass 2's. The entry DFS first reaches `nodeviewaccessor` through a lazy import from `features/search/viewexpressionsetup`, which is outside the 26-module I-SCC. So `tableviewaccessor` evaluates while `nodeviewaccessor` is still on the stack. Fix in the spec by co-locating `Rn` with `Ye`, or moving the lazy back-reference. |
| `shared/html/taglists` → `features/navigation/node/commandsearchcontext`               | `binding_timeline` `UF`, `writer_after_writer`, both `observed`: `const Hqe = Ix.map((n) => n.toUpperCase())` (L98414) after `c9e = PPe.filter(...).map(...)` (L89995) | **Engine pessimism.** `UF` is the lazy cache in `Nbe()` (`UF \|\| (UF = new Set([...]))`, L37009). Neither statement calls `Nbe`. Both record `unknown_call_target` at the Array method call and `callback_pool_limit`, so the "write" comes from running the escaped callback pool. `Ix` is a literal string array.                                                                                             |

The strict variant drops the pool-labelled `binding_timeline` edges. It
replaces the second arc with another real one,
`domains/search/index/core/part-7` → `part-6`. There
`N2t([Z], P5.prototype, "isTextIndexing")` (L187639) reads `class P5`
(L187550), whose methods lazily reach `_2t` back in `part-7`. A decorate
application split from its class is a spec issue, fixed by co-location.
Which of these TDZs fire depends on the import order the remaining edges
produce, so the Pass-2 residue is not stable across edge sets.

Recommendation for the real core: co-locate `tableviewaccessor` with
`nodeviewaccessor`, and every `__decorateClass` application with its class.
Neither needs a premise.

## 3. Attribution

### Pass 1: heap barriers

All 1,051 heap-barrier edges join consecutive heap barriers. 777 of them have
a source statement the engine rates pure, for example `b.forwardRef(...)`,
`ut.createContext(...)`, `b.memo(...)` and `Se({...})`. SPEC § Dependency-graph
array ordering keeps such calls as barriers once tracked arrays are live.
Purity does not prove heap locality (see
[pure-call heap frontier](2026_10_31_pure_call_heap_frontier.md)).

| Heap-barrier subclass (union of the endpoints' `heap_frontier` causes)     | Edges | Both endpoints pure |
| -------------------------------------------------------------------------- | ----: | ------------------: |
| `unresolved_callee` (an imported or unresolved callee has no heap summary) |   481 |                 390 |
| `incomplete_synchronous_execution`                                         |   334 |                  45 |
| `callee_heap_effects` (a resolved local callee touches arrays)             |   192 |                 185 |
| `array_content_effects` only                                               |    44 |                   9 |

All fallback edges are kept and only heap-barrier subclasses are removed.
Results:

| Removed                                              | Pass-1 SCCs        |
| ---------------------------------------------------- | ------------------ |
| none                                                 | 963, 2             |
| `unresolved_callee`                                  | 525, 4, 3, 2       |
| `incomplete`                                         | 325, 8, 5, 5, 5, 3 |
| `unresolved_callee` + `incomplete`                   | 4                  |
| `unresolved_callee` + `incomplete` + `array_content` | none               |

So Pass 1 is a heap-ordering problem. The 963 survives removing every
`eager_use` fallback edge, and it shrinks only to 894 without the
`binding_timeline` fallback edges.

### Fallback edges: why the statements are incomplete

All of the following describes the fallback-derived edges (eager fallback
and timeline fallback, 2,063,375). 94.7% of the eager fallback edges
(1,005,502 of 1,062,195) target a statement outside the incomplete
statement's own lexical read closure. That closure comes from `lazy_use`,
`hoisted_use` and resolved `eager_use` edges. Such targets come from the
fallback's other roots: `prior_effect_owners` (every earlier impure owner)
and the escaped-callback pool (`facts/sync_effects/interpreter.rs`,
`promote_property_writes` and the unknown-call fallback). Narrowing the roots
alone does not clear Pass 2, though. With the heap barriers also removed, it
leaves 108, 30, 18 and 6. Removing every eager fallback edge leaves 108, 18
and 6, because the timeline fallback edges carry the rest.

Responsibility is assigned per statement, not per edge. An eager fallback
edge belongs to its source statement. A timeline fallback edge belongs to
each side whose provenance is `fallback`. The statement's requirement is the
set of loss classes at its own span or in its lexical read closure.
`callback_pool_limit` and pool-caused `recursive_invocation` are left out,
because the pool runs only because of the other opaque operations. A
statement's edges disappear when every class in its requirement is
understood.

### Greedy: which classes break the cycle

Families group the loss classes by callee or helper; the next table defines
them. Each step adds the family whose understanding most shrinks the
verdict. Pass-1 size is minimized first, then Pass-2 size, then TDZ pairs.
`Fallback edges left` counts assumed edges whose responsible statements are
not yet all understood.

|  Step | Family understood                                        | Unrealizable SCCs after (pass)       | TDZ/cycle pairs | Fallback edges left |
| ----: | -------------------------------------------------------- | ------------------------------------ | --------------: | ------------------: |
|     0 | none                                                     | 1,800 (2), 963 (1), 2 (1)            |          66,853 |           2,064,426 |
|     1 | heap barrier: opaque imported callee                     | 1,713 (2), 594 (1), 3, 2, 2, 2 (1)   |          34,568 |           2,064,080 |
|     2 | other vendor calls (mobx, react, orpc, unmapped exports) | 1,708 (2), 375 (1), …                |          17,439 |           1,990,268 |
|     3 | heap barrier: local callee with array effects            | 1,704 (2), 272 (1), …                |          15,536 |           1,990,040 |
|     4 | heap barrier: array-content operation                    | 1,539 (2), 201 (1), 14 (2), …        |           9,663 |           1,989,890 |
|     5 | Zod schema builders                                      | 1,522 (2), 166 (1), 10 (2), 3, 2 (1) |           7,619 |           1,127,135 |
|     6 | returned callbacks                                       | 1,498 (2), 135 (1), …                |           6,748 |           1,119,309 |
|     7 | other app callee bodies                                  | 1,487 (2), 10 (2), 3, 2, 2 (1)       |           4,074 |           1,036,325 |
|     8 | member reads on chunk bindings                           | 1,432 (2), 9 (2), 2, 2 (1)           |           2,383 |           1,026,559 |
|     9 | host globals                                             | 1,394 (2), 9 (2), 2 (1)              |             594 |             946,014 |
|    10 | `array_spread`                                           | 1,390 (2), 9 (2); **Pass 1 clean**   |             797 |             940,692 |
|    11 | bootstrap/infra late startup statements                  | 213, 106, 9, 6, 2 (2)                |             220 |             907,457 |
|    12 | UICommand factories                                      | 135, 106, 9, 6, 2 (2)                |             135 |             761,683 |
|    13 | `implicit_coercion`                                      | 135, 29, 9, 6, 2 (2)                 |             102 |             760,241 |
|    14 | TS `__decorateClass` helper                              | 49, 29, 28, 7, 6, 2 (2)              |              18 |             242,573 |
|    15 | method calls on chunk-bound instances/aliases            | 31, 28, 6, 2 (2)                     |              15 |             203,954 |
|    16 | unchecked local class construction                       | 29, 21, 6, 2 (2)                     |               7 |             150,730 |
|    17 | env lookup `Me(n) = TA[n]`                               | 29, 21, 2 (2)                        |               5 |             117,135 |
| 18–25 | the remaining small families                             | 26, 18 (2)                           |               2 |                  83 |

The last two SCCs are the two arcs in section 2. Pass 1 needs the heap
barriers understood, plus the statement families behind the `incomplete`
barriers, which steps 2 and 5–10 supply.

Pass 2 needs most of the remaining families. After step 10 a 1,390-module
SCC remains. Steps 11, 12 and 14 do most
of the work: late startup statements, UICommand factories and
`__decorateClass`.

A finer greedy works at the level of individual callees, starting from no
heap barriers (`greedy3.py`). Its first picks are `__decorateClass`, `uf`,
`Se`, unchecked construction, `Zt` and `ft` (all UICommand), Zod, host
globals and Zod values in chunk bindings. After 27 picks it leaves only the
18-module `UF` arc.

### Classes

`Stmts` counts incomplete statements whose requirement includes the family.
`Edges (any)` counts fallback edges with at least one responsible statement
in the family. `Edges (alone)` counts the edges whose whole requirement is
this family, so they would disappear if only this family were understood.
`In 963` counts edges with both endpoints in the 963-module SCC (before
sequenced dedup), out of 361,240.

| Family                                                                                                                                                                                                                                                                                                                                                                                                                                    | Stmts | Edges (any) | Edges (alone) |  In 963 | Knowledge that would make it understood                                                                                                                                                                                                                                                                                               |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----: | ----------: | ------------: | ------: | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Zod schema builders: calls to vendor `zod` exports (`string` 1,294 sites, `array` 609, `object` 345, `number`, `boolean`, `enum`, `literal`, `union`, `record`, `lazy`, …), `.optional`/`.nullish`/`.describe`/`.extend` chains, schema values in chunk bindings (`Kwe = br([...])`), and the `z.lazy` closures of `jwe`/`uC`/`Bwe`                                                                                                       |   374 |     903,386 |       857,302 |  70,725 | **Audited premise.** Zod 4.1.12 builders and schema methods allocate fresh schema objects and call no caller callback during construction; `z.lazy` stores its getter. Declare it as `chunk_export_purity.static/vendor-BPNhzNbc` `fluent_exports` plus no-reentry, or derive it as a vendor callee summary from the bundled package. |
| TS `__decorateClass` helper: 145 MobX decorate applications such as `H5([Z], oF.prototype, "currentStepIndex", 2)` through 20 helper copies (`Ww`, `ek`, `gJ`, `iy`, `Cu`, `wI`, `p0`, `TI`, `V2`, `X2`, `cn`, `ule`, `Q0`, `tF`, `nF`, `my`, `sF`, `H5`, `iF`, `bi`). The losses are the aliased `Object.getOwnPropertyDescriptor`/`defineProperty` calls, `i(e, t, s)` on decorators read from the array parameter, and `n.length - 1`. |   145 |     517,663 |       517,087 | 115,710 | **Audited premise, existing mechanism.** These copies have no `effect:`. Other copies already carry `effect: typescript_metadata_decorate_helper` for the same MobX `observable`/`action`/`computed` decorators (e.g. `app/state/core`). Audit and annotate the 20.                                                                   |
| UICommand factories: `new Fx(n, new eL(), …)` in `Se`/`Zt`/`uf`/`ft`/`fs`                                                                                                                                                                                                                                                                                                                                                                 |    71 |     147,240 |       145,705 |  55,825 | **Modelling fix.** Two checks fail: `field_may_exist_on_object_prototype(checker)` (the census leaves 63,089 possible writers) and `unchecked_property_source` for records allocated inside the helper. The [pure-call heap frontier](2026_10_31_pure_call_heap_frontier.md) note has the chain.                                      |
| Host globals: `new URL`, `new URLSearchParams`, `new Set`, `console.log`, `Object.keys/defineProperty/freeze`, `Promise.all`, storage, `performance.now`, `fetch`                                                                                                                                                                                                                                                                         |    46 |     103,730 |        74,189 |  13,622 | **Builtin rows** for the pure constructors and `Object.*`. **Host premises** for `console`/storage/`performance`. `fetch` and service-worker calls are real effects, so **refuse** those.                                                                                                                                             |
| Other app callee bodies: losses inside resolved app functions, none dominant                                                                                                                                                                                                                                                                                                                                                              |    46 |      95,309 |        69,350 |  20,034 | Case by case.                                                                                                                                                                                                                                                                                                                         |
| Property writes: `window.indexedDbSendQueue = …`, `X.displayName = …`, `Symbol.dispose` polyfill, `ND.all = …`                                                                                                                                                                                                                                                                                                                            |    23 |      83,551 |        82,424 |  30,841 | **Premises.** `deferred_global_publications` for the `window.*` debug hooks and `local_property_effects` for annotation writes the recognizer misses. The polyfill is a real global write.                                                                                                                                            |
| Other vendor calls: `mobx` (`observable`, `makeObservable`, `runInAction`), `react` `createContext`/`lazy`, `@orpc/server` `os`, and exports missing from `vendor_marks` symbols (`aN`→`Uw`, `dg`→`Vs`, `hE`, `lp`, `eP`–`eT`, `hi`)                                                                                                                                                                                                      |    34 |      83,254 |        73,791 |  28,375 | **Audited premises or vendor summaries**, per export. Identify the unmapped exports first.                                                                                                                                                                                                                                            |
| Late startup statements: bodies in `app/bootstrap/*`, `infra/*` and `app/environment/*` (boot progress, preload choice, Sentry, service worker) reached from the last statements, each about 6,400 edges                                                                                                                                                                                                                                  |    21 |      53,209 |        32,541 |   4,615 | **Refuse modelling.** This is the real startup. Step 11 shows Pass 2 needs these statements out of the cycle, so the practical fix is placement. In the entry module, the residual rule drops their fallback `eager_use` edges, though their timeline fallback edges stay. Untested.                                                  |
| Unchecked local class construction: `new $g()` and other classes with decorated fields                                                                                                                                                                                                                                                                                                                                                    |    30 |      53,208 |        50,516 |  14,530 | **Modelling fix:** construction certificates for classes whose fields are MobX-decorated.                                                                                                                                                                                                                                             |
| Method calls on chunk-bound instances or aliases: `uJ.resetData()`, logger `T`, `Object.*` aliases outside the decorate helper                                                                                                                                                                                                                                                                                                            |    37 |      47,039 |        19,799 |  14,628 | **Modelling fix:** intrinsic-alias resolution at call sites (`var X = Object.defineProperty`) and stable singleton instances.                                                                                                                                                                                                         |
| Env lookup `Me = (n) => TA[n]` and its callers `$i`, `NY`                                                                                                                                                                                                                                                                                                                                                                                 |    21 |      46,021 |        25,031 |   5,363 | **Modelling fix:** reads from a frozen plain-data env record with a string key.                                                                                                                                                                                                                                                       |
| Returned callbacks                                                                                                                                                                                                                                                                                                                                                                                                                        |   135 |      35,234 |             0 |  11,441 | Only matters jointly with the other families.                                                                                                                                                                                                                                                                                         |
| Member reads on chunk bindings: `TA[n]`, `$[n]`, `NR[n]`, `bd.length`                                                                                                                                                                                                                                                                                                                                                                     |    17 |      19,966 |         8,989 |   5,713 | Modelling: key domains of plain-data records.                                                                                                                                                                                                                                                                                         |
| Smaller classes: `implicit_coercion`, spreads, membership, `invocation_expansion_limit`, `opaque_execution`, `callable_inputs`                                                                                                                                                                                                                                                                                                            |    39 |      56,661 |        16,233 |  11,477 | Case by case.                                                                                                                                                                                                                                                                                                                         |

The heap-barrier families sit apart from these (section 3, Pass 1). An
imported callee needs a heap summary: `pure_exports` and callback-factory
contracts do not certify heap locality. The cheaper general fix is the
escaped-cell set from the
[pure-call heap frontier](2026_10_31_pure_call_heap_frontier.md) note: a
foreign callee can reach only cells passed or escaped to foreign code.

The fallback roots are a further, cross-cutting class. 94.7% of eager
fallback edges reach targets only through `prior_effect_owners` or the
escaped-callback pool. Narrowing those roots to callables actually stored
on the heap would remove about 1.0 M edges. It clears neither pass alone,
because the timeline fallback (1.0 M edges, 73% of them
`fallback_summary/callback_pool_limit`) carries the same orderings.

## Recommendation

1. **Heap locality first.** Pass 1 is a heap-barrier artifact. Give
   imported calls a heap summary, or build the escaped-cell set, and
   re-measure. Until Pass 1 is clean, the reported Pass-2 SCC and its cut are
   noise, because the simulator runs with an empty linker order.
2. **Two audited premises worth about 1.37 M edges** (66% of fallback
   edges), both on existing mechanisms:
   - `effect: typescript_metadata_decorate_helper` on the 20 unannotated
     `__decorateClass` copies;
   - a Zod fluent/no-reentry premise for the vendor chunk.
3. **Placement:** keep the late startup statements in the entry module.
4. **Two modelling fixes:** UICommand `new Fx` construction (in-call record
   property tracking) and intrinsic-alias calls.
5. **Spec co-location for the real core:** `Rn` with `Ye`, and every
   decorate application with its class (`N2t([Z], P5.prototype, …)`).
6. **Engine reporting:**
   - mark effects found by running the escaped-callback pool as `fallback`,
     not `observed` (21,812 of 23,102 observed timeline edges);
   - have the gate explain that a Pass-2 verdict computed with a cyclic
     Pass 1 uses `ModuleId` order.

Everything here is measured on the emulated gate over the saved graph. No
change was applied to the engine or the spec, and no regenerated run checks
these predictions. The edge removals assume understanding a family deletes
its statements' fallback edges, though modelling could replace them with real
ones.
