# Debundler rename + lowering pipeline

Reference for the `LoweringPlan` collect → check → execute seam
in `devinfra/js/debundle/lowering/`. Each emitted JS file gets
its own per-file plan; `lower_chunk` constructs three (chunk
residual-side, entry-body-local disambig, per-moved-module) so
each application target gets a name pool / submission set
distinct from the others.

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
