# Shared inference

The `inference` crate defines `Judgment` (`inference/lib.rs`);
`inference/iterative.rs` provides the
dependency worklist (`IterativeInference`) for finite growing facts and
positive certificates. `facts/semantic_inference.rs` composes native,
construction, and callable rules in that store. Structural analysis supplies
candidates; candidates alone cannot certify an operation. Rules use typed
`FactContext` access so an enclosing rule family can project their keys without
copying a private summary or hiding a read from the worklist.

## Value subscriptions

Each solve assigns bookkeeping IDs to registered rules and fact cells. A rule
retains the set of cell IDs it has ever read, including missing facts. Each
cell stores its reverse vector of reader IDs: only a successful insertion into
the reader's set appends an edge. Repeated reads look up the cell and check
integer membership without another reverse-map lookup. Subscriptions outlive
an evaluation because an invocation can reuse a cached dependency without
reading it again.

The key-to-cell map and cell-key vector each retain one key. A missing fact has
no judgment payload: present judgments live in separate reusable fact slots.
`take_final` removes a completed payload, its proof edges, and its references
in the current activation's reads and changes. It can then retire and reuse
an unwatched cell. A historical reader prevents cell retirement because that
reader's membership or pending changes can still refer to its ID. Proof
premises retain semantic keys, never cell IDs. An absent read remains watched
until completion; consuming an absent fact does not discard its subscription.

IDs do not define evaluation order. The pending set still compares
`(round, Rule)`, including for dynamically discovered producers, and writes
remain immediately visible. Each rule's scheduled round and changed-cell set
are addressed by its ID. Every changed key reaches its subscribed rules even
when a rule is already scheduled; change-aware rules must receive all dirty
keys at that evaluation. Typed proof propagation and new-edge backfill use the
separate proof index and do not schedule value evaluations.

Pending dirty cells use integer hash membership. On activation, consume that
set into the existing sorted unique semantic-key input before evaluating the
rule. The producer's changed cells are also delivered in semantic-key order.
For `W` wake attempts and `D_i` distinct keys delivered at activation `i`, pending
membership costs expected `O(W)` plus `O(sum_i D_i log D_i)` ordering. Retained
pending IDs are `O(sum_pending D_i)`; freezing adds temporary `O(D_i)` key
storage for the active rule. Expression dependency sets remain unchanged.

Let `R` be registered rules, `K` peak allocated cell slots, `F` peak allocated
fact slots, `E` distinct historical reader/cell pairs, `A` read-registration
attempts, and `C` total reverse-vector capacity. The value index retains
`O(R + K + F + E + C)` entries. It stores integer IDs per forward membership,
two keys per live cell, two rule payloads per registered rule, and `C` integer
slots. Growing reader vectors give `C = O(E)` but singleton fanouts can reserve
several slots. Cell/fact vectors retain their high-water capacity; retiring
unwatched cells releases their keys and makes their slots reusable. This does
not remove historical edges or expression-local dependency storage.

Expected registration work is `O(A)` cell-map lookups and integer-set operations
plus `O(E)` amortized reverse-vector appends. Wake bookkeeping costs `O(W)`
indexed accesses, plus dirty-ID insertion and pending-set work on actual
schedules. Semantic key/rule comparison, hashing and copying costs are
additional. At completion, watcher and proof indexes are dropped before
positive facts move into the completed map; absent cells are not published.

Test-only aggregates count all reads and proposals, value-changing proposals,
registration attempts/new edges, wake attempts/schedules, live/peak fact counts
and reverse capacity. They exclude seeding before the solve and compile out of
production. Small controls establish operation bounds and semantic invariants;
their counts are not measurements of production corpus cardinality, memory or
CPU cost. Source-only stores already omit the live proof index.

### Opt-in production work metrics

`--metrics-file PATH` on `spec project-analysis`, `analyze`, their standalone
executables, or `run` exports cumulative OpenTelemetry metrics as OTLP JSONL.
For example, `OTEL_METRIC_EXPORT_INTERVAL=1000 debundle analyze ...
--metrics-file analysis.metrics.jsonl` requests one-second SDK exports.
`OTEL_SERVICE_NAME` and `OTEL_RESOURCE_ATTRIBUTES` configure SDK resources.
Without the flag no provider, reader thread or worker batch is constructed.

Inference worklist accounting currently covers the whole-program **source
value-flow solve**, not every `IterativeInference` caller or the subsequent
effect/candidate closures. Fixed event and key-family categories distinguish
reads (including absence), first/repeated rule executions and fact rows.
`debundle.inference.operations` distinguishes changing/no-op proposals,
subscriptions, wake attempts/coalescing, schedules and bitmap join decisions.
New facts also count as changing proposals. Bitmap decisions cover value-fact
and delivery-frontier joins; expression-local unions are outside that counter.
The `fact_rows`, `subscriptions` and `pending` gauges describe this solver's
latest published state, not process memory or all retained analysis state.
No source names, key IDs or rule IDs become metric attributes.

`debundle.source_flow.unknowns` counts decisions to introduce `Top` while
solving source expressions, by a fixed cause: undeclared binding, missing or
dynamic property, non-object receiver, non-function callee, unsupported
expression or opaque import. Copying a stored value set adds no count; projecting
an unknown receiver/callee does count the corresponding conservative expression
rule. `Top` does not enumerate all program allocations. Counts accumulate
on the stack per expression and publish when that evaluation returns; completed
projection queries and subsequent effect/candidate closures are excluded.

`debundle.execution.precision_losses` counts newly selected conservative
interpreter paths, before site deduplication. Attributes retain the existing
reason, unsupported operation, bounded/global reach, global reach cause and
refusal/rejection variant. No source identity, property text, budget value or
pool size is a label. Inheriting a child's loss or copying the bounded fallback
receipt into its returned outcome adds no occurrence. These counts describe
fallback decisions, not affected heap cells or unique source sites. They do not
include syntax-classifier havoc or unlabelled taint/heap invalidation paths.

The solver updates scalar worker-local counters, publishing every 4096 counted
operations and when the batch drops. The SDK reader exports independently,
including during one long producer evaluation. Counters can lag by one batch;
gauges reflect its last snapshot. Collection never traverses facts or bitmaps.
Projection counts prepared chunks and attempted initializer validations; source
flow preparation counts its statement views and owned declarator clones.
These counts identify work families; allocation attribution still requires a
heap profiler. Progress diagnostics separately bracket program flow, its solve
and its subsequent closures.

The command shuts down the provider on success or operation error and reports
the first export failure, including an earlier periodic failure. Completed
lines survive abrupt termination; the last worker batch may not. An output I/O
failure may leave a partial trailing line. Periodic benchmark runs preserve the
raw file alongside separate CPU or heap traces; interpretation is a separate
investigation step.

## Source property producer census

Each value-flow solve builds one `PropertyProducerCensus` from its complete,
qualified and rekeyed `FlowEvent` list. The exhaustive producer match covers
every abstract property write: fixed-receiver property events and dynamic
`AssignProperty` receiver events, including writes with an empty right-hand
side. A property key omitted from this census has no initial fact and no
producer in the source relation, so its read stays missing and continues to
map to `Top`; this is not a proof that the property is absent at JavaScript
runtime. Such a read installs neither a value watcher nor a cached dependency.
All possible reads keep their existing dependencies, watches, scheduling and
fact order. `FlowRules::SOURCE_ONLY` remains unchanged: the census adds no
typed premise or proof dependency. Construction takes expected `O(events)` time
and `O(fixed receiver/property pairs + dynamic property-slot prefix)` state;
each may-produce query takes expected `O(1)`. Add any new property-producing
event to the exhaustive census match before it can participate in this
optimization.

## Heap-content value-query reuse

When one heap-content visit needs both Set and array classification, or escaped
reachability and direct array classification, it evaluates that exact
expression once and borrows the immutable `Values` for both consumers. This
preserves each consumer's flags, candidate walk, `Top`/ambiguity handling,
opaque effects and output ordering. The result is retained only through that
visit, then dropped before child expressions are visited, so temporary result
storage does not grow with AST nesting. No expression or AST-span cache crosses
visitor events or source views.
The pair removes exactly one full expression lowering/evaluation at the paired
site and needs only the one temporary result already produced by a query.

## Numeric computed property reads

Source value flow uses the existing primitive-result evaluator with `NoBindings`
to distinguish a computed key whose normal result can only be Number or BigInt.
For example, `items[items.length - 1]` selects numeric properties; a bare index
binding or string-capable concatenation retains the all-property union. This is
an operator-result fact, not a proof of array identity, purity or completion.
Existing exact property reads, such as `items["0"]`, remain exact.
Coercion calls, getters, assignment effects and throws retain their ordinary
analysis. No builtin integrity premise or function execution is assumed.

Each receiver has a growing numeric-property union alongside its existing
all-property union. Property names are classified once, using a conservative
superset of Number/BigInt string representations: an initial ASCII digit, sign
or decimal point, plus `NaN` and `Infinity`. Extra admitted names only lose
precision. Every static property contribution whose name belongs to that set
also updates the numeric union. Reads consume that union through the same fact
store and keep the dynamic-read `Top` alternative, including for an absent
union. An absent numeric union still installs a dependency: a later numeric
write must wake the reader. This projection does not strengthen unknown,
prototype, accessor or opaque-write semantics.

The normal-result proof is compositional. The primitive evaluator establishes
that a completing key expression yields a numeric primitive; `ToPropertyKey`
therefore produces a string in the admitted name set. Every modeled property
write with such a name contributes to the receiver's numeric union, and its
growth wakes every subscribed numeric reader. Thus filtering out other modeled
names cannot remove a possible value of this read. Existing `Top` and effect
handling covers the same unmodeled alternatives as before. Earlier/later writes
remain pooled; no source-order or call-context precision is invented.

The index adds at most one aggregate fact per receiver and one additional
aggregate join per numeric property contribution. A read performs one aggregate
lookup per possible receiver, rather than visiting every property name. Origin
union costs and the size of the receiver relation remain explicit costs; this
does not make the entire source-flow solver linear. Deterministic tests vary
unrelated named fields and retain late-write checks. Fresh/saved CLI controls
preserve an existing accepted cut and refuse unsafe numeric, named-key and
coercion cases. They also pass on the baseline; they do not demonstrate a newly
accepted cut. Synchronous execution may independently fall back to global reach
for a computed member read, even when source value flow is more precise.

### Counted-loop read certificates

A separate source-site proof admits a bare counter key in a classic local
`for` loop with one `var`/`let` numeric-literal initializer and a matching
`++`/`--` update. It certifies only the member reads in that loop's test and
body. It does not assign a numeric type to the binding: a later read after the
counter is reused still follows ordinary unknown-key analysis.

Admission requires an unambiguous function-local declaration, no parameter
alias, no other declaration of that binding, and no counter write in the test
or body. Assignment, destructuring, nested loop heads and updates all count as
writes. A capture by any other callable or deferred class body excludes the
binding, even when the captured use appears read-only. Direct `eval` or `with`
in the owning function or a nested boundary excludes its loops. Async and
generator bodies are excluded. Nested callables do not inherit a certificate.
These restrictions are sufficient conditions, not a completeness claim.

The proof is induction over loop entries. The initializer establishes a numeric
primitive. The test and body cannot replace it: explicit writes are excluded,
and another activation cannot reach the non-parameter local through a capture
or dynamic scope. A completing increment/decrement preserves numeric kind.
Abrupt completion creates no additional normal read result. Thus each admitted
key evaluates to Number or BigInt and can use the numeric-property aggregate.
Getters, receiver evaluation, calls and their effects still use ordinary
analysis; this proof neither executes the loop nor certifies its termination.

One census records each function's declarations, references, writes, loops and
member sites. A binding-to-owner index identifies captures without copying
reference sets through every ancestor; a reverse function-tree fold propagates
the dynamic-scope barrier. Writes and reads are indexed by binding and sorted
by source position. Each admitted binding has one declaration and therefore at
most one admitted loop; range queries do not rescan unrelated loop bodies. For
an AST with N nodes, preparation uses O(N log N) expected time and O(N) space,
including hash indexing, sorting and certificate insertion. The retained certificate set contains only
complete member spans `(lo, hi)`, and a query costs O(log N).

The immutable certificate index is shared with event lowering, the completed
chunk and program-projection expression queries. A member's start offset alone
is insufficient: nested members can start at the same byte. Unknown keys,
later reuse and reads in another callable retain the all-property union.

### Guarded snapshot read certificates

The same function census can relate a computed snapshot to one literal switch
arm: `value = record[key]`, followed by `if (value != null) switch (key)` and
`case "payload": selected = value; break`. It certifies that particular RHS
identifier use, not every use of `value`. The snapshot may be a declaration or
assignment immediately before the switch, immediately before an exact
`value != null` guard whose consequent is the switch, or the final comma pair
of an `if` condition ending in `value != null`. An `if` prefix before the read
may run arbitrary code; its condition and effects stay in ordinary analysis.

Admission requires three distinct, unambiguous locals owned by the same
synchronous function, with no captures or dynamic scope. Between the read and
the certified use, only the exact noncoercing null comparison, literal case
labels and a simple identifier assignment are admitted. A preceding arm must
terminate; fallthrough, effectful labels, intervening calls/rebindings,
coercing or compound null conditions, and unsupported arm shapes decline the
certificate. Reuse elsewhere in the owning function is allowed. The proof is
regional, rather than a claim that those bindings never change.

On any execution reaching the certified use, the snapshot assignment completed
with the original receiver and key. Neither another activation nor the admitted
straight-line operations can replace those locals. Entering the literal arm
therefore proves that the original key was that literal. The source value query
can project that property from the original receiver, including all of its
ordinary property facts. Abrupt completion does not introduce another normal
value. This is a value-provenance refinement, not a second runtime read or a
purity/termination certificate: the original read, getters, calls and writes
remain subject to ordinary effect analysis.

The sealed index maps the exact RHS span to a resolved receiver identity and
property name. Event lowering and completed chunk/program queries consume the
same index, including coordinate rekeying; the reference property oracle stays
independent of tracked property caches. The existing census supplies the local
ownership and capture proof without a second AST traversal. Shape collection
and retained entries are O(N), insertion/query are O(log N), and the combined
census keeps its O(N log N) expected preparation bound. Unsupported uses retain
ordinary flow; this recognizer is not complete path-sensitive analysis.

### Guarded non-function property-write origins

A separate exact-use certificate can narrow the value source for a simple
static property assignment such as `receiver.type = value`. It recognizes only
an assignment that is the final term of a pure `&&` chain containing
`typeof value != "function"` or `typeof value !== "function"`. Other accepted
prefix terms are limited to identifier-versus-`null` and `typeof identifier`
versus string-literal inequalities; they cannot call user code or rebind a
local. The assignment receiver, RHS value, and every identifier read by the
guards must resolve to unique bindings owned by the same synchronous function,
with no capture or dynamic scope. Unsupported syntax keeps ordinary flow.

The certificate is keyed by the exact RHS span and resolved binding. It removes
only known Function and Class allocation origins from that one transfer (a
class expression also has JavaScript `typeof` value `"function"`);
unknown/Top, Proxy and every other known origin remain. It does not narrow the
binding or later reads. If the same source use already has a guarded snapshot
projection, that projection is evaluated first and the non-function filter is
applied to its result. Both paths use the ordinary read and retain the same
dependency subscriptions. This is a source-value refinement only: property
writes, calls and their effects remain in the usual analyses, and the
certificate proves neither purity nor successful completion.

The census and certificate storage reuse the existing per-function AST pass.
It inspects only conjunction nodes whose direct RHS is the candidate
assignment, then walks preceding terms iteratively from right to left and
stops at the first unsupported term. Thus a long chain with an earlier write
does not cause every later assignment to rescan and store the whole prefix.
The origin table precomputes a bitmap of Function/Class origins once. A
certified read first checks whether its value bitmap intersects that set; a
disjoint value retains its shared bitmap, while an intersecting value uses the
bitmap difference and preserves its independent unknown bit. These bounds
cover this certificate work, not the total source-flow solve.

### Scalar local overwrites in comma expressions

The same exact-use non-function filter also applies after a definite primitive
overwrite within one comma expression. For example,
`(fallback = "" + fallback, value = condition ? "" + value : fallback, box.value = value)`
cannot store a Function/Class origin through the final `value` read, even if
the function's joined parameter facts contain functions from earlier calls.

The census examines sequence terms in evaluation order, starting with no known
scalar locals. Before inspecting a term's reads, it invalidates every tracked
local written anywhere in that term, including conditional and nested writes.
After the term, only an unconditional simple local assignment can establish a
new scalar fact. Its RHS must be a primitive literal, untagged template, unary
or non-short-circuit binary operation, already certified scalar local, or a
conditional whose two result arms both meet those conditions. Calls, member
reads, short-circuit expressions and unsupported forms do not establish facts.
Facts do not carry beyond the enclosing sequence.

Every binding used in a sequence's proof must be a unique, uncaptured local of
the same synchronous function. Dynamic scope, nested dynamic scope, `arguments`
use, async functions and generators decline this certificate. The existing
census treats references in nested functions and class initialization as capture
barriers. This also prevents coercion or getters from changing a certified local
through a closure. Their ordinary effects and possible exceptions remain.

The proof is induction over sequence terms: a later term is reached only after
earlier assignments complete normally; their allowed RHS results are primitive.
An uninvalidated, uncaptured local cannot change during the current term, so
its certified reads cannot contain Function/Class values. A completed scalar
assignment establishes the invariant for the next term. The proof narrows only
those reads, retaining Top, Proxy, other origins, ordinary reads and their
dependencies; it does not narrow the binding's joined set or prove purity.

Only outermost sequences within each function are scanned. Each disjoint term
is visited a bounded number of times, and proof bindings are shared by the
sequence rather than copied into every use. Collection is O(N) apart from set
operations, and preparation retains the census's O(N log N) expected bound.

## Native premises

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

## Prototype capability containment

`execution_index/prototype_properties.rs` prepares one immutable value graph per
capability-producing binding. Compilation records source-binding dependencies,
callable captures, class static fields and child-to-parent edges once. It builds
an owner-to-class-callables index once, so a class value visits only its owner's
members rather than scanning every callable. Each compiled operation keeps its
children as graph-local binding leaves and delegates root transfer to the same
`intrinsic_roots_read` rules used by the source census. Captured reads and
contained fields project intrinsic roots only. `CapabilityContainmentRules`
still reads and proposes through the shared `FactContext`, so source aliases,
typed proof dependencies, conservative unknowns and completed-census policy
remain owned by the existing inference store.

Each graph is emitted in postorder. Its first evaluation visits each node once;
later alias changes start at the matching leaves and follow deduplicated
reverse edges, evaluating the affected nodes bottom-up. Internal node values
are recomputed exactly because an empty alternative can stop contributing an
unknown arm after another arm becomes known. The producer's target fact still
joins every prior contribution, preserving the existing conservative output.
Class and function containment remains source-only. A record that contains a
native capability does not become a native receiver or gain instance identity.
The final prototype-call argument projection still walks each argument once
after closure; this graph removes the repeated value walk from inference.

**Preservation.** By induction over postorder node IDs, each compiled root
transfer receives the same child roots as the recursive source expression.
Because each operation delegates to `intrinsic_roots_read`, its object,
alternative, member, call, and global rules are unchanged. The graph's
containment edges mirror the former traversal of object fields, callable reads,
class callables/static fields, members, call arguments and returned
alternatives. Every source binding is retained as a dependency even when its
operator has no root output, so absent and later-growing facts keep their
watchers. Internal alternatives may shrink, but only the per-producer
contribution is joined, exactly as before. This optimization establishes no
receiver identity and does not convert captured assertions into source-only
evidence.

The rules keep the existing source-ordered first sweep. All dependencies,
including currently absent aliases, are read through `FactContext`; later
binding growth wakes only plans that read that key. A producer's compiled
expression graph is acyclic and evaluated bottom-up. Cross-producer root
transfers are not plain union edges: an alternative can contribute an opaque
root while an arm is empty, then remove that root from its exact internal value
when the arm gains a capability. Only the published target fact remains a
monotone join. A static SCC closure over root tokens would need a lifted domain
that records this arm state, so cross-producer growth stays in the existing
`IterativeInference` store. This also preserves absent-key watchers and typed
proof dependencies. The justification is the transfer's non-monotone internal
value, not merely that producer assignments can cycle.

**Cost.** Let `N` and `E` be the total compiled nodes and root/containment edges
over all producer plans, `C` the callable count, `D` the source-binding
dependency count, `U` the maximum root-set cardinality held at a node, and `L`
the longest compared source or graph-local binding ID in bytes (graph-local
IDs are `O(log(N + 2))` bytes). Let `R` bound the finite input-derived
root-token universe per published binding. Compilation uses `O(C log(C + 2))` ordered
operations for the owner index and `O((N + D + E) log(N + D + 2))` ordered
graph/index operations; ID comparisons add a factor of `L`. It stores
`O(C + N + D + E + N U)` records/root tokens, plus `O(N log(N + 2))` bytes for
the graph-local IDs. The initial transfer pass visits each node once; root-set
scans and unions add at most `O((N + E) U log(U + 2))`. For one changed-key
batch, each node in the reverse dependency cone is queued at most once, giving
`O((N_x + E_x) log(N + 2) + (N_x + E_x) U log(U + 2))` transfer and scheduling
work for that cone. If producer plan `p` has `D_p` watched source keys and
`N_p` compiled nodes, the shared cross-producer store schedules at most
`O(R * sum_p D_p)` value-growth rule wakeups; each can touch at most `N_p`
nodes. Exact root-set transfer cost is charged per affected cone. Typed proof
dependency additions use the shared proof queue described under
[Scheduling](#scheduling), without reevaluating containment nodes. Its proof-edge
index and typed dependency output costs remain separate from these value-graph
bounds. The implementation makes no matched runtime speedup claim. Work-count tests check one visit per node on deep chains,
fanout/shared joins and cyclic assignments, and one owner-member visit with
1,000 unrelated callables; they make no elapsed-time claim.

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

Own-property inspection consumes an owned query at the current program point.
It reads completed semantic judgments and the selected object's current prefix
shape and property state, then seals the optional names/descriptor proof in a
`CompletedTransfer`. Its sole reader consumes the transfer immediately,
forwarding its typed `ProofDependencies` into the active runtime context.
The occurrence still reserves its child ordinal in source/prefix order.
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

## Local class provenance

`NamespaceValueFacts::from_assignments` prepares possible local class values
before any namespace-capability fast path. `LocalClassProvenance` compiles each
assignment into Boolean binding edges and class seeds, then solves them in the
source-only `IterativeInference` store. Class allocations seed their targets;
bindings contribute edges, alternatives contribute their arms, and members
contribute their receiver's provenance or a uniquely resolved class surface.
Conditional tests, containers, calls, bare construction and sequence values do
not gain class provenance. This preserves the previous value vocabulary rather
than inferring new class identities.

Binding cycles without a reachable seed stay false at completion. A seed reached
after a back edge propagates through the entire cycle; an absent in-progress
fact never becomes a cached negative. Possible class provenance remains separate
from unique constructor/instance resolution. Only a possible class value without
one uniquely resolved assignment enters `unstable_class_bindings`. Class
publication retains its separate conservative treatment of recursive surfaces.

Let `S` be the retained value-expression occurrence count, `B` the binding count,
and `L` the maximum binding-name byte length. Compilation visits at most `S`
occurrences and retains at most `S` edges and `B` seeds. Each edge reads one
binding and proposes one Boolean target fact. A source grows at most once, so
closure evaluates at most `2S` rules and retains `O(S + B)` records, including
watchers. Ordered scheduling and key operations cost
`O(L * (S + B) log(S + B))`; the shared solver's hash tables have the usual
expected constant-time operations, or a conservative polynomial collision bound.

The separate unique-class resolver follows one successor at each step: the
single retained assignment of a binding, or a constructor's callee. It does not
follow alternative arms or member receivers. Its binding guard therefore bounds
each walk by `O(S + B)`, including false cycles; there are at most `S + B` such
walks during compilation and final unstable-binding classification. Including
these walks and their ordered guards gives a conservative preprocessing bound
of `O(L * (S + B)^2 log(S + B))` time and `O(L * (S + B))` retained bytes, plus
the already retained class metadata. No assignment paths are unfolded.

This bound covers class-provenance preprocessing only. Namespace equality and
publication still recursively follow assignments, and namespace alias inference
still wakes every assignment on growth of its single alias-set fact. Those
queries combine alias-dependent own-data projection with class publication;
their remaining polynomial-closure work is owned by PF6 in the analysis
performance plan. This class family neither changes author assumptions nor
certifies namespace privacy or publication on its own.

Small cycle and class-surface semantic controls run in `analysis_test`. Generated
depth sweeps are ignored there and run explicitly with
`bbr test //devinfra/js/debundle/e2e/bench:analysis_work_bounds_test` (without
`--config=ci`, which filters manual targets). They count actual expression visits
and inference edge evaluations; they do not measure elapsed time or allocation.

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
Multiple producers can contribute to one fact. `FactContext::propose` returns
separate value and proof changes. Only value changes wake semantic consumers;
typed dependency additions propagate directly through the premise index.
`evaluate_changed` therefore receives value changes only. Source-ordered rounds retain the
conservative initial-sweep semantics of source analyses. Growing dependency
families, including cross-module function effects and recursive purity, still
run to closure in their worklist. Only a completed closure is exposed outside
a producer.

Each proposal records edges from the live keys read before that proposal to its
output, including absent reads and unchanged proposals. Later reads cannot
retroactively become premises of an earlier output. New edges backfill the
source's current typed dependencies. A proof addition is joined into its target
before entering the proof queue, and only newly inserted atoms are forwarded.
Thus cycles terminate without executing a value rule or rediscovering producers
for evidence-only growth. The queue drains inside `propose`, so subsequent
reads and completed transfers in the same producer see the accumulated proof.
Explicit `assume`, `read_final` and `take_final` carry already established
dependencies without installing live edges. Their existing completion promises
cover both values and evidence; a consumed input cannot have a future producer
or a still-growing prerequisite.

The proof algorithm computes the least union closure of explicit typed
dependencies over all recorded premise edges. Initially every edge receives
the source's existing atoms. Each later source atom crosses each existing edge
once; insertion before enqueue prevents a cycle from forwarding an atom twice
from the same fact. Induction on edge creation and atom insertion establishes
that every consumed premise's dependencies reach its outputs, including edges
activated after the source acquired evidence. Value transfer results are
unchanged: rules can read values but cannot inspect typed dependency sets;
evidence is not a semantic branch condition. A witness that affects a decision
must instead be an explicit value fact.

For `K` fact keys, `E` distinct recorded proof edges and `D` distinct typed
dependency atoms, proof propagation attempts at most `2ED` atom deliveries
(new-edge backfill plus later additions). Membership and insertion cost
`O(log(D + 2))` comparisons per atom, giving `O(ED log(D + 2))` propagation work
and `O(E + KD)` retained entries, with expected constant-time hash lookup.
Even adversarial hash collisions give a polynomial bound:
`O(ED(K + log(D + 2)))` operations. Charge atom/key comparison and copy bytes
separately. Edge registration also costs one hash-set check per
read/proposal pair encountered by semantic evaluation. The source invocation
adapter below avoids repeating unchanged reads and delivers value deltas; other
adapters can still perform full value joins. This is not a linear-time solve.
Both bounds are polynomial in the finite fact/atom universe, without path
enumeration. `take_final` retires its incoming and outgoing proof edges with
the completed payload, in time proportional to those incident edges; no scan
of the whole graph is needed. Incoming edges reuse the retained explanatory
premise set, or a live input index when explanations are omitted. This keeps
ordered request/result transfers from accumulating proof indexes for dead
prefixes. Any remaining index lives only until this closure completes;
explanatory premise retention is a separate projection choice. Empty dependency
sets neither allocate payloads nor perform dependency-set cloning or joins.

`FlowRules::SOURCE_ONLY` is a checked contract: its source-origin, parameter and
property facts cannot introduce or inherit typed dependencies. `assume` and
`with_sealed` reject violations. It therefore allocates no proof-edge index
for the dense whole-program value store. Invocation argument invalidation uses
the returned value-change bit, rather than treating evidence growth as a
parameter-value update. Families carrying typed evidence, including recursive
purity, use the shared proof queue instead of a family-local evidence replay.

### Source invocation origin deltas

`FlowRules` owns the context-insensitive local/program-qualified origin closure.
Its inputs are the prepared `FlowProgram` events, declared-binding bits and
immutable origin table; imported identities enter through the same resolved
binding-link events. It produces binding, property and return may-origins and
parameter metadata. `CallEffectRules`, completed candidate reachability and the
`ChunkValueFlow`/program projections consume the completed closure. The event
cache, dependency indexes and delivered frontiers live only during that solve;
they are neither saved artifact semantics nor execution/initialization proofs.
There are no policy or author-premise inputs to this source-only stratum.

| Transfer                    | Inputs and evaluation boundary                                                                                                                      | Output and consumers                                                                                                         |
| --------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| Invocation target discovery | Exact callee expression at event entry; absent reads are watched. Recompute only after a consumed value key grows.                                  | Newly discovered function source sites, indexed per invocation; readiness remains separate from identity.                    |
| Formal-edge activation      | `ReadyParameters(function)`, produced by metadata after `ValueFlowActive` exists. An absent readiness fact is watched without evaluating arguments. | One edge per invocation, source argument position and identifier formal. Destructured/absent formals create no edge.         |
| Argument delivery           | Exact argument result at its first ordered use, then only when a consumed key grows; activated consumers are indexed by argument.                   | Only origins not previously delivered on that edge are joined into its formal. Late edges backfill the current exact result. |

This is growing inference with a specified source-order compatibility contract,
not arbitrary queue independence or JavaScript execution order. The common
solver retains its original source-ordered initial sweep and rounds. An event
snapshots its callee targets before writing any formal. Each activated target's
argument edges run in function-source-origin order, then argument-position order.
A formal write is immediately visible to later arguments and targets in that
event. It dirties arguments through an indexed read-to-argument relation,
including missing/property/return reads. Only later edges run again in this event;
already-passed edges and callee discovery retain their next-event wakeup. A
cached argument is never evaluated until an identifier formal actually uses it.
Metadata-only programs preserve the `ValueFlowActive` readiness exception.

Expression results stay exact and event-local. In particular, a nested missing
property can initially yield unknown and later yield a known callback after its
receiver/property becomes available. The old formal retains its earlier unknown;
a newly activated edge receives the current argument result, without that
historical transient unknown. Do not replace these exact intermediate results
with a shared joined argument fact or activate flattened expression nodes early.
A missing property read contributes conservative unknown when it is actually
observed; no later update retracts it from a formal that received it.

`FactContext::propose_delta` delegates atom subtraction to the rule family's
`difference` algebra and keeps one monotone delivered frontier per activated
edge. Unsupported algebra panics instead of silently reporting an empty delta.
It submits only new origins to the formal's join. Sealed input ownership is
checked even for a value-empty delta; sealed reads retain evidence and explanatory
premises without adding live watchers. Empty value deltas still attach
the proposal's current proof reads and backfill evidence on a newly activated
proof edge, without waking value consumers. Common proof propagation remains
owned by `IterativeInference`; the source-only invocation adapter cannot introduce
typed premises and therefore needs no cached-read evidence replay. A future
premise-bearing adapter must supply its actual proposal-specific proof reads,
including on late edge activation.

**Preservation.** The first callee evaluation is unchanged. Known function origins
of these expressions only grow (an unknown alternative may disappear), so bitmap
subtraction discovers each target once. Metadata is immutable once ready.
Induct over the ordered argument edges in one event: each exact argument sees
the same facts as the previous transfer, because every earlier write dirties
all of its readers and later edges use their refreshed results. Skipping an
unchanged edge contributes nothing new. Subtracting its delivered frontier
preserves its accumulated formal contribution by set-union idempotence, including
origins that disappear and recur in exact results. A late edge starts with an
empty frontier and receives only its current contribution. Complete persistent
solver watchers preserve the next-event wakeups for already-passed edges and
callee changes. This argument preserves the existing conservative closure; it
does not strengthen its authority over effects, readiness or execution order.

**Cost and retained state.** Let `I` be source invocations, `A` source argument
positions, `F` function sites, `U` explicit origins (including unknown), `K` fact
keys, `E ≤ AF` activated argument/formal edges and `W` registered watched reads.
No identity contains a call path, world, argument subset or solver visit. Each
edge delivers each distinct origin at most once: at most `EU` origin deliveries.
The exact expression cache costs `O((I + A)U)` bitmap positions; its retained
read/consumer indexes cost `O((I + A)K + E)`, and frontiers cost `O(EU)` positions.
All are retired at completion. New edges cost their current backfill, not an
empty-only subscription. New callee-origin discovery and parameter-edge creation
are separate from formal-origin delivery.

This delivery bound is not a bound on all work. Each dirty argument schedules
its indexed consumers; an event's ordered pending set visits an edge at most once
in that event, even with self-feeding formals. Charge each edge visit's exact-set
subtraction and frontier union to bitmap operands (`O(U + 1)` worst case), and
its queue/read-index operations to `O(log(E + K + A + 2))` comparisons. Recurring
origins in refreshed exact results cost subtraction work even though they are
never redelivered. Recomputed callee/argument expressions keep their existing
bounded syntax walks, dynamic property/return discovery and bitmap joins;
charge those separately, including absent reads. Bitmap operations are not
constant time. Test counters distinguish callee/argument evaluations, target
births, edge activation/visits, difference-input cardinality and delivered origins.

For an explicit source-byte bound `n`, fixed source-coordinate keys give
`I,A,F,U = O(n)`, `K = O(n²)`, `E = O(n²)`, and retained frontier/read/solver
positions `O(n³)`. Lowered expression copies cost at most `O(n²)` source nodes.
There are at most `O(n³)` strict fact insertions and `O(n³)` watched rule/key
pairs, hence at most `O(n⁶)` source-event evaluations under the common bound.
An event visits at most `O(n²)` ordered edges; each exact expression walk and its
bitmap joins cost at most `O(n³)` with expected constant-time key lookup. This
supplies a loose `O(n¹¹ log(n + 2))` time bound (`O(n¹³ log(n + 2))` under worst-case hash collisions),
plus preparation and completed downstream strata. These loose bounds establish
polynomiality, not near-linear scaling or a measured corpus improvement. Native
optimized corpus profiling is a separate acceptance obligation.

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
contribution is already merged. Purity consumes changed callee keys and compiled
capability containment consumes changed alias keys this way. Sealed candidate
reachability instead propagates completed summaries on its SCC DAG.

Recursive purity grows body-completion facts and effectfulness bits. The
canonical classifier emits an obligation at an inferred local or imported call
instead of reading an optimistic Pure verdict. All function effects in an
export-analysis context, across module boundaries, share one
`IterativeInference` worklist. Each completed body is indexed once into direct
effect witnesses and unique caller-to-callee edges. An Effects rule initially
reads its callees; subsequent evaluations consume only changed callee keys.
Effects facts retain typed captured-input dependencies, including dependencies
from pure callees, but do not retain diagnostics. After closure, one pass over
the original ordered body events materializes each direct witness and each
callsite whose callee is effectful. This keeps diagnostic growth out of the
effectfulness fixed point and preserves one reason per source callsite.

For `V` completed bodies and `E` unique call edges, Boolean effectfulness
propagation visits `O(V + E)` facts/edges after body indexing and has no path or
SCC expansion; pure recursive cycles need no optimistic seed. In the
dependency-free case each edge is read at most once initially and twice more
for fact creation/effectfulness growth, and output materialization scans each
body event once. The general bound must also charge proof dependencies: if the
program contains `D` distinct typed dependency atoms, one Effects judgment can
gain up to `D` atoms. Those additions use the shared proof queue without
rechecking effectfulness or rescanning call obligations. Dependency propagation
and retained per-function output sets add the costs above; the `O(V + E)` statement applies only
to the Boolean closure, not total proof or diagnostic output. Unknown targets
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
so value changes wake semantic consumers and proof additions follow their live
premise edges.

## Sealed callback source effects

Deferred-callback and class-member promotion consume one source-only
synchronous-effect index in `ExecutionInputs`. It closes the existing
`synchronous_owner_adjacency` with the same coverage-checked local reads and
rebinds the interpreter already uses. `SourceUnitReadClosure` condenses that
fixed union graph and transfers each completed component summary across each
condensation edge once. Owners in an SCC borrow the same completed sets. The
index is initialized on first use, shared by all prefixes and callback requests,
and retired with `ExecutionInputs`.

For a root `r`, the summary is exactly the union of local effects at every owner
reachable from `r`. SCC condensation preserves reachability; bottom-up union
therefore equals the per-request source traversal. These consumers attribute
all promoted effects to the active top-level effect owner, so unioning before
promotion preserves their read/rebind records. Every request still applies the
current assumption/provenance and initialized-binding state; source summaries
carry no execution, normal-completion, heap or availability authority. The
source inputs are sealed and introduce no author premise, so this closure
needs neither runtime watchers nor a proof-change protocol.

Dynamic callback roots are rediscovered from the current heap, captures,
exports and escaped capabilities before querying this index. Missing/wildcard
properties and mutable capture cells stay in that live traversal. Class
superclass promotion also remains dynamic. Incomplete reachability retains the
whole-lazy-effect fallback. The separate prefix-availability traversal and its
deferred lexical roots are unchanged: a sealed source effect union never
initializes a future binding or publishes its deferred callbacks.

Let `N,E,L` count owners, source edges and local effect entries, `B` declared
bindings, and `C,D` SCCs and condensation edges. Preparation/indexing requires
`O((N+E+L)log(N+B+2))` ordered operations. Bitmap closure costs
`O((C+D)B)` in the worst case; materializing ordered identifier sets costs
`O(CB log(B+2))`, with identifier comparisons/copies additionally charged to
their source bytes. Retained state is `O(N+E+CB)` records. All domains are
source-bounded. A root query performs `O(log N)` index work and borrows its
summary; current-prefix effect application still costs its output size, and
unions for distinct roots can overlap. This removes repeated source-edge
traversal and per-execution class-summary construction, not dynamic heap
walking, opaque-pool replay or all callback work.

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

Value-only closures retain typed dependencies and read subscriptions without
an explanatory premise graph. Evidence-carrying families keep their live reverse
proof index until closure even when that explanatory graph is omitted;
source-only families keep neither index nor typed dependency payloads.
Each judgment allocates its premise set only
when it gains a direct premise; empty sets cost one optional pointer. The
premise iterator still yields sorted, unique keys, including when a contribution
adds premises without changing the fact's value.

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

### Bounded call-return relationships

The source collector recognizes bounded normal-return relationships through
two kinds of proof. Immediate-expression bodies of synchronous functions and
arrows with one plain identifier parameter admit a return of that parameter,
a static property read from it, and the exact
`value && value.__esModule &&
Object.prototype.hasOwnProperty.call(value, "default") ? value.default : value`
interop shape. The property-return form also admits a sequence prefix of
zero-argument calls through identifier callees (excluding direct `eval`).
Those calls remain in the ordinary invocation/effect analysis. Recognition
uses resolved parameter identity, not a library or binding name.

The source lowering pass also prepares finite normal-return candidates for
ordinary synchronous functions with plain parameters. A candidate has one final owned
return, whose sequence tail is either its unchanged first parameter or a
conditional of two direct helper calls on that parameter. The same lowering
walk checks canonical binding writes, including captured writes, declaration
initializers and loop targets, and rejects dynamic scope. Sources parse as strict
modules, so arguments writes do not alias parameters. Property writes and other call effects remain ordinary; this relation grants no
purity, successful-return or native-integrity proof.

At a forwarding call, both helper target sets are retained. Only leaf argument
or argument-property relations are instantiated with this call's actual input;
forwarding, recursive and unsupported helper targets use their ordinary global
return facts, and unknown targets remain unknown. The query never recursively
expands forwarding summaries. Global returns, formal transfers, containment and
all effects are unchanged. Provider helper binding coordinates are rekeyed with
the program, and local/completed queries use the same finite interpretation.

Async/generator functions, parameter defaults/destructuring and other bodies
retain the ordinary pooled return relation. Census-certified bodies may read
captured cells and perform arbitrary prefix effects; their proof establishes
that none of those operations can rebind the returned parameter cell.

Each recognized function allocation has one constant-size certificate. A call
with an actual first argument and no spreads instantiates its relationship with
that argument; other targets retain their pooled returns and opaque alternatives.
The default-wrapper result includes both branches. Its property projection uses
the ordinary flow transfer, including mutable-field producers and unknowns for
missing properties, accessors and unsupported receivers. First-argument
expressions are interned once per source call; nested calls reference the arena
instead of copying nested argument trees into every expression consumer.

The proof is a restricted substitution argument. Immediate-expression bodies
cannot expose their parameter cell; census-certified bodies may capture it but
have no canonical rebinding or dynamic scope. On normal completion a leaf
returns that call's actual argument, the result of reading a named property
from that argument, or the result of reading its `default` property. Property
projections use the ordinary property transfer, retaining its known producer values and its unknown result
for missing or unsupported properties. A static property name is only a
value-origin relation: it does not establish that the property is an own data
field or that a getter is harmless. The guard can select either interop branch,
so that value relation needs no assertion that the guard denotes a native
operation. These summaries supply no effect, purity, initialization or
completion certificate. Original invocation events and body effects remain
live, including shadowed guard calls and mutations.

The fixed point tracks the actual argument and projected property dependencies,
so later facts wake their consumers. Function identities and argument references
are qualified before whole-program solving. Completed expression queries use
the same return relationships; saved analysis persists their derived statement
facts through the existing artifact boundary. These source facts do not depend
on a module assignment and introduce no precision setting or author premise.

This avoids unrelated call-result origins rather than specializing arbitrary
function executions. The independent identity/projection family has linear
property-read work, pinned by a deterministic growing-input test and the public
identity benchmark. That bound describes this family, not the total analysis
cost of arbitrary programs. CLI regressions exercise the corresponding module
cut, fresh/saved parity and preserved invocation effects.

Declared callback factories also refine direct result origins before source
flow. Raw export stability and a complete, dynamically closed static provider
closure excluding the importer establish allocation ownership. Namespace-member
calls additionally consume the source-only namespace privacy receipt from the
shared raw execution index. All configured potentially aliasing namespace roots
are checked together, including roots with no asserted independent members.
Only declared factory members also covered by that receipt are eligible; the
later value-flow-dependent namespace analysis cannot feed this solve.

The fresh-input proof is structural induction over immediate calls. An inline
function/arrow starts with a fresh ordinary graph. An eligible single-argument
factory returns a fresh ordinary graph preserving its callback, by its existing
author contract. Passing that immediate result to another eligible factory
introduces no intervening exposure or mutation. Thus every certified call's
direct result excludes importer allocation sites. The exclusion does not touch
the global return/formal cells, retained members, unknown alternatives or any
effects. Named callbacks, aliases, projections and other expression shapes do
not receive this certificate. Real CLI tests distinguish independent wrapper
metadata from actual callback/namespace alias mutation and check saved parity.

The collector reuses each lowered argument's certificate from its existing arena,
with no heap traversal or alias search per call. Namespace privacy piggybacks on
the existing source visit; policy intersections are prepared once per importer,
and provider closure queries remain memoized per distinct provider edge. Each
call adds bounded shape checks and indexed identity lookups. The importer
allocation mask is still shared, and ordinary source propagation retains its
existing cost.

`facts/value_flow/program.rs` prepares a source-qualified instance of the existing
`FlowRules`, lexical-access rules and candidate reachability closure.
`cross_module_purity` builds it after the raw
native census and inferred Set roots have closed, then hands its chunk views to
callback-capability inference and structural/purity preparation. The transient
`ProgramValueFlows` also travels through `ProgramSummary` to final chunk-fact
computation, replacing its second local solve. Saved summaries restore derived
chunk facts and never silently substitute a local solve for missing flow.
Standalone chunk analysis uses the same collector, native-root preparation and
value-plus-candidate closure.

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
composes source witnesses over those completed values. Candidate containment is
then a sealed graph relation: an origin has an edge to a carrier or binding when
that completed value fact contains the origin and it passes the candidate-origin
mask. Missing carrier facts contribute no edges. Explicit candidate seeds are
the only non-bottom facts; only seeded or reached keys become output facts.
This stratum never wakes source events or observes their scheduling rounds.

### Borrowed candidate-containment adjacency

Do not expand the completed origin bitmaps into an individually allocated edge
table. Each carrier/binding fact already supplies its incoming neighbors. The
closure borrows those rows as the **reverse** graph, using dense node identities
and an origin-to-node index for neighbor lookup. The existing candidate-origin
mask, native Set identity, function-return carriers and explicit `Top` seed
semantics still determine the exact relation. Extra isolated unseeded origin
nodes, if allocated for indexing, cannot produce output facts.

SCC discovery traverses that borrowed graph. It returns producer components
before consumer components for these reversed edges. For each component, scan
its members' incoming neighbors and union each distinct external component's
completed summary once. One reusable component-mark vector deduplicates those
inputs; there is no retained condensation-edge table. Members of one SCC share
one completed candidate bitmap. Typed proof dependencies travel with the summary
until the existing value-only projection.

**Preservation.** Let the previous forward graph contain `p -> q` exactly when
`q`'s completed carrier fact contains eligible origin `p`. The borrowed iterator
contains `q -> p` under exactly the same condition, so reversing it preserves
SCC membership. The component summary is its seed bits union the summaries of
its predecessor components in the original graph. Processing those predecessors
first computes exactly the origins reaching every component, by induction on
the acyclic component order. Deduplicating predecessor components changes no
union result.

Proof dependencies require an additional condition: a node's local dependencies
are consumed only when a nonempty candidate set traverses an incoming edge.
An external predecessor with nonempty bits activates the destination component's
local dependencies. A reachable internal cycle does the same for all its members.
A singleton without a self-edge and without a reached predecessor keeps only its
own seed and does not acquire local dependencies. This preserves the distinction
between a seed and a consumed carrier input; empty predecessor summaries cannot
manufacture evidence. The independent reachability/proof oracle covers these
cases, including seeded and unseeded cycles.

**Cost.** Let `A` be the origin-ID domain, `F` completed fact rows, `L` origin
memberships examined while filtering rows, `V` graph nodes, `E` eligible logical
edges, `S` components, `D` distinct component edges and `R` candidate roots.
Preparation scans the completed facts and borrows their bitmap rows. SCC and
summary preparation stream logical edges a constant number of times, charging
mask checks over `L`; graph traversal itself is `O(V + E)`. Seed insertion and
summary propagation perform at most `R + D` bitmap unions, each bounded by `R`
root IDs. Proof-set unions must also be charged by their actual payload size.

The graph's additional retained storage is `O(A + V + S)`, including dense
indexes, row references, SCC state and the reusable deduplication marks. It has
no additional `O(E)` or `O(D)` edge records. Existing input bitmaps remain owned
by the completed value store. Result bitmaps hold at most `O(SR)` memberships
plus `O(V)` shared references; emitted proof payloads are an additional cost.
An SCC algorithm's bounded traversal count alone is insufficient if preparation
first expands the compressed relation into a large hash-indexed graph.

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

**Completed-value read index.** After solving, move each value fact into an
immutable index over the existing dense binding and allocation-site domains.
Bindings, return summaries and dynamic-property summaries have direct slots;
each site's populated static properties form a sorted sparse row. No table is
allocated for the product of sites and property names. Chunk preparation visits
that site's row directly instead of searching a program-wide ordered map.

This is an exact representation change. Each supported key identifies one slot
or one `(site, property)` row; construction rejects duplicate keys. Absent rows
remain absent, and an explicitly stored empty value remains present. Sorting
changes only row order. Moving each `Values` preserves its bitmap Arc and opaque
alternative, so immutable-identity projection caches retain their lifetime and
sharing guarantees. No transfer, source qualification or projection rule changes.
Tests compare the index with the original completed key/value map from a real
source solve, including sparse properties, missing reads and bitmap identity;
fresh/saved CLI tests remain the behavioral check.

For `B` binding slots, `S` sites, `F` populated value facts and `P_s` static
property facts at site `s`, construction costs
`O(B + S + F + sum_s P_s log P_s)` with `O(B + S + F)` index storage, excluding
the unchanged shared bitmap payloads. Binding and summary lookups are `O(1)`;
static-property lookup is `O(log(1 + P_s))`, and enumeration is `O(P_s)`.
Production retains no second full map of the same value facts.

**Candidate-summary projection.** Preserve the closure's shared SCC bitmap
through coordinate conversion. Each source view interns local projections by
the identity of an immutable summary in its retained `shared.reachability`
store. Per-origin output rows clone the resulting Arc. Candidate-binding rows
use a construction-scoped interner: their source summaries are not retained by
the view, so their addresses must never enter its persistent identity cache.
Transient expression queries reuse prepared summaries, but any previously
unrequested summaries are projected only for that query.

Within one translated value set, union each distinct source-summary identity
once into a local candidate-index bitmap, then convert the resulting candidate
union to local origin IDs once. Candidate indices and origin IDs are separate
domains. Owned origins retain their source offsets; every erased foreign origin
still contributes `Top`, even when its candidate summary is empty. `Top` remains
the separate unknown bit in `Values`.

This preserves the previous result because the source summary and module's
coordinate map are immutable, projection distributes over set union, and union
is idempotent. Repeated occurrences of one summary cannot add a new member.
Different summaries remain separate inputs even if they happen to have equal
contents. No transient query result enters the completed-value identity cache;
no construction-only source address outlives the object it identifies.

**Bounds.** Let `n` count explicit source/selected-input bytes, `A` origins, `B`
binding slots, `P` static property names, `C` candidate roots, `R` source/import
constraints and `S` their total expression/parameter bytes. Each is `O(n)`;
none depends on the number of concrete calls, paths, environments or schedules.
There are `K = O(B + AP + A + 1)` value/metadata keys and at most `K(A+1)`
strict value insertions. Parameter/readiness metadata is immutable and changes
at most once per site. There are `O(A+B)` candidate keys and at most
`O((A+B)C)` output candidate memberships. One source rule can watch at most
`K` keys, so value-rule evaluations are bounded by `O(R(1+K(A+1)))`.
Candidate containment instead uses the sealed borrowed-adjacency closure above;
it creates no live rule watchers or iterative candidate evaluations.

For an expression of size `s`, traversal reads at most `O(s(A+1))` sets; each
set operation costs at most `O(A+1)`. An invocation visits at most `A` targets
and source-bounded parameter/argument vectors. Each invocation argument owns one retained result
and the exact value keys its last evaluation consumed. Changed-key notifications
invalidate intersecting results; a parameter proposal immediately invalidates
arguments that read that binding before later arguments or targets consume them.
A reused result replays its reads once per event evaluation, including missing
cells, retaining watchers and current proof dependencies. Proof-only growth is
also a changed-key notification. These slots live only for the value stratum;
they are dropped before candidate closure and never contain execution Worlds.
There are at most `S` slots, each retaining `O(A+K)` origins/read keys, so their
additional space is `O(S(A+K))`, still `O(n³)`. The expression walk for an argument
whose consumed inputs stay unchanged runs once even when its target set grows;
callee walks, target visits, parameter joins and dependency replay remain.
Property writes retain exact RHS results by source event too. Dynamic writes
also retain the exact receiver result and a set of reached Object/Set sites;
fixed literal receivers retain only the RHS slot. Immutable literal fields have
no slot; primitive-only Empty RHS transfers publish no may-origin and retain no
state. Each refresh reads the RHS before the receiver, before any property
proposal. An RHS refresh checks for added origins against the previous exact
result, with a bitmap pointer-equality shortcut. Only actual additions trigger
existing-receiver fanout, which proposes the shared **full current** RHS so
subset destinations adopt its immutable bitmap. A newly reached receiver always
receives the current exact RHS. In particular, an early missing-property `Top` that
has since disappeared is not backfilled into a late receiver. Published property
facts still join monotonically and retain any `Top` previously delivered there.

Property destinations reuse exact bitmap joins within one rule activation. An
eight-entry memo keys each result by the ordered pair of input allocation
identities. A hit either preserves the destination, adopts the incoming bitmap,
or shares the previously computed union. The independent unknown bit is still
joined for every destination, and every proposal still follows the ordinary
changed-key scheduling and proof-transfer path. Binding and return joins keep
their original path; pointer-equal property inputs bypass the memo too.

Weak input witnesses keep cached identities valid without retaining old bitmap
payloads. Copy-on-write either preserves a shared old allocation or dissociates
its Weak witnesses before mutation; a dead witness cannot validate a hit. A
unique-owner union creates no new Weak witness solely for caching. The memo can
retain at most eight union payloads and is dropped with the activation's context,
so it adds `O(A+1)` peak space rather than per-event or per-destination history.
Each lookup examines at most eight entries. A miss uses the original exact
subset/adoption/union algebra, including after eviction; worst-case join work is
unchanged. The `bitmap_join_memo_hit` and `bitmap_join_memo_miss` counters measure
reuse attempts. Existing subset, adoption and copy-on-write counters count only
work executed on misses, rather than replaying those counts on a cache hit.

Retaining reached receiver sites is valid for this expression grammar: concrete
origins are monotone by structural induction. Binding cells only grow; constants
and unions preserve concrete origins; Member keeps every previously concrete
receiver and reads only growing property cells; Call likewise keeps concrete
targets and reads growing return cells. An absent-cell `Top` may disappear, but
cannot remove a concrete Object/Set receiver. Both expression snapshots precede
writes, so self-property feedback invalidates consumed keys for the next event
run. Cumulative source watchers retain absent reads without replaying unchanged
cached reads. These rules remain source-only and introduce no proof dependency,
completion or runtime-availability receipt.

For a dynamic write with `a` reached receivers, each slot holds at most `A+1`
origins and `K` current read keys, plus `a` receiver IDs: `O(A+K+a)` space,
without an `a` by `A` delivery frontier. All property slots together add at most
`O(R(A+K))` space, retired with the value stratum. Receiver-only growth walks no
unchanged RHS and visits only new receivers. RHS-only growth walks no unchanged
receiver expression; it visits existing receivers only when the RHS gains an
origin. Refreshes still traverse the complete changed expression; RHS growth
checks compare complete bitmaps and receiver discovery subtracts complete
old/new bitmaps, `O(A+1)` per set operation. Destination bitmap joins and full-RHS
proposals can still scan accumulated sets. Thus `u` refreshes and `j` destination
proposals cost at most `O(u(A+1) + j(A+1))` beyond expression evaluation and
ordered indexing; submitted origin operands can remain `O(jA)` during RHS
growth. Per-receiver origin-delta joins remain deferred: applying a small delta
to many destinations sharing an old bitmap makes each copy-on-write join retain
a separate expanded bitmap. Full-current fanout preserves the original shared
payload when destinations are subsets of the RHS. Test sweeps compare unique
bitmap payload counts and bytes against the independent original transfer,
alongside set-comparison operands, receiver scans/visits, proposals/submitted
origins, and retained cache slots/origins/read keys. These counters do not
claim constant-cost joins or linear whole-solver work. Run the ignored generated
work-bound sweeps explicitly with
`bbr test //devinfra/js/debundle/e2e/bench:analysis_work_bounds_test` (without
`--config=ci`); semantic unknown/feedback and CLI tests stay in routine CI.
Property-read subtree incrementality remains separate work; the activation memo
does not make joins between distinct input allocations constant-time.

Charging all argument syntax to `S` gives
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
The stated projection bound is conservative. For one view, let `U` count the
distinct retained candidate summaries requested during preparation. Remapping
costs are paid once per such summary, with at most `O(UC)` local bitmap
memberships plus `O(U)` cache records. Output rows still require their keys and
shared references. A translated set visits every present origin, unions at most
one bitmap per distinct encountered summary, and converts at most `C` resulting
candidate indices. Charge bitmap unions by their actual operand sizes;
deduplication does not make them constant-time. Query-only summaries have the
same per-set bound but are not retained across queries. Candidate-binding
interning is bounded separately by the distinct summaries used during that
view's construction. Whole completed-set aliases require only a lookup and an
Arc clone. The stated time bounds
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

### Source initialization sites

`InitializationCensus` extends the existing top-level preparation visit. For
each binding it records its declaration owners, direct callable definitions and
single-binding lexical read guards. Bare `var` redeclarations and hoisted `var`
sites inside compound statements count as declaration owners even when they
write no value. Default exports, destructuring and conditional declarations do
not acquire a direct callable/lexical-guard certificate from their possible
values. Split declarators keep the prepared source-unit coordinates.

`FlowRules` publishes these records under `Initialization(binding)` keys;
their join is set union over immutable source records. Binding slots and
callable sites use the same program qualification as value flow. An owner
ordinal is relative to that binding's defining source, and projection moves
the records back only to that source's binding. Imports and ordinary value
copies never copy initialization records. Thus neither a possible callee
origin nor an imported live-cell identity makes a caller-local cell ready.

Two consumers read the completed census:

- Interpreter preparation installs a deferred-read guard only for a direct
  single-binding `let`/`const` owner with no other declaration owner for that
  binding. Ordered execution still checks `initialized_bindings` before
  traversing that owner's deferred bodies; incomplete reachability retains
  its existing conservative bypass. A source guard is not an observation
  that initialization completed.
- Foreign-call preparation gets direct function/class/initializer candidates
  and passive-provider eligibility from the same records. Function and
  class declarations remain distinct. Publishing their body summaries does
  not prove class readiness or the readiness of captured cells. A callable
  initializer is eligible when its closed provider has passive initialization
  and the derived call has no provider-cell read or retained-capture obligations.
  Other providers keep their existing fallback. The availability proof below is independent of
  the body summary and does not import future values into a caller's prefix.

**Preservation.** Preparation records every owner previously counted by the
statement census. A lexical guard's source shape implies the removed direct
lexical/definite-initializer checks; uniqueness excludes hoisted/`var`
redeclarations and imported bindings cannot satisfy the local declaration
shape. All derived-body, stability, native-integrity and captured-cell checks
still apply. Uniqueness and passive-provider eligibility are queried
only after the census closes; an intermediate singleton is never a certificate.
No value rule reads initialization facts, so their insertion changes no
possible-value result or parameter-readiness activation.

**Bounds and lifetime.** Let `D` be total binding/declaration-owner pairs and
`B` binding slots. Both are `O(n)` in source size, including repeated sites.
The census and union preparation cost `O((n+D) log(n+2))` integer/index work
and `O(B+D)` records. Each initialization producer runs once, watches no keys
and contributes its fixed source set; it never wakes on a growing value set.
Projection visits each record once. Charging source-name copying/comparison
gives a conservative `O(n² log(n+2))` added time bound and `O(n²)` space bound;
there is no whole-module scan per callable candidate. The shared store retires
these facts into source views during projection, and those views own them for
their existing analysis lifetime. Saved artifacts retain the derived chunk
facts and callable obligations, with no new persisted declaration graph.

#### Closed passive provider availability

The same source visit checks that every item is an ordinary simple `const`
initialized by a function, arrow, or primitive literal; a function declaration;
an empty statement; or a local named export. No initializer reads a binding,
accesses a property, calls, coerces, destructures or executes a function body.
Imports, reexports, mutable declarations and every other executable shape
refuse the certificate. The completed census marks direct callable initializer
sites only after all source items have been checked. Callable inference still
requires one stable function value, no dynamic scope, a successful shared body
derivation, and no provider-cell read or retained-capture obligations on that
call summary. Deferred methods can still refer to the provider's immutable
primitives; those cells are initialized before importer execution, and later
method calls retain their opaque execution and callback handling. Passive
initialization does not certify a method's effects or make an invoking or
effectful factory body eligible.

**Availability under emission.** Every emitted provider owner contains only
these inert source items. Generated imports and export forwards point to other
owners of the same source: no source item imports an external module. Such
owners can form cycles through deferred body references, but no owner's body
calls user code or reads another owner's lexical cell during initialization.
Consider the DFS induced by any admitted emitted import priority. A provider
SCC has no edge back to an external consumer. Its inert bodies run to completion
before the DFS returns to that consumer; in particular the owner of the imported
function initializes it first. Export forwarding stays inside this closed
subgraph and preserves the same conclusion. Thus the body summary can be used
without dropping a possible preinitialization call. Existing local graph and
ordering validation remains required. Hoisted declarations and classes retain
their separate readiness rules; active/cyclic external providers gain none.

**Cost and lifetime.** Passive admission inspects each source item and each
local export specifier once, testing only the initializer's outer AST node.
Finalization marks each existing declaration record once. For `n` source bytes
and `D` declaration records this adds `O(n+D)` time, one census bit and one bit
per callable initializer record. No body or transitive graph is rescanned per
export, and no runtime request, context, cache or relation universe is added.
The existing initialization census's polynomial bounds and retirement apply.

### Read-only opaque callback pools

Source preparation records a closed shape certificate for synchronous,
zero-parameter functions and arrows whose bodies contain only empty statements,
bare identifier/primitive-literal expression statements, and returns of those
operands or `undefined` by a bare `return`. Calls, writes, allocations, local
declarations, control flow, property operations, coercions, regex literals,
implicit receivers and parameter evaluation are outside this family. The
certificate reuses `CallEffectBody.reads`; it does not infer effect absence
from an empty may-read set. `CallEffectRules` publishes each immutable
`ReadOnlyCallback(function)` fact once in the existing inference engine.
Its returned-binding set is separate from the read set. Sites and binding slots
are program-qualified, then projected only to the defining source.

`summarize_read_only_callback_pool` requires the complete admitted pool to
consist of these source function values. Captured closures, foreign callbacks,
mixed pools, active recursion, dynamic scope, incomplete reachability and array
iteration keep the existing execution path. Every read must resolve to a local
module binding; imports, globals and implicit `arguments` are excluded. At each
request the shared returned cells are checked once against current prefix
values. Every alternative must be primitive or `Unavailable`; empty/unknown,
object and callable results retain replay. Potential whole-program values do
not establish this condition. Function-surface taint is harmless for this
grammar: mutating a function's properties cannot change its source body, and
the body performs no operation on that surface.

**Correctness.** A covered member cannot change any lexical cell, heap surface,
native capability or publication root. Its only possible abrupt outcome is a
lexical read throwing; its successful return exposes no object or callable.
Consequently any finite skip/repeat/permutation of the admitted pool contributes
a subset of the union of its lexical may-reads at this cut. Each member's read
set is promoted once with the existing owner, opaque-call assumption and current
uninitialized-cell obligations. Unreachable reads may conservatively remain.
No normal-read, ordered-call or whole-owner completion receipt is created.
Owner/admission bookkeeping is preserved; extending owner reachability cannot
change the pool's export-prefix/escaped-value/heap roots, and dynamic scope is
excluded. The ordinary argument taint and retention transfer still follows
the summary. Unsupported members prevent the shortcut for the entire pool;
there is no assumption that a covered member stays read-only beside a writer.
There is no invocation or whole-world observation for a covered pool, and no
invocation-limit loss manufactured solely by its size.

**Bounds and lifetime.** Let `n` count source/input bytes, `F` admitted source
functions, `R` their total explicit read/return incidences, and `B` distinct
returned bindings. `F,R,B=O(n)`: no source body can create a fresh summary key
on invocation. Shallow shape preparation, source qualification and projection
visit each incidence once. Each certificate producer has no watched inputs,
runs once, and asserts agreement if its immutable fact is contributed again.
Charging copied/compared identifier bytes gives `O(n² log(n+2))` added
preparation time and `O(n²)` retained certificate space.

For one pool request, membership/owner/read unions cost
`O((F+R) log(n+2))` index operations. Each returned binding is queried once;
primitive-kind alternatives have a fixed finite universe, and the first
nonprimitive alternative refuses the shortcut. Name copying/comparison and
effect insertion give a conservative `O(n² log(n+2))` bound, in addition to
the existing owner-reachability transfer and callback-pool discovery. There
is no enumeration of orders, subsets, argument products or execution paths.
For `K` upstream requests the added work is `O(K n² log(n+2))`; this is a
per-request/phase bound, not a new bound on the entire ordered interpreter or
ESM scheduler. Certificates live with the existing source-flow views; the
per-request unions retire at return. Saved analysis retains their derived
effects and obligations, with no process-local execution cache or new saved
graph. Later requests recheck current cells, including after initialization
or mutation; no recency policy controls validity.

## Sealed inputs and completed transfers

`IterativeInference` owns growing judgments and worklist indexes. Consuming a
completed `IterativeFacts` with `seal()` produces `SealedFacts`, which exposes
shared reads and has no producer, resumption or removal API. `with_sealed`
borrows that capability and builds a separate growing overlay. Proposing or
consuming a key present in the sealed input is rejected. Reading it forwards
its typed dependencies and direct explanatory premise key without installing a
value watcher or live proof edge. A missing key still installs an ordinary
overlay watcher: a sealed positive closure never certifies absence.
The result contains only overlay judgments; the semantic owner keeps the
sealed input alive for inherited judgments and premise expansion.

The production boundary is source argument/callable evidence to native-aware
`SemanticFacts`. Source inference seals after the canonical value-flow solve
and argument-input validation, before PlainData and the native census. The
native-aware solve borrows that source store and retains its existing
`Arc<SemanticFacts>` for later purity, timing, heap and diagnostic consumers.
Inherited positive keys have no later producer; candidate keys absent from the
source store can still derive using the completed native census. Each stratum
keeps its own consulted-rule map, and diagnostic lookup follows the same
shared source handle. This boundary accepts one source stratum; arbitrary
chains of overlays are outside its contract.

Own-property projection is the migrated ordered transfer. The active executor
moves an `OwnPropertyQuery`, including its current prefix shape, into the
existing projector. The projector finishes synchronously before constructing
`CompletedTransfer<Option<OwnPropertyProof>>`. The transfer has no shared-read
or clone API; consuming it moves the result and forwards its proof dependencies.
`None` stays missing evidence. There are no request/result keys, producers,
watchers or proof indexes for this transfer, so payload ownership and dependency
scratch retire at that consuming call. The transfer grants no authority beyond
the unchanged own-property proof.

Let `S` be source fact/proof bytes, `O` overlay fact/proof bytes, `E` overlay
live/explanatory dependency entries and `C` consumer handles. This boundary
retains `O(S + O + E + C)` space; an independent consumer does not clone `S`.
Each sealed read costs one lookup plus the actual copied proof atoms and
premise key. Each own-property transfer moves its query/result once and clones
its `D` proof bytes once for dependency forwarding, retaining `O(D)` temporary
proof bytes until consumption. It retains zero worklist index entries across
that call. These are ownership bounds for this slice, not a whole-engine
complexity or measured performance claim.

Runtime prefix, general request/result, prepared/completed purity and invocation
memo families still use the unchecked `read_final`/`take_final` contract. Their
PS2 migrations must separately enforce completion, source/prefix order and the
last-reader boundary; they cannot infer absence or reuse a retired version.
