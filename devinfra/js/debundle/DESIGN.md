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
> and `Imported` bindings flow through `Schedule.bindings` /
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
the hot graph paths intern local names into compact typed ids:

```rust
pub struct BindingId(pub usize);

pub struct BindingTable {
    names: Vec<BindingName>,
    ids_by_name: HashMap<BindingName, BindingId>,
}
```

Reports and specs still spell bindings by `BindingName`; internal
owner graph edges, owner declarations, and per-chunk indexes use
`BindingId` so algorithms can use vector lookups instead of repeatedly
keying hot paths by strings.

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
- "What else must move with it?" is a closure/SCC/cut question over
  owner-level read and side-effect edges.
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

### Relationship

```
R  ⊆  I            (R is the at-init projection of I)
I  ∪  S            the full constraint graph
```

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
  references; `I` would be incomplete.
- **A2. No top-level `await` in any emitted module.** TLA changes
  the linker's evaluation algorithm to AsyncCycleRoot semantics
  (TC39 §16.2.1.5.4 + §16.2.1.5.5). The proof's reverse-DFS
  argument doesn't apply unmodified — async modules form their own
  ordering hazards, and a cycle through two async modules has
  semantics this design has not analyzed. **Enforced**:
  `materialize_logical_modules` calls `find_top_level_await`
  before fact analysis and `bail!`s with the offending statement
  ordinal if any module-top `AwaitExpr` is found (excluding lazy
  positions like function/arrow/method/getter/setter bodies and
  instance class fields). Production chunks we target are TLA-free
  in practice; the rejection turns the assumption into a verified
  precondition.
- **A3. No `import()` of debundled internal modules.** Dynamic
  imports of vendor / route chunks are fine — the proof treats
  those as black-box leaves with their own evaluation. Internal
  dynamic imports would route around the static `import` graph,
  invalidating `I` as a complete picture.
- **A4. No `with` blocks.** Banned in strict mode (which ESM is
  by spec); listed only for completeness.
- **A5. No reflection on module namespaces during evaluation.**
  No `Function.prototype.toString` reads of cross-module bodies
  whose result depends on evaluation order; no `Reflect`-based
  property descriptor inspection on the module namespace object
  during link; no `import.meta` introspection that depends on
  order. (Standard `import.meta.url` is fine.)
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
  spec format admits an optional per-member `purity: "pure"` field
  that asserts: calls to the bound function value have no
  observable side effects. The validator does not re-verify the
  function body — the annotation is an explicit author override
  that wins over both the inferred classification and A8's
  shadowing fallback. Used when the function body is too dynamic
  for static analysis (dynamic dispatch, dynamic property access)
  but the author knows by construction that the binding is pure.
  An incorrect annotation can produce a buggy debundle the same
  way an incorrect spec selector can — soundness shifts to the
  spec author. See AGENTS.md "Declared purity".

A1–A5 are statically checkable on each chunk: grep for top-level
`await`, dynamic `import()` of internal paths, `eval`, and `with`.
A2 in particular is enforced by `find_top_level_await` —
materialization `bail!`s with the offending statement ordinal
before fact analysis runs. A6 (no module-eval reentry) is relied
on by observation: the unmodified bundle loads in the browser.
A7 (multi-chunk DAG) is satisfied by typical bundlers' vendor-leaf
chunking. A8 (whitelist receivers not shadowed) is dropped to
`Unknown` per-name by `compute_shadowed_globals` whenever the
chunk-top declared-name set claims one of the whitelist receivers,
so chunks that violate A8 still validate soundly. A9 (declared
purity is author-trusted) is satisfied by spec review — every
`purity: "pure"` annotation is an explicit, reviewable trust
claim.

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

**Lemma 2 (Author choice — capability, not current behavior).**
The materializer's emit construction _can_ author each emitted
module's `import` directive list in an order that makes ECMA-262
reach any chosen topological linearization `L` of `I` rooted at
the entry.

_Proof._ The DFS in `InnerModuleEvaluation` visits requested
modules in `[[RequestedModules]]` order — i.e. the syntactic order
of `import` directives in the source. The materializer emits each
module's source; it controls every module's `import` order. For
target `L`: at each module `M` with successors `{M'_1, …, M'_k}`
in `I`, sort the import list so that the earliest-in-`L` successor
appears first (DFS goes deepest into the first import). The
resulting DFS post-order is `L`. ∎

The current emitter does not actually steer for a specific `L` —
imports come out in module-plan order. **For acyclic `I` alone,
this is sufficient**: every `L` produced by the linker's default
DFS is a topological linearization of `I`, so L3 and L4 hold under
any of them. **`L` selection is only required when `S` adds
constraints** that the default DFS doesn't already satisfy — and
the validator's strict gate on `I ∪ S` rejects specs whose `S`
constraints aren't already implied by `I`. Specs that satisfy
`I ∪ S` acyclicity but require `L`-steering for `S` do exist in
principle (a chunk where two side-effecting top-level statements
in different modules have no read dependency between them); for
those, the materializer would need to actually realize the choice
this lemma proves possible. **Known impl gap**: the emitter does
not steer for a specific `L` today. The realizability gate still
rejects SCCs containing constraining edges, but if an otherwise
valid acyclic spec ever requires `L`-steering for `S` to satisfy a
constraint that's invisible to ESM's default DFS, the emit would
silently violate it. Tracked as a follow-up if a real spec exposes
it.

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
  semantics). A separate proof would be needed; the validator
  has no analysis of TLA today. If a future bundle introduces
  TLA, the materializer should refuse the chunk explicitly.
- **Dynamic eval / reflection bypassing the static graph (A1, A5
  violations).** The `I` graph is incomplete in this case;
  cycle-freeness in `I` doesn't imply cycle-freeness in the
  actual evaluation graph. The materializer cannot rule this out
  statically; treat A1/A5 as input-shape contracts on the bundler.
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
module, dissolving the SCC.

The synthetic minimization is in <e2e/realizability_test.rs>
(`rejects_cycle_through_lazy_back_edge`).

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
work**: a multi-chunk lift would have `validate_schedule` take a
`BTreeMap<ChunkId, Schedule>` and walk the union graph.

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
apply spec assignment
  (dest(owner), unowned bindings → ResidualEntry)
  ↓
quotient owner graph by dest(owner)
  ↓
derive I, R, S
  ↓
validate: realizability gate over I ∪ S
  ↓        ↓ no                       (sidecar)
  ↓        ↓                  owner-level diagnostics, cycle cuts,
  ↓        ↓                  peelability projections
emit (source-order)
  ↓ no
reject with cycle evidence
```

A spec that passes validation is _guaranteed_ to emit correctly
under the source-order strategy described in the proof. There is
no class of accepted-input that the validator may emit incorrectly
(modulo cleanly-defined precision of `reads_at_init` and
`has_side_effect`). The validator may reject specs that are in fact
realizable when analysis is conservative; that is the intended
failure mode. Those rejections should come with owner-level evidence
showing which graph edges made the split unverifiable.

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
implicit pulling: when a peelability proposal's closure includes
anonymous companions, the spec author copies their source into
`anonymous_statements` (one entry per claimed statement); the
materializer never silently co-moves an anon statement.

The owner graph is the explicit replacement for hidden closure. It
records the fine-grained "this owner uses that owner" relation before
any module-level quotienting. Diagnostics are projections of that
graph: validation cycles explain why the current explicit assignment
is not realizable, and peelability explains which residual owners can
move to a new destination without introducing a new invalid quotient
SCC or an unimportable residual dependency. The tool may rank,
summarize, or annotate those projections, but it does not silently
assign bindings on behalf of the spec author.

`ScheduleReport` remains the compact validation report. The detailed
next-action data lives in `<reports>/<chunk_id>/owner_graph.json`,
which is emitted on both success and rejection. Re-running the
pipeline on an updated spec produces fresh graph and peelability
reports.

When an output emitter materializes executable JavaScript, its root
`manifest.json` and each chunk `manifest.json` include `output_metrics`.
These metrics are computed from the exact rendered JS written to disk:
total bytes/lines/files, top-level entry bytes/lines/files, named
logical-module bytes/lines/files, residual module bytes/lines/files,
and the largest JS sinks. Peeling tools should use these manifest fields
for progress reporting instead of rescanning output trees.

### Workflow

1. Spec author writes / edits a spec — possibly partial.
2. Pipeline runs the validator. The report flags:
   - **Cycles** in the explicit-only assignment, if any.
   - **Peelability projections** for residual owners and minimal
     companion sets in `owner_graph.json`.
3. Spec author resolves: first fix any validation cycles; then peel
   owner sets from `peelability.minimal_peel_sets[]`, copying their
   bindings into new explicit spec modules and assigning good public
   names.
4. Re-run validator. Iterate until validation is green and the
   residual contains only intentionally generated or low-value noise.

The spec is now fully explicit. The validator is a one-shot
predicate: "given this spec, is the bundle realizable?" — no
implicit transformation in the middle.

## Peelability diagnostics

The same owner graph drives the next-spec workflow. A pipeline that
only reports whether the current spec passed forces spec authors
into speculative edit/build/test loops. The owner-graph side output
therefore includes peelability projections for residual owner sets.
V1 computes candidate owner sets with three statuses:

- **`peelable_now`** — assigning this owner set to a new logical
  destination leaves the quotient graph realizable and all imports
  resolvable.
- **`blocked_cycle`** — the quotient graph would contain an
  unrealizable SCC. The report includes the owner-level cut before
  grouping it into module-level cycle evidence.
- **`blocked_residual_dependency`** — a moved owner would still
  read a binding that remains private to the residual entry. The
  report includes the owner edges and read bindings that cross that
  unimportable cut.

Direct peels and "only with companions" peels are one unified
hypergraph. Each peelable owner set is a hyperedge over owner
vertices. A direct peel is a peelable set with one owner. A
companion peel is a residual owner whose minimal peelable set has
more than one owner. The side output exposes both the set list and
a per-residual-owner incidence view, so downstream tools do not
have to infer companion relationships from unrelated candidate rows.

This is deliberately a projection of the owner graph, not a separate
heuristic system. Tooling may rank candidates by size reduction,
name quality, or path, but the validity status comes from the same
quotient + realizability check as normal materialization.

The report exposes the graph data needed to make that decision
without re-running emitted JS:

- the owner vertices and their stable report ids, statement
  ordinals, source locations, declared member bindings with
  readable export names, current destinations, and proposed
  destinations;
- the owner edges, with binding/read-kind/side-effect-order
  provenance and statement ordinals;
- candidate assignments with status, readable member lists,
  residual-dependency blockers, and quotient-cycle cut evidence.

Downstream peel skills should read it before editing YAML: promote
`peelability.minimal_peel_sets[]` first. For an individual symbol,
read `peelability.residual_owner_horizon[]`: `status: "direct"`
means its singleton set is peelable, `status: "with_companions"`
means one of its `companion_options[]` must move with it, and
`status: "blocked"` means no currently computed peel set covers
that owner. Detailed rejected candidate traces are intentionally not
serialized; the report keeps the actionable peel hypergraph compact
enough for large chunks.

### Residual peel candidates

> Detailed factorize algorithm — including how existing modules
> participate as supernodes in the proposal graph — is documented in
> the dedicated <FACTORIZE.md>. The summary below covers the
> single-owner / two-owner peel candidate primitive that the
> peelability projection emits.

The first implemented candidate family answers the immediate
operational question: "which symbols can currently peel out of the
residual catch-all?"

Let `R` be any destination marked residual (`ResidualEntry` or a
generated residual logical module), and let `o ∈ V` be an owner
currently assigned to `R` with declared symbols `decl(o)`. To test
whether `o` is peelable now:

1. Create a fresh hypothetical destination `P_o`.
2. Reassign every binding in `decl(o)` to `P_o`; leave all other
   owner destinations unchanged.
3. Quotient the same owner graph by this candidate assignment.
4. Inspect only the SCC containing `P_o`.
5. The candidate is `peelable_now` iff that SCC is absent,
   singleton/non-cyclic, or contains only lazy-read cross edges.
   If that SCC contains an at-init read or side-effect-order edge,
   the candidate is `blocked_cycle`, and the report lists the
   constraining module edges plus their owner-edge ids.

This local-SCC test intentionally ignores unrelated pre-existing
bad SCCs. When the current build is green, it is equivalent to
checking the whole candidate quotient. When the build is red, it
still identifies residual symbols whose peel is locally safe and
therefore useful for reducing the residual or breaking a larger
cycle without adding a new one.

V1 also computes bounded two-owner closures. It does not scan every
pair in the residual. Instead, for each cycle-blocked singleton
candidate, it keeps internal constraining owner-edge evidence and
seeds pairs from residual owners that are direct endpoints of those
constraining edges. Each seeded pair is tested by the same
fresh-destination quotient operation above. Only pairs whose
candidate SCC is realizable are reported as a peel set with
`owner_ids.len() == 2` / `status: "peelable_now"`. This
captures the common "A and B can move, but only together" case
while keeping the report tied to actual cycle evidence instead of
speculative all-pairs search.

The report writes this as:

- `peelability.residual_destinations[]`
- `peelability.minimal_peel_sets[]`, the currently computed
  minimal peelable owner-set hyperedges
- `peelability.residual_owner_horizon[]`, one row per residual
  owner with `status`, `peel_set_ids`, and any
  `companion_options[]`

### Detailed graph side output

The transform also writes a detailed graph side output, separate from
the compact validation error. This is the data source for analysis
scripts, notebooks, and repo-specific peel skills. It is emitted on
both success and validation rejection, because the most useful peel
data often exists precisely when the current assignment is not yet
realizable.

The detailed output is machine-readable, typed, and debundler-owned.
For each chunk it includes:

- run metadata: chunk id and source paths;
- owner vertices: report id, statement ordinal, source location,
  declared bindings as `members[]`-shaped `{binding, export_name}`
  records, owner kind, side-effect classification, and current
  destination;
- owner edges: `source`, `target`, edge kind, optional binding,
  statement ordinal, and whether the edge constrains realizability;
- quotient projections for the current assignment: module nodes,
  aggregated edge kinds, SCC membership, and SCC realizability
  status;
- residual peelability projections:
  `minimal_peel_sets` and `residual_owner_horizon`.
  These projections carry the same `{binding, export_name}` member
  records used by spec authoring, so downstream tools do not need to
  reparse repo-specific module YAML just to recover readable names.

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
logical module, is an imported binding being re-exported, is an
entry export the residual module may legally surface, or moves with
the consumer. Reads of private residual-entry bindings are not
importable unless the assignment also exports or colocates the
provider.

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
3. Every SCC in `Q` contains only non-constraining cross edges.
   Equivalently, any SCC may contain lazy-read import cycles, but no
   at-init read, rebinding write, or side-effect-order edge may remain
   inside a multi-destination SCC.

A peel is just a proposed destination assignment for a candidate
owner set. The peel is valid exactly when the resulting assignment
is valid by the definition above. Invalid peels have two primary
explanations: a non-importable read crosses the cut, a rebinding
write crosses the cut, or a constraining edge remains in a quotient
SCC.

The same definition gives a graph-theoretic target for "smallest
atomic modules that are still valid." Start from the finest
partition of owners allowed by syntax. Then repeatedly:

1. Union the endpoints of every read edge that cannot be made
   importable under the current export policy.
2. Quotient by the current partition and find SCCs.
3. For every constraining cross edge that lies inside a quotient
   SCC, union that edge's endpoints.

When this reaches a fixed point, the partition is the finest valid
partition under the current edge labels and importability policy:
any valid assignment must coarsen it. The key graph fact is that
quotienting preserves reachability between distinct blocks; if a
constraining edge's endpoints remain in different blocks and the
target can still reach the source, that edge remains inside an
unrealizable SCC. Collapsing the constraining edge is therefore
necessary, and repeating this operation is sufficient.

### Graph operations for peel tooling

Useful operations should be phrased against the owner graph first
and only then projected to modules:

- **Quotient:** `G / dest` produces the module graph the validator
  already understands.
- **Dependency closure:** starting from candidate owners, follow
  owner-level read edges that cannot be satisfied by imports. This
  yields the smallest "must move together" set for private residual
  dependencies.
- **SCC:** run on either the owner graph or the candidate quotient.
  Owner-level SCCs identify mutually-recursive or mutually-dependent
  clusters; quotient SCCs identify ESM realizability hazards.
- **Cut:** for an unrealizable quotient SCC, compute a small set of
  owner-level `at_init` or side-effect-order edges whose removal or
  colocation would make the split valid. The existing module-level
  feedback-arc cut should be reported with owner-edge provenance.
- **Ranking:** once validity is known, rank safe candidates by
  emitted-size reduction, number of owners, name quality, and whether
  the target path matches an existing human module namespace. Ranking
  is heuristic; validity is not.

These operations are pure graph transforms over immutable analysis
data. That property matters operationally: two tools reading the same
owner graph and candidate assignments should produce the same
peelability status without building emitted JS.

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
- Comparison is `EqIgnoreSpan` over `ModuleItem`. Whitespace and
  comments differences are ignored; identifier names and string
  literals are not. Identifier names match the chunk's
  pre-readability-rename form (the same form binding selectors
  use as `selector.binding.name`).
- Each anonymous statement may belong to at most one logical
  module. A duplicate claim across two modules is a spec error;
  the validator names both modules and the offending statement
  ordinal.

### Why exact source rather than line/column

Tana's bundle is delivered minified and prettified for analysis;
neither line nor column is stable across re-prettifies or
upstream Tana releases. The shape selector survives both: the
prettifier reformats whitespace but preserves AST structure, and
upstream changes that touch the statement's content surface as a
zero-match diagnostic the spec author can fix.

### Schedule integration

`Schedule` validates realizability (the cycle gate over `I ∪ S`)
by quotienting the owner graph by each owner's destination.
Anonymous owners default to `ModuleId::ResidualEntry`; the
schedule overrides that destination for any anon owner the spec
claimed. Without this override, an anon owner with a constraining
in-edge from a peeled named owner would create a fake cross-module
edge — the validator would reject the spec even though the
materializer would emit the closure correctly. After the override,
the validator sees the same module dep graph the materializer
will emit, and the cycle gate fires only on real unrealizable
splits. See `Schedule::build` in `schedule.rs`.

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
parsed/prepared chunk artifact consumed by rewrite, vendor,
logical-materialization, and output stages. The pipeline should not
grow a second mutable "state" object parallel to those phase outputs.

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

| Step                             | Module                                | Runs when                                                                                                                            |
| -------------------------------- | ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `load_transform_spec`            | <pipeline.rs>                         | Always; loads either the flat YAML spec or the tree-shaped authoring spec.                                                           |
| `validate_transform_spec`        | <spec.rs>                             | Always after spec load.                                                                                                              |
| `load_js_chunks`                 | <artifact.rs>                         | Always; configured by `inputs`.                                                                                                      |
| `prepare_js_chunks`              | <prepare_chunks.rs>                   | Always. In one parallel per-chunk pass, parses every chunk with SWC, computes shallow program facts, and canonicalizes entries.      |
| `build_artifact_indexes`         | <artifact.rs>                         | Always after preparation. Builds chunk id, source path, output path, and import-reference indexes for later stages.                  |
| `rewrite_chunk_entry_specifiers` | <rewrite_specifiers.rs>               | Always, after chunk preparation and before data-gated transforms.                                                                    |
| `apply_vendor_annotations`       | <vendor.rs>                           | When the `vendor` map is non-empty.                                                                                                  |
| `rename_vendor_exports`          | <vendor.rs>                           | When a `vendor` entry has `level: boundary_rename` or `level: swap`.                                                                 |
| `swap_vendor_chunks`             | <vendor.rs>                           | When a `vendor` entry has `level: swap`.                                                                                             |
| `materialize_logical_modules`    | <logical_modules.rs> + analysis files | When `logical_modules` or `residual_modules` is non-empty. Computes facts, quotients the owner graph into `I ∪ S`, validates, emits. |
| `write_js_tree`                  | <write_tree.rs>                       | When `write_js_tree` output config is present; writes JS tree manifests with exact `output_metrics`.                                 |
| `emit_browser_harness`           | <emit_harness.rs>                     | When `emit_browser_harness` output config is present; writes browser harness manifests with exact `output_metrics`.                  |

Within `materialize_logical_modules`, the substages are:

1. **Spec parsing** → `LogicalRequest` / `ModulePlan` per chunk.
2. **Chunk AST analysis** (<logical_modules.rs>:
   `analyze_chunk_ast`) → top-level declarations, declaration index,
   and runtime import facts in one top-level scan.
3. **Statement-facts analysis** (<facts.rs>:
   `analyze_chunk_facts`) → `Vec<StatementFacts>`.
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
7. **Diagnostics projections** — cycle evidence and peelability
   reports are projections of the same owner graph + quotient, not
   separate heuristic analyses.
8. **Cycle resolution gate** — if the validator finds an
   unrealizable cycle, the pipeline aborts with the cycle
   evidence.
9. **Source-order emission** — each module's body in source order;
   cross-module imports + source-chunk re-imports; `export { ... }`.
   No init wrappers.

Treat those stages as a functional data flow. Analysis produces
immutable facts; assignment is explicit input; quotienting derives a
validated schedule; emission consumes that schedule. Avoid designs
where emission discovers new graph edges or silently mutates the
assignment, because that reintroduces hidden closure behavior and
makes diagnostics disagree with emitted output.

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

## Schedule: owner graph plus quotient

The target runtime data structure the validator and emitter both
consume is a single per-chunk `Schedule`: it carries the chunk's
statement facts, the binding catalogue, the explicit logical
modules, the owner graph, and the module dep graph derived by
quotienting that owner graph under the spec assignment.

The implementation builds this structure directly: statement facts
feed the owner graph, and the module dep graph is a quotient of that
owner graph under the current spec assignment.

The schedule is keyed per-chunk; the chunk is contextual within
the schedule, so binding keys collapse to just `name`. Keys for
the dep graph extend `ModuleId` with an `ExternalChunk(ChunkId)`
variant so cross-chunk reads are first-class.

Conceptually, the schedule carries:

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
/// into `Schedule.logical_modules`; wrapped to keep it from
/// being mistaken for `StatementOrdinal` etc.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct LogicalModuleIndex(usize);

/// Position of a top-level statement in a chunk's source body.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct StatementOrdinal(usize);

/// Local name of a binding in a chunk's top-level scope. Used
/// as a key in `Schedule.bindings`. The string itself is the
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

- Spec entries continue to identify bindings by `binding.name`
  (already done) and optionally `owner.line` for drift
  detection (already done). Both are meaningful and unambiguous
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

Action: add a unit test per case to <facts.rs> / <purity.rs>;
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
   may block more candidate peel sets than strictly necessary.
   Pure-call inference is future work.
3. **Vendor chunk modeling.** Vendor chunks are pre-existing module
   boundaries that we don't control. They appear in the dep graph
   as nodes with no at-init reads from our chunk (the vendor
   doesn't import from us). The validator should sanity-check
   this; a vendor that imports back into the user-chunk is a
   pathological case worth detecting.
4. **Validator UX.** The cycle report should be actionable. A
   shape like "Cycle modules [M_a, M_b]; evidence: stmt#42 in
   M_a reads `X` (owned by M_b); stmt#107 in M_b reads `Y`
   (owned by M_a). Resolution: colocate X and Y in one module"
   is the goal.
5. **Pattern selectors for `anonymous_statements`.** Today the
   `match` is exact AST equality. Wildcard placeholders
   (`Ww([?], $g.prototype, ?, ?);` to match every decorator
   application on `$g.prototype` regardless of the decorator
   factory or property name) would let one selector entry claim a
   whole regular cluster — the 6 `Ww(...)` decorator applications
   in `WorkspaceInviteState` could collapse to a single
   selector. But this loses the strict-equality-or-loud-failure
   property and reintroduces narrowing, so the model would need
   to either bipartite-force across pattern selectors or ban
   ambiguity. Deferred until a real Tana spec demands it.
6. **Constraining-graph-sink refinement.** Some anonymous
   statements (Sentry debug-id IIFE, Vite modulepreload polyfill)
   are sinks in the constraining edge graph: they have side
   effects but no observable cross-binding deps. An s-edge from
   a peeled named owner to such a sink could be exempted as
   non-constraining, letting the named owner peel without an
   `anonymous_statements` co-mover. Empirically this would
   cover ~91 of 4106 currently-blocked horizon bindings in
   Tana — a small fraction. The other ~4015 still need anon
   selectors because their companions (decorator applications,
   semantic init bridges) are not sinks. Worth tracking but
   not blocking.
7. **`chunk_renames` cross-module rename propagation.**
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
   (`logical_modules.rs:528+` declared_pure collection now
   pulls from chunk_renames members) — those are separate
   passes of the same map. Track when a real Tana case wants
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

- <DESIGN.md> — this document.
- <facts.rs> — `StatementFacts` analyzer.
- <graph.rs> — owner graph and `ModuleDepGraph` builders.
- <validation.rs> — realizability checks.
- <schedule.rs> — schedule construction and linker-order reasoning.
- <peelability.rs> — residual peelability horizon.
- <logical_modules.rs> — main splitting transform.
- <pipeline.rs> — fixed transform composition.
- <program_analysis.rs> — chunk metadata + side-effect
  classification (used as input to the analyzer).

Secondary:

- <vendor.rs>, <rewrite_specifiers.rs>, <emit_harness.rs>,
  <write_tree.rs>, <identifier_rename_queue.rs> — supporting
  transforms and side-output producers.

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
