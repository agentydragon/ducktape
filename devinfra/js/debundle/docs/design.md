# Debundler — Design

> Summary of the design: the debundler takes a bundler-emitted
> ESM chunk and a path-first logical-module spec, and emits a
> multi-module ESM bundle observationally equivalent to the
> input. The primitive analysis product is an owner graph: nodes
> are top-level owners/statements, and edges are "this owner uses
> that binding" plus source-order side-effect constraints. A spec
> is an assignment from owners/bindings to output destinations.
> The validator quotients the owner graph by that assignment to
> derive the imports graph `I` plus side-effect ordering graph
> `S`, runs SCC detection, and accepts the spec iff every
> `I ∪ S` SCC's cross-module edges are all `LazyRead` — i.e. no
> at-init read and no side-effect ordering edge appears inside a
> multi-module SCC. The emit is source-order: each logical
> module's body is its assigned statements in original chunk
> order, with explicit `import` / `export` declarations. There is no
> init-wrapper machinery, no closure pass, no implicit binding
> pulls — every owned binding is named explicitly in the spec,
> and `Imported` bindings flow through `ChunkAnalysis.bindings` /
> `BindingKind::Imported`, carrying the single re-exporter
> `ModuleId` and public name that claimed it.
> Anonymous (empty-`declared`) top-level statements have no name
> to address as a member; a parallel `anonymous_statements`
> selector list addresses each by its AST shape (verbatim source,
> matched modulo spans). They co-move with their named-binding
> companions when the closure requires it, but the spec is still
> explicit — the author writes the selector, no closure pass
> infers it.

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
top-level owners/statements and a granular use graph between them.
The spec assigns owners to output destinations. Collapsing all
owners with the same destination yields a module dep graph; the
validator checks that this quotient graph admits an ESM evaluation
order observationally equivalent to the input.

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

### Bindings, names, and keys

A _binding_ is a named slot in a chunk's top-level lexical scope. Two
syntactic positions introduce a binding into that scope:

1. **Declaration**: `var X = ...`, `let X = ...`, `const X = ...`,
   `function X(){}`, `class X{}` introduce `X` as a _declared
   binding_. The value lives in this chunk; we own it.
2. **Import specifier**: `import { Y as X } from "<other-chunk>"`
   introduces `X` as an _imported binding_. `X` is a local alias
   for an export of another chunk; the value lives elsewhere.

Within a single chunk's top-level scope, a name `X` is introduced
_at most once_ — JavaScript rejects duplicate top-level
declarations, and a redeclaration through an import is a
SyntaxError. So the pair `(chunk_id, name)` is a one-to-one
identifier for a binding.

That pair is the conceptual _binding key_ at repo/tool boundaries.
Inside a single chunk analysis, the chunk is already contextual, so
the in-memory representation keys bindings by SWC's
`Id = (Atom, SyntaxContext)` — the hygiene-aware identity the
resolver pass mints (`graph.rs`, `facts/`). Reports and specs spell
bindings by name (`Atom`-only on the wire; see
`docs/wire_format.md`); internal owner graph edges, owner
declarations, and per-chunk indexes clone `Id`s into `BTreeMap` /
`BTreeSet` keys.

A compact interned form (a `BindingId(usize)` newtype plus a
`BindingTable` mapping names to dense indices, so hot paths do
vector lookups instead of keying by cloned `Id`s) is **hypothetical
and not implemented** — not a description of the code. Decided
2026-06: implement it only if corpus profiling shows the
binding-keyed graph paths as a material cost; such evidence would
land in `perf/proposer.md`.

Names are stable across the readability rename pass. The rename
pass changes the _emitted_ identifier in the destination module's
text; it does not change what a binding _is_. The schedule keys
on the original (scrambled) source-chunk local; the rename pass
applies as a final post-processing step that reads
`LogicalModule.rename_map` and rewrites the emitted source.

### Two binding kinds

A binding's _kind_ records which of the two introducing positions
made it, plus the metadata downstream stages need:

```rust
pub enum BindingKind {
    /// Declared by a top-level `var/let/const/function/class` in
    /// this chunk. The spec assigns each owned binding to a
    /// `ModuleId` (defaulting to `ResidualEntry` when unclaimed).
    Owned { owner: ModuleId },
    /// Introduced by an `import { imported_name as <name> } from "<chunk>"`
    /// at the chunk's top. The value lives in `imported_from`;
    /// our chunk merely aliases it. A logical module can choose
    /// to *re-export* this alias under its module path (with its
    /// own choice of public name), but it cannot *own* the value
    /// — the chunk on the other side does.
    Imported {
        imported_from: ChunkId,
        imported_name: String,
        /// The single logical module that claims this imported
        /// binding via a `kind: import_specifier` member, and the
        /// public name it gives the export. The spec's
        /// duplicate-claim check rejects two modules claiming the
        /// same import — a consumer that wants the symbol under a
        /// different local name aliases at its own import site.
        re_exporter: ModuleId,
        public_name: BindingName,
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
`owner: BindingId → ModuleId` (over keys with kind `Owned`);
bindings without an explicit owner default to the _residual
entry_ module (a synthetic module that holds whatever is left
over).

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

- **`home(S) ∈ Modules`** — the module a statement is emitted
  into. For statements with `declared(S) ≠ ∅`, `home(S)` is the
  spec's `owner` of any name in `declared(S)`. (We require all
  names in `declared(S)` to share an owner; comma-list var-decls
  with split owners are split into separate var-decls before this
  step.) For statements with `declared(S) = ∅` (bare expressions,
  side-effecting statements), `home(S)` defaults to the residual
  entry module **unless** the spec claims `S` via an
  `anonymous_statements` shape selector on some logical module
  (see §"Anonymous-statement selectors"); a single shape-selector
  match overrides the default and routes `S` into the claiming
  module.

## Owner graph

The lowest-level scheduling object is the **owner graph**. It is
independent of any particular logical-module spec.

An _owner_ is a top-level unit that will move as a unit during
emission: a function declaration, class declaration, single
var-declarator after comma-list splitting, import declaration
specifier, or residual side-effect statement. The implementation
preserves this as a first-class data structure and side output.

The micro owner graph is a directed, labeled graph `G = (V, E)`:

- **Vertices:** owners in source order. A declaration owner carries
  `declared`, source location, kind, and any readable rename/spec
  metadata. A residual side-effect owner may have no declared
  binding.
- **Read edges:** `O -> O_b` for every binding `b` read by owner
  `O`, where `O_b` is the owner that declares `b`. The edge carries
  `binding`, `read_kind` (`at_init` or `lazy`), and the source
  statement ordinal.
- **External read edges:** `O -> imported_from` for imported
  bindings. These are not ownership claims; they describe
  re-import/re-export requirements.
- **Side-effect order edges:** potential `O₂ -> O₁` constraints
  when both owners have side-effecting top-level evaluation and
  `O₁` appears before `O₂` in the source chunk. After assignment
  they become `S` edges only when their endpoints land in different
  destinations.

The owner graph is the right abstraction for tooling:

- "Can this thing be peeled?" is a proposed reassignment of one or
  more owner vertices.
- "What else must move with it?" is a candidate-construction
  question over owner-level read, write, and side-effect edges. The
  answer is not proof by itself; the resulting assignment must still
  be checked by the same quotient validator as a handwritten spec.
- "Why did this fail?" should point at owner-level edges first, then
  at the derived module edge they induced.

The emitted module graph is a **quotient** of this graph: choose a
destination function `dest(owner)` from spec assignments (defaulting
to residual entry), merge owners with the same destination, drop
intra-destination edges, and aggregate all cross-destination edge
reasons.

## Module dep graphs as quotients

The materializer validates three directed graphs derived from the
owner graph quotient. Each captures a different scheduling
constraint; the realizability theorem (next section) is stated over
their union.

The vertex set in every case is

```
V = Modules
```

— the set of logical modules introduced by the spec, including the
synthetic `ResidualEntry` that holds unowned bindings and any
external (vendor / cross-chunk) modules referenced from this chunk.

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
The linker computes a topological order of `I`; that order is
the module evaluation order. **`I` is the realizability graph.**

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
order. `R` is _not_ used for cycle detection — every `R` cycle is
also an `I` cycle — but `R` shows up in the realizability proof
as the sub-graph whose acyclicity guarantees no at-init read sees
TDZ once the linker has linearized `I`.

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

Unlike `I`, `S` is **not** encoded in any `import` directive: the
linker doesn't see `S`. The materializer satisfies `S` by choosing
the entry module's import order so the linker's reverse-DFS lands
on a topological linearization of `I ∪ S`. Acyclicity of `I ∪ S`
guarantees such a linearization exists.

#### Emission modes

`S` is materialised at owner level (`graph.rs::emit_s_chain`) and
the chunk spec picks one of two modes via
`chunk_analysis_options.<chunk_id>.dataflow_aware_s_chain`:

- **Strict chain** (default): for each impure top-level statement,
  emit one `Sequenced` edge to the immediately previous impure
  statement. This is the transitive reduction of the total order
  over impure statements; it preserves reachability and all SCCs
  while keeping the owner-edge count linear. Soundest path —
  every realizable schedule satisfies it.

- **Dataflow-aware** (opt-in): per-statement `(writes, reads)`
  effect summaries (`StatementEffectSummary` in `facts/mod.rs`)
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
  `self`, `frames`, `top`). The mode is **conditionally correct**
  (see AGENTS.md → "Conditionally-correct optimizations"):
  statements containing a shape that defeats static cell tracking
  flip `dataflow_summarizable=false` and fall back to a strict
  S-edge against every prior impure owner (also acting as an
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

  See `README.md` → "Conditionally-correct optimizations" for the
  user-facing precondition list.

### Relationship

```
R  ⊆  I            (R is the at-init projection of I)
I  ∪  S            the full constraint graph
```

## Realizability primitive

The three-clause validity predicate — importability, no cross-destination
rebinds, no multi-module SCC in the constraining-edge subgraph — has exactly
**one** implementation. Every code path that asks "is this destination
assignment realizable?" reaches it through the same primitive:

> `check_realizability(owner_graph, partition) → Verdict`

The verdict surfaces unrealizable SCCs, cross-destination rebinds, and
non-importable reads with owner-edge provenance. An empty verdict means the
partition is realizable per "Valid peels and atomic modules" below.

Two caller families consume it:

1. **The validator (the gate).** Given the spec's actual partition, the
   verdict decides acceptance or rejection. This is the load-bearing call —
   if it rejects, materialization stops.
2. **Planner checks.** Read-only tools may ask hypothetical "what if these
   owners moved?" questions against the same primitive. Advisory planning is
   now driven primarily by the emitted atomic DAG; ordinary `debundle run`
   does not serialize heuristic proposal projections.

### Iterative, undo-aware shape

Asking the primitive from scratch per query is `O(N + M)`. A chunk's
planner checks can ask many hypothetical questions, so the primitive is exposed as a
**stateful, transactional index over a working partition** rather than a
pure function:

- Callers push partition deltas (move owners, create destinations) onto the
  index. Each push updates quotient edge buckets and graph adjacency only for
  owner edges incident to moved owners. The verdict is always against the
  index's current state.
- Each push records its inverse on a journal. Callers undo deltas in LIFO
  order to back out of a hypothetical or failed exploration. The peel
  kernel (`peel/quotient.rs`) pushes committed merge deltas permanently
  (journal truncated via `commit`); its speculative merge queries read the
  index through the non-mutating overlay path instead. `peel/factorize.rs`
  is a renderer over the kernel's quotient and never touches the journal
  directly.
- Candidate peel checks use the same push/read/undo API as other
  hypothetical questions, but read only SCCs and rebinds touching the fresh
  destination. A new directed edge `u -> v` can create a cycle exactly when
  `v` reaches `u`; the index answers that against the maintained quotient
  without rebuilding owner-graph state.
- The validator does no undo: it pushes the actual partition once and reads
  the verdict.

The pure-function form `check_realizability(owner_graph, partition)` is the
correctness reference; the incremental, undoable form is the production
implementation. Differential tests assert the two agree across nested
push/undo sequences.

This is "the iterative graph that callers update and undo updates on":
the quotient is built once per chunk, then walked forward by deltas
(toward a hypothesis or a commit) and backward by undos (when backing
out). Candidate checks use localized reachability around the affected
destination after a scoped delta push; full validation can still run Tarjan
over the maintained quotient. The owner graph and quotient buckets are not
torn down and rebuilt per question.

### Invariant: no bespoke parallel walks

No production code should answer the validity question by walking the
owner graph or the quotient with its own algorithm. Bespoke per-question
walks are how planner/gate divergence repaired in this design first
appeared: two algorithms drift; one shared implementation cannot. If a new
caller needs a verdict over a hypothetical partition, the answer is to
push a delta and read the index — not to spin up a parallel walk over
`module_pair_totals` or similar derived state. Adjacent diagnostics may
read the verdict's owner-edge provenance, but the validity decision goes
through the primitive only.

### Peel planner unification

The peel planner's `QuotientGraph` kernel uses the same realizability
primitive as the materializer. It must not reimplement the gate over a
JSON `OwnerGraphReport` projection: a constraining-only projection drops
`LazyUse` edges and becomes blind to asymmetric `(eager forward, lazy
back)` I-cycles that the `EsmEvaluationSimulator` pass catches.

The unified path:

1. `OwnerGraph::from_report(&OwnerGraphReport) -> (OwnerGraph,
OwnerReportIndex)` reconstructs the typed IR from the JSON wire
   shape. The reconstructed graph carries every edge (constraining +
   lazy) with the original `DepKind`.
2. `QuotientGraph::from_report` stashes the reconstructed `OwnerGraph`
   on the kernel.
3. `would_be_cycles_after_contract` projects the current class
   assignment back to a `Partition` (one synthetic `ModuleId` per live
   class; the residual catchall becomes `ModuleId::logical(0)`) and
   calls the shared realizability primitive. The verdict is the source
   of truth — same primitive the materializer's `validate_factorization`
   calls. `merge_preserves_invariants` stays on the cheaper boolean path
   and avoids diagnostic evidence generation.
4. `cycle_set()` also runs the unified gate per call.

#### Cost and the tier ladder

A from-scratch `check_realizability` call is `O(|V| + |E|)`. The
kernel's merge-candidate greedy queries it `O(|V|)` times per round,
so a naive seeding pass would be `O(|V|² · |E|)`. For gaffer-scale
inputs (`|V| ≤ ~10³`, `|E| = O(|V|)`) that's `~10⁹` ops — measurable
but within budget.

The kernel uses the persistent `RealizabilityIndex`
(§"Realizability primitive" → "Iterative, undo-aware shape") instead of
rebuilding from scratch per query. `QuotientGraph::from_report`
initializes the index from the singleton-class partition projection
and stores it alongside the typed `OwnerGraph`. Every committed
mutation — `contract`, the partition-driven group merges in
`from_report_with_partition_extended`, the `is_pre_existing_module`
promotion in `set_class_pre_existing_module` — synthesizes the
corresponding `PartitionDelta::MoveOwners` and pushes it onto the
index. The index maintains the constraining-edge graph SCC state,
the I-graph adjacency, the `EsmEvaluationSimulator` per-pair bucket,
and the cross-rebind set incrementally, touching only quotient edge
buckets incident to the moved owners.

Speculative queries — the hot boolean gate
(`merge_preserves_invariants`, called once per candidate by the
greedy's pop loop) and the diagnostic path
(`would_be_cycles_after_contract`) — are one evaluation: the
index's tier ladder
(`ladder_decision_after_moving_owners_touching`), entered with the
merge's post-state ModuleId from
`projected_winner_module_after_merge` (mirroring the gate-residual
override `project_partition` would apply) and the `±edge` overlay
from `compute_merge_deltas`. Each tier either decides — provably
equal to the full `check_realizability` verdict, restricted to
diagnoses touching the post-merge module — or escalates:

- **Tier 0 — delta-free short-circuit.** No deltas (both classes
  already project to the post-merge module, e.g. merges inside the
  gate-residual pile): post-state == pre-state, so accept iff the
  cached pre-state touching verdict is clean.
- **Tier 1 — constraining condensation order.** A maintained
  `CondensationOrder` over the module-level constraining graph (a
  union-find of SCC membership plus a Pearce–Kelly topological
  order over the condensation DAG): reject iff the post-merge
  module's constraining SCC is multi-module — an `O(α)` find, or a
  PK window-DFS over the overlay-patched effective adjacency,
  `O(|Δ|)` — or a clause-2 cross-rebind touches it. A tier-1
  reject is exactly the `MutualConstrainingCycle` clause of the
  full verdict; a pass establishes Pass 1 is clean and escalates.
- **Tier 2 — I-graph condensation order.** A second
  `CondensationOrder` over the I-graph (constraining ∪ lazy): if
  the post-merge module's I-SCC is not multi-module, or contains
  no effective constraining pair, Pass 2 is vacuous — accept.
  When the overlay removes an edge internal to a multi-module
  I-SCC (the one case where the maintained union-find is
  stale-coarse), the tier falls back to the exact per-query
  `OverlayGraphView::scc_containing` bidirectional DFS.
- **Tier 3 — scoped ESM simulator.** The shared
  `EsmEvaluationSimulator` over the overlay-patched I-SCC: reject
  iff any TDZ pair. This is the same code `check_realizability`'s
  Pass 2 executes — drift is impossible by construction.

The tiers are short-circuits whose skip conditions are theorems
about the predicate, not a second decision procedure. The boolean
gate is the ladder with evidence materialization elided;
`would_be_cycles_after_contract` is the same ladder, materializing
owner-level evidence (through the index's non-mutating
`verdict_after_moving_owners_touching` overlay) only on a reject —
one entry point, two output shapes. Each query reads SCCs touching
the hypothetical destination only — `O(|cone|)`, not
`O(|V| + |E|)`. Speculative deltas are single-target by
construction: a merge contracts two classes into one, so
`compute_merge_deltas` only ever emits `MoveOwners` deltas
targeting the single post-merge module; the ladder dispatch asserts
this invariant, and a future non-merge speculative mutation must
implement a multi-target overlay deliberately rather than inherit
the merge path.

The kernel's `ClassId ↔ ModuleId` mapping (`class_module_id`) is
maintained alongside the index. Initialization assigns each
non-residual non-gate-residual class a fresh `ModuleId::logical(N)`;
the residual catch-all class and every gate-residual singleton
share `ModuleId::logical(0)`. Class IDs are never reused (contracted
classes leave empty slots), and surviving classes keep their
ModuleId across merges. The exception is a pre-existing-module
promotion: when `set_class_pre_existing_module` flips the
`is_pre_existing_module` bit on a class that was previously
gate-residual-only (and so mapped to the residual ModuleId), the
class is promoted to a fresh non-residual ModuleId and a
`MoveOwners` delta is pushed to keep the index's working partition
in sync.

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
realizability crate's `CondensationOrder` (window DFS, window Kahn,
epoch visited-marks), re-keyed from kernel classes to condensation
nodes of the index's maintained module graphs, with a union-find
for SCC membership so the PK order lives over a structure that
stays a DAG by construction even when the underlying graph is
cyclic. Contraction monotonicity — vertex identification only ever
coarsens the SCC partition — is what licenses a plain
non-rollbackable union-find instead of full dynamic-SCC machinery
(kernel-side index mutation is commit-only); committed removals
internal to a multi-module SCC mark the membership stale-coarse and
trigger tier 2's exact-DFS fallback rather than an eager split.

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
perspective of any caller that invokes the function at-init. The owner
graph carries promoted **EagerUse** and **EagerRebind** edges from
at-init callers to the transitive closure of their callees' lazy
reads/rebinds, so the primitive's clause-3 verdict is sound for the
canonical `console.log(readB())` shape (top-level call whose body
crosses a module boundary).

The promotion runs once in `build_owner_graph` and is
partition-independent: intra-module promoted edges are dropped by the
quotient automatically, so the same promoted owner graph drives
hypothetical planner partitions and the validator's actual one.

**Algorithm.** For each top-level chunk statement S with `calls.eager`
non-empty, walk the chunk call graph (among chunk-declared functions)
in reverse topological order to compute, per function-owner F,
`reachable_lazy_reads[F]` and `reachable_lazy_rebinds[F]` — the
fixpoint closure of F's body lazy reads/rebinds plus the closures of
every chunk function F calls. Then for each callee C in S's
`calls.eager`, emit promoted edges from S's owner to every owner
declaring a binding in C's reachable closures.

**Hoisted-target filter.** Promoted reads filter out targets whose
owner is a `FnDecl` — function declarations are hoisted at Phase-1
module instantiation, so reading them from another module never
observes a TDZ. Without this filter, mutual recursion across modules
(`function even(){odd()}` / `function odd(){even()}` split into
mod_a / mod_b) would spuriously close a constraining-edge cycle that
isn't actually unrealizable at runtime.

**Per-statement dedup.** A single at-init call to a function with N
transitive lazy reads would otherwise emit N edges from the caller
statement, and multiple at-init calls in the same statement would
multiply that. The promotion pass dedupes per `(caller, target-owner)`
pair per kind, keeping the per-statement cost bounded by the
transitive closure size rather than (closure × call-sites).

**Resolvable callees.** A callee is precisely resolvable iff its
binding is declared _directly as a function value_ (`function f`,
single-declarator `const f = () => ...`, exported forms) and is
never rebound anywhere in the chunk. Only resolvable callees feed
the precise first-order closure above.

**Unresolved-callee fallback.** Every other at-init call shape —
member calls (`api.read()`), aliases (`const g = readB; g()`),
IIFEs, optional-chain calls, tagged templates, conditional or
rebound function bindings, and calls into resolvable functions
whose own first-order bodies contain such calls — takes a
conservative fallback (`graph.rs::UnresolvedCallFallback`). The
fallback's premise: whatever function value the call invokes must
have reached the call site through a binding the call expression
mentions (callee root, arguments, computed keys) or through an
inline function expression at the call. Each such root owner is
expanded to the **full lazy closure** of every owner reachable from
it through the chunk's read graph (eager + lazy reads), and the
calling statement gets promoted `EagerUse` edges to every collected
target — rebind targets included (order-only: write legality is
separately enforced by the direct `LazyRebind`/`DeferredRebind`
edge from the statement lexically containing the write).
Fallback edges sourced in residual are dropped at partition
projection: residual is the ESM DFS root and evaluates last, so an
at-init call from residual code cannot observe a TDZ, and the
emitter emits no entry-side phantom imports the gate could
otherwise assume.

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

**Residual limitations.** The fallback (and promotion generally)
does not model function values that reach a call site through:

- global/object property stashes (`globalThis.f = readB;` …
  `globalThis.f()`),
- rebound bindings (`let g; g = readB; g();` — the rebind forces
  co-location of the rebinder with `g`'s declarer, but the flowed
  value's closure is not followed),
- parameters of chunk-declared functions
  (`function h(cb) { cb(); } h(readB);`),
- `new` expressions (constructor bodies are not promoted), or
- other chunks (imports are outside single-chunk analysis).

These are documented preconditions on the input bundle, not
checked invariants — a bundle using one of these shapes to fire a
TDZ-able cross-module read at init can be accepted and break at
runtime. There is no separate validator safety net behind the
promotion pass.

## Lemma 2: entry-side import ordering

The realizability primitive's relaxed clause-3 rule
(`check_realizability` accepts a spec iff the constraining-edge
subgraph of `Q` has no multi-module SCC) admits mixed cycles in the
imports graph `I` — cycles where every back-edge is `LazyUse`. For
the ESM linker to actually evaluate these without TDZ, the entry
module's `import` directives must be emitted in a specific source
order so that depth-first link traversal lands on a Phase-2
evaluation order matching the constraining-edge linearization.

The materializer computes the source-import order on
`esm_import_order::EsmImportOrder` (held by `ChunkFactorization`)
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
   ESM DFS therefore always enters a non-residual SCC at its
   most-dependent member (Lemma 2's intra-SCC reversal) before
   any mediator's dependency-first imports can reach it. This is
   why a non-residual mediator reaching into an SCC does NOT
   reject: the entry's own import of the SCC's dependent wins
   the race (pinned by
   `e2e/mediator_reaches_asymmetric_cycle_test` and
   `e2e/asymmetric_non_residual_cycle_test`).
2. **Residual in the cycle, with a constraining edge whose
   target is residual, always TDZs.** Residual is the ESM DFS
   root — post-order evaluates every other cycle member first,
   then residual. A constraining read of residual's
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
   residual:
   - At residual, fan out to EVERY module in the I-graph (the
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
validator (`validate_factorization` in
`devinfra/js/debundle/validation.rs`) and read-only planner
checks share the realizability primitive's verdict — a
`peelable_now` proposal is a peel the gate accepts and Node will
execute correctly at runtime (the contract pinned by
`e2e/lemma_two_rescued_asymmetric_cycle_test`,
`e2e/mediator_reaches_asymmetric_cycle_test`, and
`e2e/runtime_tdz_on_imported_class_test`).

## The realizability theorem

> **Theorem (correctness).** If `R ∪ S` is acyclic, the source-
> order emit construction below produces a multi-module ESM bundle
> observationally equivalent to the input chunk. (Note this is
> stated over `R ∪ S`, not `I ∪ S` — see "Gating rule" below.)
>
> **Design rule (gating).** The materializer accepts a spec iff
> every SCC in the imports + side-effect graph `I ∪ S` is
> realizable. An SCC is realizable iff every cross-module edge
> between its members is a `LazyRead` — equivalently, the SCC
> contributes no `R` (at-init) and no `S` (side-effect-ordering)
> cross-module edges. Cycles whose only back-edges are lazy reads
> are accepted: the ESM linker walks `I` to pick _some_
> evaluation order for the SCC, every module finishes evaluating
> with all bindings assigned, and the lazy reads only fire later
> (after `L`'s call sites run). Cycles with `R` or `S`
> cross-module edges are rejected: `R` would TDZ during cycle
> evaluation; `S` has no consistent topological emit order.
>
> **Implementation.** The implementation computes per-statement
> facts, builds the owner graph, then quotients that graph by the
> spec's destination assignment to derive the module dep graph. Each
> cross-module edge keeps owner-level provenance: the triggering
> statement, optional binding, and whether the reason is an at-init
> read, lazy read, or side-effect-order constraint. Diagnostics can
> explain both the module-level cycle and the lower-level owner edges
> that induced it.

Here "spec assignment" means whatever the validator sees: every
declared binding either has an explicit `owner` from the spec or
defaults to `ModuleId::ResidualEntry` (see [Spec explicitness
and diagnostics](#spec-explicitness-and-diagnostics)).
There is no implicit transformation between the spec and the
assignment the theorem reasons about.

### Why the gate is over `I ∪ S`, not `R ∪ S` or full `I ∪ S` acyclicity

The validator builds the imports graph `I` (every cross-module
reference, at-init or lazy) plus the side-effect ordering graph
`S` (one edge per pair of cross-module side-effecting top-level
statements in source order). It runs SCC detection on `I ∪ S`,
then accepts SCCs whose cross-module edges are all `LazyRead`.

Two strictly weaker checks would be wrong:

1. **Building only `R ∪ S`.** A cycle in `I` whose at-init
   projection (`R`) is acyclic still affects the linker: lazy
   reads emit `import` directives that the linker uses for its
   DFS evaluation order. A spec where two modules mutually
   import — one direction at-init, the other lazy — has acyclic
   `R` but cyclic `I`; the linker enters the SCC, picks an
   evaluation order, and the at-init read TDZs when the
   late-evaluated module hasn't finished its body. Building `I`
   directly catches this.
2. **Rejecting any `I ∪ S` cycle.** A cycle whose only
   cross-module back-edges are `LazyRead` is realizable: the
   linker still walks `I` and picks some evaluation order, but
   no read fires until all SCC members have finished evaluating.
   Function bodies don't run during linking; the lazy reads only
   resolve when their call-sites do, after the entire SCC is
   live. Rejecting these cycles would over-restrict the
   realizable subset of `I ∪ S` and force colocation of bindings
   whose only inter-module relationship is mutual lazy reference.

The gate as stated picks the largest realizable subset of
`I ∪ S` and rejects everything strictly outside it.

### Conditions on the input chunk (assumptions A1–A7)

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
  `stage_one::compute_chunk_analysis` next to the A2 bail)
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
  fields), and `stage_one::compute_chunk_analysis` `bail!`s
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
  AGENTS.md "Pure-call whitelist soundness" for the contract that
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

  An incorrect annotation can produce a buggy debundle the same way
  an incorrect spec selector can — soundness shifts to the spec
  author. See AGENTS.md "Declared purity".

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
  expression purity cannot infer. The first admitted value is
  `effect: typescript_decorate_helper`, for esbuild/TypeScript
  `__decorate`-style helpers:

  ```js
  Ro([Z], C.prototype, "visible", 2);
  Ro([ClassDecorator], C);
  ```

  This annotation does **not** assert that the call is pure. It
  asserts a more precise effect: for recognized call shapes, the
  call's only modeled mutation is local to the target class/prototype
  owner. The analyzer therefore emits a target-local owner edge from
  the decorator-application statement to the class owner and suppresses
  the otherwise-global side-effect-order edge for that statement. The
  target-local edge is an atomic colocation constraint: the helper
  application and the target owner must move together, and source order
  still keeps the application after the class declaration inside the
  same emitted module.

  The annotation is intentionally shape-checked. If the callee is not
  the annotated binding, if the target class/prototype binding cannot
  be resolved, or if the call uses an unsupported shape, the analyzer
  falls back to ordinary conservative side-effect classification. An
  incorrect annotation can produce a buggy debundle, so it carries the
  same review burden as `purity: "pure"`; the difference is that the
  trusted claim is local target mutation, not absence of effects.

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
  spec field and is the author's audit obligation, like every entry in
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

A1–A5 are statically checkable on each chunk, and each now has an
enforced core. A2 is enforced by the TLA scan inside fact analysis —
`stage_one::compute_chunk_analysis` `bail!`s with the offending
statement ordinal as soon as fact analysis returns, before any
quotient or lowering work. A1, A3, and A5 are enforced (to the
per-assumption strengths described above) by the input-chunk
admission scan in `chunk_admission`, which
`compute_chunk_analysis` runs right after the A2 bail; its
diagnostics carry the chunk id, the offending statement ordinal,
and the matched shape. A4 is enforced at parse time. The admission
scan is on by default for every materialized chunk; for audited
corpora a spec can disable individual checks per chunk via
`chunk_analysis_options.<chunk>.admission_overrides`
(`[a1_eval, a3_dynamic_import, a5_import_meta]`) — every override
use prints a one-line notice, and overrides that no longer suppress
anything are reported as redundant. A6 (no module-eval reentry) is relied
on by observation: the unmodified bundle loads in the browser.
A7 (multi-chunk DAG) is satisfied by typical bundlers' vendor-leaf
chunking. A8 (whitelist receivers not shadowed) is dropped to
`Unknown` per-name by `compute_shadowed_globals` whenever the
chunk-top declared-name set claims one of the whitelist receivers,
so chunks that violate A8 still validate soundly. A9 (declared
purity is author-trusted) is satisfied by spec review — every
`purity: "pure"` annotation is an explicit, reviewable trust
claim. A10 (declared local effects) is likewise satisfied by spec
review, with analyzer shape checks limiting the trusted surface to
the admitted helper-call forms. A11 (intrinsic integrity) is not
statically checkable — pollution can originate outside the analyzed
chunk — and is relied on by observation of the target bundles.

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
`ChunkFactorization`, see "Lemma 2: entry-side import ordering"
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
shape **where residual itself is a cycle member**. Lemma 2's
"unwind through the dependency" fails there — residual is the ESM
DFS root, so post-order evaluates every other cycle member first,
and a constraining read into residual TDZs. The primitive's
tightened rule rejects this shape outright (see the residual-in-
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

**Theorem (correctness).** Under assumptions A1–A7, if `R ∪ S`
is acyclic and every cycle in `I ∪ S` has only `LazyRead`
cross-module edges between SCC members, then the source-order
emit construction produces a multi-module ESM bundle whose
observation-trace under ECMA-262 evaluation is identical to the
input chunk's evaluation.

_Proof._ Acyclic `R ∪ S` admits a topological linearization `L`
of the at-init read + side-effect graph. The full graph `I` may
have additional cycles, but every such cycle has only `LazyRead`
cross-module edges by hypothesis; the ESM linker may pick any
DFS order through these cycles, and Lemma 4 (Lazy-read
correctness) shows the resulting evaluation order is sound for
lazy reads regardless of which member the DFS enters first. By
Lemma 2 we author each emitted module's import directive list
to make ECMA-262 produce a linker order respecting both `R` and
`S`. By Lemmas 3 and 4 every binding read in the emitted bundle
sees an initialized value identical to the input chunk's read
at the same source ordinal. By Lemma 5 every side-effecting
statement fires in the same relative order as in the input
chunk. By the emit's same-module source-order invariant,
statements within a
module fire in their original source ordinal order. The
observation-trace of the emitted bundle is therefore identical
to the input chunk's. ∎

### Coverage gaps (what the proof does NOT cover)

The proof above is a sufficient-condition theorem under A1–A7.
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
- **Lazy-only cycles** are _accepted_ by the realizability gate.
  The theorem stated above (over `R ∪ S` acyclicity) covers
  them: the linker walks `I` and picks some evaluation order for
  the cycle, every module finishes evaluating with all bindings
  assigned, and the lazy reads only fire later (after `L`'s
  call-sites run). The architectural-fragility worry — a future
  edit promoting a lazy read to at-init — is caught by the
  validator the moment the new `R` edge enters the SCC.

### Worked example: cycle through a lazy back-edge

A spec that's acyclic in `R` alone but cyclic in `I` once
lazy back-edges are counted:

```
mod_a:                                      mod_b:
  const A = "a-value";                        const B = A + "-postfix";  // R-edge mod_b → mod_a
  function readB() { return B; }              // (mod_b owns B; mod_a's `readB`
  // ↑ lazy read of B; closes I-cycle        // is referenced at-init from
  // mod_a → mod_b                           // residual entry's `readB()` call)
```

`R = {(mod_b, mod_a)}` — acyclic. But the lazy `readB` body
still emits `import { B } from "./mod_b"` in mod_a, contributing
edge `(mod_a, mod_b)` to `I`. The `I ∪ S` SCC is `{mod_a, mod_b}`,
and it carries the at-init `(mod_b, mod_a)` edge — so the
realizability gate rejects.

If the gate did _not_ fire, runtime behavior would depend on
linker DFS order: if entry imports `B` first, DFS goes
`entry → mod_b → mod_a → mod_b (cycle, return)`, mod_a
evaluates first, A is initialized when mod_b reads it. If entry
imports `readB` first, DFS goes `entry → mod_a → mod_b → mod_a
(cycle, return)`, mod_b evaluates first, mod_b's
`const B = A + "-postfix"` runs while A is in TDZ. The bundle
works or breaks depending on entry-import ordering.

The realizable fix is to colocate `B` with `readB` in one
module, dissolving the SCC. This is the canonical example of a
**spec-induced atom** in the sense of §"Two classes of atom"
below: the owner-level read graph is a DAG, but the spec's choice
to split `B` from `readB` makes the module-level `I ∪ S` cyclic.
The validator's `CycleReport` names this exact binding pair (with
the now-binding-pair-blame `render_cycle_summary` format), so the
diagnostic points the spec author directly at "{B, readB} must
co-locate."

The synthetic minimization is in <e2e/realizability_test.rs>
(`rejects_cycle_through_lazy_back_edge`); the namespace-aggregator
form is in <e2e/namespace_aggregator_split_tdz_test.rs>.

### Why a static gate, not a runtime check

The gating rule is enforced **statically** by the validator. The
materializer either emits a bundle whose `I ∪ S` is provably
realizable (every SCC has only `LazyRead` cross-module edges)
and therefore observationally equivalent by the theorem above,
or refuses with cycle evidence. There is no runtime check, no
init-wrapper safety net, no per-load TDZ guard. By construction,
the emitter never produces JavaScript that the ESM linker has to
puzzle through a cyclic at-init read graph for. If the spec
describes an unrealizable analysis (`R` or `S` cycles), the
spec is wrong; we report the cycle and let the author fix it.

This is the contract the user-visible artifact relies on. Any
escape hatch — accepting `R` cyclic specs that happen to work in
testing, or deferring cycle detection to runtime — gives back
the property that "an emitted bundle from this materializer is,
by inspection of its module graph alone, free of TDZ and
side-effect-ordering hazards."

### Multi-chunk extension

The theorem above is stated for a single chunk. Real bundles have
many chunks (entry, code-split routes, vendor bundles), each with
its own logical-module split. Cross-chunk dependencies are
edges from one chunk's logical module to another chunk's logical
module (via `Imported` bindings).

The extended theorem is the natural lift: take the union of every
chunk's `I ∪ S`, with `Imported` edges contributing cross-chunk
edges to `I`. The spec is realizable iff this combined graph is
acyclic.

In practice vendor chunks are leaves — they don't import from
us, so they emit no edges into user-chunks. User-chunk to
user-chunk cycles can occur (an `index-DI2GynTv` ↔
`StoryIndex-DrlmoZTE` cycle is conceivable in any
code-split bundle) and the multi-chunk validator detects them.
A user→vendor edge that ends up being a user→user→vendor cycle
is an even rarer pathological case (a vendor chunk that
re-exports something from a user-chunk); the validator detects
it but the right fix lives in the bundler's chunking config,
not in the spec.

The validator currently runs per-chunk; cross-chunk edges appear
as `ExternalChunk(_)` leaves in the per-chunk graph. **Future
work**: a multi-chunk lift would have `validate_factorization` take a
`BTreeMap<ChunkId, ChunkFactorization>` and walk the union graph.

### Pipeline split (chunk analysis / materialization)

The per-chunk pipeline is two halves with sharply different
dependencies:

- **Chunk analysis** (spec-independent): parse → per-statement facts →
  owner graph → structural atomic units. Pure function of
  `(source bytes, analysis hints, OwnerGraphOptions)`. The composer
  is `stage_one::compute_chunk_analysis`, returning a
  `ChunkAnalysis` that bundles `ChunkFactAnalysis` (facts +
  top-level-await detection + redundant-hint warnings) with the
  `OwnerGraphAndUnits` derived from those facts.
- **Materialization** (spec-dependent): assemble the partition from the
  spec's binding claims, run the realizability gate, lower to ESM.
  Today this lives inline in `lowering::materialize_logical_chunk`.

The materializer is the composition of both. Current code materializes
chunk analysis in-memory through one named call site. Keep that shape:
it makes the boundary explicit without committing the pipeline to
cross-process fact reuse. If future work tries to cache chunk analysis
across processes, it must first solve SWC hygiene replay for pre-filter
facts; see
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
(owner level vs module-quotient level), not in _what_ the proof
requires. Both reject the same broad property: "some pair (or set)
of bindings is forced to co-locate, and the spec splits them."

| Atom class       | Where the cycle lives                                                 | Detector                                         | Diagnostic                                                                                                   |
| ---------------- | --------------------------------------------------------------------- | ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------ |
| **Structural**   | SCC of `G_atomic` on the **owner graph** — independent of any spec    | `compute_atomic_units`                           | `AtomicUnitConflict` — names the unit's owners + the modules the spec routes them to + the `DepKind` causes  |
| **Spec-induced** | SCC of `I ∪ S` on the **module quotient** under the spec's assignment | `check_realizability` + `validate_factorization` | `CycleReport` — names the binding pair(s) on each cut edge, the source and target modules, and the edge kind |

`G_atomic` over owners is acyclic-by-quotient on the chunker's
original output: bundlers can't emit JavaScript that TDZs at runtime
(the bundle ran for someone). So every owner-level SCC of
constraining edges (`EagerUse`, `Sequenced`, `EagerRebind`,
`LazyRebind`, `DeferredRebind`, `LocalEffect`) reflects a
_colocation invariant the bundler relied on_. (`DeferredRebind` —
a rebind nested ≥2 closures deep or past an `await` — joins
`G_atomic` bidirectionally like the other rebinds because ESM
imports are read-only whenever the write fires, but it does NOT
constrain init order: it is excluded from the constraining-edge
subgraph the realizability gate and the I-graph use.) Any spec assignment that routes the unit's
owners to different modules is invalid — `assemble_partition`
detects this before the materializer touches the quotient. **No
information about the spec is required to identify a structural
atom**: it is a property of the source bytes plus the analyzer's
edge classification.

A **spec-induced atom** is one level up. Each owner may be in its
own structural atom (singleton), and the _module quotient under the
spec's assignment_ may still close a cycle through them. The
worked example in §"Worked example: cycle through a lazy back-edge"
is exactly this shape: the binding-level read graph is a DAG, but
the spec's choice to put `B` and `readB` in different modules makes
the module-level `I ∪ S` graph cyclic. The fix is identical in
_shape_ — co-locate the bindings — but the _unit being preserved_ is
defined by the spec's quotient, not the source bytes.

Why two paths in the implementation: structural conflicts can be
caught with no module quotient ever built — it's an `O(|V| + |E|)`
SCC computation on the owner graph that produces a clean blame
("these owners can never split"). Spec-induced cycles require the
quotient (because they're about the assignment) and produce a more
nuanced blame (which _binding pairs_ on which _cut edges_ close the
cycle, after `petgraph::algo::greedy_feedback_arc_set` picks the
cheapest cut to break).

Both diagnostics name the implicated bindings at the source level,
not the module level. A 1300-module SCC closing through one
constraining `(mod_A, mod_B)` edge surfaces as "binding `iRe` in
`mod_A` reads binding `Y` in `mod_B` at-init; move them together,"
not "you have a cycle of 1300 modules." See
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
build owner graph
  (owners, read edges, potential side-effect-order edges)
  ↓
compute structural atoms = SCC(G_atomic over owners)
  ↓
apply spec assignment + assemble_partition
  ↓
  ├── if any structural atom is split across modules:
  │   reject with AtomicUnitConflict (owner-level blame)
  ↓
quotient owner graph by dest(owner) → I, R, S
  ↓
validate: realizability gate over I ∪ S
  ↓
  ├── if any quotient SCC contains a constraining (R/S) edge:
  │   reject with CycleReport (binding-pair blame from cut edges)
  ↓
emit (source-order)
```

Both rejections write the same kind of sidecar evidence — the owner
graph and the conflicts/cycles report under `reports/tree/<chunk>/`
— so spec authors can drill down to specific bindings + statements
regardless of which atom class caught the spec.

A spec that passes validation is _guaranteed_ to emit correctly
under the source-order strategy described in the proof. There is
no class of accepted-input that the validator may emit incorrectly
(modulo cleanly-defined precision of `reads_at_init` and
`has_side_effect`). The validator may reject specs that are in fact
realizable when analysis is conservative; that is the intended
failure mode. Those rejections should come with owner-level evidence
showing which graph edges made the split unverifiable.

### Layered mental model

Five layers, bottom-up:

1. **Owner graph** — fine-grained program facts (`graph.rs`).
   One vertex per top-level owner; edges record `EagerUse`,
   `LazyUse`, `EagerRebind`, `LazyRebind`, `DeferredRebind`,
   `Sequenced`, and the local-effect edges that target-local
   mutation produces. Each
   edge records whether it constrains init/materialization order.
2. **Atomic graph** — SCC condensation of the constraining-edge
   subgraph of the owner graph; a DAG of atomic units
   (`compute_atomic_units`). Each unit is the smallest set of
   owners a valid module assignment may not split. Surfaced in
   `owner_graph.json.atomic_graph`.
3. **Spec partition** — the author's current assignment of owners
   to modules (the spec's `members:` + `anonymous_statements:`
   plus the residual default).
4. **Module quotient** — the owner graph projected onto the spec's
   partition. Cross-module edges are the realizability evidence
   `check_realizability` operates on.
5. **Module proposals** — DAG-derived advisory recommendations
   computed by the factorizer (`debundle modules propose`) over
   `atomic_graph`. **Not** emitted by `debundle run`; they are a
   read-only planner projection.

When debugging planner output, start with the atomic unit. If an
assignment would split a unit, the assignment is wrong regardless
of how plausible the binding names look. If a proposal is too
broad, inspect the atomic-DAG edges that close it and decide
whether the edge classification is too conservative or whether the
larger module is genuinely required.

### Factor assembly inside `debundle run`

The spec's explicit claims are resolved against the owner graph in
`factor_assembly.rs`. The rules are deliberately strict:

- Two different logical modules may not claim owners from the same
  atomic unit.
- A module claim that only covers part of an atomic unit is a
  conflict, not an implicit request to move the rest.
- Unclaimed owners default to the synthesized residual module.
- The materializer consumes only the final explicit partition; it
  does not silently co-move extra owners on behalf of the author.

If the explicit partition is inconsistent, `debundle run` rejects
and emits diagnostic side outputs (`cycles.json` or
`atomic_unit_conflicts.json` under `reports/tree/<chunk-id>/`).

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

**Design rule.** The spec is fully explicit. Every owned binding
has its `owner` named in the spec or defaults to
`ModuleId::ResidualEntry`. Empty-`declared` (anonymous) statements
default to `ResidualEntry` unless explicitly claimed by a logical
module's `anonymous_statements` shape selector. There is no
implicit pulling: when an atomic unit or planner proposal includes
anonymous statements, the spec author copies their source into
`anonymous_statements` (one entry per claimed statement); the
materializer never silently co-moves an anonymous statement.

The owner graph is the explicit replacement for hidden closure. It
records the fine-grained "this owner uses that owner" relation before
any module-level quotienting. Diagnostics are projections of that
graph: validation cycles explain why the current explicit assignment
is not realizable, `atomic_graph` records which owner sets are
indivisible, and the read-only query surface (`debundle modules
propose`, `atoms`, `coverage`, `describe`, `show-source`, `scc`,
`cluster`, `graph-summary`) can rank or annotate DAG-derived proposals. None of those projections silently assign bindings on
behalf of the spec author.

Each materialized chunk has reports under `reports/tree/<chunk_id>/`.
`owner_graph.json` carries the detailed owner graph, current module quotient,
and embedded atomic DAG. Compact validation status is folded into
`chunk.json`; `cycles.json` and `atomic_unit_conflicts.json` are emitted only
for rejection cases.

When an output emitter materializes executable JavaScript, root reports live
under `reports/` and per-file/per-directory reports are mirrored under
`reports/tree/` next to the chunk reports. `reports/output.json` and
`reports/tree/<chunk_id>/chunk.json` include `output_metrics` computed from the
exact rendered JS written to disk: total bytes/lines/files, top-level entry
bytes/lines/files, named-module bytes/lines/files, residual module
bytes/lines/files, and the largest JS sinks. Peeling tools should use these
fields for progress reporting instead of rescanning output trees.

The mirrored `reports/tree/**/index.json` files project the semantic
owner/module dependency graph onto recursive emitted-directory boundaries. They
report incoming/outgoing edge counts by dependency kind plus full symbol and
file attribution maps. A symbol key has the form
`<target_file>#<export_or_binding_name>`; binding-less edges such as
`sequenced` contribute to edge-kind and file counts but not symbol maps. Uses
within the same directory do not count against that directory boundary. Use
these reports to judge hierarchy encapsulation and leaky subtree boundaries;
use the owner graph and source bodies for drill-down.

### Workflow

1. Spec author writes / edits a spec — possibly partial.
2. Pipeline runs the validator. The report flags:
   - **Cycles** in the explicit-only assignment, if any.
   - **Atomic-unit conflicts** when the spec splits an indivisible unit.
   - **Atomic DAG facts** in `owner_graph.json` for planning the next peel.
3. Spec author resolves: first fix any validation cycles or atomic-unit
   conflicts; then use `debundle modules propose`, `atoms`, `coverage`,
   `describe`, and `show-source` to choose explicit owner sets for new
   spec modules with good public names.
4. Re-run validator. Iterate until validation is green and the
   residual contains only intentionally generated or low-value noise.

The spec is now fully explicit. The validator is a one-shot
predicate: "given this spec, is the bundle realizable?" — no
implicit transformation in the middle.

## Atomic DAG Planner Data

The same owner graph drives the next-spec workflow. A pipeline that
only reports whether the current spec passed forces spec authors into
speculative edit/build/test loops. `debundle run` therefore emits the
atomic-unit DAG in `owner_graph.json`, and the top-level read-only
queries (`debundle modules propose`, `atoms`, `coverage`, `describe`,
`show-source`, `scc`, `cluster`, `graph-summary`) compute advisory
views from that stable graph.

<peel_proposer.md> is the current-state implementation note for how
`modules propose` builds and greedily extends its quotient before
rendering proposal rows.

`debundle modules propose` labels each proposal owner set with a status:

- **`peelable_now`** — this closed atomic-DAG owner set has no outgoing
  constraining edges into other residual cells; it can be promoted on
  its own.
- **`blocked_residual_dependency`** — the cell reads other residual
  cells (`edges_to_other_residual_cells > 0`); promoting it alone would
  route those reads through `residual_entry`, so the referenced cells
  must land first or together.
- **`blocked_cycle`** is reserved vocabulary that is currently
  unreachable: the quotient's contraction gate refuses cycle-creating
  merges, so no emitted class is cyclic by construction.

`landable_today` derives from the same predicate as the status (plus
anonymous-statement addressability): it is `true` only for
`peelable_now` proposals whose anonymous owners are all addressable.
A `blocked_residual_dependency` proposal is never `landable_today` —
`bindings assign --batch` rejects it; grow the closure so the
referenced cells land together, or co-locate them manually, first.

Classes whose spec-edit size exceeds `--size-cap-lines` are not
proposals at all — they surface as diagnostics with reason
`exceeds_size_cap`.

The important invariant is that the proposal queue is a projection over
`atomic_graph`, not a separate fact emitted by the transform pipeline.
Ordinary `debundle run` emits owner facts, module quotient facts, and the
atomic DAG; the read-only query surface ranks and groups those facts for
authoring workflows.

### Residual Proposal Closures

The current planner answers the operational question: "which residual
atomic units can move together next?"

It starts from residual atomic units, follows outgoing constraining
atomic-DAG edges to other residual units, coalesces overlapping closures,
and emits the closed owner set as a proposal when it fits under the
configured size cap. Oversized or conflicting closures become diagnostics.
Lazy owner edges remain in the owner graph but do not close proposals unless
they are represented by a constraining atomic edge.

This makes larger peel sets unambiguous: a proposal is "minimal" only with
respect to the current closure heuristic. The authoritative graph fact is
the atomic DAG; agents should use `atoms`, `describe`, and `show-source`
to decide whether the recommendation is a good module shape.

### Detailed graph side output

The transform also writes a detailed graph side output, separate from
compact validation errors. This is the data source for analysis
scripts, notebooks, and repo-specific peel skills. It is emitted on
both success and validation rejection, because useful graph data often
exists precisely when the current assignment is not yet realizable.

The detailed output is machine-readable, typed, and debundler-owned.
For each chunk it includes:

- run metadata: chunk id and source paths;
- owner vertices: report id, statement ordinal, source location,
  declared bindings as `{binding, export_name}` records, owner kind,
  side-effect classification, and current destination;
- owner edges: `source`, `target`, edge kind, optional binding,
  statement ordinal, and whether the edge constrains realizability;
- quotient projections for the current assignment: module nodes,
  aggregated edge kinds, SCC membership, and SCC realizability
  status;
- `atomic_graph`: atomic unit nodes, atomic DAG edges, source spans, current
  destinations, constraining owner-edge provenance, and unit causes.

New fields can add source spans, input/spec hashes, direct importability
classifications, and closure suggestions without changing the underlying
graph transform. Do not add top-level `kind` or `schema_version` fields
just to mimic an external compatibility scheme.

Keep this side output high fidelity. It is allowed to be larger than
stderr and larger than a human-facing report. The compact console
error should summarize the failed SCC and point at the side-output
paths; downstream tooling should read the files.

### Valid peels and atomic modules

Let `dest: V -> D` assign every owner vertex to an output
destination. The quotient graph `Q = G / dest` merges all owners
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
an owner that assigns `a` must stay in the same destination as the
owner that declares `a` unless the emitter grows a separate
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
3. The constraining-edge subgraph of `Q` — the result of dropping all
   `LazyUse` cross edges from `Q` — has no multi-module SCC.
   Equivalently, any SCC of `Q` that contains a constraining edge
   must already be single-module under the constraining subgraph;
   pure lazy-read import cycles in `Q` are allowed and realizable
   because ESM evaluates the lazy side without observing a TDZ on the
   eager side.

This three-clause predicate is what the "Realizability primitive" section
above defines as the single shared implementation. The validator and
planner checks all consume the same primitive — none of them re-implement
the predicate.

A peel is just a proposed destination assignment for an owner set.
The peel is valid exactly when the resulting assignment is valid by
the definition above. Invalid peels have three primary explanations:
a non-importable read crosses the cut, a rebinding write crosses the
cut, or a constraining edge remains in a quotient SCC.

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
  import. If the target binding is private to the residual entry, a
  split containing the lazy consumer is invalid until the provider is
  colocated or made importable.
- A side-effect-order edge is an ordering constraint, not a value
  dependency. It may be satisfiable by the chosen module evaluation
  order, or it may participate in an unrealizable quotient SCC.
- A target-local effect edge, such as a recognized TypeScript
  `__decorate` helper call mutating `C.prototype`, is not a global
  side-effect-order relation. It is a local mutation relation between
  the anonymous application statement and the target owner. It must
  force colocation with that target, but it must not glue the target
  to every unrelated side-effecting owner that happens to appear
  before or after it in source order.

So "factorize the graph" is not the problem statement. The problem is:
construct owner sets whose induced destination assignment is valid under
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

`factorize` exists to propose useful module assignments for code that
currently lives in the residual/deferred surface. Its output has the
same correctness contract as a handwritten YAML edit.

Terminology is strict:

- A **proposal** is an owner set plus destination assignment that is
  already proven valid.
- A **frontier item** is an internal worklist state that has not yet
  been proven valid. Frontier items may be grown, rejected, or reported
  as blocker diagnostics, but they are not proposals.
- A **diagnostic** may describe why a frontier item failed. It must not
  be presented as a module assignment the author can land.

Every emitted proposal must satisfy:

1. It corresponds to a concrete destination assignment.
2. The assignment passes the same validity predicate above:
   importability, no cross-destination rebinding writes, and no
   unrealizable quotient SCC.
3. The proof is static and local to the owner graph. It does not rely
   on emitting JS, running a browser, or observing production-scale
   behavior.

The generator may be conservative and miss valid modules. It may not
emit an invalid module as a proposal.

### Correct factorization algorithm shape

A scalable factorizer should be a certifying closure algorithm over
precomputed owner-graph indexes:

Proposal generation is not heuristic. The frontiers are generated by a
deterministic input enumeration and monotone closure under exact static
obligations. Heuristics are allowed only after certification, for
ranking or display.

1. Enumerate deterministic frontier starts from the input surface:
   binding patch streams, residual owners, known extension targets, and
   other explicitly configured surfaces. These starts are not
   candidates and are never emitted directly.
2. Close each frontier item under hard local requirements:
   - include all owners in any atomic unit split by the frontier item;
   - include owners needed to eliminate cross-destination rebinding
     writes;
   - include owners needed by target-local effect edges, because a
     target-local mutation is only realizable when the mutating
     statement and target owner are in the same destination;
   - include provider owners for private residual bindings that the
     frontier item reads, including lazy reads, unless the export
     policy makes those bindings importable.
3. Certify the resulting hypothetical assignment via the realizability
   primitive (above). Same primitive the validator and planner checks
   use; no parallel walk.
4. If the verdict reports a blocker with an exact owner-level repair,
   push the repair onto the realizability index and re-read the verdict:
   - private residual read -> add the binding's provider owner;
   - atomic-unit split or rebinding split -> add the unit/assigner
     owners;
   - constraining quotient cycle -> add a small owner-level cut or
     companion set, then revalidate.
     On a failed repair branch, undo the push and try the next repair —
     the index's undo journal makes this cheap.
5. Emit a proposal only when the verdict is empty. Otherwise stop with a
   diagnostic when the frontier item exceeds the size cap, reaches an
   active-module conflict the generator is not allowed to rewrite, has
   no exact repair, or repeats a previous owner set.

The "not too small, not too big" sizing of factorized quotients is the
termination behaviour of this loop, not a separate heuristic. A frontier
is "too small" exactly when the verdict still names an exact repair; the
loop grows. A frontier is "too big" exactly when the size cap fires before
the verdict is empty; the loop halts with a diagnostic. The closure rules
and the size cap layer cleanly on top of one shared primitive instead of
running as ad-hoc parallel passes with their own graph views.

The implementation should be staged this way:

1. Build immutable indexes from the owner graph:
   - owner -> incident owner edges, grouped by edge kind and
     constraining/non-constraining status;
   - binding -> provider owner and export/importability metadata;
   - owner -> current destination;
   - atomic unit id -> member owners;
   - owner -> atomic unit id;
   - destination quotient adjacency with owner-edge provenance.
2. Build frontier starts. Starts are just worklist seeds; they are not
   displayed as proposals and do not need to be valid.
3. Run closure for each start with a queue of exact obligations. Each
   obligation either adds owners/atomic units, proves that an import is
   legal, or produces a blocker that the closure logic cannot repair.
4. Certify the closed owner set by constructing the hypothetical
   destination assignment and running the shared validity predicate.
5. When certification fails with an exact repair, enqueue that repair
   and continue. When it fails without an exact repair, record a
   diagnostic. When it succeeds, emit a proposal.
6. Rank and coalesce only emitted proposals. This can prefer fewer
   files, better names, larger useful reductions, or existing module
   namespaces, but it must not affect validity.

This is fast enough because the expensive facts are shared: owner
edges, binding-to-owner, owner-to-destination, atomic units, and
quotient adjacency are all indexed once per chunk. Each frontier item
is grown by a monotone worklist and is abandoned as soon as it exceeds
the review size cap. The search space is a bounded set of certified
closures, not all subsets of residual owners.

The important invariant is:

> `factorize` emits only certified proposals. A reported module
> assignment is not a candidate unless the full owner set has already
> passed exact validation.

This invariant is stronger than "the SCC algorithm found a cluster".
It also explains why `LazyUse` is subtle: lazy reads should not be
treated as init-order SCC edges, but they still affect importability
and therefore proposal validity. A factorizer that simply drops
`LazyUse` and then emits singleton lazy consumers as peelable is
incorrect. A factorizer that drops `LazyUse` from the init-order SCC
closure relation, records the resulting private-residual emit blocker,
grows the frontier item to include the provider, and validates the
closed set before emitting can be correct.

It also explains why local-effect annotations belong in the shared
analysis layer, not in factorize-specific code. Once the analyzer
turns a recognized decorator helper call into a target-local owner
edge, the materializer rejects a handwritten split of class and
decorator statement, the atomic DAG records the required unit, and
factorize can grow the frontier through the same atomic-unit repair
path. No consumer gets to reinterpret `effect:` independently.

### Planned factorization complexity

Let:

- `N` be the number of owners in the chunk.
- `M` be the number of owner-level dependency edges.
- `B` be the number of binding/provider/use facts.
- `U` be the number of atomic units, with `U <= N`.
- `S` be the number of frontier starts.
- `K` be the maximum owners/atomic units reached by a frontier before
  it is emitted or abandoned by the size cap.
- `E_K` be the number of owner edges incident to those `K` owners.
- `Q` and `E_Q` be the owner/destination quotient nodes and edges
  touched by a certification pass.
- `P` be the number of emitted proposals.

The one-time preprocessing target is:

- build edge, binding, destination, and provenance indexes:
  `O(N + M + B)`;
- compute atomic units with Tarjan over constraining owner edges:
  `O(N + M_constraining)`, bounded by `O(N + M)`;
- build initial quotient adjacency and reverse indexes:
  `O(N + M)`.

Each frontier should be monotone: an owner or atomic unit is added at
most once, and each incident edge is inspected only when it becomes
relevant. The target per-frontier cost is therefore:

- closure: `O(K + E_K)` plus binding lookups for touched reads;
- certification: `O(E_K + E_reachable)` when the quotient check can be
  limited to affected quotient components, with a full fallback of
  `O(Q + E_Q)`;
- exact repairs: no asymptotic multiplier beyond closure, because each
  repair adds new owners/units or terminates that frontier.

The total target cost is:

```text
O(N + M + B)
  + O(S * (K + E_K + E_reachable))
  + O(P log P)
```

with `O(S * (Q + E_Q))` as the conservative bound if every frontier falls
back to a full quotient certification pass. The implementation must
avoid the naive shape `O(S * K * (N + M))`, where every growth step
reruns whole-graph analysis. If production graphs make full quotient
certification too common, the fix is incremental component invalidation,
a denser quotient reachability representation, or narrower exact-repair
indexing, not weakening proposal soundness.

Implementation status: the realizability index now maintains rollbackable
quotient edge buckets and validates candidate deltas with scoped push/read/undo
over localized SCC reachability. A tested non-mutating overlay predicate exists
as a future optimization path, but local profiling found the current
ordered-map overlay slower than the rollback path. The current proposer-latency
fix is the quotient boolean gate split; further dense-index or reusable-buffer
work should wait for a fresh post-fix profile that makes this path material
again. `factorize` should continue to be audited against this contract before
large-factor output is treated as authoritative.

### Graph operations for peel tooling

Useful operations should be phrased against the owner graph first
and only then projected to modules:

- **Quotient:** `G / dest` produces the module graph the validator
  already understands.
- **Dependency closure:** starting from frontier owners, follow
  owner-level read edges that cannot be satisfied by imports. This
  yields the smallest "must move together" set for private residual
  dependencies.
- **SCC:** run on either the owner graph or the hypothetical quotient.
  Owner-level SCCs identify mutually-recursive or mutually-dependent
  clusters; quotient SCCs identify ESM realizability hazards.
- **Cut:** for an unrealizable quotient SCC, compute a small set of
  owner-level `at_init` or side-effect-order edges whose removal or
  colocation would make the split valid. The existing module-level
  feedback-arc cut should be reported with owner-edge provenance.
- **Ranking:** once validity is known, rank safe proposals by
  emitted-size reduction, number of owners, name quality, and whether
  the target path matches an existing human module namespace. Ranking
  is heuristic; validity is not.

These operations are pure graph transforms over immutable analysis
data. That property matters operationally: two tools reading the same
owner graph and destination assignments should produce the same proposal
status without building emitted JS.

### Pipeline trajectory

The pipeline (`pipeline.rs`) is a fixed composition over three layers:

- **Classify (input space).** Load + prepare (one parallel per-chunk
  parse computing shallow program facts), then one read-only vendor
  resolution pass (`build_vendor_resolution_plan`) that validates every
  `vendor` mark, resolves boundary mappings / swap targets / per-symbol
  tables, and runs the plan-time consumer gate before anything is
  written. Spec selectors always match prepare-time input-space ASTs;
  nothing mutates them.
- **Plan.** Lowering plans plus the `VendorResolutionPlan` as the
  single resolution oracle for import construction — both lowering's
  per-plan directive construction and the emission-time rewriting of
  non-lowered files consult it.
- **Emit.** `materialize_logical_modules` is the single artifact
  mutation wave; `apply_emission_rewrites` then performs the
  pass-through directive rewrite (specifier canonicalization,
  boundary-rename mapping, partial-swap consumer surgery) and computes
  each partially-swapped vendor chunk's residual (self-rewrite + strip)
  as one function body. Fully-swapped chunks are an emission-set
  exclusion, not an artifact removal; wrappers / facades / manifests
  are emission outputs.

This is the landed shape of the 2026-06 vendor-into-emission
collapse: the former stage braid — specifier rewriting, vendor
renaming, and swapping braided around materialization across seven
artifact mutations — is gone, and vendor operations contribute zero
mutation waves.

The remaining trajectory step is collapsing materialize-into-emit:
`materialize_logical_modules` still writes lowered module files back
into the chunk bundle for the emission stages to re-read, where the
lowered outputs could feed `write_js_tree` / harness emission directly
without the bundle round-trip. Tracked in ARCHITECTURE_BACKLOG.md; no
timetable is committed. The e2e tests in `devinfra/js/debundle/e2e/`
pin observable chunk → emitted-JS behavior and are the safety net for
that step, as they were for the vendor collapse.

## Selector vocabulary and matching

> **Aspirational.** This section pins a _target_ selector model.
> The current implementation uses simpler primitives (`name`,
> `kind`, `owner.id`, `owner.line`) — drift-prone but adequate
> while no second-version port has exercised the limits. The
> richer vocabulary below (fingerprint / astPattern /
> containsString / relational) and the bipartite forcing
> matcher are not yet implemented. Revisit when a real
> second-version port either validates the simple primitives
> or surfaces a concrete failure.

The spec is fully explicit: every owned binding has
a spec entry naming which logical module owns it. The chunk's
top-level bindings are scrambled (`Y5`, `b8`, `Q3` …) and the
entry's **selector** is how the spec author points at the right
one. Because the spec is now `O(num_bindings)`, selector
ergonomics dominate spec-author cost.

### Selectors are narrowing predicates, not unique identifiers

Each selector is a conjunction of constraints (`kind` ∧
`memberNames` ∧ `astPattern` ∧ …). Evaluated against the chunk,
it produces a **candidate set** of bindings — possibly 0, 1, or
many. Resolution is then a constraint-satisfaction problem
across all spec entries.

This decouples per-entry tightness from global resolvability:
spec authors don't have to write fingerprints precise enough to
match exactly one binding. They write predicates that are
"specific enough in context"; the matcher disambiguates by
elimination as other entries lock in their unique candidates.

### Resolution algorithm

Iterated bipartite forcing to a fixed point:

```rust
fn resolve(entries: &[SpecEntry], chunk: &Chunk) -> Result<Assignment, ResolveError> {
    let mut candidates: BTreeMap<EntryId, BTreeSet<BindingId>> = entries
        .iter()
        .map(|e| (e.id, e.candidate_set(chunk)))
        .collect();
    let mut assignments: BTreeMap<EntryId, BindingId> = BTreeMap::new();
    let mut available: BTreeSet<BindingId> = chunk.bindings();

    loop {
        let mut progressed = false;

        // Forcing on entry side: an entry whose live candidates
        // collapse to exactly one.
        for (id, cands) in candidates.clone() {
            let live: BTreeSet<_> = cands.intersection(&available).copied().collect();
            if live.len() == 1 {
                let b = *live.iter().next().unwrap();
                assignments.insert(id, b);
                available.remove(&b);
                candidates.remove(&id);
                progressed = true;
            }
        }

        // Forcing on binding side: a binding with exactly one
        // entry that wants it.
        for &b in available.clone().iter() {
            let claimers: Vec<EntryId> = candidates
                .iter()
                .filter_map(|(id, cs)| cs.contains(&b).then_some(*id))
                .collect();
            if claimers.len() == 1 {
                let id = claimers[0];
                assignments.insert(id, b);
                available.remove(&b);
                candidates.remove(&id);
                progressed = true;
            }
        }

        if !progressed {
            break;
        }
    }

    if !candidates.is_empty() {
        return Err(diagnose(&candidates, &available));
    }
    Ok(Assignment(assignments))
}
```

Soundness: if a unique solution exists and is reachable by
iterated cardinality-1 inference (the "naked singles" rule from
constraint propagation), the algorithm finds it. Solutions that
require search past a forcing fixed point are reported as
ambiguous — that's the spec author's signal to add a
disambiguating predicate to one of the competing entries.

The algorithm runs _strata-by-strata_ under relational
selectors (see [Relational selectors](#relational-selectors)
below).

### Primitive predicates

Selectors are JSON objects whose keys are primitive predicates;
the predicate set is the conjunction.

#### Static facts (drift-resilient)

| primitive                  | applies to | semantics                                                                                                                              |
| -------------------------- | ---------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `kind`                     | any        | exact match on the canonical serde spelling: `function_declaration` / `class_declaration` / `variable_declarator` / `import_specifier` |
| `paramCount: N`            | functions  | exact match on parameter list length                                                                                                   |
| `memberNames: [a, b, ...]` | classes    | every name in the list appears as a class member (instance or static; method, prop, accessor)                                          |
| `minMembers: N`            | classes    | class has ≥ N members                                                                                                                  |
| `superClass: <selector>`   | classes    | the class extends a binding that itself matches `<selector>` (recursive — see [Relational selectors](#relational-selectors))           |

These are stable across re-minifications: minifiers preserve
declaration kinds, param counts, class member name strings, and
super-class structural relationships.

#### AST pattern

| primitive              | semantics                                                                                                       |
| ---------------------- | --------------------------------------------------------------------------------------------------------------- |
| `astPattern: "<code>"` | the binding's source contains a subtree matching `<code>` interpreted as a JS pattern with identifier wildcards |

Pattern syntax:

- Identifiers spelled `_` (single underscore) are **independent
  wildcards** — any identifier matches each `_` independently.
  No cross-position consistency.
- Other identifiers are matched **literally**. Useful for
  patterns anchored on well-known names that survive
  minification — `useState`, `console.log`, `Symbol.iterator`,
  framework hooks.
- Operators, keywords, control flow shape, literal values
  (numeric, string, boolean) match exactly.

Examples:

```json
{ "astPattern": "for (var _ = 0; _ < _; _++) _;" }
```

A C-style for-loop with constant-zero start, simple comparison,
postfix-increment, single-statement body. Loose: matches any
identifier names, any comparison RHS, any body.

```json
{ "astPattern": "useEffect(() => { _; }, [_])" }
```

A `useEffect` call with an arrow callback containing a single
expression-statement and a single dep. Tight on `useEffect` (the
literal name must match), loose on contents.

```json
{ "astPattern": "throw new Error(\"unreachable: \" + _)" }
```

A throw of an `Error` whose message starts with the literal
prefix `"unreachable: "`. Pinpoint when this string is unique.

Subtree match semantics: at each AST node in the binding's body,
attempt a recursive structural match against the pattern's AST.
A match exists if any subtree matches anywhere in the binding's
syntactic span.

A future extension (not in this iteration): named captures
(`$name`) for cross-position consistency. Simple wildcards are
adequate for selector use today; captures become useful when
patterns describe relationships ("the same identifier appears
both as the loop variable and in the body").

#### Source text

| primitive               | semantics                                                                                                 |
| ----------------------- | --------------------------------------------------------------------------------------------------------- |
| `containsString: "..."` | the binding's source contains the literal string anywhere — typically inside a string literal in the body |

Useful for unique strings: error messages, RPC names, prop
keys, debug labels, regex sources, embedded GraphQL or JSON.
Stable to the extent the literal survives minification (most
do; minifiers don't rewrite string contents).

#### Relational selectors

These reference _other_ spec entries:

| primitive                  | semantics                                                                                            |
| -------------------------- | ---------------------------------------------------------------------------------------------------- |
| `calls: <selector>`        | the binding's body contains a call to a binding that itself matches `<selector>`                     |
| `calledBy: <selector>`     | the binding is called from inside the body of a binding that matches `<selector>`                    |
| `references: <selector>`   | the binding reads (call, member access, etc.) a binding matching `<selector>` — broader than `calls` |
| `referencedBy: <selector>` | converse — somebody matching `<selector>` reads this one                                             |
| `superClass: <selector>`   | (also listed above) class-only — extends a binding matching `<selector>`                             |
| `renamedName: "..."`       | shortcut: the binding's spec entry renames it to `"..."`                                             |

Relational selectors create **selector-resolution dependencies**:
to evaluate `calls: { renamedName: "JsxRuntime" }`, the matcher
must first know which binding gets the renamedName `"JsxRuntime"`,
which means resolving the entry that defines that rename.

The matcher computes a topological order over entries based on
their selector-reference deps. Strata-by-strata: leaf entries
(no relational deps) resolve first; then entries whose
relational deps now point at known bindings; etc. Within a
stratum, the bipartite forcing algorithm runs.

A cycle in selector-resolution deps is an error
(`SelectorRefCycle`). Note this is **separate** from the
realizability graph `I ∪ S`; selector-ref cycles are entirely
within the spec layer and are usually a spec author's mistake
(`A.calledBy: B`, `B.calledBy: A`). Resolution: anchor one of
them with a non-relational predicate (kind + astPattern, etc.)
so the cycle breaks at one node.

#### Direct (drift-prone, escape hatches)

| primitive    | issue                                                                                                                          |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------ |
| `name: "Y5"` | the scrambled local name. Minifier renames between builds → spec re-pin needed. Use only when no stable predicate is available |

#### Rejected primitives

These have been considered and rejected; they don't make it
into the runtime:

| primitive                                     | issue                                                                                                                                                                                            |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `owner.line`                                  | Source is typically prettified; line numbers shift with formatter changes. No semantic stability                                                                                                 |
| `owner.id` (`owner_NNNNN`)                    | opaque sequential index minted by program analysis; carries no identity (see [Identifiers are typed, not stringly-typed](#identifiers-are-typed-not-stringly-typed))                             |
| `bodyHash` (function sha)                     | whitespace, minor edits, and minifier comment-stripping invalidate constantly. Use `astPattern` or `containsString` instead                                                                      |
| `fingerprint.memberNamesPrefix` (gaffer-only) | this gaffer-side resolution helper is what the runtime now exposes as a first-class `memberNames` predicate; "prefix" was a quirk of how gaffer extracted member lists, not a stability property |

### Composition rules

- Selectors are JSON objects with one or more primitive keys; the
  selector is the conjunction.
- `kind` is the most common refinement; combines with anything.
- `paramCount` / `memberNames` / `minMembers` / `superClass` are
  kind-specific; using them on a binding of the wrong kind makes
  the candidate set empty (which is fine — that's how
  disambiguation works).
- Multiple primitives of the same shape (`memberNames: [a, b]`
  _and_ `memberNames: [c]`) are not currently meaningful;
  collapse to `memberNames: [a, b, c]`.
- An empty selector `{}` matches every binding in the chunk —
  which is rarely useful, but the matcher's forcing algorithm
  still handles it (it gets resolved last by elimination).

### Errors

All errors are validate-time. Each carries enough evidence for a
spec author to fix the spec without re-running the tool.

```text
SelectorResolution::Unsatisfiable
  entry "ui/widget/MyClass"
    selector: { kind: class_declaration, memberNames: ["render", "create"] }
  reason: after forcing, candidate set is empty.
    Y5 (the only binding with this shape) was claimed by entry
    "ui/dom/Component" via its more specific `astPattern`.
  resolve by: relaxing this entry's selector, or moving the
    `Component` entry to a different binding so Y5 is freed.
```

```text
SelectorResolution::Ambiguous
  entries: [
    "ui/widget/MyClass"  selector { kind: class_declaration, memberNames: ["render", "mount"] },
    "ui/dom/Component"   selector { kind: class_declaration, memberNames: ["render", "mount"] },
  ]
  candidates: [Y5 (line 12345), X4 (line 23456), Q3 (line 34567)]
  reason: no cardinality-1 forcing remains; both entries match
    all three candidates equally.
  resolve by: adding a disambiguating predicate (an `astPattern`,
    a `containsString` for a unique literal, a `superClass`
    referring to one's parent class) to at least one entry.
```

```text
SelectorRefCycle
  cycle: ["entry_a", "entry_b", "entry_a"]
  reason: entry_a's selector references entry_b via `calls`,
    and entry_b's selector references entry_a via `calledBy`.
  resolve by: anchoring one with a non-relational predicate
    (kind + astPattern, etc.) so its candidate set can be
    resolved before the other.
```

### Resolution example

Three classes share a `memberNames` shape; one extends a known
base, another contains a unique string literal:

```json
[
  {
    "id": "spec_component",
    "selector": {
      "kind": "class_declaration",
      "containsString": "instanceof Element"
    },
    "rename": "Component"
  },
  {
    "id": "spec_widget",
    "selector": {
      "kind": "class_declaration",
      "memberNames": ["render", "mount"],
      "superClass": { "renamedName": "Component" }
    }
  },
  {
    "id": "spec_other",
    "selector": {
      "kind": "class_declaration",
      "memberNames": ["render", "mount"]
    }
  }
]
```

Trace:

1. Selector-ref dep order: `spec_component` (no relational
   deps), then `spec_widget` (depends on `spec_component`'s
   rename), then `spec_other` (no relational deps but resolved
   last by elimination).
2. **Stratum 1.** `spec_component`'s candidate set: classes
   whose source contains `instanceof Element`. Suppose only `Y5`.
   Force `spec_component → Y5`. The rename map now has `Y5 →
"Component"`.
3. **Stratum 2.** `spec_widget`'s `superClass` resolves to `Y5`.
   Candidate set: classes extending `Y5` with `["render",
"mount"]` members. Suppose only `b8`. Force `spec_widget →
b8`.
4. **Stratum 3.** `spec_other`'s candidate set: classes with
   `["render", "mount"]` members. After excluding `Y5` and `b8`,
   only `Q3` remains. Force `spec_other → Q3`.

All entries assigned, no ambiguity.

### Design trade-offs surfaced by the algorithm

- **No silent best-effort.** The matcher refuses to produce a
  partial assignment with "best guesses." Every entry resolves
  to exactly one binding, or the spec is rejected with a
  diagnosable error. Same philosophy as the validator's cycle
  rejection.
- **Failure surface is per-entry.** Errors point at specific
  entries, candidate bindings, and remediations. The spec author
  edits one entry at a time and re-runs.
- **Selector strength is local.** A loose selector elsewhere
  can still tighten one entry, because it shrinks the
  available-bindings set. So spec authors can write the simplest
  predicate that works "in context" rather than the most
  precise.
- **Reference depth is bounded by selector-ref dep order.**
  Even relational selectors resolve in a single pass per
  stratum; the matcher doesn't iterate the whole bipartite
  forcing across all entries indefinitely. Worst-case complexity
  is O(entries × bindings) per stratum × strata-depth, which is
  fine at typical bundle scale.

## Anonymous-statement selectors

Binding selectors (above) address an `Owned` binding by name.
Anonymous (empty-`declared`) statements have no name; they are
side-effect IIFEs, decorator applications like
`Ww([Z], $g.prototype, "invites", 2);`, runtime init bridges, and
similar bare expressions/statements. Empirically, a closure that
peels a hub-class binding (e.g. `WorkspaceInviteState`) frequently
must co-move several such statements — they apply decorators to
the class prototype, register Meticulous record/replay hooks,
push system-config bridges through helper functions — and an
algorithmic refinement (treating "fire-and-forget" preludes as
non-constraining) does not cover them: most are not preludes,
they semantically belong with the class.

The spec addresses these statements with an
`anonymous_statements: [...]` list on the same `LogicalModule` as
the named members, where each entry carries the JS source of the
target statement verbatim:

```yaml
workspace/invite/state:
  members:
    - selector: { binding: { name: $g } }
      name: WorkspaceInviteState
  anonymous_statements:
    - match: |
        Ww([Z], $g.prototype, "invites", 2);
      note: "@observable invites"
    - match: |
        Ww([Z], $g.prototype, "isChecking", 2);
```

When a decorator application matches an annotated
`effect: typescript_decorate_helper`, the owner graph records it as a
target-local effect on the class/prototype binding. That makes the
anonymous statement a required companion of the class owner for
materialization and factorize. The author still
materializes it with `anonymous_statements:` because it has no binding
name, but the atomic-unit closure that contains the class also covers
the anon owner, so the factorizer's proposal lists the anonymous
owner id under `extension_owner_ids` and will not propose the class
alone.

The unannotated case — `__decorate(...)`, `register(...)`, and
target-mutating `Foo.x = ...` installs that the analyzer cannot tag
as target-local because no helper annotation matches — relies on a
separate route. The factorizer's emit pass walks each fresh-module
cell and promotes it to an extension of an existing active module
when **every** outgoing cross-module constraining edge points at one
active module, the cell declares no named bindings, and it has no
outgoing edges to other residual cells. The cell's owners are surfaced
in `extension_owner_ids`; the downstream consumer reads owner shape
(named bindings vs anonymous statements) to decide whether to write a
`members:` or `anonymous_statements:` entry into the extended module's
yaml. The cell-level dependency-satisfaction check (no leftover
residual deps + unambiguous active target) is what makes the promotion
safe: the extension can be applied without leaving a downstream
residual dependency.

### Different from binding selectors

Binding selectors (§"Selector vocabulary and matching") are
**narrowing predicates** that may admit 0, 1, or many candidate
bindings; the resolver disambiguates by elimination across all
spec entries. Anonymous-statement selectors are **unique-by-design
shape matchers**: each `match` source is parsed as a single SWC
`Stmt` and compared structurally (`EqIgnoreSpan`) against the
chunk's top-level statements. The contract is "exactly one
match." Zero matches is a spec error (the upstream statement was
likely renamed or removed; the diagnostic points at top-level
statements with similar shape). Multiple matches is a spec error
(refine the selector or — if the chunk really contains two
identical statements — accept that they're indistinguishable
without context). There is no narrowing across selectors and no
bipartite forcing: the resolver runs per-entry, independent of
other anon entries.

This asymmetry is intentional. Bindings have a stable identity
(the declaration site) that survives mid-statement edits, so
narrowing makes sense — a candidate set of 3 collapses to 1 when
the other two get claimed. Anonymous statements have no comparable
identity; their only handle is the AST shape itself. A loose
"narrowing" matcher would silently accept an unintended statement
when an upstream change drops the originally-targeted one — the
spec would still type-check, the materializer would still emit a
module, but the wrong code would move. The strict-equality
contract pushes those failures to spec-validation time with a
loud diagnostic, mirroring the validator's "cycle = reject"
philosophy.

### Constraints on the selector source

- The `match` source must parse as JS and contain **exactly one
  top-level statement**. The resolver feeds it through the same
  parser the chunk used (`js_ast::parse_js_module_ast`); the
  parsed module's body must have length 1.
- Comparison is `EqIgnoreSpan` over `ModuleItem`, evaluated inside
  SWC's `SyntaxContext::within_ignored_ctxt` scope because selector
  source and runtime chunks are parsed in separate resolver passes.
  Whitespace, comments, spans, and syntax-context marks are ignored;
  identifier names and string literals are not. Identifier names match
  the chunk's pre-readability-rename form (the same form binding
  selectors use as `selector.binding.name`).
- Each anonymous statement may belong to at most one logical
  module. A duplicate claim across two modules is a spec error;
  the validator names both modules and the offending statement
  ordinal.

### Why exact source rather than line/column

The target application's bundle is delivered minified and prettified for analysis;
neither line nor column is stable across re-prettifies or
upstream releases. The shape selector survives both: the
prettifier reformats whitespace but preserves AST structure, and
upstream changes that touch the statement's content surface as a
zero-match diagnostic the spec author can fix.

### ChunkFactorization integration

`ChunkFactorization` validates realizability (the cycle gate over `I ∪ S`)
by quotienting the owner graph by each owner's destination.
Anonymous owners default to `ModuleId::ResidualEntry`; the
factorization overrides that destination for any anon owner the spec
claimed. Without this override, an anon owner with a constraining
in-edge from a peeled named owner would create a fake cross-module
edge — the validator would reject the spec even though the
materializer would emit the closure correctly. After the override,
the validator sees the same module dep graph the materializer
will emit, and the cycle gate fires only on real unrealizable
splits. See `ChunkFactorization::build` in `chunk_factorization.rs`.

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
residual module as a (possibly single-declarator) comma-list.
Source-order side effects across declarators are preserved by the
ESM linker, which evaluates the destination modules in source
order. See <e2e/comma_list_owner_split_test.rs> for the full
shape matrix.

**Destructuring declarators are atomic.** `const { x, y } = obj`
is one declarator binding two names; the lowerer's
`split_var_decl` moves it as one unit. Claiming any one of its
bindings (e.g. `x`) pulls every sibling (`y`) into the same
module — sibling bindings join their claimed module with their
local name as the export name. Claiming siblings into different
modules is rejected with an explicit error from
`build_module_plans`; the only legal options are claim-all or
claim-none.

## Cycle resolution

When the validator rejects a cycle, the spec author must remove the
constraining cross edge from the quotient SCC. In spec-only work,
that usually means:

**Colocate the constraining owner endpoints.** Move the owner that
performs the at-init read or side-effecting work together with the
owner it depends on. Once both endpoints share a destination, that
edge disappears from `I ∪ S`.

Rewriting source so the constraining read becomes lazy can also make
the SCC realizable, but the debundler normally does not rewrite
program structure. Lazy read edges still emit import directives and
still participate in `I`; they are accepted only when every
cross-module edge in the SCC is lazy.

The validator should suggest the colocation explicitly: "Cycle
through `M_a`, `M_b`, `M_c`. Constraining edge:
`owner_x --at_init--> owner_y`. Resolution: colocate `owner_x`
and `owner_y`, then re-run quotient validation."

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
pipeline below. The owner-graph reports are the bridge: they expose
source owner ids, readable member names, destinations, blockers, and
peel-set hyperedges so authoring tools can mostly project and filter
debundler facts instead of re-analyzing JavaScript or private repo
YAML conventions.

| Step                           | Module                        | Runs when                                                                                                                                                                                                                               |
| ------------------------------ | ----------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `load_transform_spec`          | <pipeline.rs>                 | Always; loads either the flat YAML spec or the tree-shaped authoring spec.                                                                                                                                                              |
| `validate_transform_spec`      | <spec.rs>                     | Always after spec load.                                                                                                                                                                                                                 |
| `load_js_chunks`               | <artifact.rs>                 | Always; configured by `inputs`.                                                                                                                                                                                                         |
| `prepare_js_chunks`            | <prepare_chunks.rs>           | Always. In one parallel per-chunk pass, parses every chunk with SWC, computes shallow program facts, and canonicalizes entries.                                                                                                         |
| `build_artifact_indexes`       | <artifact.rs>                 | Always after preparation. Builds chunk id, source path, output path, and import-reference indexes for later stages.                                                                                                                     |
| `build_vendor_resolution_plan` | <vendor/plan.rs>              | Always after index build. Read-only: validates every `vendor` mark, resolves boundary mappings / swap targets / partial-swap symbol tables, runs the plan-time consumer gate.                                                           |
| `materialize_logical_modules`  | <lowering/> + analysis files  | When `logical_modules`, `unassigned_mode`, or `chunk_renames` is non-empty. Computes facts, quotients the owner graph into `I ∪ S`, validates, emits.                                                                                   |
| `apply_emission_rewrites`      | <vendor/emission.rs>          | Always. Pass-through directive rewrite (specifier canonicalization, boundary-rename mapping, partial-swap consumer surgery) over files emitted without lowering, plus the per-vendor-chunk residual composition (self-rewrite + strip). |
| `validate_emitted_exports`     | <validate_emitted_exports.rs> | Always; duplicate-public-export tripwire over the emission set (excluded full-swap chunks are skipped).                                                                                                                                 |
| `write_js_tree`                | <write_tree.rs>               | When `write_js_tree` output config is present; writes JS tree reports with exact `output_metrics` and directory reports when logical modules exist.                                                                                     |
| `emit_browser_harness`         | <emit_harness.rs>             | When `emit_browser_harness` output config is present; writes browser harness reports with exact `output_metrics` and directory reports when logical modules exist.                                                                      |

Within `materialize_logical_modules`, the substages are:

1. **Spec parsing** → `LogicalRequest` / `ModulePlan` per chunk.
2. **Chunk AST analysis** (<lowering/chunk_ast.rs>:
   `analyze_chunk_ast`) → top-level declarations, declaration index,
   and runtime import facts in one top-level scan.
3. **Statement-facts analysis** (<facts/mod.rs>:
   `analyze_chunk`) → `Vec<StatementFacts>`.
4. **Owner graph construction** (<graph.rs>) → owner vertices plus read and
   side-effect-order evidence. This is a first-class intermediate
   and report side output.
5. **Binding assignment** → `BTreeMap<BindingName, ModuleId>` from
   the spec's explicit member list. Bindings with no spec entry
   default to `ResidualEntry`; nothing pulls implicitly. (See
   [Spec explicitness](#spec-explicitness-and-diagnostics).)
6. **Quotient + validation** (<graph.rs>, <validation.rs>).
   The quotient graph
   collapses owners by destination, aggregates edge reasons, and
   validates the resulting `I ∪ S`.
7. **Diagnostics projections** — cycle evidence, atomic-unit conflicts,
   and atomic graph reports are projections of the same owner graph +
   quotient, not separate heuristic analyses.
8. **Cycle resolution gate** — if the validator finds an
   unrealizable cycle, the pipeline aborts with the cycle
   evidence.
9. **Source-order emission** — each module's body in source order;
   cross-module imports + source-chunk re-imports; `export { ... }`.
   No init wrappers. Import-directive ordering comes from the shared
   `esm_import_order::EsmImportOrder` (the same object the
   realizability gate's evaluation simulator consults): the entry
   imports every logical module — side-effect-only for binding-less
   plans — in entry-import order, and each module's intra-chunk
   imports (binding, phantom side-effect, residual-entry) form one
   merged list in module-import order.

Treat those stages as a functional data flow. Analysis produces
immutable facts; assignment is explicit input; quotienting derives a
validated schedule; emission consumes that schedule. Avoid designs
where emission discovers new graph edges or silently mutates the
assignment, because that reintroduces hidden closure behavior and
makes diagnostics disagree with emitted output.

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

### Full swap: callers are proxy/import-map-dependent

`level: swap` excludes the vendor chunk from the **emission set** —
nothing removes it from the artifact, and the owner graph never
contained vendor chunks; `write_js_tree`, harness emission, the
emit-shape check, and the chunk reports skip it, and a wrapper module
is written in its place. Caller imports of the excluded chunk are
**intentionally left dangling** in the plain `write_js_tree` output:
the live-proxy / import-map layer (<../live_proxy/>) resolves the
dangling chunk specifier to the swapped package at serve time. The
plain emitted tree is therefore not directly runnable under bare Node
when a full swap is configured. Pinned by
`full_swap_with_caller_keeps_dangling_chunk_import_for_live_proxy`
in <../e2e/vendor_swap_test.rs>.

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
which enumerates input-space directives — cannot see. Retiring it
requires construction-site coverage plus fixtures first (see
ARCHITECTURE_BACKLOG.md "Post-strip consumer scan retirement
condition").

Over-restriction is the accepted failure mode for both gates: a
namespace consumer that happens to read only non-swapped members is
still rejected, because per-member usage is not analyzed.

### Deliberate split-brain bypasses in the strip pass

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

## Empty logical modules

A spec entry that resolves to zero owned bindings and no re-exports
comes out as an effectively-empty file. Either:

- The spec author wrote a `logical_modules[chunk_id][target_path]`
  entry whose explicit members all turned out to be names that
  don't exist in the chunk (typo, stale spec). The validator surfaces a
  `MissingMember` warning per name; emit proceeds with whatever
  _did_ resolve.
- All listed members are `Imported` bindings with no `Owned`
  members. The module is a re-export-only file (just `import` +
  `export {}`). Valid, common for "barrel" modules; no warning.

The validator distinguishes the two and only warns on the first.
A logical module with literally zero members in either category
is rejected (the spec author probably meant to define something
that didn't materialize).

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
   so the imports graph `I` it constructs matches the linker's
   view exactly. There is no opt-out path that bypasses the dep
   graph. If a cycle exists in `I ∪ S`, the validator surfaces
   it.

## What this design rejects

Examples of unrealizable splits — shapes that the realizability
gate refuses:

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

If `mod_a` imports anything from `mod_b` at module-top (say
because the spec assigned a binding `mod_a` reads to `mod_b`),
the cycle `mod_a ↔ mod_b` contains a class extends-clause read.
Class declarations are TDZ-prone, so this fails at module load
with `ReferenceError: Cannot access A before initialization`.

Resolution: colocate `A` and `B`, or otherwise remove every
constraining cross edge from the SCC. Pushing only the reverse
`mod_a` import inside a function body is not enough: the lazy read
still emits an import directive, `I` still has both edges, and the
SCC still contains the at-init `extends A` edge.

### Cycle through lazy back-edges (mixed at-init / lazy)

```
// in mod_a — at module-top, EAGER:
import { ssym as m } from "<mod_b>"; // I-edge mod_a → mod_b
const askAICommandId = m.askAICommandId;  // R-edge mod_a → mod_b

// in mod_b — only inside function bodies:
import { definitions } from "<mod_a>";    // I-edge mod_b → mod_a
function helper() { return definitions.foo; } // not in R
```

Edge set:

- `R = { (mod_a, mod_b) }` — only `mod_a`'s read is at-init.
- `I = { (mod_a, mod_b), (mod_b, mod_a) }` — both reads emit
  imports.

A validator that builds `R` (the previous design) sees no cycle
and accepts. The ESM linker, however, walks `I`, sees the SCC,
picks an order; whichever module evaluates second sees its
imports as TDZ at init time. `mod_a`'s `m.askAICommandId` read
fails with `Cannot access 'm' before initialization`.

Resolution: colocate one back-edge's binding with its reader so
the `I` cycle dissolves.

### Computed property key reading another module

```
// in mod_a
const m = { dataTypeNumberId: "SYS_D08" };

// in mod_b — at module-top:
const dataTypeIconMap = { [m.dataTypeNumberId]: numberIcon };
```

`mod_b → mod_a` edge through the computed key. If `mod_a` doesn't
edge back to `mod_b`, fine. If it does (because the spec assigned
something `mod_a` reads to `mod_b`), cycle, rejected.

Computed keys aren't special: they read identifiers at-init like
any other expression, and the dep graph captures that read
without any special-case logic.

## ChunkAnalysis + ChunkFactorization: owner graph plus quotient

The target runtime data structures the validator and emitter both
consume are a pair: a per-chunk `ChunkAnalysis` (inputs + IR) and a
`ChunkFactorization` wrapping it (partition + derived realizability
views). Together they carry the chunk's statement facts, the binding
catalogue, the explicit logical modules, the owner graph, and the
module dep graph derived by quotienting that owner graph under the
spec assignment.

The implementation builds these directly: statement facts feed the
owner graph (stored on `ChunkAnalysis`), and the module dep graph is
a quotient of that owner graph under the current spec assignment
(stored on `ChunkFactorization`).

Both are keyed per-chunk; the chunk is contextual within the
analysis, so binding keys collapse to just `name`. Keys for the dep
graph extend `ModuleId` with an `ExternalChunk(ChunkId)` variant so
cross-chunk reads are first-class.

Conceptually, the analysis + factorization carry:

- chunk identity;
- per-statement facts in source order;
- the binding catalogue;
- logical modules from the spec;
- the owner graph;
- the module dep graph `I ∪ S`, derived by quotienting the owner
  graph by the spec's owner-to-destination assignment.

Everything downstream needs is here:

- `home(stmt)`: for statements with `declared(stmt) ≠ ∅`, look
  up any declared name in `bindings` — its `Owned.owner` is
  `home`. For statements with empty `declared`, `home` is
  `ResidualEntry`.
- "What owners/statements live in module M" =
  `owner_graph.nodes.filter(dest(owner) == M)`.
- "What does M export" = bindings whose `Owned.owner == M`
  (under their original or rename-pass-rewritten name) plus
  bindings whose `Imported.re_exporter == M` (under
  `Imported.public_name`).
- "What imports does M need" =
  - For each owner-level read edge from an owner with `dest == M`:
    - If `b` is `Owned { owner: other }`: `import b from <other>`.
    - If `b` is `Imported { imported_from, imported_name, .. }`:
      `import { imported_name as b } from <imported_from>`.
- "What can be peeled without backtracking" = candidate assignments
  whose owner-graph quotient validates, whose cross-destination reads
  are importable, and whose rebinding writes stay inside one
  destination.
- "Identity of cross-chunk deps" = `bindings.values()` filtered
  to `Imported { imported_from, .. }` give us the set of
  external chunks our schedule talks to; that's the
  `ExternalChunk(_)` nodes in `dep_graph`.

The reason to make `BindingKind` explicit (rather than
collapsing imports into the same `Owned` map):

**Re-export semantics aren't ownership.** A logical module that
re-exports `import { Y as X } from "<vendor>"` does not own
`X` — modifying our spec to "claim" `X` should not rename `Y`
in the vendor chunk; it should emit a re-export in our logical
module. A flat `BindingName → ModuleId` map conflates these
two cases; tagging via `Owned` vs `Imported` keeps them
distinct. The validator additionally rejects duplicate claims
of either kind, so each imported binding has exactly one
re-exporter (a consumer that wants a different local name
aliases at its own import site instead of authoring a separate
re-export module).

### Identifiers are typed, not stringly-typed

Strings that identify a thing of a known kind get a newtype.
Untyped `String` is reserved for free-form text (error messages,
log lines, the actual JavaScript identifier _as text_). The
risks `String` introduces:

1. **Same-shape strings, different things.** A chunk id, a
   logical-module path, and a destination file path within a
   chunk are all stringly-shaped. A function that takes one of
   these has a plausible-looking signature even when called
   with the wrong one. Newtypes turn that into a compile error.
2. **Opaque numeric ids drift.** Synthesizing a stringified
   sequential index per top-level decl (`owner_03565`) is
   drift-sensitive: any source insert shifts every later
   ordinal. The validator addresses this by identifying
   bindings by `binding.name` (unique within a chunk's
   top-level scope) plus optional `owner.line` for drift
   detection.
3. **Stringly-typed module ids leak into emit.** A module id
   that doubles as a JS identifier suffix or a JSON report key
   ties three unrelated concerns together; separating them
   keeps the emit clean.

The principled set of identifier types:

```rust
/// Path-style identifier for a chunk. Never a path inside a
/// chunk; never a logical-module path.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct ChunkId(String);

/// Path within a single chunk's emitted file tree, relative to
/// the chunk's root. E.g. `runtime/vendor/symbols.js`.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct ChunkRelativePath(String);

/// Path of a logical module as named by the spec. E.g.
/// `feature/some_module`. Does *not* include `.js`.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct LogicalModulePath(String);

/// Stable id for a logical module within a chunk. Internal,
/// distinct from `LogicalModulePath` (different abstraction
/// — paths can change in a spec edit; ids stay stable through
/// a single materialize run). Implemented as a `usize` index
/// into `ChunkAnalysis.logical_modules`; wrapped to keep it from
/// being mistaken for `StatementOrdinal` etc.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct LogicalModuleIndex(usize);

/// Position of a top-level statement in a chunk's source body.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct StatementOrdinal(usize);

/// Local name of a binding in a chunk's top-level scope. Used
/// as a key in `ChunkAnalysis.bindings`. The string itself is the
/// JavaScript identifier (scrambled in source, possibly
/// renamed in emit).
pub type BindingName = String;
```

`BindingName` stays a plain `String` (with a type alias for
documentation) because it _is_ the actual identifier text — a
JavaScript identifier passed to `Ident::new_no_ctxt()` and
emitted into source. Wrapping it would force `.0` everywhere a
real-text-vs-id distinction doesn't exist. The other four are
genuinely different things and earn a wrapper.

`ModuleId` (in <ids.rs>) is a tagged
union over these:

```rust
pub enum ModuleId {
    Logical(LogicalModuleIndex),
    ResidualEntry,
    ExternalChunk(ChunkId),
}
```

Artifact metadata follows the same rule. `FileRole` is a typed enum
(`entry`, `module`, `runtime` in JSON via snake_case serde), and
module extraction state is a typed `ModuleExtractionState` record. Do
not encode known roles or extraction state as raw strings, generic maps,
or compatibility-only JSON fields.

#### Killing the `owner_NNNNN` opaque id

The `OwnerRecord.id` system in <program_analysis.rs> mints a
stringified sequential index per top-level decl, exposes it in
the chunk analysis output, and the gaffer spec references it.
Replacing it:

- Spec entries identify bindings by `binding.name` and can use
  `owner.line` for drift detection. Both are meaningful and unambiguous
  within a chunk's top-level scope.
- The chunk analysis output continues to enumerate top-level
  decls with their `ordinal`, `name`, `kind`, `line`, etc.,
  but no longer mints a synthetic `id` field. Tools that
  cross-reference do so by name + ordinal + line.
- `OwnerRecord` shrinks to a debug record without the `id`.
- Spec entries that set a synthetic `owner.id = "owner_03565"`
  in addition to `binding.name` are noise: the validator
  resolves binding identity via `name` + `kind` (+ optional
  drift-detection `line`), so the synthetic id can be dropped.

## Known sharp edges

Concrete gaps, oversimplifications, or unsound corners not yet
folded into the design body.

### Soundness gaps

#### Backward-direction proof is loose on "WLOG M₁ is first"

The proof says "some module has to be first" in the cycle. ESM
doesn't actually let us pick the starting module — it's
determined by the entry's import graph and reverse-DFS order.
The argument that _needs_ to be there: regardless of which cycle
member ESM picks as the first to evaluate, _some_ cycle edge
will fire in the wrong order (because a cycle has no topological
linearizer that respects all edges). The current text's gist is
correct; tightening it is a doc edit, not a design change.

#### Side effects we can't see locally

`reads_at_init(S)` only catches local-name reads. These ordering
constraints are real but invisible to the analyzer:

- `globalThis.X = ...` / `window.X = ...` writes that another
  module reads via `globalThis.X`. Module A "sets" the value, B
  "reads" it; the dep graph has no edge.
- `eval(some_string)` running arbitrary code at init.
- `new Function(...)` likewise.
- `with` statements scoping things our visitor doesn't track.
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

### Coverage audits the analyzer needs

#### Lazy positions: complete list pending tests

Current visitor handles: function bodies, method bodies (via
`visit_function`), arrow bodies, getter/setter prop bodies,
class instance fields, class method bodies, computed prop names
on class members.

Not yet pinned by tests (and at least some not implemented):

- **Default parameter values** (`function f(x = compute()) {}`)
  — per spec, these evaluate on call, so lazy.
- **Decorator expressions** (`@decorator class C {}`) — eager
  (run at class-decl time).
- **Object literal getter/setter bodies** (vs `MethodProp`).
- **Tagged template strings** (`` tag`...` ``) — eager (tag
  function is read).
- **Spread elements** (`[...x]`, `{...x}`) — eager (read x).
- **Dynamic `import(expr)` argument** — eager (expr evaluates).
- **Optional chaining / nullish coalescing** — eager.
- **Top-level await** (`await expr`) at module top — eager;
  also blocks the importer's evaluation, which our model
  doesn't track.
- **JSX** — usually transformed to function calls, so eager;
  if transform isn't applied, the JSX visitor needs explicit
  handling.

Action: add a unit test per case to <facts/mod.rs> / <purity/mod.rs>;
fill the visitor's gaps. Aim for an exhaustive table.

#### Side-effect classification is conservative

Right now `has_side_effect = true` for any non-pure expression
(function calls, member access on possibly-mutating objects,
etc.). That over-imposes side-effect-order edges in `G'`,
potentially creating cycles that aren't really there.

A spec where both modules' side-effecting statements are
mutually independent (don't observe each other's effects) can
still produce a spurious `S` cycle under the conservative
`has_side_effect = true` for any function call. **Known impl
gap**: pure-call inference would let the validator drop edges
between observably-independent side effects. Pragmatic
short-term: only add `S` edges when both statements' side
effects are _observable_ (write to global, throw, console,
DOM, network), not pure-by-pattern (literal-only initializers).

### Out-of-scope JS features

The model assumes a synchronous, single-pass ESM evaluation
without runtime-dynamic effects. These features are out of scope:

- **Top-level await.** Our model treats module evaluation as
  synchronous; TLA changes that. Bundled output today doesn't
  use TLA but vendored libs increasingly do. If a TLA pattern
  appears, the cycle analysis is unsound — the awaited promise
  yields, the importer continues evaluating with the awaited
  value still pending. We'd need an "async-eval" extension to
  the model.
- **`eval` / `new Function`.** Arbitrary code at init time.
  We can't statically see what they read or write. Conservative
  approach: classify as side-effecting and refuse to split
  modules that contain them across cycle boundaries.
- **Generators with side-effecting `next()` calls** — same as
  function calls, we treat the call site as side-effecting and
  rely on the conservative classification.
- **`with` statements.** Lexical scoping changes; our visitor
  treats names as referring to a global scope. JS strict mode
  forbids `with` so bundled output doesn't use it.

Each of these should produce a clear analyzer warning if
detected, not silent acceptance.

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
   lazy positions. The current implementation handles function
   bodies, method bodies, instance class fields, getters, setters.
   Open: decorator factory bodies (decorators run at class-decl
   time but their factories close over bindings); default-parameter
   evaluation timing (ECMAScript spec says default params evaluate
   on call, which is lazy); dynamic `import()` arguments. The
   visitor's gaps should be exhaustively pinned in unit tests.
3. **Side-effect classification precision.** Without alias analysis
   we have to assume `const X = f()` is side-effecting if `f` is
   any function call. This over-imposes side-effect edges, which
   may block more candidate peel sets than strictly necessary.
   Pure-call inference is future work.
4. **Vendor chunk modeling.** Vendor chunks are pre-existing module
   boundaries that we don't control. They appear in the dep graph
   as nodes with no at-init reads from our chunk (the vendor
   doesn't import from us). The validator should sanity-check
   this; a vendor that imports back into the user-chunk is a
   pathological case worth detecting.
5. **Validator UX.** The cycle report should be actionable. A
   shape like "Cycle modules [M_a, M_b]; evidence: stmt#42 in
   M_a reads `X` (owned by M_b); stmt#107 in M_b reads `Y`
   (owned by M_a). Resolution: colocate X and Y in one module"
   is the goal.
6. **Pattern selectors for `anonymous_statements`.** Today the
   `match` is exact AST equality. Wildcard placeholders
   (`Ww([?], $g.prototype, ?, ?);` to match every decorator
   application on `$g.prototype` regardless of the decorator
   factory or property name) would let one selector entry claim a
   whole regular cluster — the 6 `Ww(...)` decorator applications
   in `WorkspaceInviteState` could collapse to a single
   selector. But this loses the strict-equality-or-loud-failure
   property and reintroduces narrowing, so the model would need
   to either bipartite-force across pattern selectors or ban
   ambiguity. Deferred until a real spec demands it.
7. **Factorize soundness audit.** `factorize` must be brought fully
   into line with [Factorization proposals](#factorization-proposals).
   The audit should pin tests for the invariant: every emitted
   proposal is accepted by the same owner-graph quotient predicate as
   a handwritten spec, without relying on production-scale browser
   loads to discover invalid splits. Internal frontier states may be
   numerous and rejected, but generated proposals must all be
   certified.
8. **Constraining-graph-sink refinement.** Some anonymous
   statements (Sentry debug-id IIFE, Vite modulepreload polyfill)
   are sinks in the constraining edge graph: they have side
   effects but no observable cross-binding deps. An s-edge from
   a peeled named owner to such a sink could be exempted as
   non-constraining, letting the named owner peel without an
   `anonymous_statements` co-mover. Empirically this would
   cover ~91 of 4106 currently-blocked horizon bindings in
   one representative bundle — a small fraction. The rest still need anon
   selectors because their companions (decorator applications,
   semantic init bridges) are not sinks. Worth tracking but
   not blocking.
9. **`chunk_renames` cross-module rename propagation.**
   `chunk_renames` members rename a binding's local-alias
   references in entry's body via the lowerer's body-rename
   pipeline. When a binding referenced by the chunk_rename is
   used INSIDE a logical-module-peeled body too (e.g. an
   imported `cx` is also called by a binding `b` that the spec
   peels into `b_module`), the rename does NOT follow into the
   peeled module's emit — the peeled module retains the
   original `import { f as cx } …; const b = cx();` shape
   while residual gets `getMobxGlobalState(...)`. To make the
   rename uniform, the rename map would need to thread into
   the per-module emission too (`lower_chunk` /
   `apply_module_lowering` paths), not just the entry-body
   rewrite. Out of scope for the purity-propagation change
   (`lowering/` declared_pure collection now
   pulls from chunk_renames members) — those are separate
   passes of the same map. Track when a real spec wants
   the renamed name everywhere.

## What this design does not solve

- **Intentional cyclic init semantics.** If the original chunk
  _relied_ on partial-eval state during a cyclic load (a rare
  but legal pattern), the input is inherently un-debundle-able
  into clean ESM modules. The realizability gate would surface
  the cycle; the resolution is to leave that subset of bindings
  as residual rather than fight ESM semantics.
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

- <design.md> — this document.
- <facts/mod.rs> — `StatementFacts` analyzer.
- <graph.rs> — owner graph and `ModuleDepGraph` builders.
- <validation.rs> — realizability checks.
- <chunk_analysis.rs> — `ChunkAnalysis` (inputs + IR + input-derived caches).
- <chunk_factorization.rs> — `ChunkFactorization` construction and linker-order reasoning.
- <atomic_units.rs> — owner-level hard colocation units.
- <factor_assembly.rs> — spec claims projected onto atomic units.
- <peel/factorize.rs> — advisory factorization proposal construction
  and reporting.
- <lowering/> — main splitting transform (`mod.rs` plus per-concern
  sibling files: `chunk_ast.rs`, `lower.rs`, `materialize/`,
  `plans.rs`, `naturalize.rs`, `imports_cross.rs`,
  `imports_runtime.rs`, `exports.rs`, `plan_references.rs`,
  `runtime_imports.rs`, `body_facts.rs`, `chunk_renames.rs`,
  `rewrite_runtime.rs`, `visitors.rs`, `anonymous.rs`, `util.rs`).
- <pipeline.rs> — fixed transform composition.
- <program_analysis.rs> — chunk metadata + side-effect
  classification (used as input to the analyzer).

Secondary:

- <vendor/>, <emit_harness.rs>, <write_tree.rs>,
  <identifier_rename_queue.rs> — supporting transforms and
  side-output producers.

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
- Keep sections phrased as current contract or future design. Do not
  accumulate completed phase logs in this canonical design doc; Git
  history and dedicated lessons-learned notes carry historical detail.
- Open design questions go in <#open-design-questions>. Once
  resolved, move the resolution into the body of the doc and
  delete the question.
- Keep the proof intact. The realizability theorem is the
  foundation; if a future change breaks the theorem (e.g.
  introduces an emit strategy that bypasses it), the proof
  needs revision and any dependent claims need re-checking.
