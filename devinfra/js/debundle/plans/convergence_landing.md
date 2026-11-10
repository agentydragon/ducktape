# Convergence landing plan

How the semantic-convergence refactor reaches `devel`. The destination is
<target_architecture.md>; the gate migration is <translation_validation_gate.md>;
the interpreter migration is <abstract_interpreter.md>. This file owns the landing
order, the debt that must close before the refactor counts as done, and the exit
gates. It burns down as slices land and is deleted with the last one.

## Where the work is

Draft [#14552](https://github.com/agentydragon/ducktape/pull/14552) is a
**reference implementation, not a merge candidate**. It carries the whole flipped
design as one squashed commit above `devel`: +113k/-21k lines across 231 files,
with the effect-based relocation verdict authoritative from its first commit.
Its hosted history: the best full-suite result was 201 passing / 51 failing test
targets (run `42382520793`, 2026-11-07); the last run that reached tests was
63 / 193 with a shared resolver assertion in 192 of the 193 failures (run
`42510569601`); every later head, including `ee4eb4ab`, fails to build. No head
of the branch has ever been green.

`devel` is the integration branch. It is green, keeps the legacy realizability
gate authoritative, computes the effect assessment as a report, and already holds
roughly 160 slices peeled from this work during 2026-11-04 to 2026-11-08.

Rules that follow from this:

- Nothing else is squashed into #14552. New work branches from `devel`, takes
  code from #14552 by reference, and lands as its own PR.
- The flip (effect verdict decides, legacy gate becomes a diagnostic) is one
  small PR near the end (<translation_validation_gate.md> § Migration step 3),
  not the base of a stack.
- Local `bbr`/`bazelisk` runs are the normal iteration loop. Hosted CI is the
  landing check, not the only compiler.

## Slice contract

Every PR in this plan:

1. Adds one capability or removes one duplicate, and **deletes the decision path
   it supersedes** in the same PR (legacy evaluator arm, recognizer, row
   interpretation, join). A slice that only adds is incomplete.
   **Exception:** a path the legacy gate still reads cannot be deleted while
   that gate is authoritative (deleting it loses legacy acceptances; routing
   the legacy gate through the kernel is a second evaluator). Such a path
   stays, with a `CLEANUP` tombstone gated on L4e that names the kernel rule
   replacing it, and the slice adds exactly one kernel rule: no second
   kernel-path copy beside the legacy one. The PR lists each exempted path.
2. Is validated through the CLI: the fixture runs original and emitted programs
   under Node, the candidate has `swapped_pairs > 0` where the slice claims a
   relocation, and the `DEBUNDLE_TV_ORACLE` legacy/effect comparison is on.
3. Uses one of two test forms, both through the CLI. A **positive**: this
   bundle and spec debundle to this layout, and the original and emitted
   programs give the same Node (or browser) output. A **refusal**: this spec is
   rejected with this typed reason, and the test shows why the split is unsafe
   (run the emitted program and show the divergence) or names the declared
   refusal (A1/A3/A5, top-level await, cross-module rebinding). A split that is
   valid but not yet provable gets the positive test wrapped in the
   `e2e/support` xfail helper: it runs the positive assertion, requires today's
   typed refusal, and fails with "capability landed, convert to a positive"
   once the assertion passes. A test asserting that the debundler lacks a
   capability does not land. Exact `emitted_order` arrays, example-row ranking
   and receipt span pins are change detectors (<../../../STYLE.md> § Testing)
   and do not land either.
4. Has no `#[cfg(test)]` hooks or forced-target overrides in production modules
   (`AGENTS.md` § Testing forbids stage mocking). Test needs are met by fixtures.
5. Keeps the legacy gate authoritative until the flip PR. A refusal the legacy
   gate would not have made is a regression unless the PR proves the acceptance
   was unsafe with a Node divergence.
6. Keeps every `devel` test or replaces it with a stronger one in the forms of
   item 3. A refusal that becomes an acceptance needs a relocation with Node
   parity; an acceptance that becomes a refusal needs a Node divergence. The
   tests #14552 dropped are listed in § Test debt; the slice that owns them
   restores the behavior they protected in those forms.
7. Stays reviewable: target under ~3k changed lines. The kernel slices in Wave 2
   cannot meet that and say so in their description.
8. Runs the canonical-spec check (§ below) when it changes any acceptance,
   placement, emitted order or emitted bytes: always the public half, and the
   private half where the session can reach it. Mechanical refactors,
   report-only additions and test-only splits skip it and say so.

## Canonical-spec check

Two halves: a public corpus every session can run, and the private corpus.

### Public: Excalidraw

`//devinfra/js/debundle/excalidraw:canonical_summary` reduces the Excalidraw
corpus's accepted tree (<../excalidraw/README.md>) to one JSON file: per chunk,
the ordinary gate verdict and the translation-validation verdict and counts;
per emitted file, its SHA-256. Build it at the slice's head and at its `devel`
base, and diff the two. Cloud sessions use `bbr`; `bbapi artifact download`
fetches the output by name from each invocation that `bbr` prints:

```sh
TASK=${TMPDIR:-/tmp}/agents/<date>/<slug>
git worktree add --detach "$TASK/base" "$(git merge-base HEAD origin/devel)"
bbr build //devinfra/js/debundle/excalidraw:canonical_summary   # head; note the invocation
(cd "$TASK/base" && bbr build //devinfra/js/debundle/excalidraw:canonical_summary)
bbapi artifact download <head-invocation> canonical_summary.json -o "$TASK/head.json"
bbapi artifact download <base-invocation> canonical_summary.json -o "$TASK/base.json"
diff "$TASK/base.json" "$TASK/head.json"
```

A changed verdict, translation-validation count or emitted hash is judged by
item 8's rule, as for the private corpus. The corpus's `test_debundle_tree`,
`spec_output_test` and `test_smoke` run in ordinary CI whenever a slice changes
the analyzer or pipeline, and also block a slice that breaks acceptance or
browser loading. It covers one esbuild-minified program and does not replace
the private half.

### Private

The private corpus lives in the sibling repository `../gaffer-private`, which
pins Ducktape by commit (`DUCKTAPE_COMMIT` in its `MODULE.bazel`; procedure in
its `DUCKTAPE_PINNING.md`). Sessions without ordinary network access (cloud
agents: GitHub source-archive fetches are refused, and a local module override
cannot reach a remote runner) do not run this check. The orchestrator runs it
after merge by repinning gaffer-private to the merged `devel` commit and
building the five targets with `bbr --env=REPO_TOKEN="$REPO_TOKEN" build`; a
regression found there is reverted or fixed forward before the next repin.
Batch at most one Wave 3 slice family per repin so a regression has one
suspect. To test a Ducktape worktree without repinning, from a host with
network access:

1. In the Ducktape worktree, apply `patches/ducktape_cargo_source_labels.patch`
   from gaffer-private (it rebinds the persisted Rust source-patch label);
   reverse it before pushing the worktree's branch.
2. From gaffer-private, build the accepted products through RBE with the
   override:

   ```sh
   bazelisk build --config=rbe \
     //claude/cli/re/2.1.292:debundle \
     //claude/web/re/2026-10-23-dd67f2f258:debundle \
     //tana/re/web/78d928dca7:debundle_passing \
     //tana/re/desktop/spec:debundle_v1_515_0 \
     //platform_sessions/quest/re/2026-11-08:debundle \
     --override_module=ducktape=/path/to/ducktape-worktree
   ```

3. Compare each product's ordinary gate and translation-validation summaries
   and emitted-file inventory against the same build at the slice's `devel`
   base. A new rejection, a changed emitted order or changed emitted bytes is a
   regression unless the PR shows the old acceptance was unsafe with a Node
   divergence. Run the focused runtime targets named in `DUCKTAPE_PINNING.md`
   when the slice touches browser or vendor loading.

Not every spec passes cleanly at the current pin; that is the recorded state,
not a target. A slice may update a gaffer-private spec for compatibility with a
changed contract (new premise key, renamed field, explicit claim replacing a
removed implicit rule) in a paired gaffer-private PR, and must not make a spec
pass by weakening an obligation. A slice does not have to make a spec more
correct than it is today.

## Debt inventory

Everything below exists at `ee4eb4ab`. "Keep" means port as a slice; "redo"
means the idea lands but not this code; "drop" means do not port. Rows leave
when their disposition has landed on `devel`.

| Area                       | Finding at `ee4eb4ab`                                                                                                                                                                                                                                                                                              | Disposition                                                                                                                                                               |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Evaluator count            | The shared kernel is added beside every path it should replace: `evaluate_legacy_compat` (devel's `evaluate_transfer` tail, grown), the flat callable lane `invoke_with_cells`, row-driven `execute_native_call_transfer`, string-matched `private_frozen_namespace` (deleted by Wave 3 slice 1).                  | Each Wave 2/3 slice deletes the path it covers; `legacy/` tombstones name the last caller.                                                                                |
| Giant dual-mode functions  | `execute_call_transfer_inner` (1,458 lines, operands-supplied and self-evaluating modes), `execute_owner` (1,206 lines), `LocalContinuation::step` (1,520 lines).                                                                                                                                                  | Redo: split by mode and by phase; functions under ~300 lines.                                                                                                             |
| Coverage notions           | `Outcome` carries an optimistic `complete: true` beside the pessimistic coverage ledger; forgetting `lose_precision` certifies.                                                                                                                                                                                    | Redo with the flip (L4d): delete `complete`; the ledger is the only coverage authority (AR1).                                                                             |
| Heap models                | New may-state `ObjectSurfaceStore` plus the old literal heap (`mark_unknown_enumerability`); `DefineProperties` commit invalidates different attributes in application and strict provider.                                                                                                                        | Keep `object_surface` + `descriptor_transfer` + `native_operation`; delete the literal-heap flags; one commit.                                                            |
| Provider execution         | Left after slice 3 PR 1: the reference head runs unsummarized provider bodies in the caller world (`provider_activation`, strict `private_heap`, `provider_graph`) from a provider program registry devel does not have.                                                                                           | Keep summary replay as substitution (landed); run unsummarized bodies through the provider adapter once the registry lands (<kernel_design.md> § Provider execution).     |
| Native rows                | Row `Mutation` interpretation, plan/commit path and syntactic recognizer all decide `defineProperty`.                                                                                                                                                                                                              | Keep plan/commit; delete the other two.                                                                                                                                   |
| Gate authority             | Legacy verdict reduced to a " Legacy SCC comparison" diagnostic string; `preparation_readiness` decides on effects + placement only. Plans and TODO still say the legacy gate is authoritative.                                                                                                                    | Flip lands last as its own PR; until then devel's authority stands.                                                                                                       |
| Over-restriction           | `module_initialization::preserve_initialization` returns `UnsupportedCandidateGraph` for any multi-chunk or vendor-transformed candidate with required cells.                                                                                                                                                      | Must be removed before the flip; otherwise multi-chunk specs regress.                                                                                                     |
| Second ESM loader          | `lowering/module_initialization.rs` has its own `evaluation_order`/`resolve_requests`.                                                                                                                                                                                                                             | Redo on `EsmImportOrder`/`esm_simulator` (one loader model).                                                                                                              |
| Final-file checkers        | `final_global_resolution.rs` is a genuinely new obligation (emitted output must not capture a source global). `final_source_correspondence.rs` (4.2k lines) re-derives what typed lowering, pruning and export validators establish, with three independent output AST walkers.                                    | Keep global resolution (report-only first). Redo correspondence from the declarative vendor plan plus one indexed walk of the output, after showing which bug it catches. |
| Report seal                | `FinalCandidateOutputSeal` plus runtime self-hash of the report's serde JSON and of `logical_modules`, beside the type-state `ValidatedPlan`.                                                                                                                                                                      | Redo: type-state only; the serializable report is a projection of a non-serializable verdict.                                                                             |
| Receipt plumbing           | Vendor rewrites and pruning emit `CandidateRewriteReceipt`s (~2.7k lines) consumed only by the correspondence checker.                                                                                                                                                                                             | Drop unless the slimmed checker needs them; derive routes from the plan.                                                                                                  |
| Debug-formatted identities | `context_identity`, `candidate_assignment_identity` hash `{:?}` output; `GateRejected` diagnostics are `{:?}` dumps that tests match.                                                                                                                                                                              | Redo as typed identities and typed rejection payloads.                                                                                                                    |
| Saved analysis             | Typed anchors and `SavedChunk`/`SavedModuleCell` identities are sound. The provider-proof sidecars (~2.6k lines) are restored, validated, then **replaced by a full recomputation** on load; `conditional_bodies` serialize twice; `#[serde(default)]` legacy `Option`s are unreachable past the engine-hash gate. | Keep codec identities and anchors. Hold sidecars until a load path consumes them. Delete dead defaults. Move `CallableSummary` off `derive(Serialize)` to a DTO.          |
| CLI contract               | `claims_checked`, `needs_full_assessment`, verified writes require `--analysis`, edit gate deleted.                                                                                                                                                                                                                | Keep the vocabulary. Graph-only writes stay allowed with a warning until the flip.                                                                                        |
| Tests                      | Two single-target files of 10,960 and 8,395 lines; receipt helpers duplicated in four files; 26 staged tests execute nothing; ~30 stderr substring checks; `assert_potential_suppression_rejection` pins internal cell taxonomy.                                                                                   | Split by capability into `*_test.rs` targets; helpers in `e2e/support`; see § Test debt.                                                                                  |
| Process                    | 67 rounds published as a single squashed commit with hosted CI as the only compiler; commit history survives only in a local clone and bundles.                                                                                                                                                                    | Small PRs with history; local builds; no squash-only publication.                                                                                                         |

## Target code shape

`facts/sync_effects/` becomes, in this order of extraction:

| Module                                | Owns                                                                                                                        |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `kernel/{continuation,completion}.rs` | `LocalContinuation`, `Step`, `Completion`, `TransferLedger`; the five narrow context traits.                                |
| `alternatives/`                       | `Family`, the leaves of a forked body, and the join of their path states; refusal is a conservative outcome.                |
| `world/`                              | `SemanticWorldData`, `ObjectSurfaceStore`, `DescriptorHeap`, lexical cells; `World::join`; no literal-heap flags.           |
| `adapters/{application,provider}.rs`  | The two execution modes, implementing only the narrow traits; strict-provider private heap is the provider adapter's world. |
| `native/`                             | Builtin table rows → one `plan`/`commit` path.                                                                              |
| `roots/`                              | `execute_owner` as named phases: operands, body, assignment/export, owner finalization.                                     |
| `legacy/`                             | Whatever is not yet deleted, each item with a `CLEANUP` tombstone naming its last caller.                                   |

`execution_index.rs` stops nesting the interpreter module (AR4). Whether
`kernel`/`world` become separate Bazel crates is decided after the moves, when the
dependency direction is visible.

## Dependency graph and waves

Slices are independent within a wave unless a `needs` is stated. `src` names
the #14552 code to port by reference (tag `debundle-reference-14552`; the PR
is closed, never merged).

**Waves 0 and 1: landed** on `devel` 2026-11-09 as #15046 (ownership-edit
assessment vocabulary), #15047 (typed completion), #15048 (explicit claims
replace rebind folding; companion spec change gaffer-private#1070), #15049
(e2e split by capability), #15050 (preflight and emission draft without
authority change), #15051 (report-only final global resolution), #15052
(own-property surface, descriptor heap, one native plan/commit path).
Withdrawn: L0a (the reference head's `lowering/` is a half-applied refactor;
Wave 3 is ordered from the hosted inventory below instead) and L1e (devel has
no module-cell identities yet; the saved identities land with Wave 3 slice 6).
Cleanups landed alongside: #15055,
#15056, #15058, #15060, #15061, #15062, #15063, #15066, #15071, #15074.

**Wave 2: the kernel.** Large by nature. Its landed contracts (five context
traits, returned observations, one `World`, forks and their join) are in
<../docs/design.md> § The synchronous execution kernel; what the fork leaves
out and the provider adapter remain in <kernel_design.md>.
`World` is the per-prefix execution state the interpreter already carries
(today one 44-field struct plus the strict provider's private copy), not a
new analysis; precision stays bounded by the existing finite-height widening
and budgets. Each Wave 2 slice adds a cost gate to its canonical-spec check:
Tana Web (`//tana/re/web/78d928dca7:debundle_passing`) analysis wall time and
peak RSS from the override build, branch versus base. The reference head's
cold Tana analysis was about 320 seconds; a slice that regresses wall time or
RSS by more than 10 percent is profiled with `perf` before it lands, and a
persistent regression means the structure, not a budget knob, is wrong.

- L2a. **Landed as #15069** (2026-11-09): `kernel/{context,continuation,
driver,frame}.rs`, both adapters on the five traits, `local_transfer.rs`
  deleted. Tana cost gate: +3.3 % wall, +0.4 % RSS. Kept with tombstones gated
  on L2b: the cell-owning `BindingContext` methods, `settled_dependencies`,
  closure creation, the effect sink; gated on L2c, now on L4e: `AllocationContext` and the
  value bridge; the `invoke_with_cells` flat lane with its four gates
  (`requires_keyed_collection_new` needs an application keyed-collection lane).
  `Fork`/`ForkPolicy` landed with the conditional/logical IR slice (§ IR
  growth).
- L2b. **PR 1 landed as #15090**: `world/` (one `World` for both adapters,
  no `Deref`, copy-on-write), the `RefCell` ledgers as the threaded
  `Observations` struct, the L2a sink methods deleted. `World::join` landed
  staged as #15091; `alternatives/` calls it since the conditional/logical
  IR slice (§ IR growth). PR 2 closes L2a's L2b-gated
  tombstones: lexical cells and closure creation are kernel rules
  (`kernel/{cells,closure}.rs`; adapters supply `cell_policy`/`cell_effect`,
  `capture_policy` and `admit_closure`), closures are
  `AbstractValue::Closure(HeapAddress, CapturedContext)` in both adapters, and
  the prefix prerequisites live in `World.prerequisites`. needs L2a.
- L2c. **Landed** as #15116 (`adapters/application.rs`), the provider-adapter
  move (`adapters/provider.rs`), #15125
  (`execute_owner` as `roots/` phases) and the call-transfer PR, which split
  `execute_call_transfer` by mode: the operands-supplied mode is in
  `roots/call_operands.rs`, and the self-evaluating mode and the evaluator are
  in `legacy/`. The `AllocationContext`/`LocalValueContext` tombstones now
  wait for L4e, which deletes their last non-kernel implementor. Deleting
  `complete: bool` moved to L4d (below): it is legacy-gate authority, and
  swapping its readers is the authority flip.
- L2d. **Landed as #15073** (2026-11-09): the six `#[path]` children of
  `execution_index.rs` are sibling modules; `ExecutionIndex` fields the
  interpreter reads are `pub(super)`; devel had no provider program registry.

**IR growth.** Devel's ordered `LocalBody` IR covers fewer constructs than the
reference's (no regions, `if`/`try`/`catch` bindings, argument spread,
optional calls, class definitions and their static members, `new` outside a
root initializer, or `Function.prototype.call`/`apply`). Several kernel rules
in <kernel_design.md> (regions, catch cells) have nothing to apply to until
the construct is lowered. Each construct family lands as its own slice inside
Wave 3, ordered with the coverage producer that needs it: lowering in
`local_operations.rs`, the kernel rule, the e2e positive with Node parity, and
the deletion of whatever recognizer or flat-lane gate the construct retires.

Landed: conditional (`?:`) and logical (`&&`, `||`, `??`) operators, the
first fork producer, with `ForkPolicy`, `LocalSuspension::Fork` and
`alternatives/`; the flat value lane's result-only conditional arm
(`allocation_transfer`) is deleted. What the fork still leaves out is in
<kernel_design.md> § Forks and joins: what is left.

**Wave 3: coverage producers.** Each one capability, each deleting its
recognizer, each with a positive relocation, its opposing controls and the
legacy/effect oracle. Ordering input: the 51 failing test targets at the
reference head `ceaa8713` (hosted run `42385432562`), 235 failing cases, of
which 208 are acceptances the effect verdict refused (`BlockedByEffect` 191,
`BlockedByEffectAndPlacement` 15, `BlockedByPlacement` 1), 16 are rejection
controls it accepted, and 11 are report-shape assertions; none panicked. The
group assignment comes from test contracts, not observed root causes, so a
slice may recover more or fewer cases than its row says.

| Order | Slice                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | Cases | Targets owned / touched | Needs                                   |
| ----: | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----: | ----------------------: | --------------------------------------- |
|     1 | **Landed**: descriptor and native rows, frozen-namespace recognizer deleted. Left: descriptor carriers escaping to later calls (L4c)                                                                                                                                                                                                                                                                                                                                                                                                           |    38 |                 10 / 11 | L1b, L2a                                |
|     2 | **Iteration rows landed** (`native/array_iteration.rs`: `forEach`, `map`, `filter`, `reduce` with an initial value, non-capturing callbacks; legacy executor tombstoned for L4e); array spread landed (`IterateSpread`). Left: `new Set(iterable)` (`set_iterable_reentry`) and the multi-declarator `stable_array_spread` owner, kernel rules only with both recognizers tombstoned (§ Slice contract item 1); the early-exit rows and `flatMap`, which need a kernel-issued callback call so each result forks; capturing callbacks (row 12) |    23 |                   7 / 7 | L1b, L2a; forks for the early-exit rows |
|     3 | **PR 1 landed**: the provider adapter runs every provider body, and summarized imported calls are substituted with an effect receipt. Left: unsummarized provider bodies in the caller transfer (needs the provider program registry, slice 6), imported construction (class TDZ, slice 6), then callbacks and retained factories as PR 2 (`imported_callback_timing_test` spans both)                                                                                                                                                         |    77 |                 11 / 16 | L2a, L2b, L2d                           |
|     4 | Host timing and reentry (`bundle_owned_globals_test` carries six accepted controls that must stay refusals)                                                                                                                                                                                                                                                                                                                                                                                                                                    |    26 |                   2 / 5 | L2b, L1f                                |
|     5 | **Root `new` landed** (`kernel/construct.rs`). Left: `new` in callable bodies, constructors reading argument properties, class definitions with static members                                                                                                                                                                                                                                                                                                                                                                                 |    15 |                   2 / 5 | L2a; parallel to 4                      |
|     6 | Qualified module cells and provider initialization, with the withdrawn L1e saved identities                                                                                                                                                                                                                                                                                                                                                                                                                                                    |     7 |                   1 / 1 | L2a, L2b; required by L4a               |
|     7 | Purity-annotation authority (`purity_test`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |    16 |                   1 / 1 | L2c                                     |
|     8 | Downstream naming, CLI and audit fixtures that fail at the gate before their feature runs                                                                                                                                                                                                                                                                                                                                                                                                                                                      |    26 |                   8 / 8 | re-measure after 1–5; not a slice       |

Rows 9–12 have no red target at the reference head. They come from the L4c
audit's missing-coverage classes (<../debug/2026_11_09_l4c_discrepancy_audit.md>
§ Uncovered havoc, by missing rule) and from slice 2's finding, and they block
the flip as much as rows 2–7 do. "Evaluations" is the audit's e2e count
touching the rule.

| Order | Slice                                                                                                                                                                                                | Evaluations | Needs                 |
| ----: | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------: | --------------------- |
|     9 | Anonymous-statement owner coverage (`unsupported_owner_kind`: global publications, heap writes, bare assignments; <abstract_interpreter.md> step 7)                                                  |         110 | none                  |
|    10 | **Landed** for local calls with arguments, IIFEs and comma initializers: an unsealed kernel attempt rewinds to the flat lane. Left: destructured call results (pattern initialization in the kernel) |          75 | none                  |
|    11 | Non-invoking literal coverage                                                                                                                                                                        |  unmeasured | measure in next audit |
|    12 | Call-effect coverage for callees with live captures (`(x) => out.push(x)` with `out` local gets open coverage today), which rows 2 and 3 need for callbacks                                          |  unmeasured | forks                 |

**Order from here.** Wave 3 is ordered by what the canonical inputs need, not
by reference-head case counts: at `fa49ae5ed1` the flip would refuse Tana Web,
Tana Desktop, 14 Claude CLI chunks and 22 Claude Web chunks. The first job
after the current batch (rows 2 and 3 in flight) is the canonical conflict
export aggregated by missing rule for those chunks (L4c); rows 4–7 and 9–12
are then dispatched in the order that export gives, widest canonical recovery
first. Until it exists, rows 9 and 10 go next: they have no dependencies and
the largest e2e reach.

Saved-analysis parity has no red target; every slice keeps the direct/saved
parity tests green.

**Wave 4: gate.**

- L4a. `module_initialization.rs` on `EsmImportOrder`; remove
  `UnsupportedCandidateGraph` for multi-chunk and vendor candidates. needs Wave 3
  module cells.
- L4b. Slimmed correspondence checker from the declarative vendor plan and one
  indexed output walk, report-only, with the bug it catches named in the PR.
  needs L1f.
- L4c. Discrepancy audit (<translation_validation_gate.md> § Migration
  step 1). First pass on `fa49ae5ed1`:
  <../debug/2026_11_09_l4c_discrepancy_audit.md>. It found no unsafe legacy
  acceptance and no gate defect. Every legacy-accepted / effect-refused case
  is uncovered havoc. Rerun with `--test_env=DEBUNDLE_TV_AUDIT=1` after
  each Wave 3 slice. Left: the canonical conflict export, aggregated by
  missing rule per rejected chunk (it orders the rest of Wave 3).
- L4d. **Flip**: `preparation_readiness`, `emit_reports_and_resolve_gate`,
  `ensure_candidate_emittable_for_write`, plus the README/design/SPEC text that
  #14552 already drafted. needs L4a–L4c green.

  The flip also deletes `Outcome.complete`. Legacy enumeration (`complete`)
  and kernel receipts (a closed `TransferLedger`) are different facts, and the
  gate decides which one is authority. Today most legacy-executed owners are
  `complete` with an open ledger, so swapping the readers before the
  discrepancy audit (L4c) would refuse them. The readers become:

  | Reader kind                                                                                        | Reads                                                                                                                      |
  | -------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
  | may certify, seal or retire (`roots/{assignment,finalize}.rs`, `Outcome` barriers in the executor) | `ledger.has_closed_effects() && precision_losses.is_empty()`; `unknown_call` becomes a `Havoc` cell in the ledger          |
  | child completeness passed upward (`inherit_completeness` sites)                                    | `child.ledger.closed_effect_dependencies()`; parents already compose the child ledger                                      |
  | setters (`complete: true` literals, `lose_precision`)                                              | deleted; each site composes `EffectCoverageFragment::default()` where it does not already                                  |
  | export (`roots/body.rs`, `outcome.effects.complete = outcome.complete`)                            | the one writer of `SynchronousCalls.complete`, computed as `ledger.has_closed_effects()`, behind an engine-hash codec bump |

  Premise policy (decided 2026-11-09): a `commuting_statements` premise is
  an axiom. A swapped pair it covers is discharged even when either statement
  is havoc, so the decision's `Incomplete { premise_covered: true }` arm
  (`realizability/effect_translation_validation.rs`) becomes an acceptance
  that keeps the premise in the audit list. Uncovered havoc pairs stay
  `incomplete`.

  Two preconditions:
  - The terminal-call stop in the application adapter clears `complete`
    without recording a precision loss. It needs a typed loss first.
  - The test that a refused body leaves the root ledger open
    (`refused_body_after_a_covered_call_leaves_the_root_binding_effects_open`)
    must stay green.

- L4e. Delete the legacy gate, `legacy/`, dead `#[serde(default)]` options; move
  `CallableSummary` to a DTO; graduate contracts into `SPEC.md` and
  `docs/design.md`; delete this plan and the burned-down parts of the others.

**Interpreter split (parallel to Waves 3 and 4).** `interpreter.rs` is 12.5k
lines against the exit gate's 3k and grows with each slice that adds a rule
beside a legacy path. Mechanical moves only, one owner per PR, into the
§ Target code shape modules; no behavior change, so no canonical-spec check.
A slice never adds a new rule to `interpreter.rs`: it goes in `kernel/`,
`native/`, `roots/` or `alternatives/`.

**Wave 5.** Crate split if the dependency direction warrants it; real-profiler
performance pass on representative inputs (<analysis_performance.md>).

## Test debt

`ee4eb4ab` re-baselines `devel`'s expectations in both directions. Of the 50+
devel test functions that no longer exist under their name, about nine are
renames or splits with equal or stronger expectations, and the rest fall into
the groups below. Each group names the slice that restores the devel
expectation or proves the change.

**Devel refusals that became expected acceptances (~17).** A slice may keep
such a flip only with a relocation (`swapped_pairs > 0`) and Node parity, or by
showing the candidate order equals source order and the refusal signal is
retained in a typed field. Where the devel refusal pinned a missing capability
rather than an unsafe split, the slice replaces it with the xfail positive
form of slice-contract item 3 instead of restoring the refusal:

- Host-timing contracts in `reentry_free_operations_test.rs`: every exact Bun
  host-contract requirement around `AsyncLocalStorage` and `Promise.resolve`
  became "opt-in evidence"; several of the new acceptances reorder. One case's
  soundness rests on the pinned Node build not reading a getter argument. Wave 3
  host/reentry slice restores the requirements or proves each reorder.
- `native_slot_integrity_test.rs`: the eight `assert_refused` cases now accept
  under `no_reordering`; the refusal kind is asserted nowhere. Keep the
  no-reordering acceptance only beside a typed incomplete-native field.
- `finalization_registry_guards_keep_unsupported_calls_conservative`: the
  replacement accepts either gate outcome. Restore one explicit refusal per
  shadowed or aliased form.
- `export_availability_test.rs` (`namespace_import_stays_conservative`,
  `unproved_initializers_keep_conservative_callback_dependencies`): accepted
  with reordering while execution is incomplete. Needs a source-grounded proof
  or the refusal returns.
- Single-module authority gains without any relocation:
  `local_declaration_receipts_refuse_captured_cells_and_rebinding`,
  `numeric_local_updates_refuse_bigint_coercion_and_captured_cells`. Wave 3
  adds the relocation or restores the refusal.
- `without_the_premise_or_naming_another_family_map_stays_unknown`,
  `compound_call_root_keeps_its_unexecuted_remainder_fallback`: narrowed
  conservatism with parity kept; acceptable if the typed loss stays asserted.

**Devel Node-verified acceptances that became expected refusals (7).** Each is
either a deliberate stricter policy that needs a design-doc entry and a
`cross_destination_rebinds`-style typed reason, or a coverage regression:

- Deliberate, keep: automatic co-location of rebinding methods
  (`peeled_method_reassigning_top_level_let_executes_under_node`) and
  rebind-only atomic folding
  (`folds_unclaimed_assigner_with_extracted_mutable_binding`), both landing
  with L1c and documented in `docs/design.md`.
- Regressions until proved otherwise: `descriptor_capture_accepts_an_assignment_of_a_fresh_ordinary_source`,
  `selected_original_object_read_removes_the_heap_frontier_cycle`,
  `intact_object_read_has_an_independent_source_certificate`,
  `resolved_member_certifies_only_the_selected_relational_alias`,
  `inferred_and_selected_set_spread_bindings_survive_saved_analysis`. The devel
  acceptance stands until a Node divergence is shown (Wave 3 descriptor, native
  read and Set-spread slices).

**Deleted CLI-contract coverage.** `cli_mutation_outcome_test.rs` lost
`atom_split_reports_both_destinations_and_writes_the_same_artifact`,
`atom_split_also_writes_current_cycles_for_gate_queries`,
`completed_edit_results_replace_both_rejection_artifacts` and
`merge_cycle_report_drives_gate_list_then_a_passing_edit_clears_it`; the
replacements only assert that `cycles.json` and `atomic_group_conflicts.json`
are no longer written, so the rejection JSON contract (`rejection.kind`,
`blocking_sccs`, `cut`, `gate list` clearing) has no e2e coverage. L1g keeps
these tests until the flip PR deliberately retires that contract. The
graph-only `modules_*`/`bindings_*` acceptances that became
`needs_full_assessment` refusals are part of the flip, not L1g.

**Deleted structural controls to restore as-is:**
`direct_callee_heap_contents_win_over_assumed_path_in_both_orders`,
`borrowed_set_array_keeps_its_read_across_an_unrelated_constructor_barrier`,
`borrowed_and_static_member_sets_keep_their_existing_frontier`,
`ownership_subset_still_rejects_catchall_file`,
`strict_signatures_leave_asymmetric_lazy_eager_shape_for_final_gate`,
`empty_chunk_options_keep_their_analysis_projection_key`,
`early_destructuring_split_refusal_matches_with_saved_facts`,
`early_nested_var_split_refusal_matches_with_saved_facts`,
`an_initializer_export_gets_no_call_or_record_summary`.

**Renamed with tightened internal pins** (keep the behavior, drop the pins):
`hoisted_functions_have_no_evaluation_write_but_function_expressions_do`,
`local_declaration_operations_cover_private_cells_and_compose_receipts`,
`bounded_examples_prefer_modeled_rows_after_early_havoc_only_rows`, and the
`assert_candidate_schedule` exact `emitted_order` pins in the reentry tests.

**Headline witnesses.** Only two tests show `legacy_gate_accepts == false`
together with an accepted effect verdict and Node parity
(`translation_validation_differential_test.rs` error-preserving swap,
`native_operation_input_test.rs` seven-swap case). Each Wave 3 slice that
claims recovered acceptance adds one such witness for its family.

## Exit gates

The refactor is done when all of these hold on `devel`:

- One kernel: no `evaluate_legacy_compat`, no flat callable lane, one native
  plan/commit path, one `World::join`, one ESM loader model, one coverage ledger.
- One verdict: `run`, previews and every ownership write consume the same
  candidate assessment; the legacy gate code is deleted.
- Full hosted suite green with the oracle on; every classified discrepancy from
  L4c resolved or recorded as a sound refusal with its Node evidence.
- Canonical inputs: acceptance not below the pre-flip baseline except where a
  Node divergence shows the old acceptance was unsafe.
- No production module over ~3k lines, no function over ~300 lines in
  `facts/sync_effects/`; no `#[cfg(test)]` hooks in production.
- A real-profiler measurement on representative inputs within the
  <analysis_performance.md> budget.

## Delegation

- The orchestrator owns interfaces (the kernel context traits and `World`),
  reviews every diff for the deleted path and the restored tests, and runs the
  oracle on the result.
- Wave 1 and Wave 3 slices are bounded and verifiable: default worker tier
  (Luna `xhigh` in Codex terms; <../../../docs/agent_delegation/README.md>).
  Each dispatch names the immutable `devel` base, the #14552 source files, the
  path to delete, the test targets, the oracle check and the stop condition
  ("stop if the deletion needs a second evaluator; report instead").
- L4c and L4d, and any slice that changes a join in `alternatives/`, carry
  subtle invariants (join soundness, completion partitions, discrepancy
  classification): a stronger tier, and the orchestrator reviews the join and
  completion code line by line.
- One worker per slice in an isolated worktree; no shared branch; no squashing
  across slices.
