# Debundler — Design

> Status: in active migration. The legacy "init wrapper" lowering is
> still in place; a static schedule validator is being introduced
> alongside it (Phase 1; see <#status>). This doc describes the
> _target_ design and is updated as we land each phase.

## Mission

Ducktape's debundler takes a single ESM chunk produced by a bundler
(no internal module boundaries, statements in a linear evaluation
order) and recovers a multi-module ESM bundle along human-meaningful
boundaries described by a spec. The recovered bundle must be
observationally equivalent to the input chunk and must be a normal
ESM bundle that consumers can import, mock, or swap without
scaffolding.

A bundler erases module boundaries; the debundler reconstructs them.
This design treats reconstruction as a **scheduling problem**: the
flat chunk gives us a total order over statements; we must produce
a partial order (the module dep graph) such that any linearization
of that partial order is observationally equivalent to the input.

## ESM execution model (the constraints)

ESM module evaluation is more constrained than ad-hoc discussion
admits. Pinning the rules down once:

1. **Imports are linked before evaluation.** All transitively
   reachable modules are parsed and have their bindings allocated
   before any module body runs. The `import` line at the top of
   each module is a declaration of a dep, not a runtime statement.
2. **Module evaluation order is a topological sort of the dep
   graph.** For a DAG, deepest-first. Deterministic for non-cyclic
   graphs.
3. **For cycles, ESM evaluates one member at a time, in reverse-DFS
   from the entry.** The "first" member to evaluate sees the
   others' exports in their _partially-initialized_ state. The
   exact partial state depends on the binding kind:

   | Binding kind            | State before its declaration line runs                                     |
   | ----------------------- | -------------------------------------------------------------------------- |
   | `var X`                 | Hoisted; reads as `undefined`; written when the line runs                  |
   | `let X` / `const X`     | TDZ; read throws `ReferenceError: Cannot access X before initialization`   |
   | `function X() {}`       | Hoisted with its definition; callable from the start                       |
   | `class X { ... }`       | TDZ; **not hoisted**; reads (including `extends X`) throw `ReferenceError` |
   | `import { X } from ...` | Live binding to the target's binding; whatever the target currently holds  |

4. **Side-effecting top-level statements run when their module's
   body reaches them.** Module bodies run once per program; no way
   to re-run. The order across modules is determined by the
   topological evaluation order, **not** by the source order of the
   original flat chunk.
5. **The entry module's body runs last** — every transitive
   dependency evaluates first.

Two consequences are central to debundling:

- **Reads at module-top are time-sensitive.** They see whatever is
  set at the moment they execute. In a cycle, that may be undefined
  or TDZ.
- **Reads inside function bodies, method bodies, class instance-
  field initializers, etc. are time-deferred.** They see whatever
  is set at the moment the function/method is _called_, typically
  much later in program time.

## Definitions

Let the input _chunk_ be a sequence of top-level statements
`S_1, ..., S_n` in source order. Let the spec be a partial map
`owner: Bindings → Modules`; bindings without an explicit owner
default to the _residual entry_ module (a synthetic module that
holds whatever is left over).

For each statement `S`:

- **`declared(S) ⊆ Bindings`** — top-level identifiers introduced
  by `S`. `var X`, `let X`, `const X`, `function X`, `class X`, and
  `import { X }` each declare `X`. Comma-list var-decls declare
  every name.
- **`reads_at_init(S) ⊆ Bindings`** — bindings whose values are
  read during `S`'s evaluation, _excluding_ references inside
  function/method bodies, instance class-field initializers,
  getter/setter bodies, and other lazy syntactic positions.
  Including: `extends`-clauses, decorator expressions, computed
  property keys, default-export expressions, RHS of var
  declarators, `static` field initializers, static blocks, default
  parameter values that get evaluated at class-decl time.
- **`has_side_effect(S) ∈ {true, false}`** — whether `S`'s
  evaluation has externally-observable side effects beyond binding
  declaration. Pure `function X() {}` and `class X {}` (no static
  init) are side-effect-free. `const X = computed()` has the side
  effects of `computed()`. Bare expressions are side-effecting by
  default.

The spec induces, for each statement:

- **`home(S) ∈ Modules`** — the module a statement is emitted
  into. For statements with `declared(S) ≠ ∅`, `home(S)` is the
  spec's `owner` of any name in `declared(S)`. (We require all
  names in `declared(S)` to share an owner; comma-list var-decls
  with split owners are split into separate var-decls before this
  step.) For statements with `declared(S) = ∅` (bare expressions,
  side-effecting statements), `home(S)` is the residual entry
  module.

## The at-init module dep graph

Define `G = (V, E)` over `V = Modules`:

- `(home(S), owner(b)) ∈ E` for every `(S, b)` where
  `b ∈ reads_at_init(S)` and `owner(b) ≠ home(S)`.

`G` records exactly the cross-module read-at-init dependencies that
the source chunk's evaluation requires: an edge `M → M'` reads "M's
body has a statement that reads a binding owned by M' at the time
the statement evaluates."

A _side-effect order extension_ `G'` adds edges:

- `(home(T), home(S)) ∈ E'` for every pair `(S, T)` with
  `S.ordinal < T.ordinal`, `has_side_effect(S)`, `has_side_effect(T)`,
  and `home(S) ≠ home(T)`.

These edges encode "S must execute before T," which under ESM means
"T's module evaluates after S's module."

## The realizability theorem

> **Theorem.** A spec assignment is _realizable_ — there exists an
> emitted multi-module ESM bundle that is observationally
> equivalent to the input chunk — iff the combined dep graph
> `G ∪ G'` is acyclic.

Realizability is exactly acyclicity. A spec that introduces a cycle
is unrealizable: no emit strategy can make the resulting bundle
behave like the input.

### Proof

**Forward direction (acyclic ⇒ realizable).**

Assume `G ∪ G'` is acyclic. Construct the emit:

- For each module `M`, emit one file:
  - `import { b₁, b₂, ... } from "<owner-module>"` for every cross-
    module binding read by any statement in `M`.
  - All statements `S` with `home(S) = M`, in their original source
    ordinals, unmodified.
  - `export { ... }` for every binding owned by `M`.

The ESM linker computes a topological order of the module dep
graph as constructed; this is exactly the topological order of
`G ∪ G'`. Since the graph is acyclic, ESM evaluates modules in some
linearization of that topological order. For each module `M`, all
modules `M'` with `M → M'` in the graph have fully evaluated
before any line of `M`'s body runs.

Take any `S ∈ M` and any `b ∈ reads_at_init(S)`:

- If `owner(b) = M` (same module), the source-order invariant
  guarantees `b`'s declaring statement ran before `S` within `M`'s
  body. So `b` is initialized.
- If `owner(b) = M' ≠ M`, by construction the graph has edge
  `M → M'`, and `M'` evaluates before `M`. So `b` is initialized.

Therefore every `reads_at_init(S)` access sees an initialized
binding, identical to the input chunk.

For side-effect ordering, the side-effect edges in `G'` ensure that
for any pair `(S, T)` with `S.ordinal < T.ordinal` and side effects
in different modules, `home(S)` evaluates before `home(T)`. Within
a module, source order is preserved by construction.

The emit produces an observationally equivalent bundle. ∎

**Backward direction (cyclic ⇒ unrealizable).**

Suppose `G ∪ G'` has a cycle `M_1 → M_2 → ... → M_k → M_1`. We show
no ESM emit can preserve the input's behavior.

For each edge `M_i → M_{i+1}` (subscripts mod `k`), pick a witness:
either a `(S_i, b_i)` pair from `G` (a read-at-init edge), or a
`(S_i, T_i)` pair from `G'` (a side-effect edge).

Without loss of generality, `M_1` is the first cycle member to
start evaluating in any ESM execution (some module has to be
first). When `M_1`'s body reaches its witness:

- If a read-at-init edge: the read on `b` (owned by `M_2`) sees
  `b`'s pre-init value (TDZ or `undefined`, depending on binding
  kind). The input chunk's evaluation at `S_1` saw `b`'s
  initialized value. These differ for any nontrivial `b`.
- If a side-effect edge: ESM has evaluated `M_1`'s side-effecting
  statement before `M_2`'s, but the source order required the
  reverse. The observable effects fire in the wrong order.

Either witness produces a difference between the emitted bundle
and the input. So no realization exists. ∎

### Corollary: the role of the validator

The pipeline runs:

```
parse chunk
  ↓
analyze per-statement facts (declared, reads_at_init, has_side_effect)
  ↓
apply spec assignment (+ closure)
  ↓
build G ∪ G'
  ↓
validate: acyclic?       ←—— Phase 1 lives here.
  ↓                                         ↓ no
emit (source-order)                  reject with cycle evidence
```

A spec that passes validation is _guaranteed_ to emit correctly
under the source-order strategy described in the proof. There is
no class of correct-input that the validator rejects, and no
class of incorrect-input that the validator misses (modulo
cleanly-defined precision of `reads_at_init` and `has_side_effect`).

## Cycle resolution

When the validator rejects a cycle, the spec author has two paths:

1. **Colocate the cyclically-coupled bindings.** Move every binding
   along the cycle into a single module. Once `owner(b)` is the same
   for every `b` in the cycle, the cycle's edges (which require
   `home(S) ≠ owner(b)`) disappear from `G ∪ G'`.
2. **Make the read lazy in the source.** If the cycle is caused by
   a read at module-top that could be deferred (move the expression
   into a function body), the rewriter can do that during the
   readability rename pass — but this changes program semantics
   and is not always sound.

Path 1 is the typical resolution and is always available. The
validator should suggest it explicitly: "Cycle through `M_a`, `M_b`,
`M_c`. Resolution: colocate {b₁, b₂, b₃} in one module."

## Architecture

The pipeline is a sequence of stages over a shared `JsPipelineArtifact`:

| Stage                            | Module                                         | Role                                                                                                                   |
| -------------------------------- | ---------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `compute_chunk_metadata`         | <program_analysis.rs>                          | Parse chunks; record top-level decls, imports, side effects, observable module effects. Pure, no spec.                 |
| `apply_vendor_annotations`       | <vendor.rs>                                    | Mark vendor packages per `mark_vendor` ops.                                                                            |
| `rename_vendor_exports`          | <vendor.rs>                                    | Rewrite vendor symbol exports per `rename_vendor_symbols`.                                                             |
| `swap_vendor_chunks`             | <vendor.rs>                                    | Substitute vendor chunks with package resolves.                                                                        |
| `materialize_logical_modules`    | <logical_modules.rs> + <schedule_validator.rs> | **Main split.** Computes per-statement facts, applies spec, builds `G ∪ G'`, validates, emits modules in source order. |
| `rewrite_chunk_entry_specifiers` | <rewrite_specifiers.rs>                        | Rewrite cross-chunk import paths to be relative to chunk entries.                                                      |
| `write_js_tree`                  | <write_tree.rs>                                | Persist the artifact to disk.                                                                                          |
| `emit_browser_harness`           | <emit_harness.rs>                              | Generate HTML + bootstrap for browser runtime.                                                                         |

Within `materialize_logical_modules`, the substages are:

1. **Spec parsing** → `LogicalRequest` / `ModulePlan` per chunk.
2. **Statement-facts analysis** (<schedule_validator.rs>:
   `analyze_chunk_facts`) → `Vec<StatementFacts>`.
3. **Binding assignment** → `BTreeMap<String, usize>` (module
   index per binding). Explicit assignments first, then closure
   over dependencies.
4. **Module dep graph + validation** (<schedule_validator.rs>:
   `build_module_dep_graph`, `validate_schedule`).
5. **Cycle resolution gate** — if the validator finds cycles,
   the pipeline aborts with the cycle evidence. (Phase 1: warn-
   only; Phase 3: hard error.)
6. **Source-order emission** — each module's body in source order;
   cross-module imports + source-chunk re-imports; `export { ... }`.
   No init wrappers.

## Invariants

The implementation must maintain:

1. **Identity.** Each binding has exactly one canonical module
   owner. Every consumer reads it through the same import chain
   so `instanceof` and identity comparisons survive splitting.
2. **Source-order within a module.** Statements emit in their
   original source ordinals. If two statements `S` and `T` share
   a module and `S.ordinal < T.ordinal`, `S` precedes `T` in the
   emitted module body.
3. **Comma-list integrity.** A `const A = …, B = …, C = …` whose
   declarators are split across modules is broken into
   per-declarator var-decls _before_ the source-order emit, so
   each declarator can be independently moved. Splitting must
   preserve the original initialization order across the
   resulting separate statements.
4. **Live-binding propagation.** Cross-module imports use the
   provider's exported name (after the readability rename pass);
   consumer-side locals may be aliased but always reference the
   live binding so reassignments by the provider are visible.
5. **No runtime ordering scaffolding.** The emit produces no
   `__dt_generated_init__*` symbols, no idempotency flags, no
   manual init-call sequences. ESM's natural evaluation order
   carries the load.
6. **Static schedule check is total.** The validator inspects
   _every_ statement and _every_ read; there is no opt-out path
   that bypasses the dep graph. If a real cycle exists in the
   spec, the validator surfaces it.

## What this design rejects

Examples of unrealizable splits — these are real shapes that have
surfaced in the Tana corpus and that the legacy emit silently
papered over:

### Cycle through two logical modules

```
// chunk
const TVe = "stop1";              // owned by mod_p
const Vn = { stop1: TVe };        // owned by mod_p
function buildBackgroundPattern(node) {
  return { className: Vn.stop1 }; // lazy, owned by mod_p
}

const BackgroundPatternStyles = { stop1: TVe }; // owned by mod_bp
class BackgroundPattern { … } // owned by mod_bp
```

If the spec assigns `TVe → mod_p` and
`BackgroundPatternStyles → mod_bp`, then `mod_bp` reads `TVe` at
init from `mod_p`. Edge `mod_bp → mod_p`. But if `mod_p` also
imports anything from `mod_bp` at module-top (e.g. another piece
of the same comma-list landing in `mod_bp`), we get the reverse
edge `mod_p → mod_bp`. Cycle. Rejected.

Resolution: colocate `TVe` with `BackgroundPatternStyles` (or vice
versa) so they share an owner.

### Class extends across cycle

```
// in mod_a
class A { … }

// in mod_b — at module-top, EAGER:
class B extends A { … }
```

If `mod_a` imports anything from `mod_b` at module-top (say through
the closure pulling a transitive dep), the cycle `mod_a ↔ mod_b`
contains a class extends-clause read. Class declarations are TDZ-
prone, so this fails at module load with `ReferenceError: Cannot
access A before initialization`.

Resolution: colocate `A` and `B`, or move the cross-`mod_a` import
inside a function body so it becomes lazy.

### Computed property key reading another module

```
// in mod_a
const m = { dataTypeNumberId: "SYS_D08" };

// in mod_b — at module-top:
const dataTypeIconMap = { [m.dataTypeNumberId]: numberIcon };
```

`mod_b → mod_a` edge through the computed key. If `mod_a` doesn't
edge back to `mod_b`, fine. If it does (because the closure
disagreed about ownership), cycle, rejected.

The legacy emit handled this via heuristics in
`is_plain_import_safe_initializer` that special-cased computed
keys. The new design just reads the dep graph and lets the
validator decide.

## Migration from the legacy init-wrapper design

The legacy lowering sat on an ad-hoc runtime: every module that
contained any "unsafe" initializer was wrapped in a
`__dt_generated_init__<plan>()` function, called from the residual
entry's body in source-ordinal order. Cross-module init deps were
threaded through an idempotency-guarded cascade. Symptomatic of the
gap: heuristic safety checks, `var`-vs-`let` debates for the
placeholder, idempotency flags to break runtime cycles.

The fundamental issue: the legacy design _fights_ ESM. ESM's linker
computes a topological eval order from static imports. Any init-
order constraint expressed only through runtime function calls is
invisible to the linker, so the linker can pick an arbitrary order
that violates the constraint. The wrapper runtime is a hand-rolled
re-implementation of what ESM already does — and worse, because it
can't see class declarations, partial initialization across module-
top reads, or side-effect ordering.

What is being removed (Phase 3-4):

- `is_plain_import_safe_initializer` and the heuristic prop-name check.
- `var_requires_init_wrapper_for_module`, `init_required_modules`.
- `initialized_module_body`, `push_initialized_var_decl`,
  `init_dep_names_for_body`.
- The init flag declaration helpers, the idempotency-guard helpers.
- All `__dt_generated_init__*` emission.
- `init_call_statement` from the entry assembly path.

What stays (unchanged or lightly renamed):

- Spec parsing into `LogicalRequest` / `ModulePlan`.
- `close_module_bindings_over_dependencies` (with cycle-aware
  refusal — Phase 2 enhancement).
- `cross_module_imports_for_body`,
  `source_chunk_imports_for_moved_body`,
  `import_specifier_member_decl`.
- `rewrite_chunk_entry_specifiers` and the rest of the orthogonal
  pipeline.
- Naturalization renames — moved to a clearly-separate
  post-processor pass, not intertwined with init.

## Status

| Phase | Description                                                                                          | State                                                                                                             |
| ----- | ---------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| 1.1   | `StatementFacts` analyzer producing `(declared, reads_at_init, has_side_effect, kind)` per statement | **Done** (<schedule_validator.rs:69>; 7 unit tests pass)                                                          |
| 1.2   | `ModuleDepGraph` builder (`G ∪ G'`)                                                                  | **Done** (<schedule_validator.rs:243>)                                                                            |
| 1.3   | Tarjan SCC validator + JSON report                                                                   | **Done** (<schedule_validator.rs:283>)                                                                            |
| 1.4   | Wire the validator into `materialize_logical_modules` as a report-only side output                   | In flight                                                                                                         |
| 1.5   | Run against Tana spec; record every cycle                                                            | **Done.** Spec produces 1 SCC of 9 modules, 99 edges (see [Phase 1 findings](#phase-1-findings-tana-78d928dca7)). |
| 2     | Update gaffer spec to break each surfaced cycle                                                      | In flight                                                                                                         |
| 3     | Switch emit to source-order; drop init-wrapper machinery                                             | Pending 2                                                                                                         |
| 4     | Cleanup: remove legacy code, update e2e fixtures, update AGENTS.md                                   | Pending 3                                                                                                         |

The work that landed during the legacy-design era — cross-module
import emission, ImportSpecifier handling, source-chunk re-import
logic, spec parsing — survives the migration. What dies is the
init-wrapper substrate. The phased rollout keeps every interim
state shippable.

## Phase 1 findings: Tana 78d928dca7

Running the validator (commit 23b300154) against the Tana spec
produces a single strongly-connected component containing
**9 modules** and **99 evidence edges**:

```
ai_mcp_prompting_runtime
  ↔ ai_tooling_fetch_website_tool
  ↔ runtime_calendar_journal_nodes
  ↔ workspace_system_bootstrap_command_schema
  ↔ runtime_logging_boot_platform_services
  ↔ commands_search_runtime_actions
  ↔ billing_redeem_code_widget
  ↔ runtime_app_state_search_commands_core
  ↔ graph_core_node_model
```

`ai_mcp_prompting_runtime` is the centre — it has the most in/out
edges and corresponds to the 49,742-line generated module that the
spec defines as a giant catch-all. Most of the cycle dissolves
once that module is split into smaller, dependency-coherent
pieces.

The legacy init-wrapper machinery has been silently coping with
this cycle by deferring binding initialization through
`__dt_generated_init__*` calls. Each Tana smoke failure of the
"TDZ" / "TypeError on undefined" / "Cannot access X before
initialization" shape we have hand-patched was a different
manifestation of the same SCC.

Phase 2's job is to redraw the spec so this SCC dissolves. Three
techniques apply:

1. **Pull the giant `ai_mcp_prompting_runtime` apart.** The
   right boundary is "real React component" or "domain feature"
   — not the current bag of everything that touches AI. Each
   smaller module's at-init reads will fall outside the SCC.
2. **Colocate cyclic-coupled bindings.** For pairs of modules
   where the cycle is small (e.g. CSS-class identifiers consumed
   only by one component), move the bindings into the
   consuming module.
3. **Push reads into function bodies.** Rare; only applies if
   the read can be deferred without changing semantics. Usually
   path 1 is preferable.

The full evidence list is in
`bazel-bin/tana/re/web/transforms/debundle_78d928dca7.out/analysis/logical_modules/static/index-DI2GynTv.schedule.json`
on a built artifact; aggregating by `(from, to)` pair gives a
direct work list for spec edits.

## Open design questions

These are unresolved precision issues. Each is worth its own
exploration before crossing the relevant phase.

1. **Lazy-position completeness.** `reads_at_init` is implemented
   as a visitor that descends into eager positions and stops at
   lazy positions. The current implementation handles function
   bodies, method bodies, instance class fields, getters, setters.
   Open: decorator factory bodies (decorators run at class-decl
   time but their factories close over bindings); default-parameter
   evaluation timing (ECMAScript spec says default params evaluate
   on call, which is lazy); dynamic `import()` arguments. The
   visitor's gaps should be exhaustively pinned in unit tests.
2. **Side-effect classification precision.** Without alias analysis
   we have to assume `const X = f()` is side-effecting if `f` is
   any function call. This over-imposes side-effect edges, which
   may force more closures than necessary. Pure-call inference is
   future work.
3. **Closure refusal vs. rejection.** When the closure pass
   considers pulling `B` into module `M` because `M` reads `B`,
   but the pull would create a cycle, options are:
   - Refuse and surface a cycle error to the spec author.
   - Try the alternative (leaving `B` un-pulled) and re-check.
   - Pull anyway and let the validator reject downstream.

   Phase 2 picks "refuse and surface"; the spec author fixes the
   spec. This is the strict path consistent with the realizability
   theorem.

4. **Vendor chunk modeling.** Vendor chunks are pre-existing module
   boundaries that we don't control. They appear in the dep graph
   as nodes with no at-init reads from our chunk (the vendor
   doesn't import from us). The validator should sanity-check
   this; a vendor that imports back into the user-chunk is a
   pathological case worth detecting.
5. **Validator UX.** The cycle report should be actionable. A
   shape like "Cycle modules [M_a, M_b]; evidence:
   stmt#42 in M_a reads `X` (owned by M_b); stmt#107 in M_b
   reads `Y` (owned by M_a). Resolution: colocate X and Y in
   one module" is the goal. Worth prototyping against real Tana
   cycles.
6. **Spec backward compatibility.** During the migration window
   (between Phases 1 and 3), the legacy emit is still active and
   the validator runs in report-only mode. After Phase 3 the
   validator is hard. Existing specs that violate the rules need
   to be updated atomically with the cutover; otherwise a stale
   spec will fail-closed.

## What this design does not solve

- **Intentional cyclic init semantics.** If the original chunk
  _relied_ on partial-eval state during a cyclic load (a rare but
  legal pattern), the input is inherently un-debundle-able into
  clean ESM modules. None of the surfaced Tana cases fall into
  this category — they are all spec choices that drew the cut in
  the wrong place.
- **Choosing module boundaries.** The debundler executes the
  spec; it does not author it. Higher-level analysis tools
  (React component detection, big-string clustering, scrambled-
  identifier statistics) help the human author write better
  specs, but the spec is still chosen by humans.
- **Identifier readability.** The rename pass that converts
  scrambled names to readable ones is orthogonal to this
  scheduling design and stays.

## File references

Primary:

- <DESIGN.md> — this document.
- <schedule_validator.rs> — `StatementFacts` analyzer,
  `ModuleDepGraph` builder, `validate_schedule`.
- <logical_modules.rs> — main pipeline stage; legacy init-
  wrapper machinery (slated for removal in Phase 3).
- <pipeline.rs> — pipeline composition.
- <program_analysis.rs> — chunk metadata + side-effect
  classification (used as input to the analyzer).

Secondary:

- <vendor.rs>, <rewrite_specifiers.rs>, <emit_harness.rs>,
  <write_tree.rs>, <scrambled_id_frequencies.rs> — orthogonal
  pipeline stages, unaffected by the migration.

Spec authoring:

- <../../../tana/re/web/transforms/78d928dca7/operations/logical/main_index_modules.mjs>
  - sibling member files (private repo) — Tana spec input.

Tracking:

- <TODO.md> — open work items.
- <AGENTS.md> — operating principles for contributors.

## Conventions for updating this doc

- This is the **canonical design source.** When a design choice
  changes (because we learn something new, hit a constraint, or
  pivot), update this doc _first_ and only then change the code
  to match. Code that disagrees with the doc is a bug — either
  in the code or in the doc. Decide which and bring them back in
  sync.
- Add a status line under each phase as it progresses; never
  delete completed phases — the historical record matters when
  later questions surface ("why did we do it this way?").
- Open design questions go in <#open-design-questions>. Once
  resolved, move the resolution into the body of the doc and
  delete the question.
- Keep the proof intact. The realizability theorem is the
  foundation; if a future change breaks the theorem (e.g.
  introduces an emit strategy that bypasses it), the proof
  needs revision and any dependent claims need re-checking.
