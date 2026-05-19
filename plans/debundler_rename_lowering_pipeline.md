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

| Step | Status      | Notes                                                      |
| ---- | ----------- | ---------------------------------------------------------- |
| 1    | NOT STARTED | Inventory contributors and consumers (Phase 1 below)       |
| 2    | NOT STARTED | Define `LoweringOp` algebra + `LoweringPlan` builder       |
| 3    | NOT STARTED | Implement `seal()` → `CheckedPlan` with conflict diagnosis |
| 4    | NOT STARTED | Single-pass `execute()` driving AST + side-table updates   |
| 5    | NOT STARTED | Migrate `chunk_renames` consumer onto plan; delete shim    |
| 6    | NOT STARTED | Migrate naturalizer collect-funcs onto plan                |
| 7    | NOT STARTED | Migrate `disambiguate_import_locals` onto plan             |
| 8    | NOT STARTED | Migrate `materialize_logical_chunk` declaration moves      |
| 9    | NOT STARTED | Retire `plan_module_reference_needs` reverse-lookup        |
| 10   | NOT STARTED | Retire `normalize_relative_module_specifier` defensive use |

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
        new_name: Atom,
        reason: RenameReason,
        priority: Priority,
    },
    MoveDecl {
        ordinal: usize,
        from: ModuleId,
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
        public_name: Atom,
    },
    ReorderHoists {
        in_module: ModuleId,
        new_order: Vec<usize>,
    },
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
struct LoweringPlan {
    ops: Vec<LoweringOp>,
    by_scope: HashMap<Scope, Vec<usize>>,
}

impl LoweringPlan {
    fn submit(&mut self, op: LoweringOp);
    fn seal(self) -> Result<CheckedPlan, ValidationError>;
}
```

Submission is order-independent. Contributors don't know about
each other. The buffer is the only writer; the AST and side
tables are unchanged through Phase 1.

### Check phase

`seal()` runs these checks in order:

1. **Identifier validity.** Every `new_name` is a valid JS
   identifier. Catches the `chunk_renames target … is not a valid
JS identifier` class of error currently raised inline in
   `lower_chunk` (`lower.rs:204-208`).
2. **Per-scope rename consistency.** No `(scope, original)`
   appears in two `Rename` ops with different `new_name` at the
   same priority. Same-priority disagreement = hard error citing
   both `reason`s.
3. **Rename target collision.** No two `Rename` ops in the same
   scope name the same `new_name` for different `original`s
   (`occupied` collision today, surfaced in `lower.rs:182`).
4. **Move target consistency.** No two `MoveDecl` ops route the
   same ordinal to different `to` modules. (The current
   `binding_assignment` map enforces this implicitly by being a
   map; the plan makes it explicit.)
5. **Cross-op coherence.** Renames + moves agree: if a binding's
   declaration is moved to module M, every `Rename` op on that
   binding has scope ⊆ M. (The current chunk_renames-vs-body
   split is exactly this check, currently expressed as the
   `binding_assignment.contains_key(top_level_id(name, …))`
   filter in `lower.rs:117-124` and `lower.rs:183-188`.)
6. **Realizability preservation.** No `MoveDecl` op produces a
   partition that the realizability gate (`realizability.rs`)
   would reject. Today the partition is fixed before lowering and
   the gate runs once; in the new pipeline the moves _are_ the
   partition refinement, so the gate runs over the post-execute
   partition before execute begins.

Output is `CheckedPlan { ops, rename_index, move_index,
specifier_index, export_index }` — immutable, indexed for O(1)
lookup by `(scope, original)` and `(in_module, ordinal)`.

### Execute phase

A single visitor walks each module body once. At every node:

- If a declaration ordinal is in `move_index`, the visitor routes
  the ModuleItem to the destination module's body and skips it in
  the source.
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

1. **Same-priority heuristic conflict policy.** Panic at seal,
   citing both contributors by `reason`. Silent suppression is
   the trap this pipeline closes.
2. **Disambiguation naming scheme.** Keep `_N` suffixing for the
   first implementation. Readability heuristics (e.g.
   `name_from_module`) are a follow-up Plan _contributor_, not a
   change to the minter.
3. **Structural mutations during COLLECT.** Pick "all structural
   moves pre-COLLECT" — moves become `MoveDecl` ops, submitted
   before any rename collection runs. Type-level barrier: rename
   contributors take `&Module`, not `&mut Module`. Pragmatic
   alternative (no structural moves between seal and execute)
   reserved as fallback if the reordering proves too invasive.

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
   the new pipeline, `MoveDecl` ops change the partition during
   Plan collection. Open: does the gate run inside `seal()` (so
   check #6 above is real), or does Plan keep an invariant that
   no `MoveDecl` may produce an unrealizable partition (enforced
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
4. A new test that submits a `MoveDecl` op producing an
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
