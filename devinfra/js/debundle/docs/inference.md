# Shared inference

`inference.rs` defines `Judgment`; `inference/iterative.rs` provides the
dependency worklist (`IterativeInference`) for finite growing facts and
positive certificates. `facts/semantic_inference.rs` composes native,
construction, and callable rules in that store. Structural analysis supplies
candidates; candidates alone cannot certify an operation. Rules use typed
`FactContext` access so an enclosing rule family can project their keys without
copying a private summary or hiding a read from the worklist.

Native premises are flags, not judgments. `facts/abstract_domain/integrity.rs`
answers `integrity(slot)` for an intrinsic slot (`%Set%`, `%Object.freeze%`,
`%Array.prototype%`, `%ArrayIteratorPrototype%`) from the completed census and
the `IntrinsicIntact` axioms resolved from validated author inputs; rules and
builtin rows read those flags without a key of their own. The current rules
compose as follows:

```mermaid
flowchart LR
    census[Completed native census] --> flags[Slot integrity flags]
    inputs[Validated author inputs] --> flags
    flags --> calls[Guarded native call summaries]
    flags --> owndata[Native own-data read]
    shape[Canonical source shapes] --> origin[Own-property source origin]
    origin --> ownshape[Own-property static shape]
    ownshape --> prefix[Own-property prefix state]
    calls --> projection[Own-property projection]
    prefix --> projection
    projection --> names[Own-name and descriptor results]
    flags --> class[Local class construction]
    flags --> set[Primitive Set iteration]
    flags --> spread[Stable primitive array spread]
    shape --> stable[Stable source function]
    stable --> provider[Stable provider]
    stable --> noescape[Argument no-escape]
    inputs --> noescape
    stable --> callback[Returned callback factory]
    stable --> record[Returned record]
    flags --> record
    stable --> wrapper[Namespace wrapper]
    flags --> wrapper
    census --> dcprovider[Descriptor-copy provider]
    inputs --> dcprovider
    dcprovider --> dcfactory[Descriptor-copy factory call]
    shape --> dcfactory
    shape --> class
    shape --> set
    shape --> spread
    class --> consumers[Purity, synchronous execution, heap ordering]
    set --> consumers
    owndata --> consumers
    spread --> consumers
    noescape --> consumers
    provider --> consumers
    dcfactory --> consumers
```

A slot is intact only when the census is closed (every analyzed chunk
contributed, no dynamic scope) and no census write reaches it (SPEC.md § Native
integrity inputs): an own data slot by the write's receiver and key, a
prototype slot by its family or the global object. The `Function` family is
included so source-visible changes to `Function.prototype.call` can invalidate
only proofs that consume that capability. Native prototype crossings,
`Object.getPrototypeOf` result aliases and global-slot read-backs retain an
opaque native root: a later write through one reaches its key on every native
object rather than trusting the original receiver's family.
An axiom covering every requested slot at the queried site is consumed instead
of the census and recorded as that input's proof dependency.

## Slot-write closure

`OwnSlotWrites::resolve` uses `IterativeInference` over positive may-invalidation
facts keyed by `IntrinsicSlot`. Its inputs are the completed mutation witnesses,
unwitnessed mutated families and immutable whole-prototype flags. The existing
write transfer (`OwnSlotWrites::add`) supplies both inference and diagnostics;
there is no second interpretation of a witness.

Prepare the finite query set `Q` from every constructor/prototype receiver's
global binding and every computed-key derivation's own-slot dependencies. Those
are exactly the mutable slot identities the transfer reads. Whole-prototype
dependencies are already closed inputs. Seed the slots that unwitnessed family
mutations invalidate. Register one rule per reachable witness:

1. Read its receiver/key identity facts through `FactContext`. Absence means
   provisionally intact for this transfer, and installs a watcher.
2. Apply the existing transfer to that one witness. Losing an identity widens
   a receiver or key; it never narrows the slots that the write may reach.
3. Propose each slot in `Q` reached by that contribution. A fact is a unit
   proposition: its join is idempotent, and it can be inserted only once.

After closure, apply each witness once against the completed identities to
produce the existing full write surface. Consumers and `CensusExplanation` read
that closed view. Querying another property does not start another solver.
Intact-slot certificates never escape from an intermediate absence. Unsupported
receivers, unknown keys, prototype coupling and open-census refusals keep their
existing treatment. This migration does not add callable or callback precision.

**Correctness.** Let `p(W)` be the slots of `Q` invalidated by a write surface
`W`, and let `F(W)` be the previous round transfer: unwitnessed base writes plus
all witness transfers evaluated against `W`. Every mutable read of `F` is in
`Q`, so `F(W)` depends on `W` only through `p(W)`. Thus it factors as
`F(W) = G(p(W))`, with the immutable prototype inputs held fixed. The growing
solver computes the closure of `p(base) ∪ p(G(I))` over invalidation sets `I`.
Each receiver/key widening preserves the previous denotation of possible writes,
so this map is monotone. Induction on the old rounds and on individual rule
firings gives the same least invalidation closure. Complete watchers, including
absent reads, ensure every newly invalidated input is consumed. The final
`G(I)` projection therefore gives the same reached-slot answers and witness
interpretation as the old closure, without depending on rule order. This is a
preservation argument for this phase, not a new proof of every census transfer.

**Cost.** Let `W` count witnesses, `K = |Q|`, and `S` count explicit census
bytes, including target/key/dependency entries and key strings. There are at
most `K` fact insertions and `WK` registered dependencies. Each rule runs once
initially and at most once per watched slot insertion: at most `W(K + 1)`
evaluations. A transfer builds only its witness's contribution, and its `K`
own-slot probes scan only that contribution. With the existing ordered sets,
preparation, solving and final projection take
`O((K + 1)S(log(S + 2) + K) + W(K + 1)log(W + 2))` time and
`O(S + WK)` space. String comparisons are charged to the inserted strings;
queried names are the fixed intrinsic names and derivation slots, not generated
strings. For the fixed native-slot schema, `K` is constant, giving
`O(S log(S + 2))` time and `O(S)` space. A new dependency must extend `Q` and
this bound. There are no fresh identities, path enumeration, callback execution
or whole-world snapshots in this closure. The solver's existing test-only rule
counter checks that invalidating one constructor revisits its readers while an
unrelated constructor's readers remain untouched.

## Bundle-owned-global closure

`execution_index/bundle_globals.rs` resolves bundle ownership across all prepared
chunks on one `IterativeInference` store. This is conditional on the existing
`predefined_globals` host premise. The candidate names, registered-symbol
category and fresh-collection premise keep their existing meaning (SPEC.md
§ Bundle-owned globals); the closure does not infer callable availability.

Prepare declared bindings, assignments, fresh-value shapes, symbol-key bindings,
property-key evidence and accessor-premise matches once per chunk. Collect the
candidate universe with the ordinary closed alias transfer. Then close aliases
once under the optimistic candidate set. Both preparations use
`intrinsic_roots_read`, the same root transfer as the native census. Its input
adapter supplies closed maps during preparation and watched reads during solving.

The growing facts are invalidations of a candidate name, registered-symbol
ownership, collection freshness, a chunk's `Symbol.for` key evidence, or a
chunk-qualified alias. An `All` fact invalidates every global category without
materializing one fact per name. A separate rejected-chunk fact controls final
premise projection. Every key is input-derived; joins are unit propositions.
Register rules for assignments, native writes and possible legacy accessor calls:

- Global-property reads watch only the candidate category they consume, plus
  `All`. A constructor allocation watches collection freshness.
- An alias rule propagates a newly possible opaque root through its assignment.
  Alias readers watch the corresponding chunk-qualified key, including when
  its invalidation is still absent. No rule restarts an alias solver.
- A write or accessor rule consumes those roots and its prepared key evidence,
  preserving the existing widening, deletion and refusal cases. A refusal
  contributes `All` and the responsible chunk's rejection.
- Key-to-name matches are indexed during preparation. A write's value is
  transferred once to `Ownership::{Any, SameName, None}` and shared by all its
  matching names. `SameName` preserves the sloppy self-slot read exception;
  alternative values meet these constraints. Registered-symbol stores require
  `Any`.

Only the completed closure becomes `BundleGlobalNames`. Accessor author
references are projected once using the final roots and key evidence; a rejected
chunk contributes none, exactly as an early refusal in the local transfer did.
Source facts and author assertions retain their separate ownership. Existing
consumers continue to attach the projected references to their proof dependencies.

**Preservation.** With no ordinary-value or callable-return inputs, the raw
alias transfer has a fixed finite native-root vocabulary. Changing candidate
ownership can replace `BundleGlobal` with `UnknownGlobalValue`; it cannot create
a different native identity. Empty root sets stay empty. A reflection call's
`Object` receiver test is therefore invariant, and its admitted result already
contains the opaque root. These observations follow by induction over the
canonical value expression and the assignment closure.

Consequently, an optimistic alias that already contains an opaque root never
needs refinement. For each other alias containing `BundleGlobal`, one growing
bit suffices to record whether it can acquire that opaque root. The watched
adapter retains its optimistic roots and adds the opaque root when this bit is
set. Retaining an extra `BundleGlobal` token does not affect either consumer
predicate: membership of a concrete native identity, or whether all roots are
bundle-owned. The nonempty predicate used for stored values is also preserved.
Thus this representation gives the same ownership decisions as recomputing the
entire alias closure after every loss of a global premise.

Write, key and accessor transfers only lose ownership as these invalidations
grow. Their complement is a monotone operator on a finite set of bits. The old
nested local/program loops and the joint worklist impose the same constraints:
each chunk must respect every other chunk's invalidations, and each assignment
must propagate their effects to its consumers. Closing local constraints before
a program round or interleaving individual constraints reaches the same least
invalidation closure. Complete watchers, including missing reads, ensure no
constraint is left stale. The final ownership and accessor-reference projection
therefore preserves the previous completed result, including conservative
refusals. This is a phase-preservation argument, not a proof of every census
recognizer or host assumption.

**Bounds.** Let `S` be the explicit prepared census and assignment syntax size,
including source names, key expressions, premise references and chunks; aliases
remain references rather than inlined expressions. Let `K` count possible
invalidation keys, `R` rules and `T` prepared write-key/name matches. Then
`K,R = O(S)` and `T = O(S²)`. The native-root vocabulary in this phase is fixed.
Property-key preparation walks shared binding graphs without enumerating paths;
string affixes come from source literals/templates rather than generated string
combinations. The reflection guard and member transfer share their receiver
result, so nested reflected callees do not duplicate child evaluation.

The two initial alias closures, key evidence, candidate collection and indexed
key matches cost `O(S² log(S + 2))` time and `O(S²)` space. Each key can be
inserted once, there are at most `RK` watchers, and each rule runs initially and
at most once per changed key it has read: at most `R(K + 1)` evaluations. Summed
transfer work per complete sweep is `O(S + T)`: expressions are visited once per
transfer and a write shares its value result across all matching keys. With
ordinary hash-table lookup costs, preparation, solving and projection take
`O(S² log(S + 2) + (K + 1)(S + T)log(S + 2))` time and `O(S²)` space. Even
pathological collisions in the existing hash-based fact store only multiply
lookup work by `O(K)`, yielding a polynomial worst-case bound of `O(S⁴ log(S + 2))`.
There are no generated execution identities, subset contexts or callback runs.
The test-only evaluation counter checks the exact `3w + 1` rule bound when one
of two independent width-`w` alias groups loses its global premise.

## Consumers

A native callee's identity is its spelling (the unshadowed, undeclared global
binding of the row's family, or an unshadowed `globalThis` qualifying it) plus
the flags of the row's `requires` slots at the call. Empty collection
construction is that identity for a zero-argument keyed-collection constructor;
local class construction requires it for every native field allocation. Primitive Set
iteration requires the Set and Array iterator slots and a checked source shape.
The original restrictions on class identity, construction sites, arguments,
primitive elements, mutation and escape remain in their structural analyses.
Returned records and inferred callback factories are projections of a
derived foreign call summary (`facts/abstract_domain/foreign_call.rs`), not
certificates. Each certificate
records which native premises it consulted, so its report names
`native_call_identity`, `native_integrity` and `empty_collection_construction`
exactly when the census established them.

A native own-data read certifies one exact original `Object.<name>` own-data
member read from the `%Object%` and `%Object.<name>%` flags. Stable primitive
array spread requires the checked multi-declarator source shape and the Array
iterator flags. Stable provider and argument no-escape both require the
stable-function judgment on a hoisted, never rebound binding; argument
no-escape additionally requires a validated `argument_inputs` reference. A
descriptor-copy provider requires the closed census, exactly one authored
factory signature and that author reference; a descriptor-copy factory call
requires its provider plus the candidate call's exact signature: one object
argument, every eager own-data key present and no assigned data key backed by
an accessor.

Own-property inspection is a direct producer in the shared runtime fact
context, keyed by the query's program point. It reads completed semantic
judgments and the selected object's current prefix shape and property state,
then projects names or descriptors. The producer forwards the judgments'
typed proof dependencies; it does not rebuild trust from a report. A terminal
projection is consumed with `take_final` after its dependencies are recorded.
No prefix fact survives a later mutation. Name projection follows ECMAScript
own-string-key ordering, while descriptor projection retains current data
identities and accessor closures without invoking them.
Namespace-wrapper source checks can request the same ordinary prefix-state
fact without a native call key; the consumer composes that origin with its
separately guarded provider proof.

Native call identity feeds the builtin summary table,
`facts/abstract_domain/builtins.rs`. Once identity holds for a call's
callee, the interpreter looks up the operation's row and admits the call only
when the row's arity holds, it invokes nothing, every write it lists is proved
on the call's operands, and every coercion it lists lands on a primitive or
proven ordinary operand. The row's `returns` and `allocates` columns then give
the value lane its result, and the keyed-collection rows give empty collection
construction its allocation model. The table is the only place that states
what a builtin does; identity establishes only which builtin it is.

The namespace summary is conditional on an ordinary input and an exact literal
`true` second argument. Its source shape preserves the private WeakMap cache,
default reference and deferred forwarding getters. Shared rules establish the
function's identity, native cache allocation, Object operations, Array iteration
and Function call integrity. The caller retains an opaque result and its input
callback graph, including possible cached aliases and throws. It gains no
purity, fresh-result or exception-free guarantee.

The loader-closed fact (`loader_closure.rs`, SPEC.md § Loader-closed
derivation) is a program-level family in its own store: `Reach` grows each
chunk's static and loader closure, and each target's verdict grows its failure
witnesses with it. A target is closed only in the completed closure; no rule
consumes an intermediate verdict. A closed verdict assumes the consumed
`host_behavior` keys as typed `ProofDependencies`, so an author host assertion
stays distinct from the source facts.

## Context and results

The rule context owns compact source facts and the resolved native census from
the same preparation. It does not parse or walk source again. A changed program,
author input, import summary or analysis policy requires a new context; no cache
is shared by path or module name. Prepared native-call, Set and class proofs
share one `Arc<SemanticFacts>`, including the heap projection of the class
proof.

A `Judgment` has private construction. It contains a fact, direct premise keys,
and the union of captured-input dependencies used to establish it. Reading a
premise through the rule context automatically records both the premise key and
its dependencies. Semantic certificates use `GuardedContext::finish` around
positive `require` calls; any absent requirement prevents publication, while a
later contribution wakes the consumer. Author and runtime assumptions enter through
`assume`; an author assertion remains distinct from an inferred source fact.
Consumers of completed judgments carry their actual typed
`ProofDependencies` forward with `assume`. Reports are explanatory projections
only and are never decoded back into trusted judgments.
An evaluator cannot publish a fact after requesting an unproved prerequisite,
even if it accidentally returns a successful value.

Native flags require a closed write census. Earlier callback-argument
analysis can derive source-only callable facts while every flag is still
unknown; it cannot use an empty, unfinished mutation set as proof of
integrity.

The typed key names the judgment, rather than a universal "safe" boolean.
Consumers still make purpose-specific decisions: an allocation certificate does
not suppress callee or argument evaluation, establish a no-throw guarantee, or
make a later method call pure. Independent builtin whitelist admissions retain
their own runtime assumptions.

## Scheduling

`IterativeInference` owns the facts and reverse watchers. Producers read typed
keys and join contributions; both missing and present reads install watchers.
Multiple producers can contribute to one fact. Changes to values or captured
input dependencies wake consumers. Source-ordered rounds retain the
conservative initial-sweep semantics of source analyses. Growing dependency
families, including cross-module function effects and recursive purity, still
run to closure in their worklist. Only a completed closure is exposed outside
a producer.

### Shared source binding graph

`program_bindings::ProgramBindings` is prepared once by program-summary
construction from the existing resolved named/default imports and local export
maps. It borrows that prepared linkage during construction and performs no AST
census or path resolution. Primitive-value sources contribute any additional
export references they consume. Stability, primitive-export propagation and both
callable-summary projections use this same graph; purity classification consumes
the already completed stability result instead of solving it again.

The graph interns two distinct identity spaces:

- `ExportId` names one source-qualified `(module, public export)` reference,
  including an unresolved provider reference.
- `BindingId` names one source-qualified `(module, local)` cell. Local export
  aliases share that cell. An imported local has an edge to its provider export;
  a local initialized from an import has its own cell instead.

Import targets, local export cells and reverse importer lists are prepared once.
They supply linkage only: an ID is not evidence of a value, stable binding,
initialized cell, purity or callable capability. Primitive copy dependencies
remain value-flow edges, with their existing source-cell provenance; they never
become live-alias edges. Stability still follows only local re-exports of
imports, and callable projection still requires the provider's existing proof.
Direct source re-exports, namespace members and unresolved routes gain no new
stability or callable certificate.

**Preservation.** Export interning is injective in the old `(module, export)`
keys; local interning is injective in `(module, local)`. Every primitive copy
read and callable import edge is relabeled with the corresponding ID, without
changing its rule, proof dependencies or join. The stability rule uses exactly
the previous local-export/import edge and immutable/asserted seeds. Extra
unresolved identities have no producer, and unreferenced assertions cannot
affect a projected importer. Thus relabeling rule firings in either direction
gives the same completed judgments and name-based outputs. Sharing the closed
stability result preserves the classifier's original inputs; primitive read
capabilities are added to the caller's stable-import set afterward, as before.

**Preparation bound.** Let `R` count explicit module, import, export and extra
reference records, and `L` bound their name lengths in bytes. Construction makes
`O(R)` ordered-map operations, each costing `O(L log(R + 2))`, and stores
`O(R)` graph/index records with `O(RL)` copied name bytes. It therefore takes
`O(RL log(R + 2))` time and `O(RL)` space. It traverses no paths, expands no
alias closure and creates no identities during solving. ID edge reads are
constant time; a complete traversal of module imports scans the indexed local
bindings once. Semantic domains and rule contributions retain their existing
bounds; this preparation claim does not bound arbitrary placement search.

IDs live only for this analysis. Final summaries project back to source names,
so saved-analysis DTOs, binding restoration and source/projection compatibility
checks remain unchanged. The graph is released after program-summary
construction; no graph cache or second serialized representation is retained.

### Stable imported bindings

`cross_module_purity::resolve_stable_imports` assigns one Boolean fact to each
canonical `(module, export)` identity. An export seeds `true` when it is an
explicit `asserted_pure` assumption or its provider has an immutable top-level
binding. Otherwise, a local export of an imported binding reads the canonical
provider identity and becomes true only when that fact does. A missing export,
missing module, mutable binding, or unseeded re-export cycle therefore remains
false; an asserted export remains an assumption even when its provider is not
analyzed. This is the existing import-cell stability premise, not a claim
about module initialization availability or function purity.

This closure preserves the old chain-walk result. By induction over proposals,
every true fact has a finite re-export path to an assertion or immutable
binding. Conversely, the initial rules publish each seed and watched edges
propagate it backward; induction on path length makes every identity on such a
path true. The old walk returns true exactly when it reaches one of those
seeds, and otherwise ends at a missing identity, mutable binding, or repeated
identity. The same import-to-identity projection therefore yields the same
stable local import set, including false unseeded cycles.

Preparation runs the existing immutable-declaration census once per module and
reads the shared graph's export, import-target and reverse importer indexes.
Assertions seed the graph identities they name; an unreferenced missing export
needs no rule because it has no consumer. Solving starts one rule for each of
`V` identities.
Each rule reads at most one re-export dependency, each of the `E` dependency
edges can wake its consumer at most once, and each Boolean fact grows only once
from absent/false to true. Thus closure has at most `V + E` rule evaluations,
reads, and proposals (`E <= V`); projecting stable results visits each of the
`I` import bindings once. No consumer restarts a chain walk, including when
many imports share its tail.

For input bounds, let `S` be the AST node count across analyzed modules, `M`
the analyzed module count, and `L` the longest compared module, export, local,
or declaration name in bytes. The immutable-declaration census visits each
module body and, in the existing write collector, rescans assignment-target
subtrees; conservatively this is `O(S^2)` node/name visits and
`O(S^2 log S)` ordered-set operations, with at most `O(L)` bytes per name
comparison. Canonical identity indexing and final projection use
`O((V + I + M) log(V + I + M))` ordered key operations, each comparison also
costing at most `O(L)` bytes. The scheduler's ordered pending set performs
`O((V + E) log V)` operations on integer rule IDs; fact and watcher tables make
`O(V + E)` ordinary expected constant-time hash operations. Adversarial hash
collisions give a conservative `O(V^2)` solver-table bound, still polynomial.
Temporary declaration-census state, canonical identities, watchers, and output
projections use `O(S + V + I + M)` records and at most
`O(L * (S + V + I + M))` bytes of copied names. The complete resolver
therefore has polynomial time and space in its source AST and import/export
input.

Binding, property, parameter and return flow shares a store with candidate-object
reachability. Return and property contributions propagate candidate identities
through the same worklist; escape consumers get filtered read-only views.
Candidate roots are registered before solving, including caller-supplied roots
outside the source-origin table. Each chunk keeps a deterministic ordered origin
table, while value and candidate sets store only present IDs in Roaring bitmaps.
Thus a singleton's payload does not grow with its origin rank, and seeding `C`
candidate roots does not allocate `C` dense bitmaps of `C` bits each. Iteration
still follows origin order; `Top` remains a separate value included after known
origins. Global escape taint, Set aliases, native and namespace aliases,
capability rejection, primitive/fluent value classes, and read/rebind closure use
the same typed transfer interface.

A family whose contribution is a join over its reads can set
`EVALUATES_CHANGES`: a re-evaluated rule then receives the keys it read that
changed after it read them and joins only those, since its earlier
contribution is already merged. Candidate reachability does; its carriers are
final when it runs.

Recursive purity grows body-completion facts and direct/call-site effects.
The canonical classifier emits an obligation at an inferred local or imported
call instead of reading an optimistic Pure verdict. All function effects in an
export-analysis context, across module boundaries, share one worklist. Effects
and captured-input dependencies flow through those edges until closure. A
function is projected as Pure only after its body and every reachable call
obligation have been analyzed and the completed effect fact is empty. Pure
recursive cycles therefore need no optimistic purity seed. Unknown targets
remain direct unknown-call effects. Pinned export assertions are explicit
axioms; export inference does not consume chunk-local declared-pure hints.

The initializer coverage consumer retains the expression and its source
coverage from canonical value lowering. Its dedicated root and child requests
compose the same allocation/operation judgments as strict provider queries.
Application binding reads additionally require an initialized module-local
declarative cell; imports and host globals remain opaque. Sequences preserve
discarded reads and abrupt outcomes. A supported last value cannot cover an
unproved operand, and a failed transfer never retries another evaluator.
Whole-owner certificates remain sealed only after binding initialization and
normal complete execution; neither a child judgment nor an invocation memo
carries one. Named exported variable declarations use the same transfers;
split declarations retain each original initializer as its own source owner.
The completed certificate retains its composed judgment's proof dependencies
and those of an applied native root transfer. Effects borrow these conditions
from the exact matching owner; reported native-input uses are diagnostic
projections, not a substitute for the typed proof.

Runtime expression, invoke, call, heap, callback, and model transfers use one
shared runtime fact context. Each child is keyed by a `ProgramPoint` (owning
source unit and nested operation path) and carries operation-specific inputs,
including its environment or captured context where required. A transfer owns
its predecessor `ExecutionState` and returns a typed result with a successor
state; already published predecessor facts remain immutable. Acyclic terminal
dependencies are evaluated eagerly in dependency order on the same fact
context. `take_final` retires their single-consumer requests and results while
preserving proof dependencies, so parents do not replay and a missing terminal
result is not turned into a provisional unknown. Growing strongly connected
fact families continue to use worklist evaluation.

Invocation memo entries are facts in the shared runtime store. `ExecutionState`
keeps links from each invocation signature to its memo program point, while the
`Outcome` itself remains a typed memo fact in the store. Replacing or clearing
those links retires the corresponding memo facts. Existing active-invocation
recursion checks and evaluation budgets remain in force; this scheduling change
does not add recursive return-summary inference.

Memo versions are completed and immutable. `read_final` carries their typed
proof dependencies into each consumer without installing a live watcher or
retaining the version key as a premise. Growing judgments use ordinary `read`
so value and dependency growth still wake their consumers.

## Dependency regions and barriers

Completion barriers are valid only at actual one-way dependencies; the old pass
order alone is insufficient.

- `facts/semantic_inference.rs` requires a completed conditional native write
  census from every analyzed chunk. Native branch refinement consumes
  `callback_object_arguments` from the source-only canonical AST collectors.
  Later generic callback-factory certificates do not produce that field or feed
  the census.
- `facts/sync_effects/execution_index.rs` completes source-only aliases and
  assignments before captured author-input validation.
  Author assertions cannot establish the source-only absence premises used to
  validate those assertions.
- Completed static read/rebind and function facts feed execution-prefix heap,
  capture, and reentry interpretation. Prefix execution does not update static
  flow or function-purity caches. Own-property queries use the selected current
  prefix state; their results do not update provider source certificates and
  cannot survive a later mutation.
- `RuntimeKey::PreparedPurity(n)` holds classifier obligations at `Prefix(n)`
  for current-owner proof preparation. It reads source facts, completed
  function effects and the whole-chunk heap state, never a later prefix.
  `RuntimeKey::CompletedPurity(n)` is published after assignment/export
  evaluation, exact existing certificate discharge and heap aggregation,
  before `Prefix(n+1)`. Earlier-owner proofs and final verdict consumers read
  Completed; residual classifier obligations remain. The vendor-prune path
  finalizes the same classifier without execution (<design.md> § Purity and
  synchronous execution share one context).

A changed source, author input, import summary or analysis policy creates a new
context. A missing fact during evaluation is bottom, not proof that a JavaScript
behavior is impossible.

## Storage and diagnostics

The store retains direct premise keys, not recursive proof-tree copies. Growing
purity effects contain function keys rather than recursively nested `Arc<Purity>`
causes. Final diagnostic projection constructs a finite acyclic cause graph after
closure, retaining provider source locations across imports. The
queue orders evaluation only through its pending set of (round, rule) pairs;
the fact store, watchers and schedule are hash maps that are looked up, never
iterated in an order that matters, so every family's key and rule types
implement `Hash`. `IterativeFacts::keys` is unordered; `into_values` returns
ordered values. The queue schedules affected dependency edges rather than
rescanning all rules. These are structural bounds, not a measured CPU or overall
heap improvement. Use matched optimized CPU and heap profiles before claiming
those wins or changing the bitmap representation again.

`synchronous_execution.inference_uses` expands the rule names and captured-input
references for certificates actually consumed by timing analysis, including
native call identity, callable conclusions and position-specific own-property
projections. These projections establish return shape only; they do not
establish purity or exception freedom. Purity and heap reports keep their
existing dependency fields. Saved dependency graphs are diagnostic artifacts,
not reusable internal inference contexts.

Tests cover queue order, dependency unions, shared prerequisites, unrelated
rules, cycles, separate contexts and incomplete producers. Real CLI fixtures
exercise the source-to-certificate path, gate decisions and original/emitted
Node behavior. Source mutations that defeat individual premises must keep
rejecting; a successful corpus run alone is insufficient evidence for a rule.

## Program value flow

`facts/value_flow/program.rs` prepares a source-qualified instance of the existing
`FlowRules`, lexical-access rules and `CandidateRules`. `cross_module_purity` builds it after the raw
native census and inferred Set roots have closed, then hands its chunk views to
callback-capability inference and structural/purity preparation. The transient
`ProgramValueFlows` also travels through `ProgramSummary` to final chunk-fact
computation, replacing its second local solve. Saved summaries restore derived
chunk facts and never silently substitute a local solve for missing flow. Standalone chunk analysis uses
the same collector, native-root preparation and two-stratum solver.

Preparation collects each module once with `FlowProgramCollector`. Bindings use
separate integer slots per source module and SWC binding ID; allocation sites
intern `(module ordinal, source offset)` before solving. Static property names
are shared, but property cells still include the qualified allocation site.
Coordinate conversion changes no event's transfer. `ProgramBindings` supplies
named/default import-to-provider-cell copy constraints, including local
re-export chains and cycles; ordinary local copies retain separate slots.
Unresolved exports, source re-exports and namespace routing gain no new linkage.
All imports also receive a durable opaque alternative. Known origins supplement
that alternative; discovering a provider function is not a certificate that
calling it is effect-free, synchronous or ready during initialization.

One growing value stratum closes assignments, static properties, call arguments
and returns, with watched reads including absent keys. A lexical-access stratum
composes source witnesses over those completed values. The candidate stratum
closes containment over the immutable value facts. Candidate propagation
uses the existing changed-key rules; it never wakes source events. There is no
source replay, fresh solver-iteration identity, second points-to transfer or
placement-dependent analysis.

The completed store is shared by chunk views. Capability, purity and final
chunk preparation borrow or clone an Arc to the same immutable source view,
never clone its binding/property maps. Solved value facts move into the
completed query index rather than retaining a second full key map throughout
projection. Existing consumers still receive
chunk-local `Value`s: owned allocations recover their original offset, while a
foreign allocation projects to `Top` and any local candidate roots reachable
through it. Thus publication through a foreign carrier cannot erase local
object reach. Expression queries lower with the existing `FlowValueExpr` and
read the shared store **before** projecting: `foreignRecord.box.run()` can find
a caller-owned callback through both property cells. Projecting the receiver
first would lose this information. Chunk-local callables alone receive canonical
local bindings. A foreign or opaque alternative keeps the target set incomplete
and preserves the syntactic fallback. May-identity facts do not establish
initialization availability, normal completion, absence of effects, or exact
heap contents. Those remain separate analyses and later migration slices.

**Preservation and closure.** Source qualification is injective on each original
module's cells and allocations and disjoint between modules. Shared property
names do not identify receivers. For every original event, rekeying commutes
with its binding/property/argument/return transfer. The linked rule set contains
all rekeyed local constraints plus import copies and opaque import seeds; these
only add may-origins. Source metadata has one immutable definition per qualified
function. Import cycles require no recursive graph traversal: copies grow in
the same finite store. Both joins are set union; every possible key is fixed by
source slots, sites and property names. Complete watched reads revisit an event
when a consumed set grows, including a producer discovered after an empty read.
The existing fair worklist therefore terminates at a closed solution of these
constraints. This extends the existing may-origin abstraction; it is not a
whole-JavaScript operational soundness proof. Unsupported operations and foreign
execution retain their prior opaque treatment.

Projection preserves every owned origin and every requested local candidate
reachable through an erased foreign origin. It adds `Top` for that erasure;
consumers cannot use a foreign identity as an exact local callable or allocation
proof. Querying before projection preserves cross-chunk property/return paths.
The store is transient source analysis; existing saved chunk facts and graph
outputs remain its persistence boundary, tested through fresh/saved CLI parity.
The analyzer retires the shared flow after all parallel chunk consumers join,
before artifact encoding allocates its output buffers.

**Bounds.** Let `n` count explicit source/selected-input bytes, `A` origins, `B`
binding slots, `P` static property names, `C` candidate roots, `R` source/import
constraints and `S` their total expression/parameter bytes. Each is `O(n)`;
none depends on the number of concrete calls, paths, environments or schedules.
There are `K = O(B + AP + A + 1)` value/metadata keys and at most `K(A+1)`
strict value insertions. Parameter/readiness metadata is immutable and changes
at most once per site. There are `O(A+B)` candidate keys and at most
`O((A+B)C)` candidate insertions. One source rule can watch at most `K` keys,
so value-rule evaluations are bounded by `O(R(1+K(A+1)))`; candidate rules
watch only their completed carriers' origin edges, with at most `O((A+B)A)`
dependencies and `O((A+B)(1+AC))` evaluations.

For an expression of size `s`, traversal reads at most `O(s(A+1))` sets; each
set operation costs at most `O(A+1)`. An invocation visits at most `A` targets,
source-bounded parameter/argument vectors, and invalidates only its evaluation's
argument results when a formal grows. Charging all argument syntax to `S` gives
a deliberately loose `O(n^4 log(n+2))` bound per source-rule evaluation, including
ordered lookup/merge work. The preceding evaluation bounds therefore give a
polynomial total (a loose `O(n^8 log(n+2))`), rather than claiming that arbitrary
dense points-to instances are linear. Retained value facts and source watchers
use `O(KA + RK)` space; candidate facts/watchers use `O((A+B)(A+C))`. These are
`O(n^3)` overall. Sparse Roaring sets avoid allocating a universe-sized zero
prefix per singleton.

Linkage and coordinate conversion cost at most `O(n^2 log(n+2))`, including
copied/compared source names; the existing source-census preparation remains
shared with standalone analysis. Projection uses
owner/site indexes and property-key ranges, not a module-count scan of every
program fact. For a projected set it visits its present origins and, for each
erased foreign carrier, at most that consumer's `C` local candidate roots.
Total projection time is bounded by `O(K(A+1)(C+1) log(n+2))`, retained projected
facts by `O(K(A+C+1))`. A later expression query of size `q` costs at most
`O(q(A+1)^2 + (A+1)(C+1)log(n+2))`, without restarting inference. These bounds
cover this preparation/flow/projection phase, not selector solving, ordered
callback execution or general ESM placement search. Projection preserves the
completed store's structural sharing: translate an immutable bitmap node
(including its opaque bit) at its first consumer in a source view, then retain
that translation for exactly the view's lifetime. Only nodes held alive by the
completed store enter this table; transient query results do not. Thus wide sets
shared by many property cells are translated once per consuming source, rather
than once per cell. There is no eviction policy or mutation invalidation.
The stated projection bound is conservative; repeated references require only a
lookup and an Arc clone. The stated time bounds
assume ordinary hash-table operations. Even adversarial collisions add at most
`O(K+R+n)` work per lookup, a further polynomial factor; no bound relies on a
hash collision being impossible.

### Shared lexical-access summaries

`FlowProgramCollector` records source-qualified binding reads and writes while
performing its existing visit. Function bodies reference the already lowered
invocation events by index; effect collection neither rescans source nor lowers
callees again. Parameter and return propagation remain owned by `FlowRules`.
After value closure, `CallEffectRules` resolves each call's possible function
origins once and runs in the same `IterativeInference` store. Effects never
refine value facts, so this completion barrier loses no feedback.

For each qualified function `f`, three keys carry pairs of sparse binding sets:

```text
source(f)   = direct(f) union { source(h) | h is a syntactic descendant of f }
invoked(f)  = direct(f) union { invoked(g) | g is a possible callee of f }
retained(f) = source(f) union invoked(f)
                      union { retained(g) | g is a possible callee of f }
```

Both coordinates join by union. These are positive source-access witnesses,
not complete operation effects: unknown targets, dynamic scope, implicit calls,
unsupported heap behavior and suspension retain their existing conservative
treatment. Even the invocation set can overapproximate execution. No consumer
may infer purity, absence of effects, completion or availability from an empty
set. Descendants contribute their source references, but their call edges are
not followed merely because the enclosing function creates them. This retains
the conservative lexical-reference policy without treating a returned
wrapper's later callback invocation as invocation at wrapper creation. Calls
inside async/generator bodies, class evaluation and object accessor bodies do
not add edges to their enclosing function's graph in this slice; these
unmodeled timing boundaries retain their source references and existing havoc.

Each rule registers all successor reads, including absent keys. Subsequent
evaluations consume only changed successors; they do not resolve targets or
revisit unchanged neighbors. Recursive components therefore share the same
finite facts as acyclic calls, without enumerating call paths or executions.
The old shallow function/import-reference scan has no remaining consumer.

The cyclic-export fallback consumes `retained` and projects reads union writes
onto caller-local exported cells. Including lvalues preserves this conservative
consumer's reference semantics. These remain assumed may-reads, not observed
reads or evidence that a provider completed. `ProgramBindings::source_cell`
resolves import/re-export aliases once by following the functional binding
graph and finishing each visited path. Shared tails reuse their terminal cell;
missing exports and alias cycles without a terminal cell remain unresolved.
Ordinary value copies keep their own cells.

Projection groups consumers by function and importing module, normalizes each
function's shared access bitmap once, and intersects it with that module's
export mask. Aliases reuse this intersection. Invocation syntax is retired
after call-graph preparation; access summaries and their source-name index are
retired at this final projection before per-chunk analysis. Saved analysis
persists the existing derived callable obligations and chunk facts, not another
transient effect graph.

**Closure and preservation.** All keys, slots and graph edges are input-derived.
Direct witnesses are seeds; union transfers are monotone and idempotent. By
induction on graph-path length, invocation edges close `invoked` and syntactic
descendant edges close `source`. The typed `retained` relation permits a path of
call edges followed by source-descendant edges; it cannot follow a call edge
after entering a deferred descendant. Complete watchers and fair scheduling
reach its least fixed point. This proves the finite source-witness relation, not all JavaScript
semantics. The existing fallback still handles behavior outside that relation.
Every import reference under a previously scanned top-level function remains
in its retained descendants or direct accesses, including lvalues. Alias
projection reaches the same caller cell and can add witnesses through helper
calls, parameter/return flow and local re-exports. It removes no prior
cyclic-cell obligation and grants no new execution certificate.

**Bounds.** Let `F` be function sites, `B` binding slots and `E` distinct edges
between the `3F` access keys. There are at most `6FB` read/write bit insertions
and `3F` initial fact insertions. Each successor changes at most `2B+1` times,
so changed-successor transfer visits are `O(E(B+1))`, in addition to `O(F+E)`
initial work. A worst-case bitmap join costs `O(B+1)`. Including ordered rule
queues and indexes gives `O((F+E(B+1))(B+1+log(F+2)))` closure time and
`O(FB+F+E)` space. This explicitly charges dense shared summaries; it does not
expand one transitive path vector per caller.

For input size `n`, `F,B=O(n)` and `E=O(n²)`. Resolving targets uses the existing
expression evaluator once per prepared invocation. With `D` total lowered
callee-expression nodes and `A` origins, its bound is
`O(D(A+1)² log(n+2))`; charging nested expressions conservatively gives
`D=O(n²)`. Source-cell resolution visits every binding once, using `O(n)` extra
time and space after linkage construction. Consumer projection costs
`O(FB+QB+U+Z)` integer/bitmap work, where `Q` is distinct function/module pairs,
`U` function/import edges, and `Z` materialized result names. All are bounded
by the input-derived function, import and export products. Charging name bytes
and ordered lookups gives a loose `O(n⁴ log(n+2))` bound for the added phase,
with `O(n³)` space including copied names. The preceding value-flow bound
continues to cover the combined phase. As above, adversarial hash collisions
add a polynomial table-size factor. These bounds add no runtime caps and say
nothing about ordered callback replay or exact ESM scheduling.
