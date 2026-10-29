# Debundler — Design

> Summary of the design: the debundler takes a bundler-emitted
> ESM chunk and a path-first logical-module spec, and emits a
> multi-module ESM bundle observationally equivalent to the
> input. The primitive analysis product is a dependency graph whose
> source-unit nodes are built from top-level statements. Its edges
> record "this source unit uses that binding" plus source-order
> side-effect constraints. The spec assigns bindings and anonymous
> statements to output destinations.
> The validator quotients the dependency graph by that assignment to
> derive the imports graph `I` plus side-effect ordering graph
> `S` and runs a two-pass realizability gate: Pass 1 rejects any
> cycle among the constraining edges (at-init reads and `S`);
> Pass 2 accepts a cycle that also runs through lazy reads only if
> an ESM evaluation simulator, run over the emitted import order,
> proves every constraining edge's target evaluates before its
> source. The emit is source-order: each logical module's body is
> its assigned statements in original chunk order, with explicit
> `import` / `export` declarations. There is no init-wrapper
> machinery, no closure pass, no implicit binding pulls — every
> owned binding is named explicitly in the spec, and `Imported`
> bindings flow through `AssignmentInputs::binding_catalogue` /
> `BindingKind::Imported`, carrying the single re-exporting module
> and public name that claimed it. Anonymous (empty-`declared`)
> top-level statements have no name to address as a member; a
> parallel `anonymous_statements` selector list addresses each by
> a `source_match` shape template. They co-move with their
> named-binding companions when the closure requires it, but the
> spec is still explicit — the author writes the selector, no
> closure pass infers it.

## Mission

Ducktape's debundler takes a single ESM chunk produced by a bundler
(no internal module boundaries, statements in a linear evaluation
order) and recovers a multi-module ESM bundle along human-meaningful
boundaries described by a spec. The recovered bundle must be
observationally equivalent to the input chunk and must be a normal
ESM bundle that consumers can import, mock, or swap without
scaffolding.

A bundler erases module boundaries; the debundler reconstructs them.
This design treats reconstruction as a **graph quotient and
scheduling problem**. The flat chunk gives us a total order over
top-level statements and a granular dependency graph between source
units derived from them. Binding claims and anonymous-statement
selectors determine each source unit's output destination. Collapsing
source units with the same destination yields a module dep graph; the
validator checks that this quotient graph admits an ESM evaluation
order observationally equivalent to the input.

Each invocation uses one exact spec and one configured input program (possibly
with several chunks). Selector resolution stays within the spec's declared
chunk and uses only the structural matching rules in <../SPEC.md>. It does not
compare versioned specs or infer migrations between source snapshots. When a
snapshot changes, authors review diagnostics and candidate selectors against the
new input, then validate the resulting spec against that input.

## Shared `var` declaration sites

A module-scoped `var` has one binding cell and can have several declaration
sites, including loop heads and conditional blocks. Statement facts retain each
site's declared binding identity, its `var` kind, and its initializer writes.
Bare redeclarations are not runtime writes. Binding lookup uses the first site
as a representative only after collecting and validating every site.

`CoDeclaration` edges join the representative and every other site in both
directions. The atomic graph and both realizability gates therefore require all
sites to share one destination. The original AST statements remain intact and
are emitted once in source order; assigning the binding claims every site.

Initializers of shared bindings participate in side-effect ordering even when
their expressions are pure. Additional source-order edges sequence eager reads,
writes, and promoted at-init observations of each shared cell, including pure
read expressions. This prevents moving an observer across a later initializer.
Unresolved-call analysis joins all declaration sites when following possible
function values. Value-specific purity inference must account for all sites.

## Soundness over completeness

The debundler's contract: **any spec the validator accepts must emit a
debundled bundle that runs correctly.** Over-restriction — rejecting a
spec that would in fact have produced a working output — is an
acceptable failure mode; the user can rework the spec, or we can loosen
the validator. Under-restriction — accepting a spec that produces a
broken bundle — is a soundness violation and is not acceptable.

This governs every static analysis the validator runs: purity
classification, side-effect graph construction, top-level-await
detection, dependency-graph cycles.

### Conditionally-correct optimizations

`AnalysisAssumptions` carries author-certified contracts, including declared
purity, callback behavior, and audited global publication. `AnalysisInputs` pairs
those assertions with imported call summaries and the caller's local-effect
policy. Imported summaries may depend on assertions in their defining chunks;
neither author assertions nor imported summaries are unconditional source facts.
For bare named imports with a return-only callback-factory capability,
`imported_callables` retains the defining module and export plus its evidence:
an author assertion or the checked immutable wrapper's local binding and
source location. Projection onto the importer's local name preserves this evidence;
the execution index uses the capability only for eligible call shapes. Inline-only,
retained-signature, and namespace-member author contracts remain distinct.
`native_operation_inputs` and `native_call_identity_inputs` are distinct
author premises resolved through the same canonical selector and call index.
The first certifies selected initializer operations; the second certifies only
the original callee at a selected direct call in a top-level function body.
Both retain exact-site evidence separately from inferred native integrity.
The call-identity input currently admits `Object.freeze`: the shared typed
slot census rejects known global `Object` or constructor `freeze` writes across
the analyzed program, while opaque and sibling write witnesses remain visible
under the explicit author assertion. The interpreter consumes this evidence
only at the selected call span; its existing argument-shape, return, heap,
throw and purity checks are unchanged. Neither input promotes a family-wide
native-integrity fact.
The per-chunk `modules.json` report projects these imported summaries for
diagnosis, including the defining source path when available.

A source-composed returned-record summary is separate from that callback-input
contract. It certifies only zero-argument construction timing under the shared
empty-Map integrity guard. The imported summary retains member/capture source
provenance, and the returned foreign capability activates conservative binding
and callback obligations when later consumed. Graph `imported_callable_summaries`
retains used record certificates, referenced separately by each source unit
through `synchronous_execution.returned_record_uses` for call timing and
returned-capability construction. See
[purity soundness](purity_soundness.md#source-composed-returned-records) for the
exact eager skeleton and fallback boundary.

Soundness does not require every inference to hold for arbitrary JS. An
inference may be **conditionally correct** — sound only when the input
satisfies a checkable precondition — provided the implementation:

1. Checks the precondition on the specific statements or chunks it
   would fire on, and
2. Falls back to the strictly-conservative path (the one already known
   sound) when the precondition fails.

Example: dataflow-aware S-chain emission (`graph/`). Per-statement
write/read summaries assume the statement contains no `with`, no direct
`eval`, no computed-key `globalThis` access, no `Function` constructor —
constructs that would invalidate static reasoning about which cells the
statement touches. Each impure statement carries a
`dataflow_summarizable` bit; statements failing the check fall back to
the unconditional adjacent-impure S-edge.

This is deliberate. The real inputs (production bundler output) are
well-behaved and admit precise reasoning even though generic JS does not.
Document each such optimization with the precondition it requires, where the
check lives, and the fallback path.

### Native browser loading and vendor replacement

`execution_runtime: native_browser_esm` declares native browser ESM loading;
the default `generic` contract permits synchronous host loaders. In the native
browser contract, a static importer outside a cycle cannot execute before its
provider completes, and dynamic import resolves asynchronously. An opaque
startup call therefore cannot obtain the provider's namespace merely because
the provider exports a hoisted callback. This does not suppress callbacks
already passed or published by the program. Complete ordinary static closure,
target-cycle and top-level-await checks still gate the inference.

Vendor preparation retains runtime facts from the replacement bundle's initial
parse. For a bundled partial swap whose bundle is a synchronous ESM leaf,
prepared residual edges safely over-approximate effective edges: stripping
deletes source edges and added facades lead only to the closed leaf. No new
path can return to the application chunk. This proof avoids a second residual
computation before materialization. Unknown package closures, bundle static
requests and bundle top-level await retain conservative fallback. The original
supergraph may reject a cycle that pruning would remove; it never accepts one
because an unexamined replacement was treated as a leaf.

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

### Source coordinates and graph identities

`StatementId` identifies an original item in a chunk's parsed `Module.body`.
`SourceUnitOrdinal` identifies a position after comma declarations are split into
movable source units. A single original statement can produce several source
units; converting back to a statement is therefore many-to-one. AST placement
and anonymous-statement comments use `StatementId`; dependency facts and planned
anonymous-unit claims use `SourceUnitOrdinal`.

Both coordinates are scoped to one chunk. `SourceUnitId` identifies a vertex in
one dependency graph. Its numeric value can match the source ordinal during
initial graph construction, but report reconstruction can allocate vertices in a
different order. Readers must use the vertex's stored source ordinal when they
need a source position. The conversion helpers in `js_ast` make the statement to
source-unit mapping explicit; the numeric report representation is unchanged.

### Bindings, names, and keys

A _binding_ is a named slot in a chunk's top-level lexical scope. Two
syntactic positions introduce a binding into that scope:

1. **Declaration**: `var X = ...`, `let X = ...`, `const X = ...`,
   `function X(){}`, `class X{}` introduce `X` as a _declared
   binding_. The value lives in this chunk; we own it.
2. **Import specifier**: `import { Y as X } from "<other-chunk>"`
   introduces `X` as an _imported binding_. `X` is a local alias
   for an export of another chunk; the value lives elsewhere.

Within a chunk's resolved top-level scope, one name identifies one binding.
A `var` binding can have several declaration sites; lexical declarations and
imports cannot redeclare an existing top-level binding. The pair
`(chunk_id, name)` therefore identifies a binding, not a declaration site.

That pair is the conceptual _binding key_ at repo/tool boundaries.
Inside a single chunk analysis, the chunk is already contextual, so
the in-memory representation keys bindings by SWC's
`Id = (Atom, SyntaxContext)` — the hygiene-aware identity the
resolver pass mints (`graph/`, `facts/`). Reports and specs spell
bindings by name (`Atom`-only on the wire; see
`docs/wire_format.md`); internal dependency graph edges, source-unit
declarations, and per-chunk indexes clone `Id`s into `BTreeMap` /
`BTreeSet` keys.

An interned binding table (dense indices instead of cloned `Id` keys)
is not implemented; adopt it only if profiling shows the binding-keyed graph
paths as a material cost.

Names are stable across the readability rename pass. The rename
pass changes the _emitted_ identifier in the destination module's
text; it does not change what a binding _is_. The schedule keys
on the original (scrambled) source-chunk local; the rename pass
applies as a final post-processing step that reads
`PlannedModule.rename_map` and rewrites the emitted source.

### Two binding kinds

A binding's _kind_ records which of the two introducing positions
made it, plus the metadata downstream stages need:

```rust
pub enum BindingKind {
    /// Declared by a top-level `var/let/const/function/class` in
    /// this chunk. The spec assigns each owned binding to a
    /// planned output (the entry destination when unclaimed).
    Owned { module: ModuleId },
    /// Introduced by an `import { imported_name as <name> } from "<chunk>"`
    /// at the chunk's top. The value lives in `imported_from`;
    /// our chunk merely aliases it. A logical module can choose
    /// to *re-export* this alias under its module path (with its
    /// own choice of public name), but it cannot *own* the value
    /// — the chunk on the other side does.
    Imported {
        imported_name: Atom,
        /// Output-tree-rooted path of the file the value comes from.
        imported_from: String,
        /// The single logical module that claims this imported
        /// binding via a `kind: import_specifier` member, and the
        /// public name it gives the export. The spec's
        /// duplicate-claim check rejects two modules claiming the
        /// same import — a consumer that wants the symbol under a
        /// different local name aliases at its own import site.
        re_exporter: ModuleId,
        public_name: Atom,
    },
}
```

Distinguishing the two kinds in the data model is what gives
the validator and emitter a single source of truth for binding
ownership: an `Owned` entry says "this logical module
contributes this binding to the chunk's namespace"; an
`Imported` entry says "this binding originates elsewhere; this
module re-exports it." Without the distinction the spec needs a
parallel side-channel to express re-export semantics.

### Statements

Let the input _chunk_ be a sequence of top-level statements
`S_1, ..., S_n` in source order. Let the spec be a partial map
`owner: binding → ModuleId` (over bindings of kind `Owned`);
bindings without an explicit owner follow `unassigned_mode`. They can remain in
the physical entry destination (`inline_in_entry`), land in a catchall file
plan (`catchall_file`), or be assigned to generated plans
(`per_atomic_group`). The entry destination is not a synthesized
`PlannedModule`; `ModuleRole::Unassigned` describes a planned output, not the
entry.

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
  declarators, `static` field initializers, static blocks.
- **`reads_lazy(S) ⊆ Bindings`** — bindings referenced from `S`'s
  syntactic span but only at the lazy positions
  `reads_at_init(S)` excludes (function/method bodies, instance
  class-field initializers, getter/setter bodies). The two sets
  are disjoint and complete: every cross-module reference in `S`
  belongs to exactly one. The full reference set is
  `reads(S) = reads_at_init(S) ∪ reads_lazy(S)`.
- **`has_side_effect(S) ∈ {true, false}`** — whether `S`'s
  evaluation has externally-observable side effects beyond binding
  declaration. Pure `function X() {}` and `class X {}` (no static
  init) are side-effect-free. `const X = computed()` has the side
  effects of `computed()`. Bare expressions are side-effecting by
  default.

The spec induces, for each statement:

- **`home(S) ∈ Modules`** — the destination a statement is emitted
  into. For statements with `declared(S) ≠ ∅`, `home(S)` is the
  resolved destination of any name in `declared(S)`, including
  `unassigned_mode` routing for names without a spec owner. (We require all
  names in `declared(S)` to share a resolved destination; comma-list var-decls
  with split destinations are split into separate var-decls before this
  step.) For statements with `declared(S) = ∅` (bare expressions,
  side-effecting statements), `home(S)` is the entry destination unless
  the spec claims `S` via an `anonymous_statements` shape selector on a
  logical module (see §"Anonymous-statement selectors") or
  `unassigned_mode` routes it into a catchall or generated atomic-group plan.

## Dependency graph

The lowest-level scheduling object is the **dependency graph**. It is
independent of any particular logical-module spec.

A _source unit_ is a top-level unit that moves as a unit during
emission: a function declaration, class declaration, single
var-declarator after comma-list splitting, import declaration
specifier, or residual side-effect statement. The dependency graph
preserves each source unit as a first-class node and report row.
`SourceUnitId` identifies the graph vertex (`unit:N`); `SourceUnitIndex`
indexes the report-row arena used by quotient analysis and is distinct.

`SourceUnitClass` distinguishes binding declarations from anonymous statements
using the graph's canonical declaration set. It is independent of syntactic
`StatementKind`, module assignment, and `ModuleRole`. Routing, placement, and
reports query this classification without storing a second anonymous flag.
Anonymous classification alone does not establish selector addressability;
import/export source units retain their separate authoring restrictions.

The micro dependency graph is a directed, labeled graph `G = (V, E)`:

- **Vertices:** source units in source order. A declaration source unit carries
  `declared`, source location, kind, and any readable rename/spec
  metadata. A residual side-effect source unit may have no declared
  binding.
- **Read edges:** `U -> U_b` for every binding `b` read by source unit
  `U`, where `U_b` is the source unit that declares `b` (the import
  declaration, for an imported binding). The edge carries
  `binding`, `edge_kind` (`eager_use`, `hoisted_use` or `lazy_use`), and the source
  statement ordinal.
- **Rebind edges:** `eager_rebind`, `lazy_rebind` or `deferred_rebind`
  from a source unit that assigns a binding to the source unit that declares it.
- **Co-declaration edges:** `co_declaration` in both directions between sites
  declaring one shared `var` binding, requiring one destination.
- **Local-effect edges:** `local_effect` from a statement whose modeled
  mutation is local to a target source unit (A10).
- **Side-effect order edges:** potential `U₂ -> U₁` (`sequenced`) constraints
  when both source units have side-effecting top-level evaluation and
  `U₁` appears before `U₂` in the source chunk. After assignment
  they become `S` edges only when their endpoints land in different
  destinations.

The dependency graph is the right abstraction for tooling:

- "Can this thing be peeled?" is a proposed reassignment of one or
  more source-unit vertices.
- "What else must move with it?" is a candidate-construction
  question over source-unit read, write, and side-effect edges. The
  answer is not proof by itself; the resulting assignment must still
  be checked by the same quotient validator as a handwritten spec.
- "Why did this fail?" should point at dependency edges first, then
  at the derived module edge they induced.

Native-integrity diagnostics retain the write census produced during the shared
AST analysis, including source chunk, statement, operation, and location. The
receiver roots precede unknown-global-value widening; `through_local_alias`
distinguishes an alias path from a direct receiver. Graph-level
`native_integrity_witnesses` stores each census site once. Source units reference
its affected native families through `synchronous_execution.native_integrity_checks`
when the ordinary-object-prototype or array-intrinsics guard is evaluated and
fails. These are failed premises, not claims that a write executed or that it
alone explains lost precision. Other eligibility conditions may also fail.
`describe` follows affected families and invalidated key dependencies to the
source witnesses, bounding rows with the same total/emitted/truncated metadata as
its other sections. Diagnostics do not
change the census, guard verdicts, or dependency edges.
Each witness also retains `target_slots`: source-derived receiver paths
(`global_object`, constructor, prototype, descendant, or unknown) paired with
string, `Symbol.iterator`, other-symbol, or unknown keys. This detail does not
narrow the existing family-wide native-integrity guard. Exact-site native
inputs can query a known receiver and key as a source contradiction; unknown
paths and keys remain opaque witnesses. Slot evidence is collected from raw
source aliases before author-conditioned receiver refinements, so an input
cannot hide a write that would contradict itself. Computed keys retain their
`key_dependencies`; an exact slot query treats them as opaque until those
native identities are independently established.
An exact native-operation input is reported independently at the call site that
consumes it, with the selector, parser hash and author rationale. It does not
erase or relabel any native write witness.

`synchronous_execution.native_prefix_observations` separately records the
conservative native-root and Object-prototype-property effects active at each
modeled guard's runtime fact path. Declaring a callable does not activate its
body; invoking it activates all indexed body writes before its first modeled
operation. Unknown execution widens to every potential effect, and imported
effects start active. These observations are diagnostic only: a clean source
prefix does not certify an emitted module layout, which may reorder a later
native mutation before the guarded read. The whole-program guard remains in
force until the graph preserves those read/write orders. The report describes
modeled evaluations, not an exhaustive runtime call trace: invocation memo hits
do not replay observations. A later guard relaxation must include native effect
state in invocation memo validity as well as preserve emitted read/write order.

### Saved-graph input contract

`debundle run` embeds a `GraphProvenance` inventory in each saved graph. It
records SHA-256 of the bytes returned to the spec, source, and vendor readers,
the complete YAML path membership of the authored module tree, absent optional
inputs, the chunk-to-source-file mapping, the engine executable's SHA-256 (computed once per process), and
an absolute-path regeneration invocation and original working directory. Paths are relative to named
spec, source, and package roots where possible, so the same inputs can be
checked after those roots are explicitly relocated. A second read of one input
during analysis must return the same digest. Final-output-only inputs, such as
browser harness assets, are outside the dependency-graph contract.

Any command combining a saved graph with current spec or source first checks
the graph's input inventory and exact selected chunk scope. The check runs
before an edit is written. An edit then evaluates its candidate assignment on
the saved dependency graph through the normal shared gates; the candidate YAML
is intentionally different from the graph's **pre-edit** baseline. A successful
edit leaves the saved graph as a pre-edit snapshot. Another graph-backed edit
requires regenerating the graph against the new spec.

The emitted module graph is a **quotient** of this graph: choose a
destination function `dest(source_unit)` from spec assignments (defaulting
to the entry destination), merge source units with the same destination, drop
intra-destination edges, and aggregate all cross-destination edge
reasons.

## Module dep graphs as quotients

The materializer validates three directed graphs derived from the
dependency graph quotient. Each captures a different scheduling
constraint; the realizability theorem (next section) is stated over
their union.

The vertex set in every case is

```
V = planned output destinations ∪ {entry destination}
```

`ModuleId` indexes planned outputs. The separate `EntryDestination` follows
those outputs in a materialized chunk and receives unclaimed source units; it
is not a fabricated `PlannedModule`. Other chunks are not vertices: a
cross-chunk import is an `Imported` binding of an import-declaration source
unit.

### I — the imports graph (the linker's view)

The graph the **ESM linker** actually walks. One edge per emitted
`import` directive.

- **Vertices:** `V = Modules`.
- **Edges:** `(M, M') ∈ E_I` iff some statement `S` with
  `home(S) = M` references a binding `b` with `owner(b) = M' ≠ M`,
  in **any syntactic position** — at-init (`extends b`,
  `const x = b + 1`, computed property key, decorator expression,
  default-export expression, RHS of var declarators, static field
  initializers, static blocks) or lazy (function body, method
  body, instance class field initializer, getter/setter body).

Each edge of `I` corresponds to exactly one
`import { b } from "<M'>"` line in the emitted source of `M`.
For acyclic `I` the linker computes a topological order, and that
order is the module evaluation order. The realizability gate judges `I`
together with `S`.

### R — the at-init read sub-graph (TDZ-relevant subset)

Same shape as `I` but restricted to `reads_at_init`.

- **Vertices:** `V = Modules`.
- **Edges:** `(M, M') ∈ E_R` iff some statement `S` with
  `home(S) = M` reads a binding `b` with `owner(b) = M' ≠ M` and
  `b ∈ reads_at_init(S)`.

The `reads_at_init(S)` predicate is defined under
[Statements](#statements) — it admits eager positions and rejects
lazy ones.

`R ⊆ I` strictly: every at-init read is a read, but lazy reads
contribute to `I` only. `R`'s edges record which cross-module
references would TDZ if the linker chose a wrong evaluation
order. `R` is not checked on its own — every `R` cycle is also an
`I` cycle, and Pass 1 checks the constraining subgraph `R ∪ S` — but `R`
shows up in the realizability proof as the sub-graph whose acyclicity
guarantees no at-init read sees TDZ once the linker has linearized `I`.

### S — the side-effect order graph

Source-order ordering constraints between side-effecting
statements that happen to land in different modules. Independent
of imports.

- **Vertices:** `V = Modules`.
- **Edges:** `(M, M') ∈ E_S` iff there exist statements `S₁`, `S₂`
  in the source chunk with `S₁.ordinal < S₂.ordinal`,
  `has_side_effect(S₁)`, `has_side_effect(S₂)`,
  `home(S₁) = M'`, `home(S₂) = M`, and `M ≠ M'`.

Edge `(M, M')` reads "`M`'s body must observe `M'`'s
side-effecting work as already complete" — equivalently, `M'`
evaluates before `M`.

An `S` edge carries no binding, so the emitter realizes it as a
side-effect-only `import "./<M'>.js"` in `M` (a phantom import) unless `M`
already imports a binding from `M'` or `M'` is the entry destination. The
materializer orders those imports so the linker's DFS lands on a
linearization of the constraining edges ("Lemma 2: entry-side import
ordering"); acyclicity of the constraining subgraph `R ∪ S` guarantees one
exists. The gate's I-graph (`ChunkConstrainingEdgeSet::i_successors`)
therefore holds every non-rebind cross-module edge, `I` and `S` alike.

#### Emission modes

`S` is materialised at source-unit level (`emit_s_chain` in `graph/build.rs`) and
the chunk spec picks one of two modes via
`chunk_analysis_options.<chunk_id>.dataflow_aware_s_chain`:

- **Strict chain** (default): for each impure top-level statement,
  emit one `Sequenced` edge to the immediately previous impure
  statement. This is the transitive reduction of the total order
  over impure statements; it preserves reachability and all SCCs
  while keeping the dependency-edge count linear. Soundest path —
  every realizable schedule satisfies it.

- **Dataflow-aware** (opt-in): per-statement `(writes, reads)`
  effect summaries (`StatementEffectSummary` in `facts/statement_facts.rs`)
  drive the emission. For each impure statement `curr`, emit
  `Sequenced(curr → prev)` when `prev` is the most recent prior
  writer of a cell in `curr.reads ∪ curr.writes`
  (read-after-write / write-after-write), and when `prev` read a
  cell `curr` writes since that cell's last write
  (write-after-read — without it a later writer could be
  scheduled before an earlier reader). Soundness follows because
  any swap of two consecutive impure statements with disjoint
  cells is unobservable to any third party.

  Effect cells are binding-storage cells (rebinds + identifier
  reads) and static-key `<global>.<prop>` cells, where `<global>`
  is any unshadowed global-object alias (`globalThis`, `window`,
  `self`, `frames`, `top`), all one object: `window.tag` and
  `globalThis.tag` are one cell. A chunk-top declaration or import
  of an alias name disables its global treatment chunk-wide. The mode is **conditionally correct**
  (see "Conditionally-correct optimizations" above):
  statements containing a shape that defeats static cell tracking
  flip `dataflow_summarizable=false` and fall back to a strict
  S-edge against every prior impure source unit (also acting as an
  opaque barrier for later statements). The bail shapes are:
  - any at-init call, `new`, optional call, or tagged template the
    purity classifier does not prove `Pure` — I/O (`console.log`)
    is not a cell, callee bodies may write global props the
    depth-0 cell recorder can't see, and indirect `eval`
    (`(0, eval)(...)`) executes arbitrary writes. Classifier-Pure
    calls are exempt: `Pure` guarantees no observable writes and
    no global-prop reads, and binding-cell ordering is enforced by
    binding edges + rebind co-location rather than the S-chain.
    A chunk may opt into the author-trusted
    `trusted_dataflow_summaries` refinement to let this class of
    opaque calls/news use the syntactic dataflow summary anyway;
    shapes that defeat write-cell extraction outright still force
    conservative barriers regardless of that flag;
  - member writes through a tracked binding (`obj.x = 1`,
    `obj.x++`, destructuring targets containing member
    expressions) — aliasing makes the written heap cell
    unattributable;
  - `with`, `Function(...)` / `new Function(...)`, computed-key
    `<global>[<expr>]`, `defineProperty` on the global object,
    `new Proxy(<global>, ...)`;
  - statements tainted by a global-object alias escape: a bare
    `globalThis`/`window`/... used as a _value_ (`const g =
globalThis`) marks the bindings it flows into (transitively,
    to a fixpoint) as suspects, and every statement reading a
    suspect bails — `g.tag` touches the same cells as
    `globalThis.tag` but the summary only sees `Binding(g)`.

  The graph also keeps an identity-based `ArrayContents(origin)` timeline for
  tracked array literals. Allocation seeds a separate pending frontier, so
  a later opaque barrier remains connected even if unsupported syntax hides
  every read or mutation. Allocation alone does not depend on preceding
  opaque operations or reset content writers/readers: one source origin can
  represent multiple runtime allocations. Opaque calls before the first live
  eager literal-origin array do not start an array-content chain. Any lazy
  allocation origin is conservatively live initially because hoisted or escaped
  functions may create it before its lexical statement. It follows aliases
  through the chunk value-flow
  facts and promotes first-order reads from directly invoked chunk functions,
  including pure readers that the S-chain intentionally omits. Per-array
  last-writer and readers-since-write frontiers preserve read-after-write,
  write-after-write, and write-after-read order in both graph modes. Array
  member writes, method calls such as `push`, escaped arrays, unresolved
  callees, and inline or constructor calls whose bodies lack the graph's direct
  call summary remain opaque heap barriers. The shared synchronous-call
  analysis also makes a source unit a heap barrier when it reports incomplete
  execution effects, regardless of a purity hint. This reuses its canonical
  precision-loss facts for observations through unsupported coercion or
  property-access protocols and unresolved synchronous calls. Key enumeration
  (`for-in`), optional calls, and tagged templates stay barriers when their
  array effects are not represented precisely. A barrier depends on the
  current array-content frontier and later array observations depend on that
  barrier; the frontier resets there, so unknown operations do not materialize
  edges against every array cell. A selected `set_from_primitive_array`
  operation adds a typed fresh Set origin to the same value-flow solve and a
  `SetContents(origin)` allocation to this heap timeline. Alias and container
  reachability use the shared flow facts. Construction identity does not
  establish later method or iterator identity, so unsupported Set
  observations retain opaque barriers. Recursive callable heap summaries
  use the shared incremental inference solver for both Array and Set cells.
  A separately certified `set_from_set_spread` operation contributes another
  typed fresh Set origin only when its spread reads a selected fresh Set across
  a source-order interval of independently certified inert constructors in
  one explicit output. That exact spread records a `SetContents` read; later
  aliases and mutations still use the shared cell frontier. This source and
  output certificate precedes value-flow inference and does not depend on the
  emitted graph's schedule conclusion.
  These facts constrain scheduling only: they
  do not reclassify calls as pure or establish a
  no-reentry/descriptor-integrity premise. In particular,
  `trusted_dataflow_summaries` does not make an array member write safe to
  summarize; absent a separate array-specific proof, it remains an opaque
  barrier.

  An exact checked implicit local construction can omit its own opaque heap
  effect. The shared local-class proof supplies accepted field shapes and stable
  construction sites; the canonical native index validates every native field's
  identity/integrity premise, and `NativeCallModel` separately supplies a
  fresh-allocation heap capability for zero-argument collection construction.
  This is not derived from purity or synchronous-call completeness. The array
  collector projects call promotions per operation before unioning a source
  unit, using the same value-flow call-target query as ordinary call facts.
  Suppressing one certified `new` does not suppress another invocation of its
  callee, argument evaluation, or unrelated operations in that statement.
  Unresolved direct identifiers retain their syntactic opaque fallback.
  Deferred methods, callbacks and getters retain their later-use obligations.

  The bit is `dataflow_summarizable` in `facts/statement_facts.rs`. Only the
  bailing statements pay the conservative cost, so the mode is safe
  on chunks mixing audited and unaudited code, but call-heavy
  top-level code gains little: every statement with an unproven call
  is chained.

### Relationship

```
R  ⊆  I            (R is the at-init projection of I)
I  ∪  S            the full constraint graph
```

## Realizability primitive

Clauses 2 and 3 of the validity predicate ("Valid peels and atomic
modules") — no cross-destination rebinding write, and the two-pass gate of
"Lemma 2: entry-side import ordering" — have exactly **one** implementation,
in the `gate` crate (`realizability/`). Every code path that asks "is this
destination assignment realizable?" reaches it through the same primitive:

> `check_realizability(dependency_graph, assignment) → RealizabilityVerdict`

The verdict lists the unrealizable SCCs (each an `SccDiagnosis`, rejected as
`MutualConstrainingCycle` or `EsmEvaluationTdz`) and the cross-destination
rebinds, with dependency-edge provenance. An empty verdict means the assignment is
realizable. Clause 1 (importability) is emit policy and not part of the
verdict ("Emit-side responsibilities").

The predicate has two forms:

1. **The pure function `check_realizability`**, `O(N + M)` per call. The
   validator (`validate_assignment`) calls it on the spec's actual
   assignment; a non-empty verdict stops materialization.
2. **`RealizabilityIndex`**, a stateful index over a working assignment for
   callers that ask many hypothetical questions — the peel kernel
   (`peel/quotient.rs`). Its only mutator, `apply`, commits a
   `ModuleAssignmentDelta`, updating quotient edge buckets and graph adjacency only
   for the dependency edges incident to the moved source units. Speculative moves never
   mutate it: they go through a non-mutating overlay of the same state, since
   the `CondensationOrder` union-find cannot be rolled back and reverting a
   move that merged SCCs costs an `O(|V| + |E|)` rebuild.

Differential tests (`realizability/tests.rs`,
`peel/gate_differential_test.rs`) assert that the two forms agree.

### Invariant: no bespoke parallel walks

No production code answers the validity question by walking the dependency graph or
the quotient with its own algorithm: two algorithms drift, one shared
implementation cannot. A caller that needs a verdict over a hypothetical
assignment asks the index. Adjacent diagnostics may read the verdict's
dependency-edge provenance, but the validity decision goes through the primitive
only.

### Peel planner unification

The peel proposer's kernel (`QuotientGraph` in `peel/quotient.rs`) decides
every merge with the same primitive as the materializer. It must not
reimplement the gate over a JSON `DependencyGraphReport` projection: a
constraining-only projection drops `LazyUse` and `HoistedUse` edges and becomes blind to
asymmetric `(eager forward, lazy back)` I-cycles that the
`EsmEvaluationSimulator` pass catches.

1. `DependencyGraph::from_report` rebuilds the typed IR from the report, with every
   edge (constraining and lazy) and its original `DepKind`.
2. `QuotientGraph::from_report` keeps that `DependencyGraph` and builds a
   `RealizabilityIndex` over the class projection: each class is one
   `ModuleId`, except that classes made only of residual-destined source units
   (and not anchored to an existing module) share the residual
   `ModuleId(0)`. Every committed mutation — `contract`, the
   `is_pre_existing_module` promotion in `set_class_pre_existing_module` —
   applies the matching `ModuleAssignmentDelta::MoveSourceUnits` to the index.
3. The speculative queries — the boolean `merge_preserves_invariants` and the
   evidence-producing `would_be_cycles_after_contract` — are one evaluation
   through the index's tier ladder (below). `realizability_verdict()` reads the
   index's maintained verdict.

Original-home rejoining begins with a fully realizable assignment and must
preserve that property after every contraction. Its pair and whole-home
commit paths retain their structural and size checks, then run
`RealizabilityIndex::verdict_after_moving_source_units` over the complete post-move
quotient. The index patches its maintained canonical I-graph, constraining
edge evidence, and cross-rebinds with the proposed incident-edge overlay;
the pure reference and this overlay use the same full SCC and ESM simulator
gate body. A merge can change global ESM import order and expose a TDZ in an
I-SCC outside the merged module, which a touching verdict cannot see. General
seed/greedy contractions can start from an unrealizable assignment while
repairing it; the final proposal verdict checks their result.

#### Cost and the tier ladder

A from-scratch `check_realizability` call is `O(|V| + |E|)`; asking it per
merge candidate would make a seeding pass `O(|V|² · |E|)`. The persistent
index instead maintains the constraining-edge graph SCC state, the I-graph
adjacency, the `EsmEvaluationSimulator` per-pair bucket, and the cross-rebind
set incrementally.

A speculative merge enters the index's tier ladder
(`ladder_decision_after_moving_source_units_touching`) with the merge's post-state
`ModuleId` (`projected_winner_module_after_merge`) and the `±edge` overlay
from `compute_merge_deltas`. Each tier either decides — provably equal to the
full `check_realizability` verdict, restricted to diagnoses touching the
post-merge module — or escalates:

- **Tier 0 — delta-free short-circuit.** No deltas (both classes
  already project to the post-merge module, e.g. merges inside the
  gate-residual pile): post-state == pre-state, so accept iff the
  cached pre-state touching verdict is clean.
- **Tier 1 — constraining condensation order.** A maintained
  `CondensationOrder` over the module-level constraining graph (a
  union-find of SCC membership plus a Pearce–Kelly topological
  order over the condensation DAG): reject iff the post-merge
  module's constraining SCC is multi-module — an `O(α)` find, or,
  when the overlay adds an adjacency pair, a cone-bounded DFS over
  the overlay-patched effective adjacency — or a clause-2
  cross-rebind touches it. A tier-1
  reject is exactly the `MutualConstrainingCycle` clause of the
  full verdict; a pass establishes Pass 1 is clean and escalates.
- **Entry-last guard.** Before any I-SCC vacuity shortcut, reject an
  effective constraining edge into the entry destination that touches the
  queried module.
  Entry evaluates last, including when its implicit import is the only return
  edge. This reads the maintained edge counts plus the overlay delta; it does
  not add universal fan-out to the I-condensation.
- **Tier 2 — I-graph condensation order.** A second
  `CondensationOrder` over the I-graph (constraining ∪ lazy): if
  the post-merge module's I-SCC is not multi-module, or contains
  no effective constraining pair, Pass 2 is vacuous — accept.
  When the overlay removes an edge internal to a multi-module
  I-SCC (the one case where the maintained union-find is
  stale-coarse), the order answers through its exact bidirectional
  reachability fallback (`exact_multi_scc`); a multi-module verdict
  then has `OverlayGraphView::scc_containing` materialize the I-SCC
  for the constraining-pair check.
- **Tier 3 — scoped ESM simulator.** The shared
  `EsmEvaluationSimulator` over the overlay-patched I-SCC: reject
  iff any TDZ pair. This is the same code `check_realizability`'s
  Pass 2 executes — drift is impossible by construction.

The tiers are short-circuits whose skip conditions are theorems
about the predicate, not a second decision procedure. The boolean
gate is the ladder with evidence materialization elided;
`would_be_cycles_after_contract` is the same ladder, materializing
source-unit-level evidence (through the index's non-mutating
`verdict_after_moving_source_units_touching` overlay) only on a reject —
one entry point, two output shapes. Each query reads SCCs touching
the hypothetical destination only — `O(|cone|)`, not
`O(|V| + |E|)`. Speculative deltas are single-target by
construction: a merge contracts two classes into one, so
`compute_merge_deltas` only ever emits `MoveSourceUnits` deltas
targeting the single post-merge module; the ladder dispatch asserts
this invariant, and a future non-merge speculative mutation must
implement a multi-target overlay deliberately rather than inherit
the merge path.

#### References

The incremental realizability path draws on the following lines of
research; cited here so the trade-offs in "Cost and the tier
ladder" and "Why not Pearce-Kelly verbatim" can be cross-checked
against the primary sources.

- **Bender, Fineman, Gilbert, Tarjan.** _Incremental Cycle
  Detection, Topological Ordering, and Strong Component
  Maintenance._ ACM Transactions on Algorithms 12(2):14, 2015. The
  BFGT amortized bound for arc-insertion cycle detection.
  ([arXiv:1105.2397](https://arxiv.org/abs/1105.2397))
- **Pearce, Kelly.** _A Dynamic Topological Sort Algorithm for
  Directed Acyclic Graphs._ ACM Journal of Experimental
  Algorithmics 12, 2007. The "PK" algorithm for online topological
  order under edge insertion.
- **Fähndrich, Foster, Su, Aiken.** _Partial Online Cycle
  Elimination in Inclusion Constraint Graphs._ PLDI 1998. Localized
  bounded-cone cycle elimination in pointer-analysis constraint
  graphs — the structural closest match to the kernel's
  contraction-driven primitive.
- **Hardekopf, Lin.** _The Ant and the Grasshopper: Fast and
  Accurate Pointer Analysis for Millions of Lines of Code._ PLDI
  2007 (with subsequent 2009 work on lazy / hybrid cycle detection).
  Practical scaling of constraint-graph cycle elimination.

#### Why not Pearce–Kelly verbatim

The literature on incremental SCC maintenance — Pearce & Kelly's
online topological order plus Bender, Fineman, Gilbert, and Tarjan's
work (BFGT) — solves cycle detection under arc **insertion** with
single-edge insertion as the user primitive. The peel proposer's
primitive is **contraction** (vertex identification): a merge fuses
two classes' incident edges and may collapse multiple
constraining-edge cycles to single classes that get dropped. Naive
"reinsert every loser-incident edge into the winner" simulations
match Pearce–Kelly's insertion model but waste work — the merge can
also delete edges (intra-class self-loops, cycles that shrink to a
single class). The persistent index instead maintains the maintained
quotient adjacency directly via `IncrementalQuotient::add/remove_current_edge`,
which is structurally closer to the bounded-cone local-search
algorithms from pointer-analysis literature (Fähndrich–Foster–
Su–Aiken; Hardekopf–Lin).

Pearce–Kelly's core does survive — as the heart of the
`gate` crate's `CondensationOrder` (window Kahn, epoch
visited-marks), re-keyed from kernel classes to condensation
nodes of the index's maintained module graphs, with a union-find
for SCC membership so the PK order lives over a structure that
stays a DAG by construction even when the underlying graph is
cyclic. Insertion monotonicity — edge insertion only ever coarsens
the SCC partition — is what licenses a plain non-rollbackable
union-find instead of full dynamic-SCC machinery (kernel-side index
mutation is commit-only); a committed removal internal to an SCC
marks the structure stale and the next query rebuilds it rather than
splitting eagerly.

The kernel itself maintains **no** decision-making derived order or
cycle state: the gate ladder lives inside the index ("Cost and the
tier ladder" above), so the "no bespoke parallel walks" invariant
holds here too — one source of truth for accept/reject.
`rank_candidate`'s cycle-reduction key byte reads the same
maintained condensation through an `O(α)` union-find probe
(`modules_share_constraining_multi_scc`) instead of an advisory
cache.

## At-init call promotion

A function body's lazy reads/rebinds fire at module-init from the
perspective of any caller that invokes the function at-init. The dependency
graph carries promoted `EagerUse` edges from at-init callers to the
transitive closure of their callees' lazy reads and rebinds, so the
primitive's clause-3 verdict is sound for the canonical
`console.log(readB())` shape (top-level call whose body crosses a module
boundary).

The promotion runs once in `build_dependency_graph_with` and is
assignment-independent: intra-module promoted edges are dropped by the
quotient automatically, so the same promoted dependency graph drives
hypothetical planner assignments and the validator's actual one.

The retained call index also records implicit object conversions performed by
arithmetic, relational and loose-equality operators, the left operand of `in`,
numeric unary operators, updates and non-logical compound assignments. The
executor skips a conversion when value flow proves its operand primitive. For
object or unknown operands it follows the conservative synchronous callback
path, which covers possible `[Symbol.toPrimitive]`, `valueOf` and `toString`
callbacks without trying to predict which method the runtime selects. Strict
equality, short-circuit operators, `typeof`, logical negation and `instanceof`
do not add a ToPrimitive call. These retained facts are collected during the
existing AST visit; they do not trigger a second source scan. The operand's
retained lazy callback closure is also promoted, so an accessor returning a
second conversion callable remains visible without modeling the runtime's
method-selection order.

**Algorithm.** The call-site execution pass records callable identities,
argument and default values, returned values, and the local environment of a
created closure. For each at-init call, it follows only bodies that can execute
before suspension at that call site. Returning a closure does not execute its
body. A complete result lists the reads and rebinds reachable through those
calls; each promoted edge retains the source location of the statement that
contains its lexical callee for the cross-module realizability check. Separate
calls to the same factory keep
separate argument environments. The pass memoizes call contexts and bounds
both recursion depth and the total number of body expansions per source
statement. Changing prefix revisions cannot reset that expansion budget. At
the bound, on reentry into an active callback, or when the callback pool would
create a large argument-value cross product, an opaque call summarizes its
callback pool together;
recursive or unknown targets, unsupported syntax, and mutable captures fall
back conservatively. Fallback read closures grow incrementally. For source statements
covered by retained callable facts, they follow and promote only reads that can
occur before a definite `await`; unsupported source statements retain their whole lazy
read set. Rebinds remain conservative. Callbacks retained by a fallback remain
escaped, so their synchronous read closure grows incrementally across source
statements. The directly truncated callable uses the shared SCC summary without
escaping its private roots. Applying either summary rechecks initialization at
the active source prefix; per-statement outcomes are reset at the next prefix.
Fallback effects accumulate directly in that statement's summary and are merged
once, rather than copied through every nested invocation. Rebinds still update
the prefix environment immediately. Callback discovery visits each closure's capture snapshot once,
including snapshots shared through several captured bindings. The executor
interns immutable capture contexts bottom-up, so value-set and invocation-key
comparisons use canonical identities rather than recursively comparing the
capture DAG. Callback discovery batches all exports or all arguments into one
traversal, so aliases share visited sets for objects, closures, and escaped
values.

The summary also records reads at the callback's source prefix. For each
binding with an ordinary eager or callback read, the graph builds a source-order
timeline of readers and known writers, including lexical and `var`
initialization, eager rebinds, synchronous-call rebinds, and trusted local
mutations. Direct
`Sequenced` edges preserve writer order and place each callback or ordinary
eager reader against its nearest preceding and following writer. This keeps
earlier values before their readers and every later writer after those reads
without a read-by-writer cross product or unnecessary reader-to-reader order.
For a read known to occur before initialization,
the graph additionally keeps a direct `EagerUse(caller → declaration)` and,
when that declaration is later in source order, a reverse
`Sequenced(declaration → caller)` edge. Their constraining cycle keeps the
callback and declaration together; the declaration then runs in original
source order. This constraint uses the synchronous callsite's own summary,
rather than the ordinary promoted-read role that the lenient projection may
drop when the known callee lives in another module. Unknown writes still use
the conservative S-chain fallback.

The `Object.assign` fast path requires static data-property source keys that
cannot be `__proto__`, and an unrebound local function target whose descriptor
and prototype have not been changed or exposed to another callable. Static
ordinary data-property writes on that fresh function retain the proof under
A11. When the proof fails, copying may run a setter or Proxy trap. The pass
keeps any reads it proved, adds the normal unresolved-call fallback, and
conservatively promotes nested lazy reads of source statements already executed while
constructing the target and source values. For a top-level call it also keeps
such reads from earlier effectful statements, which may have installed an
accessor. After an earlier unknown call, the fallback follows synchronous source-statement
read edges from earlier facts, the active call site's eager reads and rebinds,
and executed callables. Where retained callable-prefix coverage is complete,
reads after a definite `await` do not extend this reachability. Uncovered
source statements keep their whole lazy reads. The closure includes externally reachable
exports: hoisted function declarations are available from instantiation;
other values enter when their declarations execute. Export lists themselves
are linkage metadata and do not initialize their bindings. Direct `eval` or
`with` in a reachable source statement makes reachability incomplete and retains the
whole-chunk fallback. This broader promotion is confined to the unproved
intrinsic call; creating a returned closure alone does not trigger it.

During materialization, a target may omit those synthetic export roots only
when the original static-import facts prove a closed ordinary-ESM dependency
closure, with no top-level `await`, CJS interop, vendor rewrite, or pruned
chunk, and the target belongs to a singleton static-import SCC. Ordinary ESM
importers cannot run their bodies until the dependency finishes evaluation; a
cycle, including a self-import, is the only static route for an importer to
call a hoisted export while that evaluation is paused. The proof is per target,
so unrelated open or cyclic chunks do not disable it. Unknown edges,
unsupported import phases or attributes, and ineligible module syntax keep the
conservative export roots.
Executed or otherwise locally leaked callables remain roots under either
policy.

For each selected chunk, materialization records the decision and every
independently observed blocker in `export_reentry.json`, beside
`dependency_graph.json`. `global_blockers` names artifact incompleteness, CJS
interop, and nonordinary static requests across the artifact. The per-target
`closure_blockers` names non-ESM, top-level-await, CJS, and vendor-affected
closure chunks; unsupported, unresolved, missing, or non-entry static
requests; and a target cycle. Request blockers carry their source chunk and
specifier, plus the resolved chunk/file when resolution succeeded. The report
does not change the proof: any blocker keeps `policy: conservative`. Full
reports include accepted and rejected chunks; `--dry-run` writes it only for a
rejection, preserving accepted dry-run's no-report contract.

Property writes and updates have a separate accessor-effect summary. The
index records setter definitions by static property name and source statement.
For a potentially matching write, the executor promotes the setter statement's
lazy reads and rebinds when that statement may have participated in constructing
the receiver or run earlier at top level. Unknown receivers broaden the source-statement
set. After an earlier unknown call, possible accessor effects include the
synchronous source-statement closure of the execution prefix, active call site, and
available exports. Unsupported execution syntax still contributes all of its
syntactic reads to that closure; it does not expose unreferenced locals.
Dynamic lexical scope retains the whole-chunk fallback because its binding
references need not appear in the AST. Unknown accessor effects retain the
actual top-level caller statement as their promotion site, and memoized invocations
include that caller and the reachability revision. A static data-property
write to a proved fresh local function retains the same A11 fast path. This
keeps unrelated returned closures deferred when no setter can be reached.
The adjacent fresh-value proof also identifies a top-level write to an
initialized own data field of a constructorless local class instance. That
write cannot invoke an inherited setter, even after an earlier unknown call.
Its assignment and execution-order effects remain. Callable-body writes and
writes after deletion, descriptor replacement, or an intervening statement
still use the accessor fallback.

A closed inline mutation IIFE can also prove its receiver fresh at that call,
independently of whether the returned object escapes later. The shared enum-IIFE
recognizer requires a synchronous ordinary callable, one identifier parameter,
primitive property writes with safe keys, and return of that same parameter.
An empty object argument is fresh directly. A self short-circuit argument also
requires the sole declaration/writer of a `var` binding and no dynamic lexical
scope in the chunk. Only generic accessor promotion for that body's writes is
omitted; argument reads, assignments, returned values, and callback effects at
other calls remain. Async/generator bodies, prototype keys (including keys
produced by nested assignments), other writers, and custom receivers retain the
conservative path.

For statements without a complete call-site result, `promote_at_init_calls`
uses its SCC closure: it builds the call graph among chunk-declared, resolvable
callees from first-order lazy calls, takes its SCCs, and closes each SCC's
first-order lazy reads and rebinds over its successor SCCs in reverse
topological order. Then each at-init callee contributes promoted edges to the
bindings in that closure, augmented by the unresolved-call fallback below.
Rebind targets are promoted as `EagerUse` in either path, an order constraint
only: write legality is enforced by the direct `LazyRebind` /
`DeferredRebind` edge at the write site.

An `await` cuts off synchronous effects only on paths that must reach it.
Conditional branches are joined conservatively; loops, switches, and
try/catch/finally retain effects across possible paths. Under A11, the
call-site pass also recognizes `new WeakMap()` and `Object.assign` to a fresh
local function from plain object-literal data sources. A local class declaration
also has a narrow constructor path: its binding may only be used as a direct
`new` target or as the right operand of `instanceof`, and the class may have no
superclass, decorators, static initializers, computed method keys, or setter.
Instance fields may be uninitialized or use an initializer accepted by the
shared implicit-class certificate, including the exact inert `void 0` form;
arbitrary `void` operands remain excluded. The constructor
may only assign identifiers, literals, or properties read from fresh plain data
objects to static `this` properties, including comma sequences and simple
nullish/equality expressions and Boolean negation of these values. Negation
still checks its operand; getters and calls retain their effects. A write or opaque exposure invalidates the
plain-data proof. At `new`, the pass invokes the indexed constructor and retains
its argument graph and class methods on the instance value, so later member
calls and publication can still expose stored callbacks. The checked write
names also establish own-data member reads for that untainted instance; a
subsequent call still follows the retained-callback path. Writes to static
`this` properties in this checked constructor are writes to a fresh instance;
they do not publish their values through an earlier opaque call. A same-name
prototype getter rejects this path because assignment would throw. Reflective access to
an instance invalidates later construction of that class. A direct zero-argument
`new` of a constructorless, fieldless class only allocates an instance, so an
escaped class identity does not make that allocation run a method or accessor.
Unknown or reassigned callee alternatives retain the opaque call path. Other
constructor and member calls remain unknown and take the conservative path. Generator calls
also take that path because their parameter defaults execute at call time
while their bodies wait for iteration; spread arguments may likewise execute
iterator code before the callee runs.

A single-binding top-level call to a uniquely known local wrapper can produce
an unpublished checked instance graph. A source certificate requires one
returned `new` expression; all nested `new` expressions must use inert local
classes, and the wrapper's indexed calls must be exactly those allocations.
The wrapper accepts only simple parameters bound to inline functions, primitive
values, or fresh plain-data records. A record may contain a dense fresh array
of the same inputs. An exact named member read from a single-initializer TS
enum IIFE is admitted only after initialization when its binding is stable
PlainData and that member is an own primitive data property. The ordinary
prototype guard also applies. Array holes and spreads, enum mutation or
escape, and unknown member keys retain conservative reachability. Admitted
member reads still contribute their ordinary eager dependencies and execution
effects. The same constructor eligibility checks
used by actual execution validate each class, and constructor property reads
must resolve to own data or a safe missing property under A11. The analyzer
executes the wrapper and constructors normally and keeps their synchronous
effects and source ordering. For this certified initializer only, those executed
source statements are not treated as published callable or accessor capabilities. A later
call, escape, or export of the result follows the ordinary instance value model.
If actual execution is incomplete, reachability falls back to the initializer
and its checked source statements. Unsupported shapes keep ordinary roots.

The same call-site pass recognizes an ordinary unshadowed `new Map` over one
inline dense array whose entries are inline dense two-element arrays. Evaluating
the array expressions still runs their effects, but Map construction does not
invoke callbacks stored as entry keys or values. The resulting abstract Map
retains those callbacks: a later member operation or opaque call can expose
them and conservatively promote their effects. Aliased iterables, spreads,
holes, non-array entries, reassigned or shadowed `Map`, and any dynamic-scope
source statement in the chunk keep the opaque constructor path.

**Hoisted-target filter.** Promoted reads filter out targets whose
source statement is a `FnDecl` — function declarations are hoisted at Phase-1
module instantiation, so reading them from another module never
observes a TDZ. Without this filter, mutual recursion across modules
(`function even(){odd()}` / `function odd(){even()}` split into
mod_a / mod_b) would spuriously close a constraining-edge cycle that
isn't actually unrealizable at runtime.

**Per-statement dedup.** A single at-init call to a function with N
transitive lazy reads would otherwise emit N edges from the caller
statement, and multiple at-init calls in the same statement would
multiply that. The promotion pass dedupes per `(caller, target source statement)`
pair, keeping the per-statement cost bounded by the
transitive closure size rather than (closure × call-sites).

**SCC-path resolvable callees.** In the fallback path, a callee is precisely resolvable iff its
binding is declared _directly as a callable value_ (`function f`,
single-declarator `const f = () => ...`, `class F`, exported forms)
and is never rebound anywhere in the chunk. The chunk value-flow
pass also finds targets through local assignments, parameters,
returns and static object properties. Resolvable callees feed the
first-order closure above.

**Unresolved-callee fallback.** Calls that cannot be fully resolved —
dynamic member calls, IIFEs, optional-chain calls, tagged templates,
conditional or externally supplied function values, and calls into
resolvable functions whose own first-order bodies contain such calls — take a
conservative fallback (`UnresolvedCallFallback` in `graph/build.rs`). The
fallback's premise: whatever function value the call invokes must
have reached the call site through a binding the call expression
mentions (callee root, arguments, computed keys) or through an
inline function expression at the call. Each such root source statement is
expanded to the **full lazy closure** of every source statement reachable from
it through the chunk's read graph (eager + lazy reads), and the
calling statement gets promoted `EagerUse` edges to every collected
target — rebind targets included (order-only: write legality is
separately enforced by the direct `LazyRebind`/`DeferredRebind`
edge from the statement lexically containing the write).
Fallback edges sourced in entry are dropped during assignment
projection: entry is the ESM DFS root and evaluates last, so an
at-init call from entry code cannot observe a TDZ, and the
emitter emits no entry-side phantom imports the gate could
otherwise assume.

Within synchronous value flow, recursion and budget limits summarize both
escaped callbacks and the directly truncated callable through retained
synchronous statement adjacency and prefix reads. Complete raw rebinds remain
conservative. Private truncated roots are expanded once per source statement
without adding them to the persistent escaped-callback cache. Incomplete
reachability retains whole-body fallback.

**Inherited class effects.** When an opaque use can invoke a class's
constructor, field initializers, methods, or accessors, promotion follows the
class's retained superclass value too. For a top-level class declaration with
a simple binding in its `extends` clause, the execution index records the
possible superclass values at that declaration's source prefix. Later writes
to the binding do not replace the base captured by the class. Complex or
unknown heritage keeps the conservative fallback.

**Audited callback-storage APIs.** Member-specific
`no_sync_callback_members` hints narrow only the fallback roots for
calls like `registry.register(fn)` or `state.set({ onDone() {} })`
where the audited API stores callback-like arguments without
synchronously invoking them. Receiver, callee, and non-callback
arguments still evaluate and still feed normal read/rebind/effect
summaries; the call also remains impure for S-chain ordering unless a
separate purity hint applies. The hint suppresses the conservative
"the callback may have run now" edge for inline functions, object
literals containing functions, and first-order argument callbacks.
The synchronous value-flow summary honors the same argument exemption for
static event-listener registrations. Neither exemption suppresses reentry
through callbacks already escaped to host code or available exports.
Purity annotations alone do not assert that callback arguments are deferred.
The separate exact-signature `no_reentry_callback_exports` author contract
covers ordinary direct imported calls that cannot reenter any caller, importer
or host callback during the call. Checked callbacks become globally escaped
roots immediately afterward, even if the provider only returns them. The result
remains opaque. Later unknown calls and protocol effects retain the ordinary
promotion rules; this does not grant the return-only factory contract.
Property writes inside collection callbacks retain the conservative accessor
fallback when receiver integrity is unproved.

**Audited direct imports without reentry.**
`chunk_export_purity.<defining chunk>.no_reentry_exports` projects onto
resolved direct import bindings. When a direct call has only checked primitive
arguments and no spread, an author-audited stable export cannot execute
caller/importer/host callbacks or access importer-owned lexical state before
returning. The evaluator skips opaque callback promotion for that call only.
The call can still have provider-private effects; its existing purity and
ordering classification is unchanged, and it produces an `Unknown` result.
Importer argument effects and provider module
initialization stay in the ordinary model. Aliases, construction, and uncertain
arguments retain the conservative fallback; the author must audit exported
identity changes and every reachable provider path.

**Audited namespace calls without reentry.**
`chunk_export_purity.<defining chunk>.no_reentry_members` is an author assertion
about synchronous call timing for checked primitive arguments. The separate
`no_reentry_record_members` assertion covers direct static calls with at least
one fresh object literal argument and only recursively checked inert values
under static data keys. A primitive-only assertion never admits records. For
either admitted call shape, the provider cannot invoke caller/importer/host
callbacks or access importer-owned lexical state before returning. The author
must audit the provider branch for each admitted record shape, including any
property reads, coercion, or retention of the object and its prototype; the
call-site syntax alone says nothing about the provider's behavior. The imported
namespace member must also have `namespace_independent_members` lookup integrity.
The shared namespace identity, mutation, and publication checks must pass.
The evaluator skips only opaque callback promotion for the admitted call. It keeps
the result `Unknown`, visits argument effects, and leaves purity/S-chain
classification unchanged. Construction, aliases, computed/optional lookups,
uncertain arguments, and changed member identities use the ordinary fallback.

**Audited deferred browser-global publication.**
`chunk_analysis_options.<chunk>.deferred_global_publications` certifies an
ordinary inert own data slot on `globalThis` or `window`, or an absent slot
whose creation cannot invoke an inherited setter and is permitted by the
object. It also certifies no synchronous host/importer/source consumption of
the stored capability before this chunk finishes initialization. It does not
make the write or its RHS pure. A
structurally checked whole assignment of a known function/class, optionally
behind a checked `typeof window` guard, keeps all eager dataflow and ordering
effects but does not put that stored capability into the opaque-call callback
pool or possible setter pool. A direct call, independent export, separate
publication, source read, ambiguous alias, global-root replacement, dynamic
global access, or visible descriptor installation uses the conservative path.
The host descriptor and external-consumer conditions are author-audited; they
are not inferred from the source scan or intrinsic-integrity assumption.
Primitive operator results and the synthetic `in` protocol call with a
literal key and direct global-object RHS do not themselves publish the global
object as a value. The operator and operand effects still execute and
participate in ordinary reentry analysis.
An earlier anonymous, zero-argument synchronous function IIFE with a
discarded result can use a confined local alias to the global object before
the certified publication. Its alias and derived values cannot leave that
invocation, access the certified slot, or reach an uncertain call.

**Audited builtin-global membership.**
`chunk_analysis_options.<chunk>.non_reentrant_global_membership` separately
certifies protocol nonreentry for listed literal-string `in` probes on a direct
unshadowed `globalThis` or `window`. The host's exact identity and relevant
prototype/alias behavior must remain intact at every admitted probe, including
hidden host and cross-chunk paths. This is an explicit author premise, not
inferred builtin trust or a consequence of deferred global publication. Scoped
bindings, aliases, uncertain keys/receivers, dynamic scope, and recognized root,
prototype, Object-intrinsic or exposure mutations keep conservative fallback.
The existing confined earlier-IIFE alias proof can certify a nonescaping alias
that never touches protected keys or prototype/identity slots. The evaluator
still indexes/evaluates operands and retains purity, throw and dependency
checks; it suppresses only this exact host protocol's callback reentry.
See [the full contract](../SPEC.md#audited-builtin-global-membership-probes).

**Audited callback-factory exports.**
`chunk_export_purity.<defining chunk>.callback_factory_exports` and
`callback_factory_members` are separate callback-flow contracts. The latter
names static members on an imported namespace export; `pure_members` does not
imply callback deferral. The analyzer uses these contracts only when an importer
calls a listed target with exactly one non-spread inline function or arrow
callback, or a retained result from another audited callback factory;
construction and other call shapes keep the conservative fallback. The
provider must neither invoke that callback nor synchronously invoke caller-,
host-, or export-provided callbacks, and must not publish the callback outside
the returned fresh ordinary callable/plain-object graph. It must not
synchronously access importer-owned state through another route, including
imported lexical cells or caller-owned objects. Provider-private effects are
allowed. A retained input must be a fresh ordinary callable/plain-object graph
from another audited factory; the outer factory must return another such graph
that retains the original callback. The consumer may later reach or invoke the
captured callback through that result. Authors must audit all provider and type
branches, helpers, callback-bearing property access on both input and result,
and prototype integrity; neither graph may contain proxies or accessors at the
call.

A pure, single-binding declaration whose only top-level call is a direct
named import covered by `callback_factory_exports` can create its fresh result
without making the retained callback reachable through an earlier opaque call.
This root exclusion requires one inline function or arrow argument and a
definite initializer. It does not apply to namespace member calls or arbitrary
factory aliases. A subsequent call, export, or publication of the result still
promotes the retained callback through the value-flow model.

`inline_callback_factory_exports` is a named-export contract for providers
audited only with one non-spread syntactic inline function or arrow argument.
The provider returns a fresh ordinary callable retaining that callback and
cannot invoke or publish it during construction. The same importer-state
restriction applies. A retained result from another factory, a named callback,
spread arguments, or construction does not use this contract. The imported
binding identity follows the ordinary export/import resolver, including local
renames; this callback-flow fact does not imply purity.

`descriptor_copy_factory_exports` has its own author premise and source-checked
call certificate. The shared object-shape collector proves a fresh static
literal and exact own descriptors; the imported callable summary supplies
provider identity and the audited descriptor-copy behavior. The interpreter
retains copied getters in the returned object and promotes them on reads,
while preserving the registration call's ordinary ordering and throws. This
contract does not make accessor-bearing arguments eligible for the older
callback-factory rules.

The separate `retained_callback_factory_exports` and
`retained_callback_factory_members` assertions admit explicitly enumerated
arities and callback positions, including stable local named callbacks. The
call index retains the syntax/primitive-position check from the single parse;
the interpreter checks actual callable values and unions the retained callback
graphs at both value production and synchronous call execution. Namespace
privacy checks cover the union of independently audited factory members, while
the old and stronger admission rules remain separate. Unsupported values,
mutation, rebinding, exposure, spreads and construction keep ordinary fallback.
The per-call-site audit additionally covers nonfresh callable identity, aliases,
property/prototype capabilities and exclusion of additional callbacks or
cross-instance state in the returned graph; the complete contract is in
<../SPEC.md> § Retaining existing or multiple callbacks. This is an explicit
author assumption, never an inference from a provider's name.

Invoking a retained result promotes its callbacks through the shared synchronous
statement adjacency and retained prefix reads. This includes callbacks returned
before suspension while excluding reads after an unconditional await. Complete
raw rebinds remain conservative; incomplete reachability or uncovered callable
surfaces retain whole-body read effects.

Callable own/prototype properties are not tracked as ordinary object records.
Installing nonprimitive values through a recognized callable receiver or local
alias therefore promotes the supplied callbacks conservatively at the write.
This also covers nested prototype writes: otherwise an opaque factory could
return the original function and expose an untracked `result.invoke.extra()`
callback. Primitive metadata writes keep their existing narrow exception.
Opaque consumers can also retain and invoke a passed callback's synchronous
return value. The fallback promotes those returned callback graphs through the
same bounded, cycle-safe source closure, preserving unconditional-await
boundaries rather than stopping at the first invocation.

Opaque class and instance values can expose constructors, instance fields,
methods, and accessors to foreign code. Their callback promotion uses the same
synchronous statement adjacency and retained prefix reads, with the source closure
cached per class. Uncovered source statements retain raw lazy reads and all raw lazy
rebinds; incomplete reachability uses the original whole-body closure.

### Namespace capability privacy

`namespace_independent_members` is a per-member author capability summary,
separate from purity and callback timing. A listed lookup is stable, runs no
caller/importer/host code, and cannot expose the namespace or a capability that
replaces audited factories. Ordinary own data properties and explicitly audited
inert forwarding accessors are possible implementations; primitive return values
alone do not establish this property. The value and its reachable operations,
properties, prototypes, constructors, accessors, closures, and returned callbacks
must preserve that guarantee. This includes calls and tags with the namespace as
implicit receiver. Only independently supplied argument/receiver capabilities can
be propagated as inputs; that implicit receiver is excluded from the exception.

The author identifies all imported exports that could alias the same namespace
family, and A14's provider/runtime input audit excludes preexisting external
aliases and rewrite capabilities. The existing ExecutionIndex visitor classifies
all configured roots together, using resolved identifiers. Each occurrence must
be the object of a direct, static, listed member read. Assignment targets,
including patterns and loop heads, updates, deletes, whole-value uses, exports,
computed/optional lookups, missing member summaries, and dynamic scope defeat
this sufficient proof. Import declarations are linkage rather than uses.
Declarations and assignment targets require explicit handling because the
execution visitor ordinarily skips binding identifiers and named exports.

Under those preconditions no namespace capability can reach an unrelated local
unknown value, receiver, getter, or class surface: the explicit roots are consumed
only by audited independent lookups, and external routes are covered by A14.
Namespace admission can therefore omit its general provenance guards for this
family. If any precondition fails, the existing provenance proof must certify every
configured family root against the union of audited factory members. Import
names do not prove distinct object identities. With no independent-member
summaries, namespace admission retains its existing behavior. This does not change the ValueExpr lattice or claim that arbitrary `this`
values are disjoint. All member-result operations, callback arguments, exports,
reentry, and effect ordering still run through ordinary analysis. Factory timing
requires its separate existing contract; privacy cannot defer an early invocation
of a retained callback result.

Nested audited factories retain the original callback through each returned
graph. Invoking or publishing a result before the captured lexical cells are
initialized reactivates the ordinary callback-read analysis. Namespace member
contracts require an unmodified imported namespace target; reassigned or
dynamic targets fall back to opaque calls. Their authors must audit the provider
and every caller/runtime path supplying values used by admitted callbacks,
establishing that listed factory members retain their identities and behavior
through the analyzed chunk's synchronous initialization. That includes
checking that provider or other namespace members and callback/ref/hook inputs
cannot expose an alias that replaces a listed factory member. Direct static
identifier-property namespace call results are treated as independent only
under this explicit author assertion. The checker propagates explicit
namespace provenance through assignments, local aliases, property values,
returns, and call arguments. An unrelated call or constructor result with no
such path is not treated as a namespace alias solely because its runtime value
is unknown; the author audit asserts that values used by admitted callbacks
cannot alias or replace listed members. Unsupported syntax and unsupported
destructuring results remain possible namespace values, and that uncertainty
follows a local binding to later publication or call-argument sinks. For a
recognized local class, publication checks aggregate the retained static
fields or instance fields for the class value or allocation. A member
projection checks the whole receiver conservatively, so an unrelated sibling
field can also make the receiver a possible namespace carrier. On the
constructor surface, retained static fields are followed through a uniquely
resolved local class superclass. Unknown, imported, reassigned, or non-class
bases and superclass cycles remain conservative. Instance inheritance, static
accessors, decorators, and unsupported class surfaces remain opaque. Method
bodies keep their existing independent return and effect analysis. For
call-argument checks, the checker follows namespace values in
the supplied argument expressions and excludes the callee expression, which
is not passed as an argument. A value from an unknown call or constructor
used as a static or dynamic write receiver remains a possible namespace alias,
including through local aliases and member projections, and disables the
contract. The checker does not prove that arbitrary callback parameters or
unrelated call results are disjoint from the namespace; that non-aliasing
guarantee is part of the author audit. Computed, chained, and unknown member
results remain conservative when used as write receivers.

The receiver check exempts an adjacent run of assignments to declared own
data fields after a fresh local base-class allocation. Retained class facts
must prove that allocation runs no user code; each assignment must use that
same binding, a known own field, and a value that runs no user code. The
instance cannot escape or acquire an accessor during this run. Any intervening
statement, unsupported class surface, or dynamic scope ends the proof and
keeps the ordinary conservative write checks. Array iterator member values
use A11's intrinsic integrity assumption.

A fresh top-level object literal that visibly contains the namespace and has
verified static own data properties has an identity distinct from namespace
values stored in those properties. The
retained value of a one-hop property read supplies namespace provenance; the
container still publishes any namespace value it contains when passed to a
sink. Adjacent writes to existing own data properties use the private
fresh-value proof. Accessors, computed keys, spreads, `__proto__`, and
unsupported projections keep the conservative receiver checks. Literals with
no explicit namespace path also keep those checks, because a later accessor
could return the namespace.

Unary, update, and
non-logical binary expressions have primitive results on successful
evaluation; their operand expressions remain in value flow so nested calls
and unavailable reads are still analyzed. Conditional expressions and
`&&`, `||`, and `??` retain both possible result values because they can
return an object or callback. Receivers with no syntactic path to the
namespace rely on the author's audited non-aliasing assertion; this is a
trusted contract, not a proof that arbitrary callback parameters are
disjoint from the namespace.
The fresh-result guarantee also permits adjacent static property writes after
a direct audited factory call with one inline callback. The receiver must stay
private and the assigned value must run no user code; prototype writes and
intervening statements end the proof. Namespace admission is recomputed after
each removal, so a result from a rejected namespace factory cannot justify
another namespace's admission. Other result writes keep the receiver fallback.
Inside a literal traversed within an admitted inline callback, a direct static
namespace member used as a call callee is not treated as a captured namespace
value. The call's arguments and effects still flow through normal analysis;
computed and chained callees remain conservative.

Authors must also audit one-hop static namespace member reads used as callback
result object data properties. The checker treats that direct shape as a
deferred member value but cannot prove the member's runtime identity or inspect
accessor, proxy, or callback behavior reachable through it. The object-literal
`__proto__` prototype setter is not an ordinary data property and stays
conservative. Computed reads, chains, calls, spreads, arrays, and other forms
remain conservative. Direct namespace values and aliases remain rejected.
Calls remain impure for S-chain ordering unless a separate purity contract
applies.
Without an author assertion, source inference recognizes only
immutable exported function declarations with one simple identifier parameter.
Their body must only return a zero-argument function or arrow that only calls
that parameter with no arguments. Reassigned bindings and dynamic scopes stay
opaque.

**Residual limitations.** Dynamic global/property stashes and values
arriving from other chunks are outside the identity-based local flow
model. The unresolved-call fallback applies when their possible
targets cannot be proved. Direct `eval` inside a lazy function remains
unsupported: moving the function changes its lexical scope, and the
current chunk-admission check sees only eager eval.

## Lemma 2: entry-side import ordering

Clause 3's Pass 1 (no multi-module SCC in the constraining-edge
subgraph of `Q`) admits mixed cycles in the imports graph `I` — cycles
whose back-edges are `LazyUse`. For the ESM linker to actually evaluate
these without TDZ, the entry module's `import` directives must be emitted
in a specific source order so that depth-first link traversal lands on a
Phase-2 evaluation order matching the constraining-edge linearization;
Pass 2 (below) checks that it does.

The materializer computes the source-import order on
`esm_import_order::EsmImportOrder` (held by `AssignmentAnalysis`)
to drive this. The algorithm:

1. Compute SCCs of the full quotient `I ∪ S` via Tarjan.
2. Each SCC is assigned a dependency rank = the minimum
   `linker_position` of its members. `linker_position` itself comes
   from a toposort of the **constraining-edge subgraph** (which is
   acyclic per clause 3, even when `I ∪ S` is cyclic).
3. Sort all modules by `(SCC dep rank ascending, intra-SCC
linker_position DESCENDING)`.

For acyclic shapes every SCC is a singleton, so the within-SCC
reverse is a no-op and the order matches `linker_order`
(dependency-first source). For cyclic-I shapes the within-SCC
reverse is load-bearing: ESM Phase-2 evaluates by recursing into
imports before running the module's own init, so DFS into the
FIRST imported SCC member traverses the cycle and finalizes its
constraining dependency LAST in post-order. Reversing within the
SCC means the dependent is imported first; DFS unwinds through the
dependency; post-order evaluates dependency first.

For the canonical
[mod_a (lazy → mod_b) + mod_b (eager → mod_a)] case the algorithm
produces entry imports `[mod_b, mod_a]` so the linker DFS visits
mod_b → mod_a → mod_b (cycle no-op) and evaluates mod_a first, then
mod_b. mod_b's at-init read of A succeeds.

**Asymmetric-I-cycle gating: a runtime-DFS simulator.** Lemma 2's
reversal trick only rescues an asymmetric I-cycle when
ECMA-262's actual evaluation DFS, run over the materializer's
import-order choices, lands every constraining edge's target
strictly before its source. Two facts about the emitted structure
decide that:

1. **The entry imports every logical module.** The emitted entry
   file carries one `import` directive per plan — named imports
   for binding-owning plans, side-effect-only
   `import "./<plan>.js";` for binding-less plans (anonymous-
   statement-only modules) — in `source_import_position` order.
   ESM DFS therefore always enters an SCC without entry at its
   most-dependent member (Lemma 2's intra-SCC reversal) before
   any mediator's dependency-first imports can reach it. This is
   why a mediator outside entry reaching into an SCC does NOT
   reject: the entry's own import of the SCC's dependent wins
   the race (pinned by
   `e2e/mediator_reaches_asymmetric_cycle_test` and
   `e2e/asymmetric_non_residual_cycle_test`).
2. **Entry in the cycle, with a constraining edge whose
   target is entry, always TDZs.** Entry is the ESM DFS
   root — post-order evaluates every other cycle member first,
   then entry. A constraining read of entry's
   `class`/`const`/`let` bindings TDZs. ESM hoists every
   `import` above any statement, so no source-order trick fixes
   it.

The realizability primitive (`check_realizability` and the
matching `IncrementalQuotient::verdict*` helpers) makes the
verdict by:

1. Pass 1: Tarjan over the constraining-edge subgraph. Any
   multi-module SCC there is a mutual-eager cycle that no source
   order can satisfy. Reject.
2. Pass 2: Tarjan over the full I-graph (constraining ∪ lazy).
   For every multi-module SCC carrying at least one constraining
   edge, run an in-process **ESM evaluation simulator** rooted at
   entry:
   - At entry, fan out to EVERY module in the I-graph (the
     entry's universal per-plan imports), in entry-import order
     (`EsmImportOrder::sort_entry_imports` — Lemma 2's intra-SCC
     reverse of constraining linker order).
   - At every other module, visit its I-successors in
     module-import order (`EsmImportOrder::sort_module_imports`
     — dependency-first toposort of the constraining subgraph,
     `ModuleId` tie-break).
   - Walk DFS; record each module's post-order index when its
     body would evaluate.
   - For each constraining edge `(M, X)` inside the SCC, demand
     `post_order[X] < post_order[M]`. Any violation = TDZ at
     runtime → reject the SCC.

SCC results belong to the graph used to compute them. Reuse between the gate,
module quotient reports, and other graph walks requires identical edge sets
and isolated-node handling; sharing a vertex type or a Tarjan implementation
does not establish that equivalence.

The source I-graph omits entry's implicit imports of every emitted module.
Independently of its SCCs, reject every constraining dependency **into entry**:
entry evaluates last, so that dependency cannot be satisfied. The pure reference,
committed/overlay verdicts and boolean tier ladder all enforce this guard and
report the offending dependency edges as `EsmEvaluationTdz`. There is no need to inflate
the maintained I-graph with universal entry fan-out: a violated pair targeting
any non-entry ancestor already has a return path in the source I-graph and is
covered by its ordinary SCC check.

Pure-lazy I-cycles (no constraining edge inside the SCC) skip
pass 2's simulator and pass. The simulator and the emitter share
ONE ordering implementation — `esm_import_order::EsmImportOrder`,
built from the canonical `ChunkConstrainingEdgeSet` — so the
import order the gate simulates is structurally the order
`lowering::lower_chunk` emits: the entry's import list and every
module's merged intra-chunk import list (cross-module binding
imports, phantom side-effect imports, and the residual-entry
import, interleaved by one sort) come from the same two sort
rules the simulator's DFS uses for its neighbor order. A spec
accepted by the gate is one the emitted ESM bundle actually
evaluates without TDZ.

Because Lemma 2 is implemented in the materializer and the
gate's simulator decides asymmetric I-SCCs precisely, the
validator (`validate_assignment` in
`validation.rs`) and read-only planner
checks share the realizability primitive's verdict — a
`peelable_now` proposal is a peel the gate accepts and Node will
execute correctly at runtime (the contract pinned by
`e2e/lemma_two_rescued_asymmetric_cycle_test`,
`e2e/mediator_reaches_asymmetric_cycle_test`, and
`e2e/runtime_tdz_on_imported_class_test`).

## The realizability theorem

> **Theorem (correctness).** Under assumptions A1–A11, if the realizability
> gate accepts a spec, the source-order emit construction below produces a
> multi-module ESM bundle observationally equivalent to the input chunk.
>
> **Gate.** The gate accepts iff no rebinding write crosses a module boundary
> (clause 2) and the quotient's I-graph passes both passes of clause 3
> ("Lemma 2: entry-side import ordering"):
>
> 1. **Pass 1.** The constraining-edge subgraph (`R ∪ S`) has no multi-module
>    SCC.
> 2. **Pass 2.** In every multi-module SCC of the full I-graph (constraining ∪
>    lazy) that contains a constraining edge, the ESM evaluation simulator
>    evaluates each constraining edge's target before its source under the
>    emitted import order.
>
> A cycle with no constraining edge (every cross-module edge lazy) is accepted
> without simulation.
>
> **Implementation.** The implementation computes per-statement
> facts, builds the dependency graph, then quotients that graph by the
> spec's destination assignment to derive the module dep graph. Each
> cross-module edge keeps dependency-edge provenance: the triggering
> statement, optional binding, and whether the reason is an at-init
> read, lazy read, or side-effect-order constraint. Diagnostics can
> explain both the module-level cycle and the lower-level dependency edges
> that induced it.

Here "spec assignment" means whatever the validator sees: every
declared binding either has an explicit `owner` from the spec or
follows the chunk's `unassigned_mode` (see [Spec explicitness
and diagnostics](#spec-explicitness-and-diagnostics)).
There is no implicit transformation between the spec and the
assignment the theorem reasons about.

### Why two passes

The validator builds the I-graph — every cross-module edge the emitter
realizes as an `import` directive, at-init or lazy, plus the `S` edges — and
judges it in two passes. Two weaker gates would be wrong:

1. **Pass 1 alone (`R ∪ S` acyclic).** A cycle in `I` whose constraining
   projection is acyclic still affects the linker: lazy reads emit `import`
   directives that the linker's DFS follows. Two modules that import each
   other — one direction at-init, the other lazy — have acyclic `R` but cyclic
   `I`; whether the at-init read TDZs depends on which member the DFS enters
   first, that is, on the emitted import order. Pass 2 simulates that order.
2. **Rejecting every I-cycle that contains a constraining edge.** Lemma 2
   steers the DFS into the cycle at its dependent member, so the dependency
   evaluates first, and Pass 2 accepts the cycles where that works. Rejecting
   all of them would force colocation of bindings whose only relationship is
   an eager read one way and a lazy reference back.

A cycle whose every cross-module edge is lazy needs neither pass to fail: no
read fires until every member has finished evaluating, so rejecting it would
over-restrict.

### Conditions on the input chunk (assumptions A1–A11)

The proof below reasons about ECMA-262 module evaluation under a
specific JS subset. If the input chunk falls outside this subset,
the proof does not apply and the materializer's correctness
contract holds only by happenstance. The subset is what real
production bundlers (esbuild, rollup, webpack with `output.module:
true`) emit by default, so it covers the Svelte/SvelteKit
output, the Vite ecosystem, and most React/Vue/Angular SPAs.

- **A1. No `eval` at module-top reading cross-module bindings
  dynamically.** `eval` opens an arbitrary read into the lexical
  scope at the call site; the static analyzer cannot see what it
  references; `I` would be incomplete. **Enforced (partially)**: the
  input-chunk admission scan (`chunk_admission`, run by
  `chunk_analysis::compute_chunk_analysis` next to the A2 bail)
  rejects direct `eval(...)` and seq-indirect `(0, eval)(...)` calls
  at module top level (looking through parens and comma sequences to
  the callee), with the offending statement ordinal in the
  diagnostic. Function-body `eval` and aliased `eval`
  (`const e = eval`) remain unchecked — see §"Coverage gaps".
- **A2. No top-level `await` in any emitted module.** TLA changes
  the linker's evaluation algorithm to AsyncCycleRoot semantics
  (TC39 §16.2.1.5.4 + §16.2.1.5.5). The proof's reverse-DFS
  argument doesn't apply unmodified — async modules form their own
  ordering hazards, and a cycle through two async modules has
  semantics this design has not analyzed. **Enforced**: the TLA
  scan runs inside fact analysis (`analyze_chunk` /
  `facts::analyze_chunk_structural` records the first module-top
  `AwaitExpr`, excluding lazy positions like
  function/arrow/method/getter/setter bodies and instance class
  fields), and `chunk_analysis::compute_chunk_analysis` `bail!`s
  with the offending statement ordinal as soon as fact analysis
  returns — before any quotient or lowering work. Production
  chunks we target are TLA-free in practice; the rejection turns
  the assumption into a verified precondition.
- **A3. No `import()` of debundled internal modules.** Dynamic
  imports of vendor / route chunks are fine — the proof treats
  those as black-box leaves with their own evaluation. Internal
  dynamic imports would route around the static `import` graph,
  invalidating `I` as a complete picture. **Enforced (partially)**:
  the admission scan resolves every string-literal `import(...)`
  specifier through the artifact indexes (the same resolution the
  specifier rewriter uses) and rejects, at any depth, specifiers
  that resolve back into the chunk being debundled — `I` is the
  per-chunk graph, so "internal" means same-chunk; other artifact
  chunks stay black-box leaves per the sentence above (a
  same-chunk-only rule also avoids over-flagging code-splitting
  bundlers, whose route-chunk `import()` thunks are everywhere).
  Non-literal specifiers in eager (module-top) position are also
  rejected — soundness over completeness: the target cannot be
  proven external. Non-literal specifiers in lazy positions are
  allowed and remain a documented residual gap (only `Lit::Str`
  counts as literal, matching `prepare_js_chunks` and the
  pass-through emission rewriter in `vendor/passthrough.rs`).
- **A4. No `with` blocks.** Banned in strict mode (which ESM is
  by spec). **Enforced (at parse)**: even with the parser's
  `no_early_errors: true` TypeScript syntax, `with` surfaces as a
  recoverable parse error and `js_ast` fails chunk loading on any
  recovered error, naming the strict-mode violation. Pinned by
  `e2e/chunk_admission_test.rs::with_block_is_rejected_at_parse`;
  no separate admission check is needed.
- **A5. No reflection on module namespaces during evaluation.**
  No `Function.prototype.toString` reads of cross-module bodies
  whose result depends on evaluation order; no `Reflect`-based
  property descriptor inspection on the module namespace object
  during link; no `import.meta` introspection that depends on
  order. (Standard `import.meta.url` is fine.) **Enforced
  (minimally)**: the admission scan rejects only the cheap,
  low-false-positive sub-shape — `import.meta` use at module top
  level beyond `import.meta.url` (named props like
  `import.meta.env`, computed `import.meta[...]`, and bare
  `import.meta` as a value). The other A5 sub-shapes
  (`Function.prototype.toString` reads, `Reflect` descriptor
  inspection, lazy-position `import.meta`) are deliberately
  unchecked: a precise analysis would over-flag mainstream
  bundles, which is the worse failure mode here. They remain
  input-shape contracts — see §"Coverage gaps".
- **A6. The input chunk runs to completion in the original
  bundler's load.** Observational equivalence has no baseline if
  the input chunk itself diverges, hangs, or throws during
  evaluation.
- **A7. Multi-chunk DAG.** Vendor / cross-chunk imports form a
  DAG with user chunks not below vendor. The multi-chunk lift
  unions every chunk's `I` and `S`; the same theorem applies to
  the union iff every chunk satisfies A1–A6 and the union graph
  is acyclic.
- **A8. Whitelisted globals are not shadowed at chunk top level.**
  `S`-edge construction consults a small purity whitelist —
  `Math.PI`, `Number.isNaN(...)`, `Boolean(...)`, etc. — that
  classifies certain built-in property reads and calls as `Pure`.
  Soundness depends on those names referring to the global
  built-ins; if a chunk re-declares any of them at module top
  level (`const Math = userland; …; Math.PI`), the call dispatches
  through the user-bound value, which can fire arbitrary code.
  The validator's classifier consults a `compute_shadowed_globals`
  pass and falls the whitelist back to `Unknown` for any
  shadowed name, so a chunk that does shadow remains sound (just
  conservatively over-restricted). The "no shadowing" form
  documented here is the _unrestricted-whitelist_ version: chunks
  that satisfy A8 reach the precise S-edge classification; chunks
  that violate it still validate correctly, with strictly more
  S edges than necessary.

  The whitelist itself is admission-restricted: every entry must
  fire no user-defined code on any argument value (no `ToNumber` /
  `ToString` / `ToPrimitive` coercion paths, no iterator protocol,
  no proxy traps, no own-property `[[Get]]`, no mutation). See
  <purity_soundness.md> "Admission rule: PURE_STATIC_CALLS" for the contract that
  governs adding new entries; soundness is preserved iff the
  contract is preserved.

- **A9. Spec-declared purity annotations are author-trusted.** The
  spec format admits optional per-member `purity:` values that
  assert a named binding has a specific side-effect contract the
  static classifier will not re-derive.

  `purity: "pure"` asserts that calls to the bound function value
  have no observable side effects. The validator does not re-verify
  the function body — the annotation is an explicit author override
  that wins over both the inferred classification and A8's
  shadowing fallback. Used when the function body is too dynamic
  for static analysis (dynamic dispatch, dynamic property access)
  but the author knows by construction that the binding is pure.

  `purity: "pure_new"` asserts the analogous constructor contract:
  `new BoundClass(...)` has no observable side effects beyond
  evaluating its constructor arguments. The analyzer still classifies
  every argument expression normally; impure arguments keep the
  surrounding `new` expression side-effecting. The annotation does
  not make plain `BoundClass(...)` calls pure.

  `initializer_purity: "pure"` applies only to one selected, simple,
  top-level variable initializer. It asserts that evaluating the full
  initializer has no observable effects and cannot throw, but it does not
  make calls or construction of the resulting value pure. The author must
  audit any synchronous callback behavior. Direct eager reads and
  callback-body read dependencies remain in the dependency graph, so this
  annotation does not suppress their initialization-order constraints.
  Imports, functions, classes, destructuring, uninitialized declarations,
  and ambiguous repeated initialized declarations are rejected.

  An incorrect annotation can produce a buggy debundle the same way
  an incorrect spec selector can — soundness shifts to the spec
  author. See <purity_soundness.md> "Declared purity".

  `chunk_export_purity.<chunk>.pure_exports` is the cross-module form
  of the same contract, addressed to the **defining** chunk rather than
  a consumer: it asserts that calls to the named exports of that chunk
  have no observable side effects. The cross-module purity oracle seeds
  these as pinned-`Pure` axioms (exempt from fixpoint demotion) and
  propagates them to every importing chunk, so a vendor factory that the
  static classifier cannot bless — or cannot even see as a chunk-top
  function (interop re-export) — is asserted once where the audited code
  lives instead of at each call site. Same author-trust contract;
  dangling assertions (no such analyzed export) warn and are ignored.

  Inferred export verdicts are observable only through named/default
  imports. The oracle prepares a provider's value flow only when at least
  one of those imports consumes an unasserted export. It still validates
  all assertions, includes transitive import dependencies, and leaves
  namespace member calls unresolved. This prunes unused inference without
  changing the greatest fixpoint seen by any imported binding.

  `chunk_export_purity.<chunk>.pure_new_exports` is the analogous
  cross-module constructor contract. It projects onto each importing
  chunk's local binding for the named export, admitting only
  `new LocalName(...)` when its arguments classify as pure. It does not
  assert purity for ordinary calls, class definition, or the export's
  initialization. Like `pure_exports`, it is an author assertion rather
  than inferred constructor-body purity; a missing or excluded defining
  chunk stays opaque. Constructor-body inference needs separate proof for
  `super`, instance field initializers, and writes through `this` (which can
  invoke a prototype setter); function-call purity does not establish that
  `new` has no observable effects.

  A `purity: "pure_new"` annotation on a defining chunk's selected class
  member also follows that binding through its exports to importers. A
  `source_match` can select the class by structure, so this form does not
  require the minified export name in the spec. Selector resolution runs
  before the cross-module purity oracle for this reason.

  `chunk_export_purity.<chunk>.pure_members` is the member-call form, for
  CJS-interop namespace exports whose factories are reached as member
  calls at the importer (`ns.forwardRef(...)`). Keyed `export name →
member names`, it projects onto each importing chunk's local binding
  for that export and feeds the same member-call trust arm as a spec
  member's `declared_pure_members` — so React's `forwardRef`/`memo`
  reached through a default/namespace import are blessed once on the
  defining vendor chunk.

  `chunk_export_purity.<chunk>.fluent_exports` is the **deep** form, for
  builder/fluent APIs whose chain receivers are call _results_ rather
  than bindings — `z.object({...}).optional().describe(...)`. Every
  binding-keyed surface above is structurally unable to reach link 2+
  of such a chain (there is no binding to key on). A fluent assertion
  makes the export a deep-purity root: static member reads and calls on
  it, and on every value transitively derived from it through such
  reads/calls, are pure — with call arguments still classified normally
  at every link, so an impure argument anywhere in the chain keeps the
  statement impure. The trust also closes over importer-side
  `const X = <fluent chain>` derivations (`const Base = z.object(...)`,
  later `Base.extend(...)`), `const` only since a `let`/`var` cell can
  be rebound to an untrusted value. Computed members and `new` break
  the chain conservatively. The contract is intentionally broad — it
  covers the API's whole transitive fluent surface, including methods
  that run author-registered callbacks (a schema's `.parse`) — so it is
  only sound for exports whose derived-value methods are all
  side-effect-free when invoked at module top level and whose values
  the program does not monkey-patch. Same author-trust posture and
  dangling-assertion handling as the two forms above.

- **A10. Spec-declared local-effect annotations are
  author-trusted.** The spec format also admits an optional
  per-member `effect:` field for named helper bindings whose
  top-level calls have a known, narrow mutation shape that ordinary
  expression purity cannot infer. The existing value is
  `effect: typescript_decorate_helper`, for bundled TypeScript helper calls
  that decorate a class member or class.

  This annotation does **not** assert that the call is pure. It
  asserts a more precise effect: for recognized call shapes, the
  call's only modeled mutation is local to the target class/prototype
  source unit. The analyzer therefore emits a target-local dependency edge from
  the decorator-application statement to the class source unit and suppresses
  the otherwise-global side-effect-order edge for that statement. The
  target-local edge is an atomic colocation constraint: the helper
  application and the target source unit must move together, and source order
  still keeps the application after the class declaration inside the
  same emitted module.

  The annotation is intentionally shape-checked. If the callee is not
  the annotated binding, if the target class/prototype binding cannot
  be resolved, or if the call uses an unsupported shape, the analyzer
  falls back to ordinary conservative side-effect classification. An
  incorrect annotation can produce a buggy debundle, so it carries the
  same review burden as `purity: "pure"`; the difference is that the
  trusted claim is local target mutation, not absence of effects.

  `effect: typescript_metadata_decorate_helper` adds the audited callback-timing
  contract in <../SPEC.md> § Local-effect decorator annotations. For eligible
  whole-statement member decoration, the executor summarizes helper and local
  decorator bodies' static lazy dependency closures without synthesizing target
  method invocations or global callback publication. Eager argument reads and
  the target-local mutation/co-location edge remain. Supplied decorators still
  execute synchronously; the author audit covers their metadata-only behavior,
  including imported implementations. This stronger value is independent of
  purity, and the older helper value does not imply it.

  For a validated metadata application, the eager target read remains a graph
  dependency while the target class and the whole metadata application are
  omitted from that statement's opaque callback reachability roots. Rooting
  the application itself would reach the class again through its eager read.
  The same proof of initialized, stable helper and class identities, member
  decorator integrity, and absent dynamic scope controls both decisions.
  Other eager-read source units remain roots. An independent export or publication
  of the class still makes its methods reachable at that point.

  `effect: typescript_metadata_decorate_helper_with_member_decorators` also
  admits a single static property lookup on an imported decorator provider.
  The source scan rejects direct writes or rebinds of that provider; the author
  certifies an ordinary stable data property with no proxy, accessor, alias
  mutation, or hidden reentry. Other member shapes retain the opaque path.

  `effect: typescript_metadata_decorate_helper_three_args` uses the same
  target-local mutation and synchronous dependency summary for an exact
  three-argument property application. It requires a separate author assertion;
  descriptor and inherited metadata safety remain author obligations. The
  earlier four-argument assertion retains its existing shape checks.

  `chunk_analysis_options.<chunk>.local_property_effects` is the
  chunk-wide structural sibling of the same idea, for plain
  data-property annotation writes rather than helper calls: a whole
  statement of the shape `X.prop = <pure-rhs>;` (or a comma-sequence of
  such), where every member-path segment is a static non-`__proto__`
  name and `X` is a chunk-top declared binding, is classified as a
  local effect on `X` instead of a globally-ordered side effect — the
  React `C.displayName = "…"` / `C.defaultProps = {…}` idiom. The
  statement gets the same target-local co-location edge as the
  decorator helper (it cannot be split away from `X`'s declaration) and
  leaves the side-effect chain. Anything outside the shape — compound
  assignment, computed non-literal key, impure RHS, a write through an
  import — keeps conservative classification. The flag's soundness
  precondition (no cross-destination top-level read of an annotated
  binding's properties textually before the write) is documented on the
  spec field (`spec::DependencyGraphOptions::local_property_effects`) and is the author's audit obligation, like every entry in
  this section.

- **A11. Intrinsic integrity: the chunk runs with unmodified
  built-in prototypes and intrinsics.** Every purity-whitelist
  admission argument cites ECMA-262 behavior of the _standard_
  built-ins: `Set.prototype.add` doing SameValueZero,
  `Array.prototype[Symbol.iterator]` being the built-in array
  iterator, `Object.prototype.toString` firing no user code, a
  plain constructor's `prototype` being an own data property. If
  code that runs before the chunk pollutes those prototypes
  (`Set.prototype.add = function () { globalThis.boom = 1; … }`,
  replacing `Array.prototype[Symbol.iterator]`, installing
  `Object.prototype[Symbol.toPrimitive]`), a `Pure`-classified
  statement like `new Set(["a"])` fires user code, S edges drop,
  and the theorem's ordering claim no longer holds. Unlike A8
  (chunk-top shadowing), this is **not** detected — pollution can
  live in another chunk, a vendor bundle, or host code, outside
  what the analyzer sees. Production bundles we target don't
  monkey-patch intrinsics; the assumption is relied on by
  observation, like A6. (Membrane/zone-style frameworks that DO
  patch intrinsics would need the affected whitelist entries
  disabled for soundness.)

Namespace admission retains member targets nested in destructuring assignments
and loop heads during the existing execution-index visit. A member target can
replace a factory or install receiver-sensitive behavior even when SWC represents
it as a pattern expression. Receiver checks include every such target; assigned
values conservatively retain publication paths from the whole assignment source
and applicable default initializers.
Loop iteration sources and unsupported member-target syntax remain unknown.
Default initializers, computed keys, and ordinary assignment effects retain their
normal traversal and effects.

The [ECMA-262 Error constructor](https://tc39.es/ecma262/#sec-error-message)
allocates a fresh ordinary error object. Adjacent writes to a direct global
`Error(...)` result within one sequence
expression cannot mutate an imported factory namespace under A11. The execution
index retains this temporal proof only for simple static writes to the same local
receiver, with inert values or declared local reads, excluding `__proto__` and
`stack`. Any intervening expression ends the proof. Every matching write in that
source statement and callable must have a proof; earlier writes through an incoming parameter
remain conservative. Argument evaluation, reads, calls, RHS namespace publication,
and general alias propagation retain their ordinary effects. Shadowed `Error`,
dynamic scope, visible intrinsic reassignment, writes to a global object's `Error` or unknown
computed property, and `Object`/`Reflect` mutator calls targeting that intrinsic
disable the proof. Simple local aliases of those roots are included. Hidden mutation paths
and external prototype pollution remain the author's A11 obligation.

A1–A5 are statically checkable per chunk, each enforced to the
strength stated on its bullet above (A1/A3/A5 by the `chunk_admission`
scan, A2 by the TLA scan, A4 at parse). The admission scan is on by
default for every materialized chunk; for audited corpora a spec can
disable individual checks per chunk via
`chunk_analysis_options.<chunk>.admission_overrides`
(`[a1_eval, a3_dynamic_import, a5_import_meta]`) — every overridden
violation is named in a notice (one line per check and violation,
listing the statements it covers), and overrides that no longer
suppress anything are reported as redundant. A6, A7, and A11 are relied on by
observation of the target bundles; A8 degrades soundly per its
bullet; A9 and A10 are satisfied by spec review.

### Lemmas

Each lemma is stated then proved succinctly; together they
support the theorem.

**Lemma 1 (Linker order is post-order over `I`).**
Under A1–A4 + A7 with acyclic `I`, ECMA-262's `Cyclic Module
Record` evaluation algorithm — specifically `InnerModuleEvaluation`
(TC39 §16.2.1.5.4) — evaluates modules in some post-order DFS
linearization of `I`, rooted at the entry module.

_Proof._ TC39 §16.2.1.5.4, `InnerModuleEvaluation`, step 9:
"For each String required of module.[[RequestedModules]], do …
Perform ? InnerModuleEvaluation(requiredModule, …)" before
executing the module's own body. Each `RequestedModule` is one
of `module`'s `import` directives. By construction (the emit
loop above), `module`'s `import` directives correspond exactly
to the outgoing edges of `module` in `I`. The recursive call
visits each requested module before returning, and each module
runs its body after the recursive calls return — that's
post-order DFS. Acyclic `I` ensures every recursive descent
terminates without revisiting a still-evaluating module. ∎

**Lemma 2 (Author-side import-order steering).**
The materializer's emit construction authors each emitted
module's `import` directive list in an order that makes ECMA-262
reach a chosen topological linearization `L` of `I` rooted at
the entry.

_Proof._ The DFS in `InnerModuleEvaluation` visits requested
modules in `[[RequestedModules]]` order — i.e. the syntactic order
of `import` directives in the source. The materializer emits each
module's source; it controls every module's `import` order. For
target `L`: at each module `M` with successors `{M'_1, …, M'_k}`
in `I`, sort the import list so that the earliest-in-`L` successor
appears first (DFS goes deepest into the first import). The
resulting DFS post-order is `L`. ∎

**Implementation.** `esm_import_order::EsmImportOrder` (held on
`AssignmentAnalysis`, see "Lemma 2: entry-side import ordering"
above) computes `L` from the constraining-edge subgraph's toposort,
reversed within each `I ∪ S` SCC so that the cycle dependent is
imported first and DFS unwinds through the dependency.
`lowering::lower_chunk` sorts entry's emitted `import` directives
with `sort_entry_imports` and every module's merged intra-chunk
import list with `sort_module_imports`; the realizability gate's
evaluation simulator applies the same two sorts to its DFS neighbor
order, so the simulated linker order and the emitted one cannot
diverge. The validator rejects any spec the primitive's tightened
clause-3 rule does not accept, so every spec the materializer
reaches `lower_chunk` for has an `L` Lemma 2 can realize.

The remaining principled gap is the `(at-init forward, lazy back)`
shape **where entry itself is a cycle member**. Lemma 2's
"unwind through the dependency" fails there — entry is the ESM
DFS root, so post-order evaluates every other cycle member first,
and a constraining read into entry TDZs. The primitive's
tightened rule rejects this shape outright (see the entry-in-
cycle carve-out under "Lemma 2: entry-side import ordering"); the
emitter never sees one.

**Lemma 3 (At-init read correctness).**
Under any evaluation order `L` respecting `I`, every at-init read
in module `M` of binding `b` owned by module `M' ≠ M` sees `b`
initialized at the moment of the read.

_Proof._ The emit places at-init read of `b` in `M`'s body. By
the definition of `R`, this read contributes edge
`(M, M') ∈ E_R ⊆ E_I`. `L` respects `I`, so `M'` evaluates fully
before any line of `M`'s body runs. `M'` evaluating fully
includes `b`'s declaring statement (by the source-order invariant
within `M'`); `b` is initialized when `M`'s read of it fires. ∎

**Lemma 4 (Lazy-read correctness).**
Under any `L` respecting `I`, every lazy cross-module read fires
after its target binding is initialized.

_Proof._ Lazy reads are syntactically inside function bodies,
method bodies, getter/setter bodies, or instance class-field
initializers. ECMA-262 §10.2.1 (`OrdinaryFunctionCreate`) creates
the function object with `[[Environment]]` capturing the lexical
environment at definition time, but the function body does not
execute until the function is _called_. By Lemma 1's post-order
property, `M`'s body finishes evaluating (defining `M`'s
functions) before any module that imports `M` evaluates and
references `M`'s exports. Any caller of `M`'s function — entry
top-level code, another module's function call, etc. — runs
strictly after `L` has reached the caller's module. By Lemma 3
analog (the I-edge from caller's module to `M'` exists because
the function body references `b ∈ M'`), `M'` is fully evaluated
by the time the lazy read fires. ∎

**Lemma 5 (Side-effect ordering).**
Under any `L` respecting `I ∪ S`, for every source-order pair of
side-effecting statements `(S₁, S₂)` with `S₁.ordinal <
S₂.ordinal` and `home(S₁) ≠ home(S₂)`, `S₁` fires before `S₂`.

_Proof._ By the definition of `S`, the pair `(S₁, S₂)` introduces
edge `(home(S₂), home(S₁)) ∈ E_S`. `L` respects `S`, so
`home(S₁)` precedes `home(S₂)` in `L`. Each module's body runs
to completion before the next, so `S₁` (in `home(S₁)`) fires
before `S₂` (in `home(S₂)`). Same-module side-effect order is
preserved by the source-order invariant. ∎

### Theorem and proof

**Theorem (correctness).** Under assumptions A1–A11, if the realizability
gate accepts a spec (Pass 1 and Pass 2 above), the source-order emit
construction produces a multi-module ESM bundle whose observation-trace
under ECMA-262 evaluation is identical to the input chunk's evaluation.

_Proof._ Pass 1 makes the constraining subgraph `R ∪ S` acyclic, so it
admits a topological linearization `L`. The full graph `I` may have
additional cycles. A cycle whose cross-module edges are all lazy is sound
for lazy reads regardless of which member the DFS enters first (Lemma 4). A
cycle that also carries constraining edges is accepted only by Pass 2, which
fixes the evaluation order the emitted imports produce and checks that every
constraining edge's target evaluates before its source. By Lemma 2 we author
each emitted module's import directive list to make ECMA-262 produce a
linker order respecting both `R` and `S`. By Lemmas 3 and 4 every binding
read in the emitted bundle sees an initialized value identical to the input
chunk's read at the same source ordinal. By Lemma 5 every side-effecting
statement fires in the same relative order as in the input chunk. By the
emit's same-module source-order invariant, statements within a module fire
in their original source ordinal order. The observation-trace of the emitted
bundle is therefore identical to the input chunk's. ∎

Scope of the argument: Lemmas 3–5 are stated for evaluation orders `L`
respecting `I`. An I-SCC that carries both constraining and lazy edges has no
such `L`; for it the conclusion rests on the simulator reproducing the
ECMA-262 evaluation order under the emitted import order (both consume
`EsmImportOrder`; `e2e/simulator_node_differential_sweep_test` compares the
simulated order against Node), not on a proof in this document.

### Coverage gaps (what the proof does NOT cover)

The proof above is a sufficient-condition theorem under A1–A11.
It does not establish:

- **Top-level await (A2 violation).** Async modules use a
  different evaluation algorithm (`InnerModuleEvaluation` returns
  a Promise; cyclic async-module groups have AsyncCycleRoot
  semantics). A separate proof would be needed; instead the
  materializer refuses TLA chunks explicitly (the chunk-analysis bail
  described under A2 above).
- **Dynamic eval / reflection bypassing the static graph (A1, A5
  violations).** The `I` graph is incomplete in this case;
  cycle-freeness in `I` doesn't imply cycle-freeness in the
  actual evaluation graph. The admission scan rejects the
  module-top shapes (direct / seq-indirect `eval` calls,
  non-`url` `import.meta` use, eager non-literal `import()`),
  but cannot rule out the rest statically. The shapes that remain
  pure input-shape contracts on the bundler:
  - `eval` in function bodies and other lazy positions — A1's
    wording only bans module-top eval, but a lazy direct `eval`
    still reads its lexical scope dynamically when the enclosing
    function is called after the split, and the split may have
    moved the bindings it names into other modules.
  - aliased eval (`const e = eval; e(...)`) and `eval` reached
    through any value flow.
  - non-literal dynamic-import specifiers in lazy positions,
    which could resolve to an internal module at runtime.
  - `Function.prototype.toString` reads of cross-module bodies
    and `Reflect`-based descriptor inspection of namespace
    objects (the fuzzier A5 sub-shapes; checking them cheaply
    without over-flagging mainstream bundles is not possible).
- **Cross-chunk imports through CommonJS interop.** ECMA-262's
  evaluation rules don't apply to CJS modules; a CJS module's
  `module.exports` mutation can fire mid-evaluation in arbitrary
  order. We require vendor chunks to be ESM (production corpora we target satisfy this).
- **Lazy-only cycles** are _accepted_ by the realizability gate (Pass 2
  skips an I-SCC with no constraining edge): the linker walks `I` and picks
  some evaluation order for the cycle, every module finishes evaluating with
  all bindings assigned, and the lazy reads only fire later (after `L`'s
  call-sites run). The architectural-fragility worry — a future edit
  promoting a lazy read to at-init — is judged by the gate the moment the new
  constraining edge enters the SCC.

### Worked example: cycle through a lazy back-edge

A spec that is acyclic in `R` alone but cyclic in `I` once lazy back-edges
are counted:

```text
mod_a:                                      mod_b:
  const A = "a-value";                        const B = A + "-postfix";  // constraining edge mod_b → mod_a
  function readB() { return B; }
  // lazy read of B closes the I-cycle mod_a → mod_b
```

The entry destination's emitted file calls `readB()` at top level and re-exports `A`, `B` and
`readB`.

The constraining subgraph holds only `(mod_b, mod_a)` and is acyclic, so
Pass 1 passes, but the lazy `readB` body emits `import { B } from "./mod_b"`
in `mod_a`, so `I` has the cycle `{mod_a, mod_b}`. Its outcome depends on the
linker's DFS order: entering `mod_a` first would evaluate `mod_b` first, and
`const B = A + "-postfix"` would run while `A` is in TDZ. The emitter avoids
that: the entry imports every module with the SCC's dependent first
(`[mod_b, mod_a]`, Lemma 2), so the DFS runs `entry → mod_b → mod_a → mod_b`
(cycle, no-op), `mod_a` evaluates first, and `mod_b` sees `A` initialized.
Pass 2's simulator reproduces that order and accepts; the e2e test
`lemma_two_rescued_asymmetric_cycle_test`
(<../e2e/lemma_two_rescued_asymmetric_cycle_test.rs>) runs the emitted bundle
of the same shape under Node.

Pass 2 rejects the same cycle shape when the target of the constraining edge
is the entry destination: it is the DFS root, so it evaluates last and a
constraining read into it TDZs (<../e2e/pass_two_tdz_cut_diagnosis_test.rs>,
<../e2e/runtime_tdz_on_imported_class_test.rs>). The realizable fix is then
to colocate the read's binding with its reader. This is the canonical
**spec-induced atom** in the sense of §"Two classes of atom" below: the
source-unit-level read graph is a DAG, but the spec's choice to split the two
bindings makes the module-level `I ∪ S` cyclic. The validator's `CycleReport`
names the exact binding pair (with the binding-pair-blame
`render_cycle_summary` format), so the diagnostic points the spec author
directly at "{A, B} must co-locate."

The namespace-aggregator form of a rejected split is in
<../e2e/namespace_aggregator_split_tdz_test.rs>.

### Why a static gate, not a runtime check

The gating rule is enforced **statically** by the validator. The
materializer either emits a bundle the gate accepts — observationally
equivalent by the theorem above — or refuses with cycle evidence. There is
no runtime check, no init-wrapper safety net, no per-load TDZ guard. By
construction, the emitter never produces JavaScript that the ESM linker has
to puzzle through a cyclic at-init read graph for. If the spec describes an
unrealizable split, the spec is wrong; we report the cycle and let the author
fix it.

This is the contract the user-visible artifact relies on. Any
escape hatch — accepting constraining-cyclic specs that happen to work in
testing, or deferring cycle detection to runtime — gives back
the property that "an emitted bundle from this materializer is,
by inspection of its module graph alone, free of TDZ and
side-effect-ordering hazards."

### Multi-chunk extension

The theorem is stated for a single chunk. Real bundles have many chunks
(entry, code-split routes, vendor bundles), each with its own logical-module
split. A cross-chunk dependency is an `Imported` binding of the importing
chunk, not a vertex of any chunk's dependency graph.

The natural lift — union every chunk's I-graph, with `Imported` bindings
contributing cross-chunk edges, and require the union to pass the gate — is
not implemented: `validate_assignment` takes one chunk's dependency graph, so a
cycle among user chunks (two code-split chunks importing each other) is
outside its view. In practice vendor chunks are leaves — they don't import
from us — so they add no edges into user chunks; a vendor chunk that
re-exports something from a user chunk is a bundler-configuration problem,
not a spec problem.

### Pipeline split (chunk analysis / materialization)

The per-chunk pipeline is two halves with sharply different
dependencies:

- **Chunk analysis** (spec-independent): parse → per-statement facts →
  dependency graph → structural atomic groups. Pure function of
  `(source bytes, analysis hints, DependencyGraphOptions)`. The composer
  is `chunk_analysis::compute_chunk_analysis`, returning a
  `chunk_analysis::ChunkAnalysisOutput` — distinct from the `gate`
  crate's `AssignmentInputs` — that bundles `ChunkFactAnalysis` (facts +
  top-level-await detection + redundant-hint warnings) with the
  `DependencyGraphAndAtomicGroups` derived from those facts.
- **Materialization** (spec-dependent): assemble the assignment from the
  spec's binding claims, run the realizability gate, lower to ESM. This is
  `lowering::materialize_logical_modules`; `spec_resolution` compiles claims
  into requests, projects each chunk's eligible entities, and solves the
  projections jointly. Lowering prepares assignment state before projection,
  applies resolved claims afterward, then `finish_logical_chunk` performs
  chunk analysis, assignment assembly, the gate, and lowering.

The materializer is the composition of both, and chunk analysis is computed
in memory at one call site in `finish_logical_chunk`. The boundary is
explicit without committing the pipeline to cross-process fact reuse. If
future work tries to cache chunk analysis across processes, it must first
solve SWC hygiene replay for pre-filter facts; see
`docs/wire_format.md` and `docs/lessons_learned/cross_process_stage_b.md`.

The reason to call this out: every diagnostic in §"Two classes of
atom" lives in materialization (it depends on the spec's quotient), but
its inputs all come from chunk analysis. Refactors that move work
between the two halves must respect the boundary — anything
spec-dependent cannot move into chunk analysis; anything that only
depends on source bytes should not stay in materialization.

### Two classes of atom

A spec is unrealizable when its assignment forces a _constraining
cycle_ somewhere — but constraints live at two different levels of
the graph, and the validator surfaces them through two separate
diagnostic paths. They differ only in _where_ the cycle appears
(source-unit level vs module-quotient level), not in _what_ the proof
requires. Both reject the same broad property: "some pair (or set)
of bindings is forced to co-locate, and the spec splits them."

| Atom class       | Where the cycle lives                                                   | Detector                                      | Diagnostic                                                                                                          |
| ---------------- | ----------------------------------------------------------------------- | --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| **Structural**   | SCC of `G_atomic` on the **dependency graph** — independent of any spec | `compute_atomic_groups`                       | `AtomicGroupConflict` — names the group's source units + the modules the spec routes them to + the `DepKind` causes |
| **Spec-induced** | SCC of `I ∪ S` on the **module quotient** under the spec's assignment   | `check_realizability` + `validate_assignment` | `CycleReport` — names the binding pair(s) on each cut edge, the source and target modules, and the edge kind        |

`G_atomic` over source units is acyclic-by-quotient on the chunker's
original output: bundlers can't emit JavaScript that TDZs at runtime
(the bundle ran for someone). So every source-unit SCC of
constraining edges (`EagerUse`, `Sequenced`, `EagerRebind`,
`LazyRebind`, `DeferredRebind`, `CoDeclaration`, `LocalEffect`) reflects a
_colocation invariant the bundler relied on_. (`DeferredRebind` —
a rebind nested ≥2 closures deep or past an `await` — joins
`G_atomic` bidirectionally like the other rebinds because ESM
imports are read-only whenever the write fires, but it does NOT
constrain init order: it is excluded from the constraining-edge
subgraph the realizability gate and the I-graph use.) Any spec assignment that routes the group's
source units to different modules is invalid — `assemble_assignment`
detects this before the materializer touches the quotient. **No
information about the spec is required to identify a structural
atom**: it is a property of the source bytes plus the analyzer's
edge classification.

A **spec-induced atom** is one level up. Each source unit may be in its
own structural atom (singleton), and the _module quotient under the
spec's assignment_ may still close a cycle through them. The
worked example in §"Worked example: cycle through a lazy back-edge"
is exactly this shape: the binding-level read graph is a DAG, but
the spec's choice to put `B` and `readB` in different modules makes
the module-level `I ∪ S` graph cyclic (and unrealizable whenever
Pass 2 proves a TDZ). The fix is identical in
_shape_ — co-locate the bindings — but the _unit being preserved_ is
defined by the spec's quotient, not the source bytes.

Why two paths in the implementation: structural conflicts can be
caught with no module quotient ever built — it's an `O(|V| + |E|)`
SCC computation on the dependency graph that produces a clean blame
("these source units can never split"). Spec-induced cycles require the
quotient (because they're about the assignment) and produce a more
nuanced blame (which _binding pairs_ on which _cut edges_ close the
cycle, after `petgraph::algo::greedy_feedback_arc_set` picks the
cheapest cut to break).

Both diagnostics name the implicated bindings at the source level,
not the module level. A large SCC closing through one
constraining `(mod_A, mod_B)` edge surfaces as "binding `a` in
`mod_A` reads binding `b` in `mod_B` at-init; move them together,"
not "you have a cycle of many modules." See
[validation.rs::render_cycle_summary](../validation.rs) for the
binding-pair blame format.

### Corollary: the role of the validator

The pipeline runs:

```
parse chunk
  ↓
analyze per-statement facts
  (declared, reads_at_init, reads_lazy, has_side_effect)
  ↓
build dependency graph
  (source units, read edges, potential side-effect-order edges)
  ↓
compute structural atoms = SCC(G_atomic over source units)
  ↓
apply spec assignment + assemble_assignment
  ↓
  ├── if any structural atom is split across modules:
  │   reject with AtomicGroupConflict (source-unit blame)
  ↓
quotient dependency graph by dest(source_unit) → I, R, S
  ↓
validate: realizability gate (Pass 1, then Pass 2) over the quotient
  ↓
  ├── if Pass 1 finds a constraining-edge cycle or Pass 2 proves a TDZ:
  │   reject with CycleReport (binding-pair blame from cut edges)
  ↓
emit (source-order)
```

Both rejections write the same kind of sidecar evidence — the dependency
graph and the conflicts/cycles report under `reports/tree/<chunk>/`
— so spec authors can drill down to specific bindings + statements
regardless of which atom class caught the spec.

A spec that passes validation is _guaranteed_ to emit correctly
under the source-order strategy described in the proof, given A1–A11. There is
no class of accepted-input that the validator may emit incorrectly
(modulo cleanly-defined precision of `reads_at_init` and
`has_side_effect`). The validator may reject specs that are in fact
realizable when analysis is conservative; that is the intended
failure mode. Those rejections should come with source-unit evidence
showing which graph edges made the split unverifiable.

### Layered mental model

Five layers, bottom-up:

1. **Dependency graph** — fine-grained program facts (`graph/`).
   One vertex per source unit; edges record `EagerUse`,
   `LazyUse`, `HoistedUse`, `EagerRebind`, `LazyRebind`, `DeferredRebind`,
   `CoDeclaration`, `Sequenced`, and the local-effect edges that target-local
   mutation produces. Each
   edge records whether it constrains init/materialization order.
2. **Atomic graph** — SCC condensation of the constraining-edge
   subgraph of the dependency graph; a DAG of atomic groups
   (`compute_atomic_groups`). Each group is the smallest set of
   source units a valid module assignment may not split. Surfaced in
   `dependency_graph.json.atomic_graph`.
3. **Spec assignment** — the author's current mapping of source units
   to modules (the spec's `members:` + `anonymous_statements:`
   plus the routing of unclaimed source units under `unassigned_mode`).
4. **Module quotient** — the dependency graph projected under the spec's
   assignment. Cross-module edges are the realizability evidence
   `check_realizability` operates on.
5. **Module proposals** — DAG-derived advisory recommendations
   computed by the proposer (`debundle modules propose`) over
   `atomic_graph`. **Not** emitted by `debundle run`; they are a
   read-only planner projection.

When debugging planner output, start with the atomic group. If an
assignment would split a unit, the assignment is wrong regardless
of how plausible the binding names look. If a proposal is too
broad, inspect the atomic-DAG edges that close it and decide
whether the edge classification is too conservative or whether the
larger module is genuinely required.

### Assignment assembly inside `debundle run`

The spec's explicit claims are resolved against the dependency graph in
`assignment_assembly.rs`. The rules are deliberately strict:

- Two different logical modules may not claim source units from the same
  atomic group.
- A module claim that only covers part of an atomic group is a
  conflict, not an implicit request to move the rest.
- Unclaimed source units default to the typed entry destination, after the
  planned output modules.
- The materializer consumes only the final explicit assignment; it
  does not silently co-move extra source units on behalf of the author.

If the explicit assignment is inconsistent, `debundle run` rejects
and emits diagnostic side outputs (`cycles.json` or
`atomic_group_conflicts.json` under `reports/tree/<chunk-id>/`).

## Spec explicitness and diagnostics

A tempting design for spec ergonomics is an _automatic closure
pass_: when the spec assigns binding `A` to module `M` and `A`'s
init reads `B`, the closure silently assigns `B → M` too (unless
`B` is already claimed). The author names just the bindings they
care about and the system fills in transitive deps.

The trade-off doesn't pay off in practice:

1. **Spec stops being the source of truth.** What bindings end up
   in module `M` depends on a heuristic the author can't see at
   spec-edit time.
2. **Splitting co-pulled bindings is fighting a heuristic.** If
   `A` and `B` are co-pulled by closure but the author wants them
   in different modules, they have to write an explicit
   counter-claim somewhere. The "fight" is invisible — you can't
   read the spec and tell what's a counter-claim.
3. **Cycles introduced by closure are invisible at spec time.**
   The spec author never wrote down that two modules `module_a`
   and `module_b` would end up reading each other's bindings;
   the closure produced that coupling, and the cycle only shows
   up at runtime.
4. **No spec-size win at the limit.** When every binding has a
   meaningful name, the spec already enumerates every binding.
   Adding ownership info per binding is the same
   `O(num_bindings)` size — closure isn't saving spec size,
   just hiding which binding ended up where.

**Design rule.** The spec's ownership claims are explicit. Every owned binding
is named in a logical module or routed by `unassigned_mode`: to the entry,
the catchall file, or a generated per-atomic-group module.
Empty-`declared` (anonymous) statements default to the entry destination
unless explicitly claimed by a logical module's `anonymous_statements`
shape selector or routed to a catchall or generated atomic-group plan.
There is no implicit pulling into an explicitly named logical module: when an
atomic group or planner proposal includes anonymous statements, the spec author
copies their source into `anonymous_statements` (one entry per claimed
statement). `per_atomic_group` may place an unclaimed whole atomic group,
including anonymous statements, into a generated plan.
When a chunk has no explicit logical modules, `inline_in_entry` keeps the
whole statement sequence in the entry. In `catchall_file`, unclaimed named
bindings land in the catchall. Anonymous source units in a catchall atomic group and
anonymous predecessors required by its side-effect ordering edges also land
there; other unclaimed anonymous statements can stay in the entry after peeled
modules. This preserves valid splits where a later anonymous statement must
observe a peeled module, while avoiding a false catchall-to-entry ordering
cycle for earlier effects. If the catchall is the only module, it keeps the
whole statement sequence. Explicit anonymous claims keep their specified
destination; the realizability gate rejects any remaining invalid order.

The dependency graph is the explicit replacement for hidden closure. It
records the fine-grained "this source unit uses that source unit" relation before
any module-level quotienting. Diagnostics are projections of that
graph: validation cycles explain why the current explicit assignment
is not realizable, `atomic_graph` records which source-unit sets are
indivisible, and the read-only query surface (`debundle modules
propose`, `atoms`, `coverage`, `describe`, `inspect-source --id`, `scc`,
`cluster`, `graph-summary`) can rank or annotate DAG-derived proposals. None of those projections silently assign bindings on
behalf of the spec author.

Each materialized chunk has reports under `reports/tree/<chunk_id>/`.
`dependency_graph.json` carries the detailed dependency graph, current module quotient,
and embedded atomic DAG. Compact validation status is folded into
`chunk.json`; `cycles.json` and `atomic_group_conflicts.json` are emitted only
for rejection cases.

When an output emitter materializes executable JavaScript, root reports live
under `reports/` and per-file/per-directory reports are mirrored under
`reports/tree/` next to the chunk reports. `reports/output.json` and
`reports/tree/<chunk_id>/chunk.json` include `output_metrics` computed from the
exact rendered JS written to disk: total bytes/lines/files, top-level entry
bytes/lines/files, named-module bytes/lines/files, residual-module bytes/lines/files, and the largest JS sinks. Peeling tools should use these
fields for progress reporting instead of rescanning output trees.

The mirrored `reports/tree/**/index.json` files project the semantic
source-unit/module dependency graph onto recursive emitted-directory boundaries. They
report incoming/outgoing edge counts by dependency kind plus full symbol and
file attribution maps. A symbol key has the form
`<target_file>#<export_or_binding_name>`; binding-less edges such as
`sequenced` contribute to edge-kind and file counts but not symbol maps. Uses
within the same directory do not count against that directory boundary. Use
these reports to judge hierarchy encapsulation and leaky subtree boundaries;
use the dependency graph and source bodies for drill-down.

### Workflow

1. Spec author writes / edits a spec — possibly partial.
2. Pipeline runs the validator. The report flags:
   - **Cycles** in the explicit-only assignment, if any.
   - **Atomic-group conflicts** when the spec splits an indivisible unit.
   - **Atomic DAG facts** in `dependency_graph.json` for planning the next peel.
3. Spec author resolves: first fix any validation cycles or atomic-group
   conflicts; then use `debundle modules propose`, `atoms`, `coverage`,
   `describe`, and `inspect-source --id` to choose explicit source-unit sets for new
   spec modules with good public names.
4. Re-run validator. Iterate until validation is green and the
   residual contains only intentionally generated or low-value noise.

The spec is now fully explicit. The validator is a one-shot
predicate: "given this spec, is the bundle realizable?" — no
implicit transformation in the middle.

## Atomic DAG Planner Data

The same dependency graph drives the next-spec workflow. A pipeline that
only reports whether the current spec passed forces spec authors into
speculative edit/build/test loops. `debundle run` therefore emits the
atomic-group DAG in `dependency_graph.json`, and the top-level read-only
queries (`debundle modules propose`, `atoms`, `coverage`, `describe`,
`inspect-source --id`, `scc`, `cluster`, `graph-summary`) compute advisory
views from that stable graph.

<peel_proposer.md> is the current-state implementation note for how
`modules propose` builds and greedily extends its quotient before
rendering proposal rows.

`debundle modules propose` labels each proposal source-unit set with a status:

- **`peelable_now`** — this closed atomic-DAG source-unit set has no outgoing
  constraining edges into other residual classes; it can be promoted on
  its own.
- **`blocked_residual_dependency`** — the cell reads other residual
  cells (`edges_to_other_residual_classes > 0`); promoting it alone would
  route those reads through `residual_entry`, so the referenced cells
  must land first or together.

`landable_today` derives from the same predicate as the status (plus
anonymous-statement addressability): it is `true` only for
`peelable_now` proposals whose anonymous source units are all addressable.
A `blocked_residual_dependency` proposal is never `landable_today` —
`modules apply` rejects it; grow the closure so the
referenced cells land together, or co-locate them manually, first.

Classes whose spec-edit size exceeds `--size-cap-lines` are not
proposals at all — they surface as diagnostics with reason
`exceeds_size_cap`.

The important invariant is that the proposal queue is a projection over
`atomic_graph`, not a separate fact emitted by the transform pipeline.
Ordinary `debundle run` emits source-unit facts, module quotient facts, and the
atomic DAG; the read-only query surface ranks and groups those facts for
authoring workflows.

### Detailed graph side output

The transform also writes a detailed graph side output, separate from
compact validation errors. This is the data source for analysis
scripts, notebooks, and repo-specific peel skills. It is emitted on
both success and validation rejection, because useful graph data often
exists precisely when the current assignment is not yet realizable.

The detailed output is machine-readable, typed, and debundler-owned:
per chunk, the source-unit vertices and dependency edges, quotient projections for the
current assignment, and the `atomic_graph`. The field-level shape is
the serde schema (`DependencyGraphReport` and siblings in
<../reports/schema.rs>); this doc does not mirror it. New fields can
be added without changing the underlying graph transform. Do not add
top-level `kind` or `schema_version` fields just to mimic an external
compatibility scheme.

Keep this side output high fidelity. It is allowed to be larger than
stderr and larger than a human-facing report. The compact console
error should summarize the failed SCC and point at the side-output
paths; downstream tooling should read the files.

### Valid peels and atomic modules

Let `dest: V -> D` assign every source-unit vertex to an output
destination. The quotient graph `Q = G / dest` merges all source units
with the same destination, drops intra-destination edges, and keeps
the labels/provenance on every cross-destination edge.

A cross-destination read edge is **importable** iff the provider can
be named by an emitted ESM import from the consumer destination:
the provider is already in an external chunk, is exported by a
logical module, is an imported binding being re-exported, lives in
the residual entry (the emitter auto-exports residual entry bindings
on demand — see "Emit-side responsibilities" below), or moves with
the consumer. There is no first-class "binding is private to entry"
spec contract: any top-level declaration in entry is fair game for a
moved module to read, and the materializer grows entry's export
list to cover such reads.

A cross-destination **rebinding write** is never importable. ESM
imports are live for reads but read-only in the importing module, so
a source unit that assigns `a` must stay in the same destination as the
source unit that declares `a` unless the emitter grows a separate
live-mutation bridge for that binding.

Call an edge **constraining** when it is an at-init read or a
side-effect-order edge. Rebinding write edges are stricter than
constraining read/order edges: they invalidate any cross-destination
assignment, even when acyclic. Lazy read edges are
non-constraining: they still contribute imports, but a cycle made
entirely of lazy read edges is realizable.

A destination assignment is **valid** iff:

1. Every cross-destination read edge in `Q` is importable.
2. No cross-destination rebinding write edge remains in `Q`.
3. The I-graph of `Q` passes the two-pass gate ("Lemma 2: entry-side import
   ordering"): its constraining-edge subgraph — `Q` with every `LazyUse` and `HoistedUse`
   cross edge dropped — has no multi-module SCC (Pass 1), and in every
   multi-module SCC of the full I-graph that contains a constraining edge the
   ESM evaluation simulator evaluates each constraining edge's target before
   its source (Pass 2). Pure lazy-read import cycles are realizable because
   ESM evaluates the lazy side without observing a TDZ on the eager side.

Clauses 2 and 3 are what the "Realizability primitive" section above defines
as the single shared implementation; clause 1 is emit policy ("Emit-side
responsibilities"). The validator and planner checks all consume the same
primitive — none of them re-implement the predicate.

A peel is just a proposed destination assignment for a source-unit set.
The peel is valid exactly when the resulting assignment is valid by
the definition above. Invalid peels have three primary explanations:
a non-importable read crosses the cut, a rebinding write crosses the
cut, or a quotient SCC keeps a constraining cycle or a simulated TDZ.

This definition is intentionally a predicate over a complete
destination assignment, not a single graph-factorization trick. Some
relations are true equivalence constraints:

- Rebinding edges force colocation because ESM imports are read-only.
- SCCs of at-init/side-effect constraints can force colocation when a
  split would leave a constraining edge inside a quotient SCC.

Other relations are not equivalence constraints:

- A directed at-init read `A -> B` does not mean `A` and `B` must
  colocate. It can be satisfied by an import when the quotient stays
  realizable.
- A lazy read never constrains evaluation order, but it still emits an
  import, so it still takes part in the I-cycles that Pass 2 judges.
- A side-effect-order edge is an ordering constraint, not a value
  dependency. It may be satisfiable by the chosen module evaluation
  order, or it may participate in an unrealizable quotient SCC.
- A target-local effect edge, such as a recognized TypeScript
  `__decorate` helper call mutating `C.prototype`, is not a global
  side-effect-order relation. It is a local mutation relation between
  the anonymous application statement and the target source unit. It must
  force colocation with that target, but it must not glue the target
  to every unrelated side-effecting source unit that happens to appear
  before or after it in source order.

So "factorize the graph" is not the problem statement. The problem is:
construct source-unit sets whose induced destination assignment is valid under
the same static semantics as materialization. The graph is the data
structure used to compute that proof.

### Emit-side responsibilities: refuse to emit invalid JS by construction

The validity predicate above leaves one degree of freedom: which
residual entry bindings are surfaced as ESM exports of entry. The
materializer (`materialize_logical_modules`) owns that decision and
makes it **per emit pass**, not as a static property the planner
must predict.

Concretely: when a moved module body references a top-level
declaration that lives in the residual entry but isn't yet on
entry's `export {...}` list, the emit step grows the export list to
include it (see `auto_grown_residual_exports` in `lowering/exports.rs`).
The moved module then imports the binding from entry via a normal ESM
`import { name } from "../entry.js"`. Source-level exports are not
clobbered: the auto-grow only adds names that the upstream source
didn't already export.

This decoupling is load-bearing for two reasons:

1. **The planner never models the emit policy.** Asking the planner
   to predict "will the materializer accept this peel after its
   export-growth pass?" forces a duplicate implementation of the export
   logic on the planner side and
   creates an SSOT drift hazard the moment the emit policy
   evolves. Keeping the planner concerned with the
   importability/cycle/rebind predicates _only_ means a peel that
   passes the planner's check is always materializable.
2. **There is no "binding is private to entry" spec contract.** Any
   top-level entry declaration is implicitly exportable when a
   peeled module needs to read it. Spec authors who want a binding
   to stay strictly internal must keep it inside a logical module
   (or refuse to peel any consumer that would read it from entry).

The materializer's bail-on-missing-export message
(`"moved module references residual entry binding(s) … not
exported by entry"`) is therefore an internal invariant check: if
the auto-grow pass missed a name, that's a bug to fix in
`auto_grown_residual_exports`, not a verdict the spec author can
work around. A truly absent reference (binding declared nowhere in
the chunk) was already a runtime `ReferenceError` in the upstream
source and remains one in the debundled output — emit doesn't try
to invent a binding it can't resolve.

### Factorization proposals

`factorize` (`debundle modules propose`) proposes module assignments for code
that currently lives in the residual surface. Its output has the same
correctness contract as a handwritten YAML edit. The algorithm — a seed
quotient, then lazy-priority-queue greedy contraction with every merge gated
by the realizability primitive — is described in <peel_proposer.md>.

- A **proposal** is a source-unit set plus destination assignment that is already
  proven valid.
- A **diagnostic** may describe why a class did not become a proposal. It is
  never presented as a module assignment the author can land.

Every emitted proposal satisfies:

1. It corresponds to a concrete destination assignment.
2. The assignment passes the realizability primitive: no cross-destination
   rebinding write and no unrealizable quotient SCC. (Residual dependencies and
   anonymous-statement addressability, which decide `landable_today`, are
   reported separately.)
3. The proof is static and local to the dependency graph. It does not rely on
   emitting JS, running a browser, or observing production-scale behavior.

The generator may be conservative and miss valid modules. It may not emit an
invalid module as a proposal:

> `factorize` emits only certified proposals.

Local-effect annotations belong in the shared analysis layer, not in
factorize-specific code. Once the analyzer turns a recognized decorator helper
call into a target-local dependency edge, the materializer rejects a handwritten
split of the class and decorator statement, the atomic DAG records the
required unit, and the proposer never splits it either. No consumer gets to
reinterpret `effect:` independently.

### Pipeline trajectory

The pipeline (`pipeline.rs`) separates source preparation, reconstruction,
validation, and writing:

- **Prepare and resolve.** Load and parse source chunks once, compute shallow
  facts, and resolve selectors against that input syntax. One read-only
  `VendorResolutionPlan` resolves and validates replacement marks, boundary
  mappings, package targets, and input-space consumer shapes.
- **Reconstruct.** Plan assignment and linkage, lower selected syntax, then apply
  cross-chunk naming, pass-through directive rewrites, vendor residual stripping,
  and dead import/export pruning. All construction paths consult the same vendor
  oracle. Fully swapped chunks are excluded from emission; replacement wrappers
  and facades remain planned outputs. For browser output, prepare the HTML entry
  and preload targets, bootstrap, rewritten HTML, and asset destinations before
  final acceptance, including in dry runs.
- **Validate.** Consume the final `ReconstructionPlan`, checking synthesized
  partial-swap consumers, duplicate public exports, and unresolved claims. Return
  an immutable `ValidatedPlan` only on acceptance. These final checks supplement
  assignment realizability and input-space vendor checks.
- **Write.** Tree and browser writers require `ValidatedPlan`; vendor assets and
  their combined manifest use the same accepted plan. The manifest's destination
  and resolution records are frozen before acceptance, including the rewrite
  counts from lowering and pass-through construction. The accepted plan writes
  assets followed by that manifest under the existing vendor write gate; an empty
  report or absent manifest destination emits no manifest. No body transformation
  runs after validation. Rejection
  reports are diagnostic output and do not require an accepted plan. The browser
  writer copies the planned assets and writes the prepared HTML and bootstrap;
  it does not resolve loading topology or parse source HTML.

Preparation returns `SourceProgram`, which owns prepared chunks and their
matching indexes. Resolution and vendor planning borrow it immutably. Lowering
consumes its source bundle; a pass-through run instead converts it directly to
`EmissionFiles`, reusing the indexes without rebuilding them. The mutable indexed
file container is private to these phase types.

For each materialized chunk, `StructuralChunkSourceFacts` retains the shared
top-level item views and static statement facts independently of author assertions.
`spec_resolution` gives this source census to the joint selector solver, then
transfers it to lowering. After cross-module inputs are resolved, lowering
completes `StructuralChunkAnalysis` with one canonical value-flow solve and
passes it to `compute_chunk_analysis`. That pass combines it
with `AnalysisInputs`: explicit `AnalysisAssumptions`, imported summaries, and
analysis policy. Imported summaries may themselves depend on contracts declared
for their source chunks. These inputs remain distinct from inferred facts.
Semantic passes may revisit the retained AST views; reuse does not imply a
single AST traversal for the entire pipeline.

Read-only source inspection uses `PreparedSource` and the shared parser without
constructing the semantic dependency graph. Analyses of transformed syntax,
including vendor pruning, operate on that syntax rather than reusing facts
about the original input.

During assignment construction, `ChunkPlanBuilder::bindings_catalogue` is the
authoritative destination of each declared or imported binding. Its spelling
index stores binding identities and resolves destinations through the catalogue.
Module plans carry export names and comments. Rebind analysis obtains an
owned-only destination projection when needed; each construction update changes
the catalogue once. After realizability validation, `EmissionPlacement` derives
binding and whole-statement placement from the accepted `ModuleAssignment` and
checks that declaration sites, import metadata, and whole-statement requests agree.
Lowering consumes that immutable placement and its associated graph.

`EmissionFiles` owns reconstructed syntax while body transformations are still
allowed. Its output-path indexes are built once after lowering changes entry and
module paths, then retained across body-only passes behind an indexed-layout
check. Final validation consumes these files and the vendor plan; writers obtain
both files and full-swap exclusions from the resulting `ValidatedPlan`. No disk
reparse or splice back into prepared source occurs. `EmissionFiles` currently
uses `ChunkBundle` internally for existing transformation helpers; phase ownership
is explicit in its enclosing types.

## Anonymous-statement selectors

Binding selectors (<selectors.md>) address an `Owned` binding by name.
Anonymous (empty-`declared`) statements have no name; they are
side-effect IIFEs, decorator applications like
`decorate([dec], $g.prototype, "items", 2);`, runtime init bridges, and
similar bare expressions/statements. Empirically, a closure that
peels a hub-class binding frequently must co-move several such
statements — they apply decorators to the class prototype, register
instrumentation hooks, push configuration through helper functions — and an
algorithmic refinement (treating "fire-and-forget" preludes as
non-constraining) does not cover them: most are not preludes,
they semantically belong with the class.

The spec addresses these statements with an
`anonymous_statements: [...]` list on the same `LogicalModule` as
the named members, where each entry carries a `source_match` template
of the target statement:

```yaml
feature/state:
  members:
    - selector: { binding: { name: $g } }
      name: FeatureState
  anonymous_statements:
    - match: |
        decorate([dec], $g.prototype, "items", 2);
      note: "@observable items"
    - match: |
        decorate([dec], $g.prototype, "isReady", 2);
```

When a decorator application matches an annotated
`effect: typescript_decorate_helper`, the dependency graph records it as a
target-local effect on the class/prototype binding. That makes the
anonymous statement a required companion of the class source unit for
materialization and factorize. The author still
materializes it with `anonymous_statements:` because it has no binding
name, but the atomic-group closure that contains the class also covers
the anonymous source unit, so a proposal containing the class also contains the
anonymous source unit (listed under `anonymous_statement_source_unit_ids`, or
`extension_source_unit_ids` when it extends an existing module) and the
proposer never proposes the class alone.

The unannotated case — `__decorate(...)`, `register(...)`, and
target-mutating `Foo.x = ...` installs that the analyzer cannot tag
as target-local because no helper annotation matches — relies on a
separate route. The proposer's greedy contraction merges a residual
class into an existing active module when the realizability gate accepts
the merge, and the merged source units surface as an extension
(`extends_module_id`, `extension_source_unit_ids`). The consumer of the proposal
reads source-unit shape (named bindings vs anonymous statements) to decide whether
to write a `members:` or `anonymous_statements:` entry into the extended
module's yaml.

### Resolution

An anonymous-statement entry is an entity of the joint selector program
(<../SPEC.md>). Its `source_match.match` is a shape-matcher template — holes
and alpha-renaming as in any `source_match`; a bare `match:`, as above, keeps
identifiers exact — and the entry takes exactly one of the top-level
statements it matches, jointly with every other entity: `all_different` keeps
two entries, from any modules or trees, off one statement. No place is `no_match`; several assignments are `ambiguous`, never
settled by source order. Authoring: <selectors.md> § Anonymous side-effect
statements.

The exactly-one contract is what keeps these entries safe. An anonymous
statement has no declaration-site identity, only its shape, so a selector that
silently accepted a different statement after an upstream change would move the
wrong code while the spec still validated. Holes widen what a template matches,
but an unintended match must still be the only one to be taken; otherwise it
surfaces as `ambiguous` at validation time.

An anonymous `source_match.match` may include preceding top-level context
statements. It matches a contiguous window under one set of alpha/free-identifier
bindings and claims only the final statement. A context declaration may be
claimed separately by a member selector; it neither overlaps this anonymous
claim nor enters `all_different` through it. The final statement must be
anonymous in this context form, and repeated identical windows remain ambiguous.

### Constraints on the selector source

- The `match` source must parse as JS through the same parser the chunk used.
- An entry claims exactly one top-level statement. Bare `match:` must contain
  one statement; `source_match.match` may include contiguous preceding context
  statements and must end in the anonymous statement to claim. A top-level
  `STMT_LIST` hole cannot appear in the context.

### Why shape rather than line/column

The target application's bundle is delivered minified and prettified for analysis;
neither line nor column is stable across re-prettifies or
upstream releases. The shape selector survives both: the
prettifier reformats whitespace but preserves AST structure, and
upstream changes that touch the statement's content surface as a
zero-match diagnostic the spec author can fix.

### Claimed anonymous source units in the assignment

An anonymous source unit belongs to the entry destination unless a
module claims it through `anonymous_statements`.
`assignment_assembly::compute_source_unit_claims` resolves those claims (each module's
`anonymous_statement_ordinals`) into the assignment that
`AssignmentAnalysis::build_with` quotients by, so the gate sees the module dep
graph the materializer will emit. Without that resolution, an anonymous source unit with a
constraining in-edge from a peeled named source unit would create a fake
cross-module edge, and the validator would reject a spec whose closure the
materializer would emit correctly.

## Comma-list var-decls with split owners

The spec's `owner` is a function from binding name to module.
Comma-list var-decls (`const A = e1, B = e2, C = e3`) declare
multiple bindings at once; the spec can assign them to different
modules. Before any of the per-statement reasoning in this doc
applies, the materializer rewrites such a comma-list into
separate single-binding declarations:

```
const A = e1, B = e2, C = e3
   →
const A = e1;
const B = e2;
const C = e3;
```

The split is semantically transparent within a single module —
all three statements run in source order regardless of whether
they're written as one comma-list or three separate
declarations. After splitting, each declaration has a single
declared binding, the spec's `owner` map is well-defined per
statement, and `home(S)` resolves cleanly.

The rewrite must happen _before_ statement-fact analysis. Each
split-out statement gets its own ordinal (in source order); each
gets its own `reads_at_init` and `has_side_effect`. The dep
graph captures any cross-declarator reads (e.g. `C = A.x`'s read
of `A`) automatically.

### Spec surface

The spec selects a single declarator from a comma-list by naming
the bound identifier as a member. Example, given source
`const a = 1, b = 2, c = 3;`:

```yaml
logical_modules:
  static/app:
    mod_a:
      members: [{ selector: { binding: { name: a } } }]
    mod_b:
      members: [{ selector: { binding: { name: b } } }]
    mod_c:
      members: [{ selector: { binding: { name: c } } }]
```

emits three modules — each declaring exactly its claimed
declarator with the original kind (`const`/`let`/`var`) and any
`export` directive preserved. Unclaimed siblings stay in the
entry destination as a (possibly single-declarator) comma-list.
Source-order side effects across declarators are preserved by the
ESM linker, which evaluates the destination modules in source
order. See <../e2e/comma_list_owner_split_test.rs> for the full
shape matrix.

**Destructuring declarators are atomic.** `const { x, y } = obj`
is one declarator binding two names; the lowerer's
`split_var_decl` moves it as one unit. Claiming any one of its
bindings (e.g. `x`) pulls every sibling (`y`) into the same
module — sibling bindings join their claimed module with their
local name as the export name. Claiming siblings into different
modules is rejected with an explicit error from
`pull_destructure_siblings`.

## Cycle resolution

When the gate rejects a spec, the author must remove the constraining cross
edge from the quotient SCC. In spec-only work, that usually means:

**Colocate the constraining source-unit endpoints.** Move the source unit that
performs the at-init read or side-effecting work together with the
source unit it depends on. Once both endpoints share a destination, that
edge disappears from the quotient.

Rewriting source so the constraining read becomes lazy can also help, but the
debundler normally does not rewrite program structure. Lazy read edges still
emit import directives and still participate in `I`, so the cycle that is left
behind is judged by Pass 2.

`render_cycle_summary` (<../validation.rs>) names the binding pairs to
colocate, per blocking SCC.

## Architecture

The transform is a fixed data flow over explicit artifact phases:
input loading produces loaded chunk data; preparation produces the
parsed/prepared chunk artifact consumed by the vendor plan,
logical materialization, emission rewrites, and output emission. The
pipeline should not grow a second mutable "state" object parallel to
those phase outputs.

The flat transform spec is YAML decoded directly into typed serde
structures. It carries inputs, declarative data maps, and optional
output configs; it is not an operation list and has no top-level
`kind`/`schema_version` compatibility envelope. Report fields and
stage names use the same default snake_case serde names as the Rust
types, so emitted diagnostics do not maintain a second hand-written
string vocabulary.

Some downstream repos keep a higher-level authoring tree where YAML
files are laid out like the eventual emitted JavaScript modules. That
shape is a debundler-owned authoring layer: `debundle` compiles it
into the same typed flat transform spec in memory, then runs the fixed
pipeline below. The dependency graph reports are the bridge: they expose
source-unit ids, readable member names, destinations, and the atomic
DAG so authoring tools can mostly project and filter
debundler facts instead of re-analyzing JavaScript or private repo
YAML conventions.

The authoritative stage sequence — which stages exist, which module
implements each, and when each runs — is the fixed composition in
`run_transform_cli` (<../pipeline.rs>); this doc does not
mirror it. The shape to know: spec load/validate → chunk load/prepare
(one parallel per-chunk SWC parse) → artifact indexes → the read-only
`build_vendor_resolution_plan` → `materialize_logical_modules` (the only
lowering stage) → `apply_emission_rewrites` →
`validate_emitted_exports` (duplicate-public-export tripwire) →
optional tree/harness outputs.

Within `materialize_logical_modules`, analysis (chunk AST scan,
statement facts, dependency graph construction — <../lowering/chunk_ast.rs>,
<../facts/mod.rs>, <../graph/>) feeds the stages that carry design
invariants:

1. **Binding assignment** → a per-binding `ModuleId`
   (`BindingKind::Owned`) from the spec's explicit member list. Bindings
   with no spec entry default to the entry destination; nothing pulls
   implicitly. (See [Spec explicitness](#spec-explicitness-and-diagnostics).)
2. **Quotient + validation** (<../graph/>, <../realizability/mod.rs>,
   <../validation.rs>) — collapses source units by destination, aggregates edge
   reasons, and runs the realizability gate over the result; a rejection
   aborts the pipeline with the cycle evidence.
3. **Diagnostics projections** — cycle evidence, atomic-group conflicts,
   and atomic graph reports are projections of the same dependency graph +
   quotient, not separate heuristic analyses.
4. **Source-order emission** — each module's body in source order;
   cross-module imports + source-chunk re-imports; `export { ... }`.
   No init wrappers. Import-directive ordering comes from the shared
   `esm_import_order::EsmImportOrder` (the same object the
   realizability gate's evaluation simulator consults): the entry
   imports every logical module — side-effect-only for binding-less
   plans — in entry-import order, and each module's intra-chunk
   imports (binding, phantom side-effect, residual-entry) form one
   merged list in module-import order.

Local named exports (`export { binding as public }`) are also part of the
source chunk's public interface. If such a directive is assigned to a
logical module as an anonymous statement, the entry retains a facade
export of the same binding and public name. The entry's planned import
provides the binding; the logical module keeps its source directive and
does not synthesize the identical local-to-public export a second time.
Only an identical pair is redundant: another binding using the same
public name remains a duplicate-export error.

Treat those stages as a functional data flow. Analysis produces
immutable facts; assignment is explicit input; quotienting derives a
validated schedule; emission consumes that schedule. Avoid designs
where emission discovers new graph edges or silently mutates the
assignment, because that reintroduces hidden closure behavior and
makes diagnostics disagree with emitted output.

### Contract-audit preparation reuse

`ContractAudit` retains one immutable preparation within an invocation: parsed
source and its matching indexes, the read-only vendor plan, and captured input
provenance. The only editable spec section at its probe boundary is
`chunk_export_purity`; all preparation inputs remain fixed. Every independent
probe clones the prepared source before mutation and recomputes facts, assignment,
the gate, lowering, emission rewrites, and final reconstruction acceptance.
No analysis result or edited AST is reused by another probe. The audit verifies
source/spec/package bytes and path membership before and after probes and before
publishing a report or checkpoint. Resume prepares again and validates a fresh
baseline before reusing definitive checkpoint records.

## Vendor chunk swapping

The `vendor` spec map classifies chunks as vendor code at one of four
levels: `suppress` (annotation only — the chunk and its callers pass
through byte-identical), `boundary_rename` (rewrite caller imports
from vendor-local names to the vendor's public export names),
`swap` (replace the whole chunk with an upstream npm package), and
`partial_swap` / `bundled_partial_swap` (swap a subset of the chunk's
exports against upstream packages and strip the swapped bodies from
the residual chunk). Soundness edges that are deliberate contracts
rather than missing features are documented here.

### Full swap: callers retain imports for removed chunks

`level: swap` excludes the vendor chunk from the emission set. The
dependency graph never contained vendor chunks; `write_js_tree`, harness
emission, the emit-shape check, and chunk reports skip it, and a
wrapper module is written in its place. Caller imports of the
excluded chunk are **intentionally left unchanged** in the plain
`write_js_tree` output. A host consuming that output supplies the
package mapping. The emitted tree is therefore not directly runnable
under bare Node when a full swap is configured. Pinned by
`full_swap_with_caller_preserves_import_for_removed_chunk` in
<../e2e/vendor_swap_test.rs>.

### Partial swap: consumer rewrites and the consumer gates

Per-symbol partial swaps rewrite two consumer shapes — named
`ImportDecl` specifiers and `export { x } from "<chunk>"` re-exports
of swapped names (`kind: named` / `default` / `namespace`) — at two
application sites driven by the same `VendorResolutionPlan` oracle:
lowering's import construction for materialized module bodies, and
the pass-through emission rewriter for files emitted without
lowering. Shapes with no live rewrite — `import * as M` namespace
imports of the chunk, `export *` from the chunk, re-exports of
`kind: member` symbols or bundled-swap symbols, and any rewrite-needing
consumer inside a hands-off `suppress`-marked chunk — are rejected by
the **plan-time consumer gate**
(`vendor/plan.rs::validate_consumer_shapes`) before any output is
written.

A second, post-strip scan (`validate_partial_swap_consumers` in
<../vendor/mod.rs>) re-checks every retained file of the
post-materialize artifact and bails on any surviving reference to the
stripped export surface. It is load-bearing, not a redundant
tripwire: lowering can synthesize consumer directives inside
materialized module bodies (`BindingKind::Imported` re-export
imports, `export … from` re-exports in moved bodies) that have no
live rewrite at the construction site and that the plan-time gate —
which enumerates input-space directives — cannot see. Retiring the scan
requires those construction paths to consult the `VendorResolutionPlan`
(rewrite or reject before output) and fixtures proving the synthesized
directive shapes fail without the post-strip check.

Over-restriction is the accepted failure mode for both gates: a
namespace consumer that happens to read only non-swapped members is
still rejected, because per-member usage is not analyzed.

The residual strip analyzes its post-rewrite module with the shared
structural, value-flow, purity, and `VendorPrune` local-effect facts.
Value-flow call augmentation is required for its island reachability;
`StatementFacts::effects()` supplies its observable binding and global-cell
writes. The strip does not build an dependency graph or consume
`synchronous_calls`, so this analysis entry point omits only synchronous
call-effect annotation. The split-brain and swap-privacy gates still use the
same augmented calls, local effects, and cell-write summaries.

### Deliberate split-brain bypasses in the strip pass

Vendor package ownership follows source reads and rebinds, including those
inside callback bodies, plus the items that mutate a referenced local value.
The general effect analyzer also infers possible call targets, but those
targets are not ownership edges: a target supplied to another call of the
same helper does not belong to this package. The strip pass still checks
retained reads, local mutation writers, and observable writes before dropping
an item.

The strip pass bails on "split-brain" items — top-level items
reachable both from the residual chunk's exports and from a swapped
package's bodies, where stripping would leave the residual and the
upstream package each holding half of a shared value. Two bypasses
deliberately weaken that bail:

- **Multi-package items**: the split-brain bail fires only when the
  item is reachable from exactly **one** swapped package
  (`packages.len() == 1` in <../vendor/strip.rs>). An item shared by
  two or more swapped packages has no single upstream home, so it is
  retained in the residual chunk and effectively duplicated relative
  to the upstream packages.
- **Shareable helpers**: items classified `shareable_helper`
  (syntactically stateless shapes — function declarations,
  primitive-literal consts, function-valued consts, intrinsic
  aliases, Vite preload-map cells) are exempt from the split-brain
  bail and may be duplicated between the residual chunk and the
  upstream package.

Both bypasses share the same rationale and the same risk. Rationale:
duplicating a stateless value is observationally harmless, and
bailing would reject many real specs. Risk: if a "helper" actually
carries state (a memo cache, a registry object, a closure over a
mutable cell — or a multi-package item holding module-level state),
duplication forks that state — the residual chunk and the upstream
package each get their own instance, and identity checks or cache
coherence between them silently break. The `shareable_helper`
classifier is syntactic and cannot see state hidden behind a
function value; treat new classifier shapes with suspicion.

### Wrapper re-exports are init-time snapshots, not live bindings

The full-swap wrapper shapes (`named_from_default`,
`named_from_module_default`, `named_from_json_default` in
<../vendor/wrappers.rs>) emit `export const <name> = <default>.<name>;`
/ `export const <name> = <default>;` statements. These evaluate
**once at wrapper-module init**. Real ESM named exports are live
bindings; the wrapper's are snapshots. This is a documented
precondition on the upstream package: its default export's
properties must not be reassigned after module init, and consumers
must not rely on live-binding semantics for the wrapped names.
(JSON wrappers are immune — JSON modules are inert data.)

`named_from_module_default` additionally requires each non-`default`
vendor export to be a **verified alias** of the chunk's own `default`
export — i.e. the chunk binds the name to the same local as its
`default` (`export { x as default, x as alias }`). This rejects a
chunk that exports an unrelated binding (`export { x as default, y as
other }`), which would otherwise silently re-export the package
default under `other`. A chunk that re-exports the package default
under a single minified name with no `default` export of its own
(`export { Ft as c }`, as Vite emits for a default-only re-export)
cannot be verified from the chunk alone. For that case the swap mark
carries `default_export_aliases: [<name>...]`, an author-asserted list
admitted as verified aliases — the author is responsible for
confirming the binding really is the package default (e.g. by its
runtime identity/version).

## Unmatched spec claims

A `logical_modules` member whose binding selector names no top-level
declaration in the chunk is recorded as an `UnmatchedSpecClaim`
(<../lowering/materialize/plan_builder.rs>); lowering continues as if the
member were absent, so the claim neither moves the binding nor adds an export.
Final validation rejects the full list across chunks before writing the accepted
JS tree, browser harness, vendor assets, or their manifest (<../reconstruction.rs>). Diagnostic
reports remain available for inspection. The named destination would otherwise
be one export short.

A module whose members are all `import_specifier` claims owns no bindings and
emits a re-export-only file (re-imports and re-exports, no owned code).

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
   _every_ statement and _every_ read — at-init **and** lazy —
   so the I-graph it judges matches the linker's view. There is no
   opt-out path that bypasses the dep graph.

## Unrealizable splits

Examples of splits that the realizability gate refuses.

### Mutual at-init cycle

```js
// chunk
function wrap(x) {
  return { ref: x };
}
const A = "a";
const B = wrap(A);
const C = "c";
const D = wrap(C);
```

With `{A, D}` in `mod_x` and `{B, C}` in `mod_y`, `B = wrap(A)` reads `A` at
init (`mod_y → mod_x`) and `D = wrap(C)` reads `C` at init
(`mod_x → mod_y`). Both edges constrain init order, so Pass 1 finds the SCC
`{mod_x, mod_y}`; no import order satisfies both reads
(<../e2e/realizability_test.rs>, `cyclic_spec_is_rejected_with_clear_error`).

Resolution: colocate `A` with `B` or `C` with `D`, so one of the cross edges
disappears.

### Class extends across a cycle

```js
// in mod_a
class A { … }

// in mod_b — at module top, eager:
class B extends A { … }
```

`mod_b → mod_a` is a constraining edge: the `extends` clause reads `A` at
init, and class declarations are TDZ-prone. If `mod_a` also reads anything
owned by `mod_b` at module top, the cycle is mutual and Pass 1 rejects it. If
`mod_a`'s read is lazy (inside a function body), the cycle is asymmetric and
Pass 2 decides: it accepts when the emitted import order evaluates `mod_a`
before `mod_b`, and rejects when `mod_a` is the entry destination, which as the
DFS root evaluates last — `class B extends A` would throw
`ReferenceError: Cannot access 'A' before initialization`.

Resolution: colocate `A` and `B`, or remove the cross edge that closes the
cycle.

### Computed property key reading another module

```js
// in mod_a
const m = { kind: "K1" };

// in mod_b — at module top:
const table = { [m.kind]: value };
```

`mod_b → mod_a` is a constraining edge through the computed key. Computed keys
aren't special: they read identifiers at init like any other expression, and
the dep graph captures that read without special-case logic. If the spec also
gives `mod_a` an at-init read of something in `mod_b`, the cycle is mutual and
rejected.

## AssignmentInputs + AssignmentAnalysis: dependency graph plus quotient

The runtime data structures the validator and emitter both consume are a pair:
the `gate` crate's per-chunk `AssignmentInputs` (inputs + IR; distinct from
`chunk_analysis::ChunkAnalysisOutput`, the spec-independent composer output) and a
`AssignmentAnalysis` wrapping it (assignment + derived realizability views).
Together they carry the chunk's statement facts, the binding catalogue, the
explicit logical modules, the dependency graph, and the module dep graph derived by
quotienting that dependency graph under the spec assignment.

Statement facts feed the dependency graph (stored on `AssignmentInputs`), and the
module dep graph is a quotient of that dependency graph under the current spec
assignment (stored on `AssignmentAnalysis`). `AssignmentAnalysis::inputs()`
returns the shared inputs; `module_quotient()` and `module_quotient_sccs()` expose
the derived module graph. `ValidatedAssignment::assignment_analysis()` retains
the accepted analysis while lowering projects its emission placement.

`AssignmentAnalysis::validate_for_emission` returns a `ValidatedAssignment` only
when atomic-group, schedule, and cross-destination rebinding checks pass. Lowering
requires this immutable borrow of the checked assignment. Assignment inputs and
derived graph views are private and exposed only through immutable accessors, so
callers cannot change the assignment independently of its cached import order or
quotient. Rejection reports remain
available to diagnostics without constructing an emission capability. This checks
the assignment and shared import order; vendor consumer and final export checks
still run on the emitted files.

`BindingId` carries the interned source `ChunkId` together with the full SWC
`Id` (symbol and `SyntaxContext`). `BindingCatalogue` stores that chunk identity
once beside the existing `HashMap<Id, BindingKind>`; it does not rekey every
binding. Identity lookups take a `BindingId` and reject a different chunk.
Same-chunk graph and AST algorithms use a borrowed local map only after checking
the chunk scope. These are runtime identities; report and spec wire values keep
their existing shapes.

Together, the assignment inputs and analysis carry:

- chunk identity;
- per-statement facts in source order;
- the binding catalogue;
- logical modules from the spec;
- the dependency graph;
- the module dep graph `I ∪ S`, derived by quotienting the dependency
  graph by the spec's source-unit-to-destination assignment.

Everything downstream needs is here:

- `home(stmt)`: for statements with `declared(stmt) ≠ ∅`, look
  up any declared name in `bindings` — its `Owned.module` is
  `home`. For statements with empty `declared`, `home` is the
  entry destination unless a module claims the statement through
  `anonymous_statements` or `unassigned_mode` routes it elsewhere.
- "What source units/statements live in module M" =
  `dependency_graph.nodes.filter(dest(source_unit) == M)`.
- "What does M export" = bindings whose `Owned.module == M`
  (under their original or rename-pass-rewritten name) plus
  bindings whose `Imported.re_exporter == M` (under
  `Imported.public_name`).
- "What imports does M need" =
  - For each dependency edge from a source unit with `dest == M`:
    - If `b` is `Owned { module: other }`: `import b from <other>`.
    - If `b` is `Imported { imported_from, imported_name, .. }`:
      `import { imported_name as b } from <imported_from>`.
- "What can be peeled without backtracking" = candidate assignments
  whose dependency graph quotient validates, whose cross-destination reads
  are importable, and whose rebinding writes stay inside one
  destination.

The reason to make `BindingKind` explicit (rather than
collapsing imports into the same `Owned` map):

**Re-export semantics aren't ownership.** A logical module that
re-exports `import { Y as X } from "<vendor>"` does not own
`X` — modifying our spec to "claim" `X` should not rename `Y`
in the vendor chunk; it should emit a re-export in our logical
module. A flat binding → module map conflates these
two cases; tagging via `Owned` vs `Imported` keeps them
distinct. The validator additionally rejects duplicate claims
of either kind, so each imported binding has exactly one
re-exporter (a consumer that wants a different local name
aliases at its own import site instead of authoring a separate
re-export module).

### Rejected: one assignment-optional chunk IR

Folding `AssignmentAnalysis` into `AssignmentInputs` as one IR with optional
assignment state looks like it removes a layer, but the two-layer shape is
already in place: `AssignmentInputs` is the assignment-free IR, `AssignmentAnalysis`
adds the applied assignment, and `validate()` derives `AssignmentReport` from
it. Removing the `Arc<AssignmentInputs>` boundary touches the whole
materializer/emitter path for no behavior change, so the layers stay.

### Identifiers are typed, not stringly-typed

A string that identifies a thing of a known kind gets a newtype; untyped
`String` is reserved for free-form text (error messages, log lines, the
actual JavaScript identifier _as text_). The identifier types live in
<../ids.rs> and <../graph/>: `ChunkId` (an index interned by `ChunkTable`),
`ModuleId` (the in-process dense module handle), `SourceUnitOrdinal`,
`SourceUnitId` and `DependencyEdgeId`. A
module's public identity is `spec::ModulePath`, never a `ModuleId`
(<wire_format.md>). Artifact metadata follows the same rule: `FileRole` is a
typed enum (`entry`, `module`, `runtime` in JSON via snake_case serde). Do not
encode known roles as raw strings, generic maps, or compatibility-only JSON
fields.

## Known sharp edges

### Side effects we can't see locally

`reads_at_init(S)` only catches local-name reads. These ordering
constraints are real but invisible to the analyzer:

- `globalThis.X = ...` / `window.X = ...` writes that another
  module reads via `globalThis.X`. Module A "sets" the value, B
  "reads" it; the dep graph has no edge.
- `eval(some_string)` running arbitrary code at init.
- `new Function(...)` likewise.
- Mutations to objects passed across imports
  (`vendor.someConfig.foo = bar` — `vendor.someConfig` was
  imported, the mutation is observable).

The conservative `has_side_effect = true` for any function call
catches _some_ of these cases (it forces a side-effect-order
edge), but won't catch e.g. two pure-looking modules where one
quietly writes a global and the other reads it. The validator
will accept a spec that produces wrong observable behaviour at
runtime.

Mitigation: the conservative side-effect classification is
already there; bundled output rarely depends on these patterns,
because bundlers can't preserve them across boundaries either.
If a real spec ever exhibits this, the response is to surface it
as a known spec-author-side limitation.

## Open design questions

These are unresolved precision issues. Each is worth its own
exploration before crossing the relevant phase.

1. **Local-effect annotation coverage.** The
   `effect: typescript_decorate_helper` annotation is deliberately
   narrow. It covers the
   TypeScript helper shapes currently seen in bundled output:
   property/method decorators on `C.prototype` and class decorators on
   `C`. Calls with spread arguments, computed/optional targets, or
   unresolved targets must remain ordinary conservative side effects.
   Future helper forms need one test per admitted shape before they are
   added.
2. **Lazy-position completeness.** `reads_at_init` is implemented
   as a visitor that descends into eager positions and stops at
   lazy positions (<../facts/lazy_boundary.rs>). Function, arrow, method,
   getter/setter and constructor bodies and instance class-field initializers
   are lazy; `extends` clauses, decorator expressions, computed member keys,
   static fields and static blocks are eager. Open: an exhaustive table of unit
   tests for these, plus default-parameter values (ECMA-262 evaluates them on
   call), object-literal getter/setter bodies, and dynamic `import()`
   arguments.
3. **Claim-all selectors for `anonymous_statements`.** An entry claims
   exactly one statement, holes or not. Letting one entry claim every match of
   a pattern (`decorate([ANYTHING], $g.prototype, ANYTHING, ANYTHING);` for
   every decorator application on `$g.prototype`) would collapse N
   near-identical entries into one, but gives up the exactly-one contract that
   makes an unintended match loud. Deferred until a real spec demands it.
4. **Constraining-graph-sink refinement.** Some anonymous
   statements (Sentry debug-id IIFE, Vite modulepreload polyfill)
   are sinks in the constraining edge graph: they have side
   effects but no observable cross-binding deps. An s-edge from
   a peeled named source unit to such a sink could be exempted as
   non-constraining, letting the named source unit peel without an
   `anonymous_statements` co-mover. Empirically this would cover only a small
   fraction of the blocked bindings; the rest still need anon
   selectors because their companions (decorator applications,
   semantic init bridges) are not sinks. Worth tracking but
   not blocking.

## What this design does not solve

- **Choosing module boundaries.** The debundler executes the
  spec; it does not author it. Higher-level analysis tools
  (React component detection, big-string clustering, scrambled-
  identifier statistics) help the human author write better
  specs, but the spec is still chosen by humans.

Cycle handling is described in § Cycle resolution, and the emitted naming
contract is in <../SPEC.md>
§ Readable names and the chunk public surface.

## File references

Entry points (the rest of the tree hangs off these; per-concern file
rosters live in the directories, not here):

- <../pipeline.rs> — fixed transform composition.
- <../facts/> — `StatementFacts` analyzer (<../facts/analyze.rs>).
- <../graph/> — dependency graph, `ModuleQuotient`, and the canonical I-graph
  builders.
- <../realizability/mod.rs> — the realizability gate (`check_realizability`).
- <../validation.rs> — `validate_assignment`: renders the verdict as cycle
  reports.
- <../lowering/> — main splitting transform.
- <../atomic_groups.rs> — source-unit hard colocation groups.

## Conventions for updating this doc

- This is the **canonical design source.** When a design choice
  changes (because we learn something new, hit a constraint, or
  pivot), update this doc _first_ and only then change the code
  to match. Code that disagrees with the doc is a bug — either
  in the code or in the doc. Decide which and bring them back in
  sync.
- Keep sections phrased as current contract. Planned work belongs in
  `TODO.md` or `plans/`, and completed phase logs in Git history and
  dedicated lessons-learned notes.
- Open design questions go in <#open-design-questions>. Once
  resolved, move the resolution into the body of the doc and
  delete the question.
- Keep the proof intact. The realizability theorem is the
  foundation; if a future change breaks the theorem (e.g.
  introduces an emit strategy that bypasses it), the proof
  needs revision and any dependent claims need re-checking.
