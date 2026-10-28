# Shared inference

`inference.rs` computes positive certificates over an immutable analysis context.
`facts/semantic_inference.rs` owns the shared rule set. Structural analysis
supplies candidates; candidates alone cannot certify an operation.

The current rules compose as follows:

```mermaid
flowchart LR
    census[Completed native census] --> native[Native integrity]
    native --> identity[Native call identity]
    shape[Canonical source shapes] --> origin[Own-property source origin]
    origin --> ownshape[Own-property static shape]
    ownshape --> prefix[Own-property prefix state]
    identity --> projection[Own-property projection]
    prefix --> projection
    projection --> names[Own-name and descriptor results]
    identity --> empty[Empty collection construction]
    identity --> calls[Guarded native call summaries]
    empty --> class[Local class construction]
    native --> set[Primitive Set iteration]
    ownshape[Canonical own-property shapes] --> prefix[Own-property prefix state]
    identity --> projection[Own-property projection]
    prefix --> projection
    projection --> names[Own-name and descriptor results]
    shape[Canonical source shapes] --> function[Stable function identity]
    function --> callback[Callback factory]
    function --> record[Returned record construction]
    empty --> record
    shape --> empty
    shape --> class
    shape --> set
    class --> consumers[Purity, synchronous execution, heap ordering]
    set --> consumers
    callback --> consumers
    record --> consumers
```

Native integrity checks the completed census, including dynamic scope and
global-object mutations. The `Function` family is included so source-visible
changes to `Function.prototype.call` can invalidate only proofs that consume
that capability. Native call identity also checks the exact binding,
shadowing, and `globalThis` qualification before requiring both the global
object and operation roots. Empty collection construction uses that identity
judgment with the native operation's fresh-allocation model. Local class
construction requires every native field allocation. Primitive Set iteration
requires both Set and Array integrity and a checked source shape. The original
restrictions on class identity, construction sites, arguments, primitive
elements, mutation and escape remain in their structural analyses.

Stable function identity requires one exact function value for the binding.
Callback-factory conclusions compose that identity with an admitted source
body. Returned-record conclusions additionally require an unchanged hoisted
function and a fresh empty collection constructor; their summaries retain the
member and capture provenance. These are timing and retained-capability facts,
not purity or no-throw claims.
Own-property inspection uses a separate call-local inference store. The shared
semantic context supplies completed native identity and integrity judgments;
the local rules derive an exact object origin, its canonical source shape, and
the current prefix state before projecting names or descriptors. Each query
copies only the selected object's current property state, and that inference
context is discarded after the call. It cannot reuse a prefix-derived fact
after a later mutation. Name projection follows ECMAScript own-string-key
ordering, while descriptor projection retains current data identities and
accessor closures without invoking them.

## Context and results

The rule context owns compact source facts and the resolved native census from
the same preparation. It does not parse or walk source again. A changed program,
author input, import summary or analysis policy requires a new context; no cache
is shared by path or module name. Prepared native-call, Set, class, and callable
proofs share one `Arc<SemanticFacts>`, including the heap projection of the
class proof.

A `Judgment` has private construction. It contains a fact, direct premise keys,
and the union of captured-input dependencies used to establish it. Reading a
premise through `RuleContext::require` automatically records both the premise
key and its dependencies. Only completed external premises enter through
`assume`; an author assertion remains distinct from an inferred source fact.
An evaluator cannot publish a fact after requesting an unproved prerequisite,
even if it accidentally returns a successful value.

The typed key names the judgment, rather than a universal "safe" boolean.
Consumers still make purpose-specific decisions: an allocation certificate does
not suppress callee or argument evaluation, establish a no-throw guarantee, or
make a later method call pure. Independent builtin whitelist admissions retain
their own runtime assumptions.

## Scheduling

There is one rule producer for each key, and the reachable key graph must be
finite. A query discovers its prerequisites. Missing prerequisites are queued;
reverse watchers identify exactly which waiting rules to retry when a new fact
is proved. The FIFO queue deduplicates pending keys. Unrelated rules are never
scanned, and a shared prerequisite is evaluated once after it succeeds.

The queue drains to a positive closure. `Proven`, `Waiting` and `Unsupported`
distinguish a certificate, an unmet prerequisite, and an evaluated unsupported
rule. A waiting cycle does not prove itself. None of these negative states
establishes that a JavaScript behavior is impossible; graph consumers retain
their conservative fallback.

This is not a replacement for every fixed point in Debundle. Possible values
grow by union; recursive purity can start optimistically and lose purity; native
absence needs a completed census; execution-prefix facts depend on position and
captured context. Such domains need their own merge and completion contracts
before they can use shared scheduling.

## Storage and diagnostics

The store retains direct premise keys, not recursive proof-tree copies. The
current queue uses ordered maps and sets for deterministic discovery and
deduplication. It schedules affected dependency edges rather than rescanning all
rules. These are structural bounds, not a measured CPU or memory improvement.
Use matched optimized CPU and heap profiles before changing the value arenas,
bitmap representation or cross-module preparation caches.

`synchronous_execution.inference_uses` expands the rule names and captured-input
references for certificates actually consumed by timing analysis, including
native call identity, callable conclusions and call-local own-property
projections. These projections establish return shape only; they do not
establish purity or exception freedom. Purity and heap reports keep their
existing dependency fields. Saved dependency graphs are diagnostic artifacts,
not reusable internal inference contexts.

Tests cover queue order, dependency unions, shared prerequisites, unrelated
rules, cycles, separate contexts and incomplete producers. Real CLI fixtures
exercise the source-to-certificate path, gate decisions and original/emitted
Node behavior. Source mutations that defeat individual premises must keep
rejecting; a successful corpus run alone is insufficient evidence for a rule.
