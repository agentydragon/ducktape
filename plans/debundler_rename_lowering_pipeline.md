# Debundler: global plan → check → execute pipeline for renames and lowerings

Generalises <devinfra/js/debundle/TODO.md>'s "Rename pipeline:
collect → validate → execute _once_" entry to cover the whole
lowering surface, not just renames. The end state is a single
**Plan** value collected across all lowering contributors, a
single **Check** phase that proves the plan internally consistent
and realizable, and a single **Execute** phase that mutates the
AST + side tables together.

Today the lowering pipeline is structured as a chain of mutually
oblivious passes that each mutate the AST and a partial set of
side tables in place. Bugs of the shape "pass A renamed it, pass
B keyed off the old name" (PR #1627, #1631) recur because no pass
sees the final post-rename world before mutating; every consumer
either races or compensates defensively. Lowering operations
beyond renames have the same hazard — a declaration move
performed in `materialize_logical_chunk` mutates the body before
some downstream consumer (export plan, runtime imports rewrite,
report emission) has agreed on what the body looks like.

This plan describes the single seam those operations all flow
through.

## Status

| Step | Status      | Notes                                                                                                                                                                                                          |
| ---- | ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1    | DONE        | Inventory contributors and consumers — captured in "Inventory before starting" section below                                                                                                                   |
| 2    | DONE        | `LoweringOp { Rename, MoveBinding }` + `LoweringPlan` + submit/readback API in `lowering/lowering_plan.rs` (13 unit tests)                                                                                     |
| 3a   | DONE        | `seal()` cross-op rename/move coherence check (4 unit tests)                                                                                                                                                   |
| 3b   | NOT STARTED | Plumb `OwnerGraph` + initial `Partition` into `LoweringPlan::new`; build post-execute partition at seal time; call `realizability::check_realizability` and surface its rejection                              |
| 4a   | DONE        | `apply_chunk_renames` VisitMut in `lowering/lowering_execute.rs` (4 unit tests; handles `Scope::Chunk` only — scope-aware rename application lands with Phase 6)                                               |
| 4b   | NOT STARTED | `apply_moves` body-splitter that consumes `plan.move_index`, walks declarators per `Id`, splits multi-binding statements across destinations (mirrors today's `remaining_item_after_selection`)                |
| 4c   | NOT STARTED | Side-table rebuilds during execute (`runtime_imports`, `referenced_idents`, export tables, source-map fragments, cross-module binding indexes), all keyed on original `Id`                                     |
| 4d   | NOT STARTED | `LoweringOp { RewriteImportSpecifier, AddExport, ReorderHoists }` variants — added when their first contributor is migrated                                                                                    |
| 5    | DONE        | `validate_chunk_renames_via_plan` replaces the inline validation loop in `lower.rs:167-238`; AST mutation still runs through `IdentifierRenamer` (executor migration follows Phase 7)                          |
| 6    | NOT STARTED | Migrate naturalizer `collect_*` funcs + `RenameAndShorthandNaturalizer` onto Plan; thread `Scope::Function(_)` through the execute visitor                                                                     |
| 7a   | DONE        | Entry-body call site (`lower.rs:209`) on Plan via `disambiguate_import_locals_via_plan`; `imports_cross.rs` cross-module + residual-entry sites still on legacy (Phase 7b/c)                                   |
| 7b   | DONE        | `cross_module_imports_for_plan` routes through `disambiguate_import_locals_via_plan`; legacy `disambiguate_import_locals` deleted (now dead)                                                                   |
| 7c   | NOT STARTED | Migrate `residual_entry_imports_for_moved_body` (imports_cross.rs:72) onto Plan; thread chunk_top_level_mark + plan through `cross_module_imports_for_plan`/`residual_entry_imports_for_moved_body` signatures |
| 8    | NOT STARTED | Migrate `materialize_logical_chunk` declaration moves into Phase A/B `MoveBinding` ops; retire the implicit `binding_assignment`-driven body splitting in favour of `apply_moves`                              |
| 9    | NOT STARTED | Retire `plan_module_reference_needs` reverse-lookup (#1631 fix) — dead once renames + runtime imports share one final mapping                                                                                  |
| 10   | NOT STARTED | Retire `normalize_relative_module_specifier` defensive use (#1627 fix); the helper can stay as a path utility but the call site in module-specifier rewriting drops it                                         |

## Problem shape

The two reference bug families:

- **#1627** (`object_literal_import_collapse_test`) —
  `normalize_relative_module_specifier` was added at the usage
  site to compensate for a path string that had been touched up
  by an earlier pass. The fix lives where the bug surfaced, not
  where the cause is.
- **#1631** (`object_literal_return_shorthand_drops_import_test`)
  — `plan_module_reference_needs` performs a reverse lookup
  through chunk renames so the import-planning pass can find the
  pre-rename binding name in the `runtime_imports` map after the
  naturalizer has already renamed it in the AST. Same shape: one
  pass renamed, another pass kept the old key, the seam between
  them is a defensive bridge.

Both fixes are correct under the _current_ architecture, but
they're load-bearing — removing either of them silently
reintroduces the bug. The architecture itself does not stop the
_next_ such bug from being introduced; the next consumer added
between rename and emit doesn't know to bridge, and the failure
mode is "valid-looking JS that runs differently than the spec
intended."

Beyond renames, the same shape exists for non-rename lowerings:

- `materialize_logical_chunk` moves declarations between modules
  in-place during the collect phase (per "Structural mutations
  during COLLECT" open question in TODO.md).
- `disambiguate_import_locals` mints fresh local names for
  cross-module imports while iterating the body — every prior
  pass's view of "what names are taken" can be stale.
- `chunk_renames` are applied per-module by `lower_chunk` but
  consulted in a flat HashMap by other consumers; the
  module-vs-body partitioning happens implicitly via "did this
  binding land in `binding_assignment`?" rather than declaratively.

## Design

### Operation algebra

A `LoweringOp` is one of:

```rust
enum LoweringOp {
    Rename {
        scope: Scope,
        original: Id,
        name: NamePolicy,
        reason: RenameReason,
        priority: Priority,
    },
    MoveBinding {
        id: Id,
        to: ModuleId,
        reason: MoveReason,
    },
    RewriteImportSpecifier {
        in_module: ModuleId,
        import_index: usize,
        specifier_index: usize,
        new_specifier: ImportSpecifier,
    },
    AddExport {
        in_module: ModuleId,
        binding: Id,
        public_name: NamePolicy,
    },
    ReorderHoists {
        in_module: ModuleId,
        new_order: Vec<usize>,
    },
}

/// How a name-producing op (Rename, AddExport) wants the plan to
/// pick the final name when its preferred name is taken.
enum NamePolicy {
    /// Use this exact name; fail submission if the name is
    /// already taken in the relevant scope. The right policy for
    /// spec-driven and naturalizer-driven renames where the
    /// target name carries semantic meaning.
    Required(Atom),
    /// Prefer this name; if it's taken, fall back to `name_1`,
    /// `name_2`, … until a free name is found. The right policy
    /// for collision-resolution renames (today's
    /// `disambiguate_import_locals`).
    MintOrSuffix(Atom),
}
```

Every op carries:

- **Scope** — function / module / chunk / cross-chunk. Cross-scope
  writes are rejected by the plan builder at submit time, not at
  seal time. (Tightest scope wins; e.g. a function-local rename
  cannot be observed at chunk scope.)
- **Reason** — string-typed enum naming the contributor
  (`return_alias`, `shorthand_collapse`, `import_disambiguation`,
  `explicit_spec`, `chunk_rename`, `collision_resolution`,
  `materializer_move`, …). Surfaced in conflict diagnostics and
  validator errors.
- **Priority** — ordinal: `Explicit > Collision > ImportInduced >
Heuristic`. Same-priority disagreements panic at seal (per the
  PR2 default in TODO.md); different-priority disagreements
  silently win for the higher priority.

### Plan builder

```rust
struct LoweringPlan { /* opaque */ }

impl LoweringPlan {
    /// Build an empty plan seeded with the pre-existing name pool.
    /// `occupied_by_scope` enumerates every identifier already used
    /// in the pristine bodies — built once by walking each module
    /// body before any contributor runs. `is_name_taken` queries
    /// this pool *in addition to* names committed by submitted ops,
    /// so `MintOrSuffix` never picks a name that collides with an
    /// existing local. Each scope's set is the union of names taken
    /// in that scope and every enclosing scope (renaming an inner
    /// local to a name held by an outer one would silently shadow
    /// the outer reference).
    fn new(occupied_by_scope: HashMap<Scope, HashSet<Atom>>) -> Self;

    /// Submit an op. The `NamePolicy` carried by the op handles
    /// new-name conflicts (Required → ValidationError if taken;
    /// MintOrSuffix retries with numeric suffixes). The
    /// `SubmitPolicy` argument handles the orthogonal "(scope,
    /// original) is already claimed by some other op" axis.
    ///
    /// Returns `Err(ValidationError)` for spec-author-facing
    /// problems (conflicting renames at the same priority, Required
    /// name already taken, invalid JS identifier). Contributors
    /// batch errors into a Vec and return them all to the
    /// orchestrator, which unions them at end-of-phase; this
    /// preserves today's good UX where `lower.rs:199` collects
    /// every chunk_renames violation into one round-trip.
    fn submit(
        &mut self,
        op: LoweringOp,
        on_conflict: SubmitPolicy,
    ) -> Result<SubmitOutcome, ValidationError>;

    // Read-only queries — used during stratified submission to
    // compose later phases against earlier ones. Cheap; no
    // mutation, no allocation.
    //
    // `is_name_taken(scope, name)` walks the lexical chain: a
    // submitted Rename in an inner function whose `new_name` is
    // already held by an outer module-level binding *is* a
    // collision, because the renamed local would shadow the outer
    // reference and silently break it. Contributors don't need to
    // walk the chain themselves.
    fn is_claimed(&self, scope: Scope, original: &Id) -> Option<Priority>;
    fn is_name_taken(&self, scope: Scope, name: &Atom) -> bool;
    fn modules(&self) -> &[ModuleId];
    fn residual(&self) -> ModuleId;

    fn seal(self) -> Result<CheckedPlan, ValidationError>;
}

/// How to handle "(scope, original) already claimed by a
/// previously submitted op at any priority". Orthogonal to
/// NamePolicy, which handles "new_name is taken".
enum SubmitPolicy {
    /// Error on conflict, citing both contributors' `reason`s.
    /// Right for spec-driven ops where a conflict is a bug.
    Fail,
    /// Drop this op if `(scope, original)` is already claimed at
    /// strictly higher priority; same-priority disagreement still
    /// errors (always — regardless of policy). Right for heuristic
    /// contributors that should defer to the spec.
    SkipIfClaimed,
}

enum SubmitOutcome {
    Accepted { final_op: LoweringOp },
    Skipped { reason: ConflictReason },
}
```

Two orthogonal conflict axes, two enums:

| Conflict                            | Handled by                 |
| ----------------------------------- | -------------------------- |
| `(scope, original)` already claimed | `SubmitPolicy` (on submit) |
| op's preferred `new_name` is taken  | `NamePolicy` (on op)       |

`Accepted { final_op }` returns the op the Plan actually committed
to — important for `MintOrSuffix`, where `final_op.new_name` may
differ from the submitted base (e.g. `foo` → `foo_3`). Contributors
that care about the final name (the import-disambiguation pass
needs it to update its own bookkeeping for the next specifier) read
it off the outcome.

### Stratified submission

Submission proceeds in priority-ordered phases. Each phase sees the
plan state contributed by every earlier phase via the readback
queries; phases cannot see siblings within the same phase.

| Phase | Priority      | Contributors                                                   | Typical policy mix                                    |
| ----- | ------------- | -------------------------------------------------------------- | ----------------------------------------------------- |
| A     | Explicit      | spec `chunk_renames`, spec-driven `MoveBinding`s, `AddExport`s | `(NamePolicy::Required, SubmitPolicy::Fail)`          |
| B     | Explicit      | materializer structural moves (no new names)                   | `SubmitPolicy::Fail`                                  |
| C     | ImportInduced | `disambiguate_import_locals` / `_residual_entry_`              | `(NamePolicy::MintOrSuffix, SubmitPolicy::Fail)`      |
| D     | Heuristic     | naturalizer collectors                                         | `(NamePolicy::Required, SubmitPolicy::SkipIfClaimed)` |
| E     | Collision     | residual collision sweep (today's `_N` mint pass)              | `(NamePolicy::MintOrSuffix, SubmitPolicy::Fail)`      |
| —     | —             | `seal()`                                                       | —                                                     |

Within a phase, same-priority disagreements always error
regardless of `SubmitPolicy`, so the _outcome_ is
submission-order-independent — any ordering produces the same
plan or the same error. The one caveat: `MintOrSuffix` produces
order-sensitive _names_ within a phase (two ops submitting
`MintOrSuffix("x")` for different originals — first gets `x`,
second gets `x_1`). The orchestrator iterates contributors in a
deterministic order within each phase (alphabetical by
contributor `reason`, or some other fixed sort) so the final
plan is reproducible from inputs.

Between phases, readback is the only channel — no contributor
mutates earlier ops, retracts them, or observes contributors at
its own priority. That keeps the fixpoint trivial (no iteration,
just topological order over the priority strata).

The materializer-vs-spec split into phases A and B is deliberate:
spec moves are authoritative; materialiser structural moves run
second so they can read back which bindings the spec has already
claimed and route only the residual. If the spec submits
`MoveBinding { id, to: M1 }` and the materialiser submits
`MoveBinding { id, to: M2 }` for the same binding in phase B,
that's `SubmitPolicy::Fail` returning a `ValidationError` citing
both `reason`s — exactly the contradiction this design exists to
surface.

### Check phase

Most local invariants are caught at submit time, so they fail
loudly at the contributor's site (with a stack pointing at the
specific submit call) rather than at seal:

| Invariant                                                | Caught                                                                 |
| -------------------------------------------------------- | ---------------------------------------------------------------------- |
| `NamePolicy`'s preferred `Atom` is a valid JS identifier | Op constructor (`NamePolicy::Required(atom)?` / `MintOrSuffix(atom)?`) |
| `(scope, original)` already claimed                      | `submit` per `SubmitPolicy`                                            |
| New name taken in scope                                  | `submit` per op's `NamePolicy`                                         |
| Same-priority disagreement on `(scope, original)`        | `submit` (always errors, regardless of `SubmitPolicy`)                 |
| Two `MoveBinding`s route the same `id` to different `to` | `submit` per `SubmitPolicy`                                            |

What's left for `seal()` to check is genuinely cross-op:

1. **Cross-op coherence.** Renames + moves agree: if a binding's
   declaration is moved to module M, every `Rename` op on that
   binding has scope ⊆ M. (Currently expressed as the
   `binding_assignment.contains_key(top_level_id(name, …))`
   filter in `lower.rs:117-124` and `lower.rs:183-188`. Can't be
   caught at submit time because the move and the rename might be
   submitted in either order — phase-A spec move first, then
   phase-D heuristic rename, or vice versa for materializer
   moves.)
2. **Realizability preservation.** No combination of `MoveBinding`
   ops produces a partition that the realizability gate
   (`realizability.rs`) would reject. Today the partition is
   fixed before lowering and the gate runs once; in the new
   pipeline the moves _are_ the partition refinement, so the
   gate runs over the post-execute partition before execute
   begins. Can't be caught per-op because realizability is a
   cycle-level property — one move alone never violates it.
3. **Reorder/move consistency.** A `ReorderHoists` op's
   `new_order` references only ordinals whose declared bindings'
   `MoveBinding`s (if any) all land in the same module. Same
   reason as cross-op coherence — depends on the cumulative move
   plan.

Output is `CheckedPlan { ops, rename_index, move_index,
specifier_index, export_index }` — immutable, indexed for O(1)
lookup by `(scope, original)` and binding `Id`.

### Execute phase

A single visitor walks each module body once. At every node:

- For each top-level declarator, the visitor consults `move_index`
  per declared binding `Id`. If every binding in a single
  `ModuleItem` routes to the same destination, the whole item
  moves; if a multi-binding statement (`let a, b, c`) splits
  across destinations, the visitor produces one per-destination
  `ModuleItem` with the appropriate subset of declarators (same
  shape as today's `remaining_item_after_selection`, now driven
  by the move index instead of `binding_assignment`).
- Every `Ident` it visits is rewritten through `rename_index` at
  the current scope.
- Every import declaration is rewritten through `specifier_index`.
- Side tables (`runtime_imports`, `referenced_idents`, export
  tables, source-map fragments, cross-module binding indexes) are
  rebuilt from the post-execute AST in the same pass, keyed on
  the original `Id` (which is hygiene-preserving, so pre- and
  post-execute lookups agree by construction).

Direct consequence: there is no "pre-rename name" and "post-rename
name" world. The Plan owns the mapping; everything consults the
Plan; the only post-execute thing that exists is the new world.

### Migration

Phases 5-10 land one consumer at a time. Each phase deletes the
matching defensive bridge in the old code path and adds one
contributor to the plan. The plan is sealed and executed at the
existing `lower_chunk` seam; passes that haven't migrated yet
keep mutating in place against the post-execute AST. Final phase
makes Plan submission the only way to mutate.

Order chosen for risk gradient (cheap-and-clear first):

1. **chunk_renames** (Phase 5). Today's chunk_renames map is
   already in the right shape — a flat `HashMap<binding, new>`
   per chunk — and the consumer (`lower_chunk`) is the only
   place it's read. Easiest migration; proves out the
   `Rename + Execute` half of the plan.
2. **Naturalizer heuristics** (Phase 6). The five
   `collect_naturalization_renames_*` functions already build a
   `BTreeMap<String, String>`. Re-route into Plan; remove
   `RenameAndShorthandNaturalizer`'s in-place mutation.
3. **`disambiguate_import_locals`** (Phase 7). Currently mints
   names while iterating; switch to a name-mint helper on the
   Plan builder. `disambiguate_residual_entry_import_locals`
   follows.
4. **`materialize_logical_chunk` declaration moves** (Phase 8).
   Largest change; lets us pick the "all structural moves
   pre-COLLECT" answer from the PR2 open question by making
   moves first-class Plan ops.
5. **Retire defensive patches** (Phases 9-10). With the plan as
   the single source of truth, `plan_module_reference_needs`'s
   reverse lookup is dead, and `normalize_relative_module_specifier`
   can rejoin the path-building step rather than living as a
   sanitizer.

## Inventory before starting (Phase 1)

This list is the ground truth that the design above is built
against; capturing it explicitly so the design and the actual
implementation don't drift.

**Contributors today:**

- `lowering/chunk_renames.rs::collect_chunk_renames` — explicit
  spec-driven renames, per-chunk.
- `lowering/naturalize.rs::collect_return_object_alias_renames`,
  `collect_constructor_assignment_renames`,
  `collect_naturalization_renames_from_{function, class, pattern,
item, expr}` — heuristic renames inferred from object literal
  shapes and return statements, per-function and per-module.
- `lowering/visitors.rs::RenameAndShorthandNaturalizer` and
  `naturalize_object_{pattern, literal}_shorthand` — in-place
  shorthand collapse applied alongside heuristic renames.
- `lowering/util.rs::disambiguate_import_locals`,
  `disambiguate_residual_entry_import_locals` — collision
  resolution between cross-module imports and existing locals.
- `lowering/materialize.rs::materialize_logical_chunk` —
  declaration moves between modules.
- `lowering/lower.rs::lower_chunk` — applies `chunk_renames` and
  enforces collision/identifier-validity constraints; current
  conflict-diagnosis seam.

**Consumers today (each currently reads off the AST or a
pre-rename fact map):**

- `lowering/plan_references.rs::plan_module_reference_needs` —
  reverse-lookup bridge for #1631.
- `lowering/imports_cross.rs::collect_entry_exports_by_original_local`
  — keyed on pre-rename local names.
- `lowering/runtime_imports.rs` — `runtime_imports` is keyed on
  the binding atom; reads happen post-rename in some passes,
  pre-rename in others.
- `lowering/exports.rs` — export tables computed from chunk_renames.
- `lowering/rewrite_runtime.rs::rewrite_runtime_sources_for_target`
  — path string rewrite that intersects with #1627's
  `normalize_relative_module_specifier`.
- `lowering/body_facts.rs::collect_module_body_facts` — body-fact
  collection that runs against post-move bodies; needs to agree
  with the materializer's view.

## Open design questions

Already pinned in TODO.md's "RenameLedger (PR2) open questions"
section, restated here so the implementation phase doesn't need
to re-derive them:

1. **Same-priority heuristic conflict policy.** Panic at submit,
   citing both contributors by `reason`. Silent suppression is
   the trap this pipeline closes. (Moved from seal-time to
   submit-time once the stratified-phases model landed —
   `SubmitPolicy::SkipIfClaimed` only suppresses strictly higher
   priority claims, never same-priority.)
2. **Disambiguation naming scheme.** Phase 7 unifies on `_N`
   suffixing for `NamePolicy::MintOrSuffix`. `mint_fresh_local_name`
   in `lowering/util.rs` switched from `$N` to `_N` to match —
   that's a wire-shape change for chunks whose import
   disambiguation currently emits `foo$1`-style names (none of the
   existing tests pin the suffix character; e2e fixtures don't
   exercise import-disambiguation collisions). Readability
   heuristics (e.g. `name_from_module`) are a follow-up Plan
   _contributor_ submitting `NamePolicy::Required("name_from_module")`
   at heuristic priority — not a change to the `MintOrSuffix`
   suffixer itself.
3. **Structural mutations during COLLECT.** Pick "all structural
   moves pre-COLLECT" — `MoveBinding` ops are submitted in phases A/B
   before any rename collection in phases C-E runs. Type-level
   barrier: rename contributors take `&Module`, not `&mut Module`.
   Pragmatic alternative (no structural moves between seal and
   execute) reserved as fallback if the reordering proves too
   invasive.

New questions surfaced by the broader scope:

4. **Source-map fragment ownership.** Source-map fragments today
   are emitted by `materialize.rs` as the AST is mutated. In the
   new pipeline they should be emitted by Execute, keyed on
   original `Id`. Open: does the source-map index need a Plan
   op (`AttachSourceMap`) or can it be a side product of Execute?
   Default: side product, since source-map fragments are derived,
   not contributed.
5. **Realizability gate placement.** The current realizability
   gate runs over the spec-fixed partition before lowering. In
   the new pipeline, `MoveBinding` ops change the partition during
   Plan collection. Open: does the gate run inside `seal()` (so
   check #2 above is real), or does Plan keep an invariant that
   no `MoveBinding` may produce an unrealizable partition (enforced
   per-op at submit time)? Default: gate runs inside `seal()`,
   per-op enforcement is too local to catch combinations.
6. **Plan reuse across chunks.** `materialize_logical_modules`
   parallelises over chunks with `par_iter`. Each chunk gets its
   own Plan instance; cross-chunk renames (if any are introduced
   later) cannot be expressed without a second-tier Plan over
   the bundle. Defer until cross-chunk renaming becomes a
   concrete need.

## Verification

The architecture is correct when:

1. `plan_module_reference_needs` reverse-lookup is deleted, the
   debundler still builds, and the #1631 test still passes.
2. `normalize_relative_module_specifier` is deleted at the usage
   site (allowed to live as a path utility), and the #1627 test
   still passes.
3. A new test that contributes two same-priority heuristic
   renames at the same `(scope, original)` rejects at seal with
   both `reason`s named.
4. A new test that submits a `MoveBinding` op producing an
   unrealizable partition rejects at seal with the
   realizability gate's normal error.
5. Tana e2e debundle (`tana/re/web/spec:debundle_*` in
   gaffer-private) emits a wire-identical bundle before and
   after the migration, modulo non-load-bearing ordering of
   rename application.

## Out of scope

- Cross-chunk rename coordination (deferred; see open question 6).
- Source-map fidelity improvements beyond preserving today's
  shape.
- Migrating fact collection (`facts.rs` `StatementFactsCollector`)
  onto the plan. Fact collection runs before any rename, against
  the pristine AST, and stays there — the plan operates _after_
  fact collection.
- Replacing the `chunk_renames` spec input shape. The spec
  remains the same; only the internal pipeline that consumes it
  changes.
