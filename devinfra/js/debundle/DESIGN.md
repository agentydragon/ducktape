# Debundler — Design

> Status: phases 1 → 5 landed. The legacy init-wrapper lowering is
> gone; the validator gates on `I ∪ S` (the ESM linker's import
> graph plus side-effect ordering); `lower_chunk` emits source-
> order only. The Tana spec is fully explicit (every binding has
> an owner; closure pass deleted). The parallel `import_members`
> channel is gone too — Imported bindings flow through
> `Schedule.bindings` / `BindingKind::Imported`, with multi-module
> re-exports accumulating into one entry's `re_exported_by` map.
>
> Phase 5 closed a soundness gap surfaced by the Tana live-proxy
> run: the validator was building the at-init read graph `R`, but
> the realizability graph is the linker's import graph `I ⊇ R`
> (lazy reads still emit `import` directives). The realizability
> theorem is now stated and proved over `I ∪ S`, with explicit
> JS-subset assumptions (A1–A7) and lemma-by-lemma chaining; the
> validator's edge construction matches. See
> [Erratum: I vs R](#erratum-i-vs-r) for the conceptual misstep
> and the [realizability theorem](#the-realizability-theorem) for
> the proof.

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

We use that pair as the canonical _binding key_. Concretely:

```rust
pub struct BindingId {
    pub chunk: ChunkId,
    pub name: String,  // local name in the chunk's top-level scope
}
```

Within a single chunk's pipeline (the common path — most of the
debundler operates per-chunk), the key collapses to just `name`,
and the chunk is contextual. Outside that context (e.g. in tools
that walk multiple chunks at once, or in cross-chunk dep edges),
the full pair is required.

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
        /// Each module that re-exports this binding, mapped to
        /// the public name it gives the export. Different modules
        /// may pick different names for the same imported binding
        /// (e.g. one module surfaces `vendor.j` as `jsxRuntime`,
        /// another as `__jsx`).
        re_exported_by: BTreeMap<ModuleId, BindingName>,
    },
}
```

Distinguishing the two kinds in the data model is the key reason
the legacy code grew the parallel `import_members` channel: the
`binding_assignment: BTreeMap<String, usize>` of the legacy model
has no slot for "this binding is owned elsewhere; we just want to
re-export it from these logical modules." The unified model has
that slot built in.

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
  side-effecting statements), `home(S)` is the residual entry
  module.

## Module dep graphs

The materializer reasons about three distinct directed graphs over
the same vertex set. Each captures a different scheduling constraint;
the realizability theorem (next section) is stated over their union.

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

> **Theorem (correctness).** If `I ∪ S` is acyclic, the source-
> order emit construction below produces a multi-module ESM bundle
> observationally equivalent to the input chunk.
>
> **Design rule (gating).** The materializer accepts a spec iff
> `I ∪ S` is acyclic. Specs whose `I ∪ S` has any cycle are
> rejected with cycle evidence — even cycles whose at-init
> projection (`R ∪ S`) is acyclic, where the bundle could in
> principle be made to work by careful entry-import ordering.

Here "spec assignment" means whatever the validator sees: every
declared binding either has an explicit `owner` from the spec or
defaults to `ModuleId::ResidualEntry` (see [Spec explicitness
— closure as recommender](#spec-explicitness--closure-as-recommender)).
There is no implicit transformation between the spec and the
assignment the theorem reasons about.

The gating rule is strictly stronger than the correctness theorem
needs. We could in principle accept some specs with `I ∪ S` cycles
where `R ∪ S` is acyclic: the linker would have to evaluate one
SCC member first, but if no at-init back-edge points into that
member, lazy reads from inside the SCC don't fire until after the
linker finishes (function bodies don't run during linking), and
the bundle works. We don't accept these because:

1. **Architectural fragility.** Any future edit promoting a lazy
   read inside the SCC to an at-init read silently TDZs the
   bundle. The boundary the spec describes is not actually a
   separable cut — the modules are bound together in the linker's
   graph; the spec is lying about their independence.
2. **Linker ordering is non-local.** "Lazy reads don't fire
   during linking" is a property of the entire program graph and
   any imported callsite. A vendor library that calls back into
   user code during its own evaluation would invalidate the
   assumption. We cannot statically rule that out.
3. **The cyclic spec is always rewritable.** Colocating the
   bindings on the back-edge into one module dissolves the cycle
   without losing any factoring the human author actually
   wanted.

Calling cyclic specs "unrealizable" is therefore a deliberate
design choice, not a forced consequence. We adopt it because the
permissive variant is a footgun every other invariant in this
design works to prevent.

### Conditions on the input chunk (assumptions A1–A7)

The proof below reasons about ECMA-262 module evaluation under a
specific JS subset. If the input chunk falls outside this subset,
the proof does not apply and the materializer's correctness
contract holds only by happenstance. The subset is what real
production bundlers (esbuild, rollup, webpack with `output.module:
true`) emit by default, so it covers Tana, Svelte/SvelteKit
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
  semantics this design has not analyzed. Tana's chunks are
  TLA-free; we assert this assumption and reject any chunk that
  introduces TLA.
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

For Tana 78d928dca7: the chunk is grep-verifiably free of
top-level `await`, dynamic `import()` of internal paths, `eval`,
and `with`. We rely on A6 by observation (the unmodified bundle
loads in the browser); A7 by virtue of esbuild's vendor-leaf
chunking.

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

**Lemma 2 (Author choice).**
For any topological linearization `L` of `I` rooted at the
entry, the materializer can author each emitted module's `import`
directive list in an order that makes ECMA-262 produce exactly
`L`.

_Proof._ The DFS in `InnerModuleEvaluation` visits requested
modules in `[[RequestedModules]]` order — i.e. the syntactic order
of `import` directives in the source. The materializer emits each
module's source; it controls every module's `import` order. Choose
each module's import order so the resulting DFS post-order matches
`L`. Concretely: for each module `M` with successors
`{M'_1, …, M'_k}` in `I`, sort them by their position in `L`
(ascending — earliest in `L` first, since DFS visits the first
import deepest); apply recursively. The DFS post-order over the
authored import list is `L`. ∎

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

**Theorem (correctness).** Under assumptions A1–A7, if `I ∪ S`
is acyclic, then the source-order emit construction produces a
multi-module ESM bundle whose observation-trace under ECMA-262
evaluation is identical to the input chunk's evaluation.

_Proof._ Acyclic `I ∪ S` admits a topological linearization `L`
respecting both `I` and `S`. By Lemma 2 we author each emitted
module's import directive list to make ECMA-262 produce `L`. By
Lemmas 3 and 4 every binding read in the emitted bundle sees an
initialized value identical to the input chunk's read at the
same source ordinal. By Lemma 5 every side-effecting statement
fires in the same relative order as in the input chunk. By the
emit's same-module source-order invariant, statements within a
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
  order. We require vendor chunks to be ESM (Tana satisfies this).
- **Permissive cycles** (the lazy-only-cyclic case discussed in
  the gating rule above): observationally equivalent in narrow
  conditions but architecturally fragile. The strict gate rejects
  them; the proof does not consider them.

### Stress-test against the broken example

The previous formulation of this theorem (revisions before
Phase 5) was stated over `R` only; the validator computed `R`
and checked it for cycles. The Tana live-proxy regression
showed that this is unsound in practice:

```
mod_a:                                      mod_b:
  const A = "a-value";                        const B = A + "-postfix";  // R-edge mod_b → mod_a
  function readB() { return B; }              // (mod_b owns B; mod_a's `readB`
  // ↑ lazy read of B; closes I-cycle        // is referenced at-init from
  // mod_a → mod_b                           // residual entry's `readB()` call)
```

`R = {(mod_b, mod_a)}` — acyclic. The previous validator passed.
The materializer emitted. The runtime behavior:

- Linker DFS from entry: enters one of `{mod_a, mod_b}` first. If
  the entry imports `B` (via `mod_b`'s export) before `readB`
  (via `mod_a`), DFS goes `entry → mod_b → mod_a → mod_b (cycle,
return)`. mod_a evaluates first. mod_a's `const A = …` runs.
  mod_b evaluates next. mod_b reads `A` at-init from mod_a's
  exports; A is initialized. ✓
- If the entry imports `readB` first, DFS goes `entry → mod_a →
mod_b → mod_a (cycle, return)`. mod_b evaluates first. mod_b's
  `const B = A + "-postfix"` runs; A is **TDZ** (mod_a hasn't
  evaluated yet). ✗

The bundle works or breaks depending on entry-import ordering.
Author intent on the entry side is enough to accidentally flip
it. The strict gating rule rejects this spec; the new proof
notices the I-cycle (the lazy `readB` body still emits an
`import { B }` directive in mod_a, contributing edge
`(mod_a, mod_b)` to `I`); the validator surfaces the cycle and
the spec author colocates `B` with `readB` to dissolve it.

The synthetic minimization is in
<e2e/lazy_back_edge_cycle_test.rs>; the production shape was
the live-proxy `command_schema → vendor/symbols →
prompting_runtime → command_schema` 3-module SCC.

### Why a strict gate, not a runtime check

### Why a strict gate, not a runtime check

The gating rule is enforced **statically** by the validator. The
materializer either emits a bundle whose `I ∪ S` is provably
acyclic (and therefore observationally equivalent by the theorem
above), or refuses with cycle evidence. There is no runtime
check, no init-wrapper safety net, no per-load TDZ guard. By
construction, the emitter never produces JavaScript that the ESM
linker has to puzzle through a cyclic import graph for. If the
spec describes a cyclic decomposition, the spec is wrong; we
report the cycle and let the author fix it.

This is the contract the user-visible artifact relies on. Any
escape hatch — accepting cyclic specs that happen to work, or
deferring cycle detection to runtime — gives back the property
that "an emitted bundle from this materializer is, by inspection
of its module graph alone, free of TDZ and side-effect-ordering
hazards."

### Erratum: I vs R

Earlier revisions of this document defined a single graph `G` with
edges only over `reads_at_init` and stated the realizability
theorem as "`G ∪ G'` acyclic." That conflated two distinct
graphs:

- The graph the linker walks (one edge per emitted `import`
  directive — what is now called **`I`**).
- The sub-graph capturing only at-init reads (what is now called
  **`R`**, with `R ⊆ I`).

The earlier theorem was stated over `R ∪ S`, not `I ∪ S`. That is
unsound: lazy reads still emit `import` directives, which the
linker still uses for evaluation order. A spec where two modules
mutually import each other only via lazy reads has acyclic `R`
but cyclic `I`; the linker enters the SCC, picks an evaluation
order, and any subsequent edit promoting a lazy read to at-init
silently TDZs the bundle. The Tana `command_schema` regression
(at-init read on `m`/`systemIds`, lazy back-edges through
`vendor/symbols → prompting_runtime → command_schema`) is exactly
this shape: `R ∪ S` is acyclic, `I ∪ S` is not, the previous
validator passed it, the bundle TDZ'd at runtime.

The correction lifts the validator's edge construction from `R`
to `I`. `R` is retained in the design as an analytic sub-graph
useful in the proof (every at-init read is satisfied by the
linker's `I` order), but it is **not** the realizability graph.

The fix is tracked as Phase 5 in the [status table](#status).

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

For Phase 1 the validator runs per-chunk; cross-chunk edges
appear as `ExternalChunk(_)` leaves in the per-chunk graph. The
multi-chunk lift is a Phase 1.6 follow-up: `validate_schedule`
takes a `BTreeMap<ChunkId, Schedule>` and walks the union graph.

### Corollary: the role of the validator

The pipeline runs:

```
parse chunk
  ↓
analyze per-statement facts
  (declared, reads_at_init, reads_lazy, has_side_effect)
  ↓
apply spec assignment (unowned bindings → ResidualEntry)
  ↓
build I, R, S
  ↓
validate: I ∪ S acyclic?     ←—— Phase 1 lives here.
  ↓        ↓ no                       (sidecar)
  ↓        ↓                  recommendations for unowned bindings
  ↓                                        (see Spec explicitness)
emit (source-order)
  ↓ no
reject with cycle evidence
```

A spec that passes validation is _guaranteed_ to emit correctly
under the source-order strategy described in the proof. There is
no class of correct-input that the validator rejects, and no
class of incorrect-input that the validator misses (modulo
cleanly-defined precision of `reads_at_init` and `has_side_effect`).

## Spec explicitness — closure as recommender

The legacy materializer runs an _automatic closure pass_: when
the spec assigns binding `A` to module `M` and `A`'s init reads
`B`, the closure silently assigns `B → M` too (unless `B` is
already claimed). The intent was ergonomic — let the spec author
name just the bindings they care about and trust the system to
fill in transitive deps.

In practice the trade-off is wrong:

1. **Spec stops being the source of truth.** What bindings end up
   in module `M` depends on a heuristic the author can't see at
   spec-edit time.
2. **Splitting co-pulled bindings is fighting a heuristic.** If
   `A` and `B` are co-pulled by closure but the author wants them
   in different modules, they have to write an explicit
   counter-claim somewhere. The "fight" is invisible — you can't
   read the spec and tell what's a counter-claim.
3. **Cycles introduced by closure are invisible at spec time.**
   The Tana 9-module SCC we just discovered is exactly this: the
   spec author never wrote down that `runtime_app_state_search_
commands_core` and `ai_mcp_prompting_runtime` would end up
   reading each other's bindings. The closure produced that
   coupling.
4. **No spec-size win at the limit.** When every scrambled symbol
   gets a meaningful name (the eventual goal for Tana), the spec
   already enumerates every binding. Adding ownership info per
   binding is the same `O(num_bindings)` size — closure isn't
   saving spec size, just hiding which binding ended up where.

**Design rule.** The spec is fully explicit. Every owned binding
has its `owner` named in the spec or defaults to
`ModuleId::ResidualEntry`. There is no implicit pulling.

The closure's logic doesn't disappear; it relocates. Instead of
running on the runtime path, it runs as part of validation as a
**recommender**:

```rust
pub struct AssignmentRecommendation {
    /// The binding the spec author hasn't claimed yet.
    pub binding: BindingName,
    /// Modules that read this binding (at-init or lazily).
    pub candidate_owners: Vec<RecommendationCandidate>,
}

pub struct RecommendationCandidate {
    pub module: ModuleId,
    pub read_kind: RecommendationReadKind,
    /// True iff assigning `binding → module` would not introduce
    /// a cycle in `I ∪ S`. Cycle-safe candidates are preferred.
    /// Note: lazy-only reads still create `I` edges (the emit
    /// has to carry the corresponding `import` directive), so
    /// `LazyOnly` is *not* automatically cycle-safe — every
    /// candidate is checked the same way.
    pub cycle_safe: bool,
}

pub enum RecommendationReadKind {
    /// The candidate module reads this binding at-init (e.g. in
    /// a `const X = b + 1` initializer, an `extends b` clause, a
    /// computed property key, etc.). Contributes to both `R`
    /// (TDZ-relevant) and `I` (linker graph).
    AtInit,
    /// The candidate module reads this binding only inside
    /// function/method bodies — lazy. Contributes to `I` only.
    /// Lazy reads can still close `I` cycles even though they
    /// never TDZ at link time, which the strict gating rule
    /// rejects (see [the realizability theorem]).
    LazyOnly,
}
```

`ScheduleReport` carries a `recommendations: Vec<...>` list
alongside the existing `cycles: Vec<...>` list. The validator
emits both; the report is one-shot — re-running the validator on
an updated spec produces a fresh report.

### Workflow

1. Spec author writes / edits a spec — possibly partial.
2. Pipeline runs the validator. The report flags:
   - **Cycles** in the explicit-only assignment, if any.
   - **Recommendations** for every binding with no owner (i.e.
     defaulted to `ResidualEntry` or transitively required).
3. Spec author resolves: for each recommendation, copy the
   chosen owner into the spec, or restructure to break a cycle.
   The recommender flags cycle-safe candidates so the author
   can pick one without re-running.
4. Re-run validator. Iterate until the report's `cycles` and
   (optionally) `recommendations` are both empty.

The spec is now fully explicit. The validator is a one-shot
predicate: "given this spec, is the bundle realizable?" — no
implicit transformation in the middle.

### One-shot migration tool

The existing Tana spec doesn't have explicit owners for most
bindings — it relied on the legacy closure to fill them in.
Bridging:

1. Run the legacy closure once on the existing spec; capture
   the resulting full assignment as JSON.
2. Mechanically rewrite the gaffer spec
   (<tana/re/web/transforms/78d928dca7/operations/logical/...>)
   so every binding has an explicit `owner` member entry. The
   rewrite is reversible and deterministic — no information is
   lost, since the legacy closure was deterministic.
3. Drop `close_module_bindings_over_dependencies` from the
   ducktape runtime; replace with the recommender side-output
   described above.

After migration the gaffer spec is the same as today modulo
verbosity, but with the assignment fully visible to the spec
author.

This is tracked as **Phase 1.7** in the [status table](#status),
and runs before Phase 2 (the cycle-breaking spec edits) since it
fixes the spec language Phase 2 will be editing.

## Selector vocabulary and matching

> **Deferred.** This section pins the _target_ selector model.
> The Phase 1.7 implementation went with a simpler scope per user
> direction: keep the existing parser primitives (`name`, `kind`,
> `owner.id`, `owner.line`) — even though they're drift-prone —
> and handle minifier drift with a per-version port tool when a
> second Tana hash arrives. The richer vocabulary below
> (fingerprint / astPattern / containsString / relational) and
> the bipartite forcing matcher are aspirational. Revisit after
> we have a second Tana version and see whether the simple
> selectors plus diff-based porting suffice. If they do, this
> section may be retired entirely.

Phase 1.7 makes the spec fully explicit: every owned binding has
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

| primitive                  | applies to | semantics                                                                                                                    |
| -------------------------- | ---------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `kind`                     | any        | exact match on `FunctionDeclaration` / `ClassDeclaration` / `VariableDeclarator` / `ImportSpecifier`                         |
| `paramCount: N`            | functions  | exact match on parameter list length                                                                                         |
| `memberNames: [a, b, ...]` | classes    | every name in the list appears as a class member (instance or static; method, prop, accessor)                                |
| `minMembers: N`            | classes    | class has ≥ N members                                                                                                        |
| `superClass: <selector>`   | classes    | the class extends a binding that itself matches `<selector>` (recursive — see [Relational selectors](#relational-selectors)) |

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

#### Removed primitives

These existed in the legacy spec but are dropped or never make
it into the runtime:

| primitive                                     | issue                                                                                                                                                                                            |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `owner.line`                                  | Tana source is prettified; line numbers shift with formatter changes. No semantic stability                                                                                                      |
| `owner.id` (`owner_NNNNN`)                    | opaque sequential index minted by program analysis; carries no identity (see [Identifiers and types](#identifiers-and-types))                                                                    |
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
  entry "ai/mcp/jsx_runtime/JsxRuntime"
    selector: { kind: ClassDeclaration, memberNames: ["render", "create"] }
  reason: after forcing, candidate set is empty.
    Y5 (the only binding with this shape) was claimed by entry
    "mcp/dom/Component" via its more specific `astPattern`.
  resolve by: relaxing this entry's selector, or moving the
    `Component` entry to a different binding so Y5 is freed.
```

```text
SelectorResolution::Ambiguous
  entries: [
    "ai/mcp/jsx_runtime/JsxRuntime"  selector { kind: ClassDeclaration, memberNames: ["render", "mount"] },
    "ai/mcp/dom/Component"           selector { kind: ClassDeclaration, memberNames: ["render", "mount"] },
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
      "kind": "ClassDeclaration",
      "containsString": "instanceof Element"
    },
    "rename": "Component"
  },
  {
    "id": "spec_widget",
    "selector": {
      "kind": "ClassDeclaration",
      "memberNames": ["render", "mount"],
      "superClass": { "renamedName": "Component" }
    }
  },
  {
    "id": "spec_other",
    "selector": {
      "kind": "ClassDeclaration",
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
  fine at Tana's scale.

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

## Cycle resolution

When the validator rejects a cycle, the spec author's only path
is:

**Colocate the cyclically-coupled bindings.** Move every binding
along the cycle into a single module. Once `owner(b)` is the same
for every `b` in the cycle, the cycle's edges (which require
`home(S) ≠ owner(b)`) disappear from `I ∪ S`.

Note: making the back-edge read lazy in the source is **not** a
resolution under the strict gating rule. Lazy reads still produce
`I` edges (the emit must carry the corresponding `import`
directive), so a lazy back-edge still closes an `I` cycle. The
materializer rejects it. The strict rule is deliberate — see
[the realizability theorem](#the-realizability-theorem) — and
the resolution is always colocation.

The validator should suggest the colocation explicitly: "Cycle
through `M_a`, `M_b`, `M_c`. Resolution: colocate {b₁, b₂, b₃}
in one module."

## Architecture

The pipeline is a sequence of stages over a shared `JsPipelineArtifact`:

| Stage                            | Module                                         | Role                                                                                                                  |
| -------------------------------- | ---------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `compute_chunk_metadata`         | <program_analysis.rs>                          | Parse chunks; record top-level decls, imports, side effects, observable module effects. Pure, no spec.                |
| `apply_vendor_annotations`       | <vendor.rs>                                    | Mark vendor packages per `mark_vendor` ops.                                                                           |
| `rename_vendor_exports`          | <vendor.rs>                                    | Rewrite vendor symbol exports per `rename_vendor_symbols`.                                                            |
| `swap_vendor_chunks`             | <vendor.rs>                                    | Substitute vendor chunks with package resolves.                                                                       |
| `materialize_logical_modules`    | <logical_modules.rs> + <schedule_validator.rs> | **Main split.** Computes per-statement facts, applies spec, builds `I ∪ S`, validates, emits modules in source order. |
| `rewrite_chunk_entry_specifiers` | <rewrite_specifiers.rs>                        | Rewrite cross-chunk import paths to be relative to chunk entries.                                                     |
| `write_js_tree`                  | <write_tree.rs>                                | Persist the artifact to disk.                                                                                         |
| `emit_browser_harness`           | <emit_harness.rs>                              | Generate HTML + bootstrap for browser runtime.                                                                        |

Within `materialize_logical_modules`, the substages are:

1. **Spec parsing** → `LogicalRequest` / `ModulePlan` per chunk.
2. **Statement-facts analysis** (<schedule_validator.rs>:
   `analyze_chunk_facts`) → `Vec<StatementFacts>`.
3. **Binding assignment** → `BTreeMap<BindingName, ModuleId>` from
   the spec's explicit member list. Bindings with no spec entry
   default to `ResidualEntry`; nothing pulls implicitly. (See
   [Spec explicitness](#spec-explicitness--closure-as-recommender).)
4. **Module dep graph + validation** (<schedule_validator.rs>:
   `build_module_dep_graph`, `validate_schedule`). The validator
   also emits `recommendations` for every unowned binding so the
   spec author can update the spec.
5. **Cycle resolution gate** — if the validator finds cycles,
   the pipeline aborts with the cycle evidence. (Phase 1: warn-
   only; Phase 3: hard error.)
6. **Source-order emission** — each module's body in source order;
   cross-module imports + source-chunk re-imports; `export { ... }`.
   No init wrappers.

## Empty logical modules

A spec entry that ends up with zero owned bindings (after
closure) and no re-exports comes out as an effectively-empty
file. Either:

- The spec author wrote a `define_logical_module` whose explicit
  members all turned out to be names that don't exist in the
  chunk (typo, stale spec). The validator surfaces a
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

If `mod_a` imports anything from `mod_b` at module-top (say
because the spec assigned a binding `mod_a` reads to `mod_b`),
the cycle `mod_a ↔ mod_b` contains a class extends-clause read.
Class declarations are TDZ-prone, so this fails at module load
with `ReferenceError: Cannot access A before initialization`.

Resolution: colocate `A` and `B`. Pushing the cross-`mod_a`
import inside a function body makes the back-edge lazy but does
**not** break the cycle in `I` — the lazy read still emits an
`import` directive, so `I` still has both edges, the linker
still forms the SCC, and the strict gating rule still rejects.

### Cycle through lazy back-edges (mixed at-init / lazy)

```
// in mod_a — at module-top, EAGER:
import { systemIds as m } from "<mod_b>"; // I-edge mod_a → mod_b
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

This is the shape that surfaced in Tana's live-proxy run as
`workspace/system/bootstrap/command_schema → runtime/vendor/symbols
→ ai/mcp/prompting_runtime → command_schema`. Resolution:
colocate one back-edge's binding with its reader so the `I`
cycle dissolves.

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
- `cross_module_imports_for_body`,
  `source_chunk_imports_for_moved_body`,
  `import_specifier_member_decl`.
- `rewrite_chunk_entry_specifiers` and the rest of the orthogonal
  pipeline.
- Naturalization renames — moved to a clearly-separate
  post-processor pass, not intertwined with init.

What relocates (Phase 1.7):

- `close_module_bindings_over_dependencies` /
  `expand_plan_to_transitive_dependencies` — moved off the
  runtime path into a one-shot migration tool, then deleted.
  Their logic lives on as the validator's recommender side-
  output. See [Spec explicitness](#spec-explicitness--closure-as-recommender).

## Concept consolidation

After Phase 1 landed, the codebase carries two parallel views of
the splitting problem: the **legacy data model** (built around
`binding_assignment` + `ModulePlan` + `selected_by_module` +
init-wrapper bookkeeping) and the **principled data model** (built
around `StatementFacts` + the at-init dep graph). They overlap —
both encode "which module owns each binding" — but in different
shapes and through different code paths. That's a debt: every
future change has to keep both views in sync, and naming drift
between them already obscures intent.

This section pins the consolidation: a single canonical data
structure, names that match the design vocabulary, and an
itemised list of legacy concepts to delete or rename. The
consolidation is a refactor that does not change behaviour but
makes Phase 3 (source-order emit) a small change instead of a
parallel reimplementation.

### The unified schedule

The schedule is keyed per-chunk; the chunk is contextual within
the schedule, so binding keys collapse to just `name`. Keys for
the dep graph extend `ModuleId` with an `ExternalChunk(ChunkId)`
variant so cross-chunk reads are first-class.

```rust
pub enum ModuleId {
    /// A logical module within the current chunk's split.
    Logical(usize),
    /// The synthetic residual-entry module of the current chunk.
    ResidualEntry,
    /// Another chunk we depend on. Edges to `ExternalChunk` mean
    /// "we read a binding owned by that chunk at init time."
    ExternalChunk(ChunkId),
}

pub struct Schedule {
    /// Identity of the chunk this schedule was computed from.
    pub chunk: ChunkId,
    /// Per-statement analysis (one entry per top-level statement
    /// in the source chunk, in source order).
    pub facts: Vec<StatementFacts>,
    /// All bindings introduced in the chunk's top-level scope,
    /// keyed by their local name (unique within a chunk).
    pub bindings: BTreeMap<String, BindingKind>,
    /// Logical modules from the spec (paths, ids, rename maps).
    pub logical_modules: Vec<LogicalModule>,
    /// Module dep graph `I ∪ S` (linker imports + side-effect
    /// ordering). Nodes are `ModuleId`s; edges are emitted-import
    /// references or side-effect-order constraints. Acyclicity
    /// of this graph is the realizability gate.
    pub dep_graph: ModuleDepGraph,
}
```

Everything downstream needs is here:

- `home(stmt)`: for statements with `declared(stmt) ≠ ∅`, look
  up any declared name in `bindings` — its `Owned.owner` is
  `home`. For statements with empty `declared`, `home` is
  `ResidualEntry`.
- "What statements live in module M" = `facts.filter(home == M)`.
- "What does M export" = bindings whose `Owned.owner == M`
  (under their original or rename-pass-rewritten name) plus
  bindings whose `Imported.re_exported_by` has key `M` (under
  the public name that map's value gives).
- "What imports does M need" =
  - For each `b ∈ reads_at_init(stmt)` with `home(stmt) == M`:
    - If `b` is `Owned { owner: other }`: `import b from <other>`.
    - If `b` is `Imported { imported_from, imported_name, .. }`:
      `import { imported_name as b } from <imported_from>`.
- "Identity of cross-chunk deps" = `bindings.values()` filtered
  to `Imported { imported_from, .. }` give us the set of
  external chunks our schedule talks to; that's the
  `ExternalChunk(_)` nodes in `dep_graph`.

The two reasons to make `BindingKind` explicit (rather than
collapsing imports into the same `Owned` map):

1. **Re-export semantics aren't ownership.** A logical module
   that re-exports `import { Y as X } from "<vendor>"` does
   not own `X` — modifying our spec to "claim" `X` should not
   rename `Y` in the vendor chunk; it should emit a re-export
   in our logical module. The legacy `binding_assignment` of
   `X → mod_x_idx` lied about this, and the parallel
   `import_members` channel patched around it.
2. **Multiple modules can re-export the same imported binding,
   under different names.** Two logical modules in the same
   chunk could both want to surface `vendor.j` (the JSX runtime),
   one as `jsxRuntime` and another as `__jsx`. With
   `Owned { owner }` the data model forbids this entirely; with
   `Imported { re_exported_by: BTreeMap<ModuleId, BindingName> }`
   each module picks its own export name independently.

### Old → new vocabulary

| Legacy name                                                                                                                                                                 | Status                                                      | Replacement                                                                                                                                                                                                                                                       |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `binding_assignment: BTreeMap<String, usize>` (key = scrambled local name, implicit per-chunk; value = module index)                                                        | Replaced                                                    | `Schedule.bindings: BTreeMap<String, BindingKind>` (key = local name in chunk's top-level scope, explicit per-chunk via the surrounding `Schedule.chunk`; value distinguishes `Owned { owner }` from `Imported { imported_from, imported_name, re_exported_by }`) |
| `ModulePlan`                                                                                                                                                                | Trimmed and renamed → `LogicalModule`                       | Drop the `bindings` and `import_members` fields; both are derivable from `Schedule.bindings`                                                                                                                                                                      |
| `ModulePlan.bindings: BTreeMap<String, String>`                                                                                                                             | Split                                                       | `LogicalModule.rename_map: BTreeMap<String, String>` (only the local→public renames; ownership is in `Schedule.ownership`)                                                                                                                                        |
| `selected_by_module: BTreeMap<usize, Vec<ModuleItem>>`                                                                                                                      | Derivable                                                   | Project `Schedule.facts` by `home`                                                                                                                                                                                                                                |
| `selected_exports_by_module: BTreeMap<usize, BTreeMap<String, String>>`                                                                                                     | Derivable                                                   | Project `Schedule.ownership`                                                                                                                                                                                                                                      |
| `is_plain_import_safe_initializer`                                                                                                                                          | **Delete**                                                  | The dep graph answers the real question (does this read another module's binding at init?)                                                                                                                                                                        |
| `is_plain_import_safe_propname`                                                                                                                                             | **Delete**                                                  | Same                                                                                                                                                                                                                                                              |
| `var_requires_init_wrapper_for_module`                                                                                                                                      | **Delete**                                                  | Same                                                                                                                                                                                                                                                              |
| `item_requires_init_wrapper_for_module`                                                                                                                                     | **Delete**                                                  | Same                                                                                                                                                                                                                                                              |
| `init_required_modules`                                                                                                                                                     | **Delete**                                                  | No init wrappers                                                                                                                                                                                                                                                  |
| `initialized_module_body`                                                                                                                                                   | **Delete**                                                  | Source-order emit replaces it                                                                                                                                                                                                                                     |
| `push_initialized_var_decl`                                                                                                                                                 | **Delete**                                                  | Same                                                                                                                                                                                                                                                              |
| `init_dep_names_for_body`                                                                                                                                                   | **Delete**                                                  | ESM imports express the dep graph natively                                                                                                                                                                                                                        |
| `assignment_statement_for_declarator`                                                                                                                                       | **Delete**                                                  | Same                                                                                                                                                                                                                                                              |
| `init_flag_name` / `init_flag_decl` / `idempotency_guard_stmt` / `set_flag_stmt` / `init_call_stmt` / `export_init_function` / `init_call_statement` / `init_name_for_plan` | **Delete**                                                  | All idempotency-guard / wrapper helpers                                                                                                                                                                                                                           |
| `__dt_generated_init__*` symbol scheme                                                                                                                                      | **Delete**                                                  | Dropped from the emit                                                                                                                                                                                                                                             |
| `cross_module_imports_for_body`                                                                                                                                             | Keep, take `Schedule`                                       | Build cross-module imports from the dep graph                                                                                                                                                                                                                     |
| `source_chunk_imports_for_moved_body`                                                                                                                                       | Keep, take `Schedule`                                       | The "moved code references a source-chunk import" case is just a cross-module read where the owner is a sibling chunk                                                                                                                                             |
| `import_specifier_member_decl` / `ImportSpecifierMember` / `resolve_import_specifier_member` / `lookup_import_specifier`                                                    | Trimmed                                                     | An "ImportSpecifier-bound member" is just a binding that's owned by a different chunk; ownership map handles it                                                                                                                                                   |
| `LogicalRequest` / `MemberRequest`                                                                                                                                          | Keep                                                        | Spec-language AST, distinct from the resolved `Schedule`                                                                                                                                                                                                          |
| `close_module_bindings_over_dependencies` / `expand_plan_to_transitive_dependencies`                                                                                        | **Delete** after Phase 1.7                                  | Replaced by the validator's recommender side-output (`AssignmentRecommendation`); the spec becomes fully explicit, the runtime never pulls implicitly. See [Spec explicitness](#spec-explicitness--closure-as-recommender)                                        |
| `naturalize_module_body` / `IdentifierRenamer` / `ShorthandNaturalizer`                                                                                                     | Keep, separate phase                                        | The readability rename pass is orthogonal to scheduling; it consumes `Schedule.modules[*].rename_map` and rewrites identifiers in the emitted source                                                                                                              |
| `collect_referenced_idents` (the old visitor)                                                                                                                               | Replace with `StatementFacts::reads_at_init`-style visitors | `StatementFacts` already gives the eager-vs-lazy split; the old collector returned the union which is wrong for the dep graph                                                                                                                                     |

Roughly: the legacy file has ~1500 LOC, of which ~500 LOC of
init-wrapper/heuristic machinery deletes outright once Phase 3
lands, plus ~200 LOC of closure-pass machinery deletes after
Phase 1.7. The remainder splits cleanly into spec parsing
(`logical_requests_for_chunk`), schedule construction (just
collecting the spec's explicit `owner` entries into
`Schedule.bindings`), and emission (`lower_chunk` rewritten as
`emit_module(schedule, module_id)`).

### Identifiers and types

The legacy code types most identifiers as raw `String`. That's
hidden untyped state. Three concrete failure modes have surfaced:

1. **Same-shape strings, different things.** A chunk id
   (`"static/index-DI2GynTv"`), a logical-module path
   (`"ai/mcp/prompting_runtime"`), and a destination file path
   within a chunk (`"runtime/vendor/symbols.js"`) are all
   `String`. Every function that takes one of these has a
   plausible-looking signature even when called with the wrong
   one. The cross-chunk import-path drift in PR #1473 was
   exactly this — we passed a `chunk-relative path` where a
   `chunk id` was expected and the rewriter silently produced a
   wrong string.

2. **Opaque numeric ids.** `program_analysis.rs:151` mints
   `format!("owner_{:05}", owners.len())` — a stringified
   sequential index of "the Nth top-level decl in this chunk's
   source order." The gaffer spec then references these as
   `selector.owner.id = "owner_03565"`. Two problems:
   - The string is meaningless. `owner_03565` doesn't tell a
     reader anything about what binding it is. They have to
     grep the chunk-analysis output to find out.
   - It's drift-sensitive. If the chunk source changes, every
     decl after the inserted point shifts ordinal, and every
     spec entry that referenced anything past the change
     breaks. The spec already has two un-ambiguous handles
     (the binding name and the source line) — the synthetic
     `owner_NNNNN` ID adds indirection without identity.

3. **Stringified module ids.** `ModulePlan.id =
format!("logical__{}", path.replace('/', "_"))` is then used
   as the suffix of `__dt_generated_init__` symbols, as a
   key in the report JSON, and as a referent for cross-module
   relationships in spec output. After Phase 3 the init-symbol
   use disappears; the remaining uses (report keys, references)
   want a stable id that's distinct from the path.

The rule going forward:

> If a string identifies a thing of a known kind, it gets a
> newtype. Untyped `String` is reserved for free-form text
> (error messages, log lines, the actual JavaScript identifier
> _as text_).

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
/// `ai/mcp/prompting_runtime`. Does *not* include `.js`.
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

`ModuleId` (already in <schedule_validator.rs>) is a tagged
union over these:

```rust
pub enum ModuleId {
    Logical(LogicalModuleIndex),
    ResidualEntry,
    ExternalChunk(ChunkId),
}
```

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
- Gaffer spec entries that currently set `owner.id =
"owner_03565"` are noise — the validator will detect that
  the binding identified by `binding.name = "m"` is the same
  one the `owner.id` referenced, so the field can be dropped.
  Phase 4 cleanup includes a sweep through gaffer to remove
  dead `owner.id` fields.

### Refactor sequence

The consolidation runs entirely on the ducktape side and does not
touch the gaffer spec or its smoke. It can land before Phase 2
(spec cleanup) and is recommended as such — Phase 2 is data-driven
by the schedule, and a clean schedule API makes those edits easier.

1. Introduce `Schedule` and `ModuleId` as new types in
   <schedule*validator.rs>; re-export from there. *(Done — Phase
   1.6.1–1.6.3.)\_
2. In `materialize_logical_modules`, build a `Schedule` once
   after the binding assignment settles. _(Done — Phase 1.6.3;
   `binding_assignment: BTreeMap<String, usize>` still lives
   alongside as a compatibility view until 1.7 deletes the
   closure pass.)_
3. Migrate `validate_schedule`, `cross_module_imports_for_body`,
   `source_chunk_imports_for_moved_body`, and
   `init_dep_names_for_body` to take `&Schedule`. _(Done — Phase
   1.6.4 for the first two; the third dies in Phase 3.)_
4. Phase 1.7: drop the closure pass from the runtime path
   entirely (replaced by recommender side-output); the gaffer
   spec is mechanically rewritten so every binding has an
   explicit owner. After 1.7, `binding_assignment` is just the
   spec's explicit map — no implicit transformation.
5. Migrate `lower_chunk` last. After it consumes `Schedule`, the
   old `selected_by_module` / `selected_exports_by_module` locals
   disappear.
6. Phase 3 is then small: replace the init-wrapper branch in
   `lower_chunk` with source-order emit. Both branches read the
   same `Schedule`, so the change is local.
7. Phase 4 deletes the marked-Delete legacy functions in one
   sweep.

Each step keeps the build green and the tests passing. No big-
bang refactor; no spec churn until Phase 2.

## Status

| Phase | Description                                                                                                                                                                                                                                                                  | State                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| ----- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1.1   | `StatementFacts` analyzer producing `(declared, reads_at_init, has_side_effect, kind)` per statement                                                                                                                                                                         | **Done** (<schedule_validator.rs:69>; 7 unit tests pass)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| 1.2   | `ModuleDepGraph` builder (`I ∪ S`)                                                                                                                                                                                                                                           | **Done** (<schedule_validator.rs>). The Phase 1 builder constructed only `R ∪ S` (at-init reads + side-effect ordering); the Phase 5 fix extended it to walk `reads_lazy` too, producing the full imports graph `I`.                                                                                                                                                                                                                                                                                                                                                                                           |
| 1.3   | Tarjan SCC validator + JSON report                                                                                                                                                                                                                                           | **Done** (<schedule_validator.rs:283>)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| 1.4   | Wire the validator into `materialize_logical_modules` as a report-only side output                                                                                                                                                                                           | **Done** (<logical_modules.rs>: emits `<chunk_id>.schedule.json`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| 1.5   | Run against Tana spec; record every cycle                                                                                                                                                                                                                                    | **Done.** Spec produces 1 SCC of 9 modules, 99 edges (see [Phase 1 findings](#phase-1-findings-tana-78d928dca7)).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| 1.6.1 | `LogicalModuleIndex` + `ModuleId` newtypes; drop `RESIDUAL_ENTRY_INDEX` sentinel                                                                                                                                                                                             | **Done** (<schedule_validator.rs>; validator + caller migrated)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| 1.6.2 | `BindingName` type alias + `StatementOrdinal` newtype threaded through validator API                                                                                                                                                                                         | **Done** (<schedule_validator.rs>; `ModuleDepGraph::evidence` + `CycleEdge` typed)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| 1.6.3 | `Schedule` type with `BindingKind::Owned` + `LogicalModule`; validator goes through `Schedule::validate`                                                                                                                                                                     | **Done** (<schedule_validator.rs>; <logical_modules.rs:317> constructs `Schedule` after closure)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| 1.6.4 | Migrate `cross_module_imports_for_body` + `source_chunk_imports_for_moved_body` to `&Schedule`; widen `LogicalModule` with `target_file` + `rename_map`; add `Schedule::owner_of` + `logical_module` accessors                                                               | **Done** (<logical_modules.rs>; `init_dep_names_for_body` left for Phase 3 deletion)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| 1.6.5 | Migrate closure pass to `&mut Schedule` with cycle-aware refusal                                                                                                                                                                                                             | Folded into Phase 1.7 — the closure leaves the runtime path entirely; cycle-aware refusal becomes recommender output, not closure mutation                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| 1.7   | Spec-explicitness migration: validator emits `recommendations` for unowned bindings; one-shot tool rewrites gaffer spec with explicit owners; drop `close_module_bindings_over_dependencies` from runtime                                                                    | **Done.** 6283 closure-pulled bindings made explicit in gaffer (`closure_pulled_post_migration.mjs`); closure pass deleted; migration tool retired; selector vocabulary stays at today's `name`/`kind`/`owner.id`/`owner.line` per scope decision (rich vocabulary deferred — see <#selector-vocabulary-and-matching>)                                                                                                                                                                                                                                                                                         |
| 2     | Update gaffer spec to break each surfaced cycle                                                                                                                                                                                                                              | **Done.** Agent-driven sweep moved 150+ bindings across 14 commits (gaffer 6ed00ca); SCC eliminated. Edge trajectory: 99 → 90 → 79 → 67 → 64 → 56 → 48 (SCC 9→8) → 37 → 31 → 27 → 23 → 22 → 19 → 8 (SCC 8→3) → 5 (SCC 3→2) → 0                                                                                                                                                                                                                                                                                                                                                                                 |
| 3     | Switch emit to source-order; drop init-wrapper machinery                                                                                                                                                                                                                     | **Done** (ducktape 6ef003f5c, gaffer 85f240f). Validator gate hardened to `bail!` on cycles; `lower_chunk` source-order only; ~525 lines of init-wrapper helpers deleted; `promise_validator_rejects_cycle_test` + `promise_source_order_emit_test` un-ignored and pass; Tana bundle has 0 `__dt_generated_init__`                                                                                                                                                                                                                                                                                             |
| 4     | Cleanup: remove legacy code, update e2e fixtures, update AGENTS.md; collapse `import_members` into `BindingKind::Imported`                                                                                                                                                   | **Done.** Doc + Phase-N comment cleanup landed (ducktape `1533788bd`, `ef0a7a75e`). `BindingKind::Imported` migration landed (ducktape `04220beb2b04`, gaffer `fccd978`): ImportSpecifier-bound re-exports now flow through `Schedule.bindings`; multi-module re-export of the same source-chunk import works (`promise_multi_module_re_export_test` un-ignored and passing)                                                                                                                                                                                                                                   |
| 5     | **Realizability gate over `I`, not `R`.** Extend `build_module_dep_graph` to add an edge for every cross-module reference (at-init or lazy) — i.e. compute `I`, not just `R`. Switch SCC to `petgraph::algo::tarjan_scc`. The validator becomes a true realizability oracle. | **Done** (Phase 5 PR). `build_module_dep_graph` walks both `reads_at_init` and `reads_lazy`. SCC computation switched to petgraph, replacing a hand-rolled iterative Tarjan whose lowlink propagation was unsound. `cycle_safe` recommender flag now runs the SCC simulation for `LazyOnly` candidates too. `AtInitReadCollector` no longer counts export-specifier idents as reads. Synthetic e2e fixture (`lazy_back_edge_cycle_test`) covers the regression shape; the proof of the realizability theorem (assumptions A1–A7 + lemmas L1–L5) is in [The realizability theorem](#the-realizability-theorem). |

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

## Known sharp edges

These are concrete gaps, oversimplifications, or unsound corners
that aren't yet folded into the design body. Pinning them so they
don't bite during Phase 1.6 / Phase 3. (Items resolved by design
tweaks have been moved into the relevant body sections; this list
is the residual.)

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
If we hit such a case in real Tana, surface it as a
"spec-author-side known limitation."

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

Action: add a unit test per case to <schedule_validator.rs>;
fill the visitor's gaps. Aim for an exhaustive table.

#### Side-effect classification is conservative

Right now `has_side_effect = true` for any non-pure expression
(function calls, member access on possibly-mutating objects,
etc.). That over-imposes side-effect-order edges in `G'`,
potentially creating cycles that aren't really there.

For Tana this hasn't bitten yet because the SCC we found is from
real `reads_at_init` edges, not side-effect edges. But the next
spec we look at could have `G'`-only cycles that are entirely
spurious (two side-effecting modules that don't actually
observe each other). Pure-call inference is the long-term fix;
pragmatic short-term: only add `G'` edges when both statements'
side effects are _observable_ (write to global, throw, console,
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

### Validator's incomplete view today

Phase 1's validator runs on the **legacy `binding_assignment`**,
which only includes `Owned` bindings — `ImportSpecifier`-bound
locals are tracked through the parallel `import_members` channel
and _not_ fed to the validator. So the SCC the validator just
reported (9 modules, 99 edges) is a **lower bound** on the
cycles in the Tana spec.

There may be more cycles that pass through ImportSpecifier-bound
bindings (e.g. a re-exported vendor binding that one logical
module reads from another logical module's re-export). Phase
1.6's data-model unification will give the validator full
visibility; the cycle list might grow then.

This matters for Phase 2: don't declare "spec is acyclic" until
the validator is running on the unified `Schedule`. The current
report is useful for surfacing the obvious 9-module SCC, not
for proving an absence of cycles.

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
   may flag more recommendations than strictly necessary. Pure-
   call inference is future work.
3. **Recommender determinism on ties.** When binding `B` is read
   only by module `M`, the recommendation is unambiguous: `B → M`.
   When `B` is read by multiple modules `{M₁, M₂}`, all
   cycle-safe, the recommender has to either pick one (loses
   determinism between author and tool) or surface the choice for
   human resolution. Leaning toward surfacing — the recommender's
   value is in being explicit about ambiguity, not hiding it.

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
