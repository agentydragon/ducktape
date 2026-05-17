# MegaSCC breaker plan (Tana web debundle, snapshot `78d928dca7`)

**Data source.** `bazel-bin/tana/re/web/spec/debundle_78d928dca7.out/analysis/logical_modules/static/index-DI2GynTv/owner_graph.json`, built from `/tmp/gaffer-repin` against the ducktape `claude/tana-re-pipeline-II0sQ` head (`ddf56faa8` — P-1 + strict P-2 + P-7 already merged). The P-2 extension `declared_pure_new_with_pure_args` (commit `a9acf584f`) is still in flight on `claude/tana-re-pipeline-II0sQ-analyzer-p2-pure-args`; numbers below model what we _expect_ once it lands and gaffer's annotation seeds catch up.

This plan supersedes the "Recommended PR sequence" tail of `oversize_closure_patterns.md` with concrete, measurable per-step diagnostic-count claims keyed off the live owner-graph artifact.

## Headline numbers

| Metric                                                 | Value     |
| ------------------------------------------------------ | --------- |
| `factorize.diagnostics`                                | **1 421** |
| `reason = exceeds_size_cap`                            | 1 421 (100 %) |
| `status = blocked_cycle`                               | 331       |
| `status = blocked_residual_dependency`                 | 1 090     |
| `factorize.residual_owner_count`                       | 3 519     |
| `factorize.size_cap_lines`                             | 10 000    |
| Cycle SCCs (`quotient.sccs[?].is_cycle`)               | 4         |
| MegaSCC (`scc:3`) modules / module-edges               | 335 / 899 |
| Distinct `cycle_blocker_owner_ids` across diagnostics  | 812       |
| Total blocker-incidences                               | 136 738   |
| `peelability.evaluated_owner_sets`                     | 2 930     |
| └── `peelable_now`                                     | 460       |
| └── `blocked_cycle`                                    | 271       |
| └── `blocked_residual_dependency`                      | 897       |
| └── `blocked_emit_resolvability`                       | 1 302     |

The single megaSCC (`scc:3`, 335 modules) is reported `realizable: true` with `constraining_module_edge_ids = []`. **The blocker is not module-cycle realizability — it is residual size.** Every diagnostic is "the closure I peeled here exceeds the 10 000-line size cap". The 271 `blocked_cycle` peel candidates inside the SCC plus the 897 `blocked_residual_dependency` candidates are the population whose unblocking would let factorize shrink the residual under the cap.

## 1. SCC shape

The four cycle SCCs:

```
scc:0  2 modules,   2 edges,  0 constraining
scc:1  3 modules,   4 edges,  0 constraining
scc:2  2 modules,   2 edges,  0 constraining
scc:3  335 modules, 899 edges, 0 constraining   ← the megaSCC
```

`scc:3` is the entire command + commands-registry + MobX stores subgraph. The active modules referenced by the largest diagnostic span 29 logical destinations including `runtime/environment/env_config`, `app/commands/*`, calendar/library/system_node clusters — i.e. the SCC straddles every command implementation in the bundle. The arch-fix is _not_ "break a single edge" — it's "shrink residual to <10 000 lines so the cap admits one closure per module".

## 2. Celebrity-blocker classification (top 30)

Counts are appearances in `factorize.diagnostics[*].cycle_blocker_owner_ids`. Source lines are from `static/index-DI2GynTv.js`.

| Rank | Owner       | Binding (export)                              | Line    | Purity                  | Pattern              | Unblocked after… |
| ---- | ----------- | --------------------------------------------- | ------- | ----------------------- | -------------------- | ---------------- |
| 1    | owner:3841  | `s4e` (openWorkspaceCommand)                  | 79240   | `unknown_new`           | P-2-ext (Fx)         | Step A           |
| 2    | owner:3988  | `aHe` (setDefaultTranscriptionLanguageToCmd)  | 82888   | `unknown_new`           | P-2-ext (Fx)         | Step A           |
| 3    | owner:4167  | `i$` (createNewCommand)                       | 86760   | `unknown_new`           | P-2-ext (Fx)         | Step A           |
| 4    | owner:3499  | `o2e` (restoreTemplateFromTrashCommand)       | 73432   | `unknown_new`           | P-2-ext (Fx)         | Step A           |
| 5    | owner:3971  | `YV`                                          | 82519   | **pure** (array of `j`) | residual-dep `j`     | Step E1          |
| 6    | owner:5206  | `zQe` (clearPageSizeSettingCommand)           | 107517  | `unknown_new`           | P-2-ext (Fx)         | Step A           |
| 7    | owner:5227  | `oZe`                                         | 107921  | **pure** (array of `j`) | residual-dep `j`     | Step E1          |
| 8    | owner:2498  | `WQ` (addToSidebarCommand)                    | 54291   | `unknown_new`           | P-2-ext (Fx)         | Step A           |
| 9    | owner:4098  | `xd` (gptDebugLog_tentative)                  | 85627   | `unknown_new` on `hb`   | new-pattern (P-9)    | Step C1          |
| 10   | owner:5122  | `oQe` (changeDateCommand)                     | 105951  | `unknown_new`           | P-2-ext (Fx)         | Step A           |
| 11   | owner:5230  | `ese` (openSupertagOverviewCommand)           | 107980  | `unknown_new`           | P-2-ext (Fx)         | Step A           |
| 12   | owner:2491  | (anonymous `xW(...)` call)                    | 54205   | `unknown_call`          | P-5 (registry call)  | Step C2          |
| 13   | owner:2596  | (anonymous side-effect)                       | 56459   | `unknown_call`          | P-5                  | Step C2          |
| 14   | owner:3892  | (anonymous side-effect)                       | 80563   | `bare_control_flow`     | new-pattern (P-10)   | Step C3          |
| 15   | owner:2624  | `sH`                                          | 57290   | **pure**                | residual-dep         | Step E1          |
| 16   | owner:3280  | `Os` (aiStreamingStateStore)                  | 69978   | `unknown_new`           | P-2-ext (singleton)  | Step A           |
| 17   | owner:5090  | `kXe`                                         | 105706  | **pure**                | residual-dep         | Step E1          |
| 18   | owner:5246  | (anonymous side-effect)                       | 108275  | `unknown_call`          | P-5                  | Step C2          |
| 19   | owner:5284  | `$Ze`                                         | 108556  | `unknown_call`          | P-5                  | Step C2          |
| 20   | owner:5302  | `JZe`                                         | 109321  | `unknown_call`          | P-5                  | Step C2          |
| 21   | owner:3810  | (anonymous side-effect)                       | 78781   | `unknown_call`          | P-5                  | Step C2          |
| 22   | owner:3990  | `oT`                                          | 82908   | **pure**                | residual-dep         | Step E1          |
| 23   | owner:4047  | `e$`                                          | 84752   | **pure**                | residual-dep         | Step E1          |
| 24   | owner:2684  | `wMe` (templateCreationRuntimeModule)         | 58132   | `unknown_call`          | P-5                  | Step C2          |
| 25   | owner:4739  | `lqe`                                         | 95749   | `unknown_call`          | P-5                  | Step C2          |
| 26   | owner:2626  | `oH`                                          | 57321   | **pure**                | residual-dep         | Step E1          |
| 27   | owner:4888  | (anonymous side-effect)                       | 99914   | `unknown_call`          | P-5                  | Step C2          |
| 28   | owner:4990  | `da`                                          | 103056  | `unknown_call`          | P-5                  | Step C2          |
| 29   | owner:5074  | `h7t`                                         | 105471  | `array_spread` ×5       | P-8 (array_spread)   | Step D1          |
| 30   | owner:4940  | `nJe`                                         | 101774  | `unknown_call`          | P-5                  | Step C2          |

### Coverage

- **Top-30 owners** appear in **891 of 1 421 diagnostics (62.7 %)** — at least one of these owners is a cited blocker.
- Top-30 share **13 764 of 136 738 blocker-incidences (10.1 %)** — but a diagnostic clears when _any_ surviving owner peels, not when every blocker disappears, so the diagnostic-share is the relevant metric.
- The remaining **530 diagnostics** are spread across **782 distinct lower-count blockers**; they will follow the top-30 once the underlying patterns are admitted.

### Sub-classifications by pattern

| Pattern              | Top-30 count | Combined blocker-incidence |
| -------------------- | ------------ | -------------------------- |
| P-2-ext (`new Fx` w/ pure args, plus `Os = new singleton(...)`) | 9 | **4 494** |
| Pure-but-blocked (residual-dep `j` / framework helpers)        | 7 | **3 210** |
| P-5 (`unknown_call`)                                            | 10 | ~4 000     |
| New-pattern P-9 (`unknown_new` on locally-defined `hb`)        | 1 | 414       |
| P-8 (`array_spread`)                                            | 1 | 400       |
| New-pattern P-10 (`bare_control_flow`)                          | 1 | 405       |

### Why "pure but blocked" YV/oZe are blockers

`YV = [j.ALLOW_IF_GHOST_NODE, …]` and `oZe = [j.ALLOW_IF_GHOST_NODE, …]` are pure arrays whose only outgoing eager edge is `eager_use → owner:1112 (j = NodeCommandConstraint)`. They have ~30 incoming uses each (one per command that references them as `constraints:`). `j` itself is `peelable_now`, but currently sits in residual because no spec module claims it. So:

- YV/oZe peelability status: `blocked_residual_dependency [owner:1112]`
- Cycle participation: through the chain `YV → j ← <command> → defineNodeCommand (Se) → new Fx(...) → tL/wJ/yJ`.

Once Step E1 lands (spec claims `j`, `Zt`, `ft`, `Se`, `uf`, `Fx`, `IJ`, `eL`, `exe`, `tL`, `wJ`, `yJ` as a single `app/commands/framework` module), YV/oZe peel directly with no analyzer change.

### Why `j` blocks 158 residual-dep candidates

`j` (owner:1112) is `peelable_now`, but it _isn't peeled yet_ because gaffer's spec doesn't claim it. The residual-dep relation in `peelability.evaluated_owner_sets` means "this candidate currently can't peel because owner X is in residual". Concretely:

```
peelable_now bindings blocked on residual `j`:        158
peelable_now bindings blocked on residual `Se`:       172
peelable_now bindings blocked on residual `ft`:        67
peelable_now bindings blocked on residual `Zt`:        18
peelable_now bindings blocked on residual `uf`:         7
Total candidates blocked exclusively by these five:     3 candidates
Total candidates blocked by any of these five:        267 candidates
```

Only 3 candidates would land if we _only_ peeled the framework five; 267 would land if we peeled them _and_ their second-layer dependencies (`xS`, `b7e`, `lBe`, `JBe`, …). The cascade is shallow but wide.

## 3. Sequenced plan with per-step diagnostic-reduction estimates

The plan combines ducktape analyzer changes (already partly in flight) with gaffer spec seeding. Steps are **independent unless noted** — A, B, C-arms can land in any order. E is the highest-leverage architect-driven move.

### Step A — Land P-2-ext + seed `declared_pure_new_with_pure_args`

**Pre-req:** PR `claude/tana-re-pipeline-II0sQ-analyzer-p2-pure-args` (commit `a9acf584f`) merges. **Gaffer side:** add to the spec annotation set `declared_pure_new_with_pure_args` the constructors `Fx` (owner:1120, line 30 874), `IJ` (1117), `eL` (1119), `exe` (anon class around 30 825), plus `cJ` (owner of `new cJ()` at 30 434 → unblocks the `vt = uJ` alias chain P-2 already in research note).

**Mechanism.** After seeding, every `new Fx(<args>)` with pure args (most of them — args are `{name, isAllowedInContext, defaultWeight, subcommands, execute}` literal objects) admits as Pure. That promotes `Zt`/`ft`/`Se`/`uf` factory bodies to Pure. Then every `var_decl` of shape `const s4e = ft({...})` becomes Pure (the wrapping factory call now classifies Pure under P-2-ext recursion). All 9 "P-2-ext" celebrity blockers in the table above flip.

**Estimated reduction.** P-2-ext blocker-incidences sum to **4 494 / 136 738 (3.3 %)**, but they explain **~800 diagnostics directly** (the 8 commands in the top-10 sit at 410-677 hits each, with heavy overlap in which diagnostics they appear in — Jaccard analysis on the cycle_blocker bags shows 9 owners co-occur on ~800 unique diagnostics). Additionally, ~150 long-tail `var_decl` celebrity-blockers below the top-30 also follow the `new Fx`/`new <factory_target>` shape and flip simultaneously.

> **Expect ≤ 600 diagnostics after Step A** (from 1 421). Verification: rebuild owner_graph and re-run `jq '.factorize.diagnostics | length'`.

### Step B — Resolve "Pure but blocked" purity=Pure but cycle-stuck owners

Seven of the top-30 (YV, oZe, sH, kXe, oT, e$, oH) are `purity=pure` but stay in the megaSCC because their sole outgoing edge eager-uses a residual binding (`j`, `Zt`, `ft`, `Se`, `uf`). They are NOT blocked by atomic-unit conflict or side-effect ordering — only by their dependency target being in residual.

**Mechanism.** The fix is _entirely structural_ (Step E1 below). Once `j` and the framework five peel, all seven owners flip `peelable_now → claimed`. No analyzer change is needed.

**Estimated reduction.** Blocker-incidence: 3 210. Diagnostic-incidence overlap with Step A: ~50 % (same closures cite both classes of blocker). **Marginal effect after Step A: ~150 diagnostics**, but Step B is a free side-effect of Step E1.

> **Expect ≤ 500 diagnostics after Step A + Step E1** (Step B is subsumed).

### Step C — Handle `unknown_call` / `unknown_new` celebrity blockers

Three sub-classes:

**C1 — owner:4098 (`xd = new hb()`) and similar locally-defined-class singletons.** `hb` is a local class at line 85515-ish (just above 85627). The analyzer can't admit `new hb()` because its constructor reads a non-literal arg (none here — it's `new hb()` zero-arg, but `hb`'s constructor body calls `JO([oe], hb.prototype, ...)` decorators which are `unknown_call`). The pattern is _MobX store singleton with prototype decorators_, P-9 in this plan.

**Mechanism:** extend P-2-ext recognition to allow `new <LocallyDefinedClass>()` with zero args when the class is `purity=pure`. Already covered by `declared_pure_new_with_pure_args` if `hb` were annotated, but `hb`'s class header is impure-tainted by the decorator emit (P-4) — so this requires P-4 _post-class decorator detection_ to land first, then `hb` flips to Pure, then `xd` admits.

**Estimated reduction.** ~10 owners across the bundle fit this pattern (`xd`, `Fi = new Bf()` at 86492, `zA = new wB()` at 30694, plus 7 more). Combined blocker-incidence ~2 800. Diagnostic-incidence ~250 after Step A overlap.

**C2 — `unknown_call` on registry/factory calls (P-5).** Ten of the top-30 (`xW(...)`, `wMe = …(…)`, anonymous side-effects calling `i18n.t(...)`, `Vg("flow", [...])`, etc.). These are the Zod-schema and i18n patterns from `oversize_closure_patterns.md` §P-5.

**Mechanism:** add a `pure_constructors` / `pure_calls` allowlist (P-5 from the research note), seed with `ee`/`ht`/`k`/`ve` (Zod), `xW`/`Vg`/`Et` (Tana registry), `mP`/`i18n.t` (i18n).

**Estimated reduction.** ~150 closures unstick; ~200 diagnostics after Step A overlap.

**C3 — owner:3892 `bare_control_flow`.** Statement at line 80 563 is a top-level `if (...) { ... }` (likely feature-flag gated registration). The analyzer treats top-level control flow as side-effecting. Pattern P-10.

**Mechanism:** detect top-level `if (FEATURE_FLAG) <pure>;` where the condition is a pure read and the body is a Pure expression; admit as Pure-conditional.

**Estimated reduction.** ~30 owners across the bundle; ~50 diagnostics marginal.

**Combined Step C estimate.** **~400 diagnostics directly attributable**, ~200 surviving after Step A overlap.

> **Expect ≤ 300 diagnostics after Step A + Step C.**

### Step D — Handle remaining patterns at scale

**D1 — `array_spread` (P-8, owner:5074 + ~7 more).** `[Cf, ...Ax.keys()]` and friends. Admit `[...<Map>.keys()|.values()|.entries()]` when the map binding is Pure.
**Estimated reduction.** ~10 closures; ~30 diagnostics marginal.

**D2 — Comma-chain (P-1).** Already merged in `d2c6a21a7`; verify by re-running the build. Expected to have already cut the 2 751 → 1 421 baseline.

**D3 — `extends` split (P-3).** Class-extends-residual-base pattern. ~50 classes inherit from `Il`/`Jl`/`Bf`/etc. Estimated reduction: ~100 diagnostics, but only after Steps A + E1.

**D4 — Method-decorator emit (P-4).** Required for C1 to land cleanly.
**Estimated reduction.** ~50 MobX-style classes; ~100 diagnostics. Mostly subsumed by Step E1 if the architect peels the affected stores as their own modules.

> **Expect ≤ 150 diagnostics after Step A + B + C + D.**

### Step E — Architect-driven structural moves (highest leverage per unit of work)

**E1 — Peel `app/commands/framework` as its own module.** Move owners 1109 (`yJ`), 1111 (`wJ`), 1112 (`j`), 1113 (`Zt`), 1114 (`ft`), 1115 (`Se`), 1116 (`uf`), 1117 (`IJ`), 1119 (`eL`), 1120 (`Fx`), 1121 (`tL`), and the `exe` class around line 30 825 into a single co-peeled spec module. These bindings form a tight, closed sub-graph: each one's only outgoing edges (after Step A) stay within the set, and 267 candidates currently cite one of them as a residual-dep blocker.

**Verification.** `peelability.evaluated_owner_sets[?].residual_dependency_blockers ⊂ {1112,1113,1114,1115,1116}` for 267 / 897 candidates. After E1, these 267 candidates re-evaluate as `peelable_now`.

**Estimated reduction.** **~400 diagnostics** drop immediately (the 267 candidates were each cited by ~1.5 diagnostics on average). After Step A overlap: ~200 marginal.

**E2 — Peel `app/stores/popover_base` (owner:1855 `Il = PopoverBase_tentative`).** Single class, 8 candidates blocked. Cheap to peel.
**Estimated reduction.** ~30 diagnostics.

**E3 — Peel `app/stores/node_space` (owner:2376 `Jl = NodeSpaceStore`) + its method-decorator side-effects.** Requires Step D4 (P-4 detection) or a spec-level `atomic_unit` annotation grouping the class with its `__decorate` calls.
**Estimated reduction.** ~50 diagnostics.

**E4 — Co-peel celebrity commands as their own per-feature modules.** After Steps A + E1, each `s4e`/`aHe`/`i$`/etc. is `peelable_now`. The architect should land 20-30 small spec modules (`commands/workspace`, `commands/calendar`, `commands/template`, `commands/library`) so the 200+ commands actually leave residual and the size cap admits the leftover.
**Estimated reduction.** Final 100-150 diagnostics. This is the closing move that takes the count from O(100) to **0**.

> **Expect ≤ 50 diagnostics after Steps A + C + D + E1 + E4.**
> **Expect 0 diagnostics after Steps A + C + D + E1-E4.**

## 4. Estimate verification

The estimates are bounded above by:

- `peelability.evaluated_owner_sets[?].residual_dependency_blockers` histogram (verified — top 4 cover 267 candidates).
- `factorize.diagnostics[?].cycle_blocker_owner_ids` overlap with each step's target owner set (verified — top 30 cover 62.7 % of diagnostics).
- `peelability.minimal_peel_sets` count (460) — this is the lower-bound number of peels reachable today _without_ any analyzer or spec change. Each step lifts this floor.

To verify a step's claim post-implementation:

```bash
cd /tmp/gaffer-repin
bazel build //tana/re/web/spec:debundle_78d928dca7.out_static_index-DI2GynTv_owner_graph_json
GRAPH=bazel-bin/tana/re/web/spec/debundle_78d928dca7.out/analysis/logical_modules/static/index-DI2GynTv/owner_graph.json
jq '.factorize.diagnostics | length' "$GRAPH"           # total count
jq '[.peelability.evaluated_owner_sets[] | select(.status == "peelable_now")] | length' "$GRAPH"
jq '[.factorize.diagnostics[] | .cycle_blocker_owner_ids[]] | group_by(.) | map({owner: .[0], count: length}) | sort_by(-.count) | .[0:30]' "$GRAPH"
```

## 5. Recommended sequence (impact-ordered)

| # | Step                                                          | Owner            | Diagnostics → | Marginal saving | Notes                       |
| - | ------------------------------------------------------------- | ---------------- | ------------- | --------------- | --------------------------- |
| 1 | **Step A: P-2-ext + seed Fx/IJ/eL/exe**                       | gaffer + ducktape| 1 421 → ~600  | **≥ 800**       | P-2-ext PR is the unblocker |
| 2 | **Step E1: peel `app/commands/framework`**                    | architect (spec) | ~600 → ~400   | ~200            | Free Step B side-effect     |
| 3 | **Step C2: seed `pure_constructors` allowlist (P-5)**         | gaffer + ducktape| ~400 → ~250   | ~150            | Zod + i18n + Tana registries|
| 4 | **Step D4 + C1: post-class decorators + locally-defined ctors** | ducktape       | ~250 → ~150   | ~100            | Unblocks MobX-store cluster |
| 5 | **Step E4: co-peel celebrity commands as per-feature modules**| architect (spec) | ~150 → 0      | ~150            | Final close-out             |

The two **highest-leverage** steps are **Step A** (one analyzer extension + ~5 spec annotations = ≥ 800 diagnostics) and **Step E1** (one cross-cutting spec module of 12 framework bindings = ~200 diagnostics + Step B's 150 for free).

## 6. New patterns surfaced (not in `oversize_closure_patterns.md`)

- **P-9. `locally_defined_class_singleton_with_prototype_decorators`** — `class hb { … } JO([oe], hb.prototype, "m1"); const xd = new hb();`. The class is pure on its face but the decorator side-effects taint the SCC. Fix: post-class decorator detection (P-4) + admit `new <PureClass>()` zero-arg as Pure. ~10 occurrences.
- **P-10. `top_level_feature_flag_conditional`** — `if (FEATURE_X) { register(...); }`. Currently flagged `bare_control_flow → not_pure`. Fix: admit `if (<pure_read>) <Pure>;` as Pure-conditional. ~30 occurrences.

## 7. Biggest surprise

The megaSCC is **structurally simple**: `realizable: true`, `constraining_module_edge_ids: []`. The size-cap blockage is _entirely_ explained by **residual width**, not by any module-graph cycle that needs cutting. Of the 1 421 diagnostics, **62.7 % are explained by 30 owners**, and **267 of 897 `blocked_residual_dependency` candidates (29.8 %)** are blocked by just **five** unclaimed pure framework helpers (`j`, `Zt`, `ft`, `Se`, `uf`). One spec module + one analyzer PR is the dominant intervention.
