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
contributed, no dynamic scope) and neither its family nor the global object is
mutated. The `Function` family is included so source-visible changes to
`Function.prototype.call` can invalidate only proofs that consume that
capability. Native prototype crossings, `Object.getPrototypeOf` result aliases
and global-slot read-backs retain an opaque native root: a later write through
one invalidates every slot rather than trusting the original receiver's family.
An axiom covering every requested slot at the queried site is consumed instead
of the census and recorded as that input's proof dependency.

A native callee's identity is its spelling (the unshadowed, undeclared global
binding of the row's family, or an unshadowed `globalThis` qualifying it) plus
the flags of the row's `requires` slots at the call. Empty collection
construction is that identity for a zero-argument keyed-collection constructor;
local class construction requires it for every native field allocation, where
a `set_empty_private_field` axiom can stand in at its site. Primitive Set
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

Binding, property, parameter and return flow shares a store with candidate-object
reachability. Return and property contributions propagate candidate identities
through the same worklist; escape consumers get filtered read-only views.
Candidate roots are registered before solving, including caller-supplied roots
outside the source-origin table. Global escape taint, Set aliases, native and
namespace aliases, capability rejection, primitive/fluent value classes, and
read/rebind closure use the same typed transfer interface.

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

A changed source, author input, import summary or analysis policy creates a new
context. A missing fact during evaluation is bottom, not proof that a JavaScript
behavior is impossible.

## Storage and diagnostics

The store retains direct premise keys, not recursive proof-tree copies. Growing
purity effects contain function keys rather than recursively nested `Arc<Purity>`
causes. Final diagnostic projection constructs a finite acyclic cause graph after
closure, retaining provider source locations across imports. The
current queue uses ordered maps and sets for deterministic discovery and
deduplication. It schedules affected dependency edges rather than rescanning all
rules. These are structural bounds, not a measured CPU or memory improvement.
Use matched optimized CPU and heap profiles before changing the value arenas,
bitmap representation or cross-module preparation caches.

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
