# Purity classification — soundness notes

Admission rules and long-form rationales for the whitelist tables and
`ChunkBinding::PlainData` in `devinfra/js/debundle/purity/`. The code holds
3–5 line summaries; the exhaustive prose lives here so future audits don't
have to scroll past 80 lines of `///` to read the next function. Read
<design.md> § "Soundness over completeness" first — it is the contract
every rule below serves.

## Redundant purity hints

`modules.json.redundant_purity_hints` probes each `purity: pure` hint with
that hint disabled. `proof_basis: without_purity_hints` means the binding also
admits with every chunk-local `purity: pure` hint disabled;
`with_other_purity_hints` means this analyzer proof requires other hints.
The probes exclude imported purity verdicts and other author contracts. They
still assume the documented runtime and intrinsic-integrity conditions.
Remove hints individually and validate the resulting spec: individual redundancy
does not establish that a set of hints can be removed together. A missing entry
means redundancy was not proved.

Member hints are reported redundant against the builtin whitelist only for
unshadowed global receivers. An import or local binding named `Array` does not
inherit the global `Array.isArray` rule.

## Admission rule: `PURE_STATIC_CALLS`

`PURE_STATIC_PROPS`, `PURE_STATIC_CALLS`, and `PURE_GLOBAL_CALLS` in
<../purity/whitelists.rs> directly drive S-edge construction. An
over-approximating entry — a built-in flagged Pure that can in fact fire
user code on some argument — drops S edges the realizability theorem needs,
and can let a cyclic spec slip past the validator and emit a bundle whose
runtime ordering breaks.

A new entry is admissible only when:

- The operation fires **no user-defined code on any argument type**: no
  `ToNumber` / `ToString` / `ToPrimitive` / `ToPropertyKey` coercion of the
  args (these fire `valueOf` / `toString` / `[Symbol.toPrimitive]`); no
  iterator protocol (`[Symbol.iterator]`); no proxy traps; no own-property
  `[[Get]]` / `[[Set]]` (these fire user accessors); no mutation of any
  reachable object; no `toJSON` callback path.
- It cites the ECMA-262 clause that establishes this. "Common in production
  bundles" is not a soundness argument, and neither is "often called with a
  fresh-literal target or primitive args in practice" — the validator does
  not infer types and cannot rely on argument shape unless it is enforced
  syntactically at the call site.

If the whitelist proves too restrictive in practice, that is the expected
failure mode. Loosen it only by adding universally-pure operations, or by
introducing stronger argument analysis (e.g. a statically-primitive
classification) that admits a wider set _under syntactic gates on the
arguments_. Never add an entry because the common case is fine. The
analysis engine carries an inline TODO listing patterns that become
admissible once a primitive-arg gate exists — that is the queue for safe
growth.

Every admission argument here additionally assumes **intrinsic integrity**
(<design.md> A11): the chunk runs with unmodified built-in prototypes.
Prototype pollution — replacing `Set.prototype.add` or
`Array.prototype[Symbol.iterator]` — defeats every entry whose ECMA-262
citation describes the standard built-in, and the analyzer cannot detect it
when the pollution lives outside the analyzed chunk. Individual proofs may
also reject visible in-chunk mutations through the shared intrinsic-write
census; see the collection-construction rules below.

## Empty intrinsic collection construction

Synchronous callback analysis excludes an exact `new Map()`, `new Set()`,
`new WeakMap()` or `new WeakSet()` when the callee is the undeclared,
unshadowed global identifier and there are no arguments. Under A11 intrinsic
integrity, each constructor allocates its empty internal collection and
returns before looking up an adder or invoking iterator/user code; the
constructors' rows in `facts/abstract_domain/builtins.rs` cite the algorithms.
The shared intrinsic-write census disables this
shortcut after a visible write to the corresponding global constructor or
global-object property, including writes through aliases and unknown computed
global properties. Program analysis shares its resolved census across all
analyzed chunks; isolated per-chunk analysis uses its local census. Dynamic
scopes disable it too.

This does not certify subsequent collection operations. A shadowed or dynamic
callee, subclass construction, arguments and spreads keep ordinary callback
analysis. In particular, a nonempty iterable argument may invoke its iterator
or expose callbacks. The CLI cases in `empty_collection_timing_test` cover
all four empty constructors, global constructor replacement, and callback
reentry through a shadowed constructor, a replaced Set adder, and a replaced
Array iterator.

## Property-key domains in native writes

The canonical execution index retains a separate key domain for native-write
facts: a finite set of strings, definitely Symbol, or unknown. This refines only
which named global bindings a write can replace. A symbol-key write on `Map`,
`Object`, or another intrinsic still invalidates that intrinsic family.
Key evaluation, callbacks, effects, throws and returned values retain their
ordinary analysis.

A stable alias of an unshadowed `Symbol.for` call with one literal string
argument has a Symbol domain only while the Symbol family remains intact.
A lexical two-identifier `[key, value]` loop over unshadowed `Object.entries`
of an immediate object containing only noncomputed own data properties has
that object's finite string-key set. Its premises include the Object family
and Array iteration for both the outer entries array and each destructured
pair. Computed properties, accessors, spreads, default patterns, unknown keys,
shadowed operations and any binding reassignment retain the unknown domain.

A11 supplies the initial native identities. The whole-program census retains
all writes, including deferred bodies, and establishes a closed-write
invariant: each refined write preserves named native global bindings provided
its operation dependencies remain intact. Start with unconditional native
writes and unknown global writes, then propagate dependency invalidation to a
fixed point. A recognized visible Symbol, Object or Array mutation in any analyzed chunk
invalidates dependent key evidence; unknown global mutation invalidates every
native premise. A collection provider and its consumers use the same resolved
source-derived integrity input, separate from author assumptions. Code outside
the analyzed source remains covered by A11. No whole-program purity or
nonreentry conclusion follows from this census.

Key disjointness applies only to a receiver proved to be the actual global
object. A value read from a computed or unrelated static global slot can hold
an intrinsic; its unresolved native identity survives aliases and descendant
property accesses. Mutation through that value invalidates native premises.
The census does not prove the absence of every hidden native-alias path:
A11's author obligation still covers native integrity outside the recognized
source facts.

## Source callback arguments and unreachable native writes

The immutable returned-callback-wrapper inference can additionally certify
ordinary object arguments and the wrapper's direct callback uses. This stronger
capability is source-derived before resolving native integrity. A timing-only
author assertion supplies no argument or entry evidence.

The initial supported wrapper has two identifier parameters, privately retains
the callback in its returned zero-argument function, assigns its cache once to
an ordinary literal with one own data field holding an empty ordinary literal,
and passes that same field and cache to its sole callback invocation. It returns
only that cache field. The existing wrapper checks exclude foreign calls,
member writes and nested functions; the stronger check also excludes explicit
callback return, storage, unknown arguments and throw/publication paths. The
field name is arbitrary. Callback-supplied values can still escape through the
cache field, whose later access may invoke an accessor. No native operation is
admitted by this structural proof.

For a single anonymous inline callback supplied to that source-certified bare
import, absence of a self binding or arguments reflection completes entry
coverage: the callback cannot obtain and publish its own identity. The canonical
index may then prove a conditional-expression region unreachable. Predicates
cover `typeof` of an unrebound certified parameter,
string-literal equality or `<`, and conjunction. Only immediate anonymous
IIFEs can carry these stable captures into another callable. Named callbacks,
unknown imports, parameter rebinds, dynamic scope, arguments reflection and
async descendants retain fallback. A nested deferred function keeps its own
writes; lexical containment in a branch does not certify its activations.

The census retains discharged member-write witnesses with `unreachable_branch`
evidence and empty `affected_roots`. Evidence identifies the source wrapper and
its ordinary argument positions plus the consumer guard location. Graph input
provenance hashes the defining and consuming source bytes. Unconditional
binding writes and unsupported operations remain conservative. This changes
only the native write premise: ordinary effects, exceptions, loader return
reads, accessor hazards and callback execution retain their existing analysis.

## Ordinary own-property inspection and descriptor application

The canonical index recognizes exact ordinary object-literal shapes with
statically known string or number keys (including literal-computed data keys),
without spreads, decorators or `__proto__`; computed method and accessor keys
are not indexed. At capture time, the executor also
requires an exact object identity, no prior taint, and equality between the
literal's own-key set and the current identity-store key set. This admits a
local binding or alias to that object; a known added key, an unknown key or an
escaped/mutated source falls back. Tracked writes that only replace values keep
their value-set overapproximation. The Object binding and native integrity
must remain intact. Applying a captured descriptor map still requires the
separate ordinary Object prototype guard on its `Object.defineProperties`
target.

`Object.getOwnPropertyNames` shares this exact-origin, static-shape and current
prefix-state proof. It emits a dense array of exact string values in
ECMAScript own-key order: canonical array indices in numeric order, followed
by other strings in literal creation order. It does not read property values
or invoke getters. Callee and argument evaluation still happen eagerly, and
possible throws remain represented. This return/timing projection does not
admit the call as pure or exception-free. Each call derives its prefix fact in
a fresh short-lived context, so later mutations cannot reuse an earlier
projection.

`Object.getOwnPropertyDescriptors` captures the current data-value identities
and getter/setter callbacks without invoking accessors. Its carrier has no
general readable descriptor-map shape. The sole composition modeled is a
direct, unmodified `Object.defineProperties` call with that live carrier and an
untainted, currently empty fresh ordinary target. It writes captured data
values and distinct accessor callback metadata into the target's existing
identity, so aliases observe the same result. A callsite whose repeated
execution sees a conflicting source identity or descriptor values is
invalidated rather than joined. Map member access or mutation, return, escape,
or opaque consumption invalidates application while retaining its callback
graph. Copied accessor reads promote their getter; opaque target use retains
getter, setter and method callbacks. Data-valued object identities remain
connected to later mutations. Under exact native identity and intact Object
prototype guards, a result expression may retain argument 0's target identity
even when application eligibility is not proved, and it also retains a
possible throw. This return-shape fact does not authorize descriptor
application or suppress opaque timing/effect fallback. The proof does not
cover returned/readable maps,
arbitrary descriptor edits, `Object.assign`, imported wrapper bodies, general
purity or exception freedom.

## Source-composed returned records

An immediate top-level object pattern can read selected own data fields from
a fresh literal, or from a direct call to a source-proven local factory, without
running user getters or Proxy traps. The shared value-flow collector supplies
the exact callable origin and records the structural return shape separately
from its over-approximating return-value set. The factory has a straight-line
prefix of variable declarations or expression statements and a terminal
object-literal return; it is neither async nor a generator. This shape proof
does not erase prefix effects, arguments, calls, throws, eager binding reads or
later callback behavior. A whole-chunk dynamic-scope check and a rebind scan
protect callable identity, including deferred writes. The selected keys must
all be statically present as distinct own data properties. Missing keys,
accessors, spreads, computed keys, `__proto__`, alternate returns, defaults,
nested patterns and rest retain the ordinary conservative pattern reason.

This local purity fact is independent of the imported returned-record timing
summary below. That summary can contain getters and asserts no member-read
purity.

A stable directly exported hoisted function declaration can supply a separate
successful-return timing summary for a zero-argument function with exactly two statements: allocation of a fresh
empty `Map` in one identifier binding, then return of an object containing only
noncomputed ordinary methods and getters. Eager data properties, spreads,
duplicate keys, `__proto__`, decorators and other outer execution are excluded.
The proof uses the canonical execution index and the same undeclared,
unshadowed empty-collection constructor guard as direct construction. Every
analyzed module must retain Map/global integrity in that index. A11 remains the
premise for code outside the analyzed source. An exact export/entry projection
and one stable function identity are required. Function initializers and
reexports are outside this first proof.

`CallableSummary.returned_record` retains the defining module, export, local,
source locations and deferred member capture provenance. Construction does
not activate those bodies. Its returned value is an opaque foreign capability,
not plain data or a noncallable value. A later member read, invocation, or
opaque consumption activates every importer top-level binding read and the
ordinary escaped/exported callback pool. This deliberately preserves direct
live ESM-cell captures as well as callback-mediated reentry without transplanting
provider-local SWC IDs. Publication retains the token in the existing callback
value graph; callbacks installed by later writes remain ordinary escape roots.
No member precision, purity or exception-free execution is asserted.

## Guarded native call summaries

The synchronous interpreter uses one builtin summary table,
`facts/abstract_domain/builtins.rs`, for both returned values and callback
timing. Each row names one intrinsic operation and its ECMA-262 section
(`cite`), the argument counts it admits, the coercion and internal-method steps
it performs on each operand (`coerces`), the operands it invokes, the fresh
allocation it creates, the writes it performs (`mutates`) and its successful
completion value (`returns`). A row describes the operation after callee and
argument evaluation, preserving those evaluations' effects and throws; it does
not classify the call as pure, certify exception freedom, or change ordinary
source ordering. The summarized operations are the table's rows; this document
does not repeat them.

A row applies only after the native call identity judgment (<inference.md>):
an undeclared, unshadowed intrinsic or its direct static `globalThis.…` path,
no spread, no dynamic scope, and no visible mutation of the row's intrinsic
family or the global object through direct, aliased or reflective writes. The
interpreter then proves every `mutates` entry on the call's operands and every
`coerces` step silent:

- A coercion of a primitive operand runs no user code. `ToString` of a Symbol
  or pattern compilation may still throw; the throw stays represented.
- `Integrity` (`Object.freeze`) is proved only on the fresh plain-data object
  literal itself; `SetIntegrityLevel` never invokes the stored data values.
- `DefineProperties` needs an untainted, currently empty fresh ordinary target,
  the live descriptor carrier from `Object.getOwnPropertyDescriptors`, and the
  ordinary `Object.prototype` guard (§ Ordinary own-property inspection and
  descriptor application).
- The fresh-allocation writes of `Object.getOwnPropertyNames` and
  `Object.getOwnPropertyDescriptors` need that section's own-property
  projection proof.
- `FreshRetains` (`FinalizationRegistry`) needs exactly one known ordinary
  function origin or closure. The constructor checks callability and stores the
  callback through `HostMakeJobCallback` without invoking it; the default host
  operation and the
  [HTML host operation](https://html.spec.whatwg.org/multipage/webappapis.html#hostmakejobcallback)
  both only record it. The result is opaque and retains the callback graph for
  the ordinary retained-value analysis.

Any other operand shape falls back to opaque-call analysis. Argument evaluation
may still reenter, including a sequence whose final value is primitive.

`native_call_summary_test` covers the unary `Math` rows followed by another
conversion, both `RegExp` invocation forms, object coercion, global-constructor
replacement, `Symbol` calls/coercion, `FinalizationRegistry` retention,
qualified native callees, and syntax errors. `math_round_timing_test` retains
effectful argument, shadowing, aliased method mutation, and getter controls;
`fresh_freeze_callback_timing_test` retains its identity/publication controls.

### Source-selected native-call identity inputs

`chunk_analysis_options.<chunk>.native_call_identity_inputs` is a separate
exact-site author premise for a direct call in a selected top-level function
declaration. The initial typed callee is `object_freeze`; the function's sole
statement must directly return one ordinary, uncomputed `Object.freeze(arg)`
call. The parser-input digest and canonical selector pin the function, while
the existing structural call index pins its call span. The assertion covers
the original global `Object` data binding and original own data `freeze` member
at every synchronous at-init execution of that site across permitted emitted
schedules and reentry. Source-known writes to either exact slot in any analyzed
chunk reject the entire input, even if later restored. Opaque receiver/key
writes remain witnesses under the explicit premise; sibling writes are not
assumed inert because an inherited setter or Proxy trap could indirectly change
the required slots.

This input only resolves native callee identity. The existing `FreshDataRecord`
guard must separately establish an ordinary fresh plain-data argument before
the native `Object.freeze` return and no-reentry model can be used. It does not
assert call purity, suppress argument effects or throws, or establish
whole-family native integrity. For the selected call with a fresh plain-data
object literal argument, the native freeze body cannot observe an earlier
array or Set, so its synthetic opaque heap-content barrier is omitted. The
collector still visits callee and argument children, including nested calls.
The returned result has no precise ordinary-object value-flow origin from this
proof, so later escapes and mutations through it retain conservative heap
barriers. Accessor, Proxy, and other unproved arguments keep their ordinary
heap barrier. Unselected calls use the ordinary guards.

### Source-selected native-operation inputs

`chunk_analysis_options.<chunk>.native_operation_inputs` is a source-selected
author premise for one exact direct native initializer. The schema admits
`operation: symbol`, `operation: symbol_for`, `operation: promise_resolve`,
`operation: set_from_primitive_array`,
`operation: set_from_set_spread`, `operation: object_own_data_function_read`,
and `operation: array_spread_from_primitive_arrays`.
For `symbol` and `symbol_for`, the canonical selector engine identifies one
direct top-level simple binding initialized by an ordinary, nonoptional,
uncomputed `Symbol(...)` or `Symbol.for(...)` call respectively, with zero or one syntactically primitive
argument and no spread; the parser-input SHA-256 binds that selection to the
audited bytes. The execution index validates the selected binding and call
span before the synchronous interpreter can consume it.

The author asserts that, from provider evaluation start through the selected
call in every synchronous initialization schedule allowed by emission, the
ordinary global environment resolves `Symbol` to the original native function
through its ordinary data binding. For `symbol_for`, the author additionally
asserts that its direct `for` data property is the original native method.
The author must audit the host entry state: there is no hostile preseeded
`Symbol` or `Symbol.for` alias or accessor, and no foreign
reentry before the selected call. This is not a snapshot of the binding at the
call's original point: a later or deferred known replacement could move before
the call in an emitted schedule. Therefore any positively identified in-source
write to the global `Symbol` binding or global-object `Symbol` slot anywhere in
the analyzed initialization region contradicts either input. A known write to
the `Symbol` constructor's `for` slot also contradicts `symbol_for`, including
a replacement later restored. Opaque receiver mutations are the uncertainty
this premise may cover; they remain visible as their original census witnesses.
The shared native write census retains receiver path and property key alongside
its conservative native-family effects. The validators reject only source-known
writes to those exact slots; unknown receivers or keys remain visible and are
covered by the explicit premise. Alias and reflective writes with a
source-known target still contradict it.

This input sets only its asserted slots' integrity flags, and only at the
selected call span; the existing exact native model consumes them there, and
the report records the author provenance at that site. A `promise_resolve`
input is effective only together with the matching independent
`host_behavior.reentry_free_operations` entry; the pair satisfies the exact Promise timing check
without changing other native guards or Promise call sites. Other calls still
require the ordinary shared integrity proof. For `symbol` and `symbol_for`,
primitive argument evaluation and conversion may throw; a Symbol-valued or
object-valued argument is not admitted. Argument and callee evaluation, their
effects and throws, and surrounding writes, callback invocation, publication,
CommonJS behavior and ordering remain on the normal conservative paths. The
premise grants no whole-lifetime purity, general receiver guarantee or native
integrity outside analyzed code; the external A11 identity obligation still
applies.

For `set_from_primitive_array`, the selected initializer is one direct
unshadowed `new Set([primitive literals])` with a dense, nonspread array.
The premise covers the original global Set constructor, Set adder, Array
iterator and Array iterator `next` identities needed to construct that Set.
The source-only typed slot census rejects known writes to the required slots
across the analyzed program, including aliases, reflection and restoration;
opaque witness paths remain visible and are the author's audit obligation.
Only this exact site seeds a fresh native Set origin in the shared value-flow
solve. The shared heap graph records its Set-content allocation and tracks
reachable content effects. The existing constructor-purity rule may use the
author-backed native protocol dependency for the selected fresh primitive
array. Later Set method identity, own iterator shape and
Set-spread protocol are separate obligations, so this input does not remove
their barriers.

For `set_from_set_spread`, the direct Set argument is a dense array literal
whose first element is one spread of an identifier and whose remaining
elements are primitive literals. The author independently asserts original
Set construction and Set iterator/next identities at this exact call. The
source-only certificate additionally requires the source identifier to name
an earlier selected fresh primitive Set in one original top-level declaration;
every intervening split-view source unit must itself be an independently
selected primitive Set constructor. All bindings in that interval must have
the same explicit destination, so lowering's source-order declarator split
preserves the interval in one emitted module. This certificate is established
before and without seeded value-flow facts; it contributes the spread's fresh
Set identity to the single shared flow solve. Purity, synchronous callback
analysis, and the Set-content graph consume the same exact-site dependencies.
Unknown receiver/key native-write witnesses remain visible, and source-known
writes to required slots in any analyzed chunk reject the input. An own
iterator override, alias mutation, callback, separate output, or other
unproved source unit in the interval rejects the source certificate. Later
Set methods and escaped observations remain on their ordinary conservative
paths.
The own-data read operation selects a single direct `Object.<name>` initializer
whose name is in the shared native own-data function-reference table. An
unshadowed `Object` binding is checked by the canonical source index; the
native-write census supplies known-write exclusions. A known write to the global
`Object` slot or the exact `Object.<name>` slot in any analyzed chunk rejects
the assertion. Other property names, prototype mutations, and opaque receiver
effects are not misreported as known exact-slot contradictions; the author
must audit the latter. Without author input, intact Object/global-object
integrity derives the same exact-read certificate. Purity, timing, and heap
ordering consume that certificate. It certifies only a getter-free read:
invoking the resulting function remains an ordinary call with its usual
effects, arguments, and return uncertainty.
For `array_spread_from_primitive_arrays`, the selector names the receiving
binding of a top-level multi-declarator `const` whose preceding declarators
are dense primitive-literal arrays and whose initializer holds only primitive
literals and direct spreads of those bindings; no intervening declarator may
run user code or expose a producer. The author asserts the original Array
iterator and iterator `next` identities across every admitted initialization
schedule. Positively identified Array-family or iterator `next` writes
contradict the input; opaque writes are the author's audit obligation. Without
the input, the same certificate needs completed `Array` native integrity. The
source shape proves fresh dense elements and a closed interval, and the graph
co-locates producer and consumer statements so the emitted module keeps it.
No family-wide `Array` integrity follows; the external A11 obligation still
applies.
For exactly one string-literal argument, a validated `symbol_for` site has a
separate purity consequence. The original native `Symbol.for` converts that
string without user code or a throw, then looks up or inserts a symbol in the
agent-local registry. ECMAScript exposes no registry enumeration or removal:
[`Symbol.keyFor`](https://tc39.es/ecma262/multipage/fundamental-objects.html#sec-symbol.keyfor)
queries an already held symbol, and repeat `Symbol.for` calls
for a key return that key's unique symbol. Registry insertion can therefore
move with this initializer without changing observable JS behavior. The pure
verdict records the exact native-operation input reference; the array-content
collector consumes the same checked site instead of adding an opaque frontier.
This conclusion does not extend to a merely primitive-valued expression:
evaluating it or converting its result may throw. It also assumes the ordinary
ECMAScript intrinsic behavior under the author premise, not host access to
the internal registry.

## Native `Proxy` construction

`new Proxy(target, handler)` is admitted only when the global `Proxy` name is
unshadowed, there are exactly two arguments, neither is spread, both
arguments are syntactically fresh object literals, and normal expression
classification proves evaluating both literals pure. This uses the
`PURE_BUILTIN_NEW_OBJECT_LITERAL_ARGS` table and the derived
`SHADOW_TRACKED_GLOBALS` census. It does not admit arbitrary object-valued
bindings, arrays, missing arguments, or extra arguments.

ECMA-262 §28.2.1.1 delegates construction to `ProxyCreate`.
`ProxyCreate` requires object target and handler values, creates the proxy,
stores those values, and returns it without looking up or calling a handler
trap. Fresh object literals satisfy the object checks, so the constructor's
type-error path is excluded. Their own evaluation still runs normally: an
effectful property initializer or spread keeps the whole expression
non-pure. Computed method and accessor names also run during literal
creation; their key expressions and `ToPropertyKey` coercion are classified
at every object-literal depth. Effectful or possibly-object-valued keys stay
non-pure, while a statically primitive key can be admitted.

Trap execution remains observable when the proxy is later used. The rule
classifies only construction; it does not make reads, writes, calls, or
other operations through the resulting proxy pure. This boundary is pinned
by `fresh_proxy_object_literals_are_pure`,
`proxy_constructor_rejects_unproved_args`,
`shadowed_proxy_constructor_falls_back_to_unknown`, and the E2E test
`proxy_traps_run_only_after_property_access`.

See the [Proxy constructor algorithm](https://tc39.es/ecma262/multipage/reflection.html#sec-proxy-constructor)
and [ProxyCreate](https://tc39.es/ecma262/multipage/ordinary-and-exotic-objects-behaviours.html#sec-proxycreate).

## Imported primitive coercion timing

The synchronous-effect interpreter can omit callback promotion for implicit
coercion of an exact named/default imported binding when the provider proves
that every value read from the export is primitive. The canonical shallow
analysis recognizes primitive literals, untagged templates, unary/update and
non-logical binary expressions that return a primitive when they complete, and
the final value of a sequence. Conditional and logical expressions qualify
only when every possible returned value is primitive. This is a result-type
fact, not a purity fact: evaluating a template or operator may still coerce an
object, call user code, or throw, and those initializer effects remain in the
source-order graph. A `var` cell can also contain its hoisted `undefined`, which
cannot execute a conversion callback.

Stable top-level `const` or `var` bindings can copy primitive values through
local aliases, named/default imports, and named/default re-exports. Local
binding dependencies use the hygienic declaration/write census; cross-module
dependencies use the artifact import resolver and a seedless fixedpoint. Every
dependency must resolve to a proven primitive export, so an unsupported alias
cycle remains unknown. The provider must be ESM without an own CommonJS export
surface, direct `eval`, or `with` anywhere in its body, including deferred
functions. The separate full CommonJS-interop fact still includes request-only
host loaders and continues to guard generic importer-reentry analysis; those
requests do not invalidate the primitive type of an explicit ESM binding.
Primitive facts only project through artifact imports and re-exports that
resolve to the target chunk's entry file. Duplicate
declarations, assignment/update/loop writes, writes in closures, `let`, opaque
initializers, namespace properties, and vendor-affected providers remain
conservative. A shadowing local binding does not inherit an import's fact.

This is an inferred callback-timing fact, separate from purity and author
assertions. It does not remove eager binding reads, relax live-binding or TDZ
constraints, classify the operator as pure, fold source expressions, or suppress
runtime exceptions such as mixed BigInt/Number arithmetic. Other protocols and
ordinary opaque calls retain their existing behavior. `imported_primitive_test`
pins split acceptance and Node output, unchanged imported reads and impure
classification, conservative provider-write/dynamic-scope controls, and runtime
throws.

## Immutable Symbol computed keys

Chunk-top `const` values made by unshadowed `Symbol()` with at most one
primitive literal argument, and immutable aliases of those values, are safe
computed property keys. `ToPropertyKey` returns a Symbol unchanged. The
declaration's evaluation effects remain classified separately. Recognized
direct writes to `Symbol` and static property/descriptor writes named
`Symbol` disable this value proof; other mutation of built-in intrinsics
remains under A11's author-audited integrity assumption.
These bindings stay outside the result-primitive set used by string
interpolation and numeric/string coercion: `ToString(Symbol)` throws.
`let`/`var`, shadowed `Symbol`, spread or uncertain arguments, and locally
shadowed key bindings keep the conservative classification.

## `String.raw` tagged templates

`String.raw` tagged templates are admitted only when the tag is exactly the
non-shadowed global `String.raw` (not computed access) and every substitution
is both pure and statically primitive-valued. With no substitutions the
condition is vacuous. ECMA-262 §22.1.2.4 (`String.raw`) applies `ToString` to
each substitution; object substitutions can invoke user `toString` or
`[Symbol.toPrimitive]`, hence the same `is_result_primitive` gate used for
coercing binary operators. ECMA-262 §13.2.8.4 (`GetTemplateObject`) creates
and caches the template object from the literal's cooked/raw strings; under
the design's A11 intrinsic-integrity assumption, this built-in machinery
introduces no user-observable effect. The global-binding shadowing checks
match other whitelist entries.

Both directions are pinned in
`string_raw_tagged_templates_require_global_tag_and_primitive_substitutions`:
primitive/no-substitution templates are admitted; object/unknown/effectful
substitutions, a shadowed `String`, another tag, and computed `String["raw"]`
remain rejected. `RegExp` handling is unaffected.

## `Set` from a chunk-top const array alias

`new Set(arrayName)` is admitted only for an exact, unshadowed global `Set`
call whose argument resolves through chunk-top `const` aliases to a primitive
array literal. The literal has no holes, spreads, or non-literal elements.
Every reference to each alias must be either another such const initializer or
the single argument of a standard `new Set(...)` call. Any other use rejects
the whole alias chain, including member access, assignment, publication,
passing it to another call, or capture by an unrelated expression. A
chunk-wide direct `eval` also disables the proof. The implementation records
the approved constructor spans in the existing structural analysis; purity and
synchronous callback analysis consume that same proof.

Under A11, `%Set%` obtains the iterable and calls its built-in `add` for each
item. The standard Array iterator reads the indexed own data properties of the
fresh literal, and `Set.prototype.add` stores the values using SameValueZero
without coercing them or invoking callbacks. The reference rules ensure the
array cannot have been mutated, published, or given an own iterator override
before construction. The proof also requires the shared intrinsic-write
census to find no visible in-chunk mutation of the global `Set` family,
`Array` family, global object, or dynamic scope. This keeps a replaced
`Set.prototype.add` and `Array.prototype[Symbol.iterator]` observable even
when the iterable itself is fresh and primitive-valued. Pollution outside the
analyzed chunk remains covered by A11. See [Set construction and `Set.prototype.add`](https://tc39.es/ecma262/2024/multipage/keyed-collections.html#sec-set-constructor),
[`Set.prototype.add`](https://tc39.es/ecma262/2024/multipage/keyed-collections.html#sec-set.prototype.add),
and the [Array iterator](https://tc39.es/ecma262/2024/multipage/indexed-collections.html#sec-array.prototype-@@iterator).

Fresh inline arrays keep the existing element-purity checks in purity
classification. Synchronous callback analysis also admits a chunk-top
`const result = new Set([table.key])` when `table` resolves to exactly one
chunk-top `const` object literal and its own static `key` has a primitive
literal value after any duplicate-property overrides. The existing PlainData
escape/write scan must admit that table as unchanged ordinary data. This
separately proves the member read cannot run an accessor and its result cannot
be a callback or iterator. The array is freshly constructed at that call;
the proof does not apply to nested callable bodies or to array aliases whose
elements are member reads.

Other array elements remain opaque: a pure function or object can still be
stored in the resulting Set and called by a later consumer. Shadowed `Set`,
spreads, holes, getters, proxies, function-valued or unknown properties, and
mutated or escaped tables remain conservative.

## Retained dense-array iteration effects

Direct `binding.push(...)` can suppress synchronous callback promotion when
the current binding is one untainted dense ordinary array and every non-spread
argument is a retained primitive. It reuses the same Array/Object/global
native-integrity guard as iteration. Under that guard, the
[push algorithm](https://tc39.es/ecma262/2025/multipage/indexed-collections.html#sec-array.prototype.push)
reads the array's own length and appends through ordinary data writes without
intercepting accessors or calling stored functions. A11 still applies to hidden
native-alias paths. This supplies no purity, throw-freedom or return-value
claim. The existing heap-ordering analysis remains in force.

The push invalidates precise array state and advances the invocation-cache
revision while preserving the existing containment graph. Later operations
must take conservative effects, including promoting retained callbacks through
opaque consumers; they cannot reuse a pre-push iteration result. Tests cover
visible intrinsic changes, own-method mutation, argument getters, escapes,
retained callbacks and reads after mutation.

The synchronous-effects executor models direct static `forEach`, `filter`, and
`map` calls with one callback and no spread or `thisArg`. This is an effect
model, not a purity admission: each known synchronous callback executes in the
existing bounded abstract invocation engine with the retained element value,
a primitive index, and the source array. Its ordinary reads, writes, calls,
coercions, and publication effects remain observable. Empty arrays do not
invoke the callback. `filter` conservatively retains every element; its
subsequent indexes remain unknown primitives.

The source must resolve at the current prefix to one untainted dense ordinary
array. Elements must be primitives, retained ordinary data objects, or tracked
function values. Every iteration recertifies the source and element identities;
mutation, unsupported member access, implicit `arguments`, unsupported callback
parameters, or budget exhaustion uses conservative whole effects. Array
identity, element containment, publication, and property writes use the same
object-origin and taint lanes as other values. Results from repeated evaluations
of one call site join monotonically, including missing slots. Complete call
spans distinguish nested calls with the same start position.

Under A11 intrinsic integrity, standard Array iteration reads own dense slots;
array spread over a proven result consumes its values without invoking stored
functions. Visible Array/Object/global intrinsic mutation, constructor capability
mutation/publication, custom iterators, species, methods, getters, holes, proxies,
and ambiguous sources prevent this proof. Callback effects can themselves
invalidate it. This follows the standard algorithms for
[forEach](https://tc39.es/ecma262/multipage/indexed-collections.html#sec-array.prototype.foreach),
[filter](https://tc39.es/ecma262/multipage/indexed-collections.html#sec-array.prototype.filter),
and [map](https://tc39.es/ecma262/multipage/indexed-collections.html#sec-array.prototype.map).

Within a certified iteration callback, a complete set of simple member
assignments can preserve a separate fresh ordinary data receiver. Every key and
right-hand value must be a retained primitive read with literal, identifier, or
static member-chain syntax. The callback has no other mutations or calls beyond
ordinary member reads; receiver state is checked again after those reads. Source
arrays and their reachable entries are excluded. Field updates still join into
the ordinary property/containment maps. Under intact Object prototypes, even the
inherited `__proto__` setter ignores a primitive right-hand value. Unknown keys,
object values, custom prototypes, setters, mutation, and coercive expressions
retain the setter/effect fallback.

Capabilities exposed through exports become untrusted when execution can reenter
the module, using the existing export-reentry policy. Retained untagged-template
conversions, membership protocols (`in` and `instanceof`), and destructuring
source reads preserve possible mutation before a later array call. Tagged-template
substitutions are evaluated without untagged-template string conversion.
Unsupported local parameter patterns and implicit `arguments` preserve actual
input taint and raw callback effects. These guards prevent stale source slots or
newly installed setters from being mistaken for ordinary data.

Protocol fallbacks preserve the escaped capability pool, including nested
returned callbacks. Their read and taint closure reuses coverage-checked
synchronous source unit summaries: reads after a definite `await` cannot affect the
current prefix, while nested callable prefixes and generator bodies remain
conservative. Incomplete reachability, dynamic scope, or uncovered callable and
accessor facts retain whole lazy reads. All lazy rebinds remain retained because
write coverage is incomplete.

An exact single-spread object literal `{ ...source }` can retain a fresh copy
identity when its source is one untainted verified ordinary object containing
only static own data properties. The copy retains each field value and its
aliases, never the source root as the copy root. An uncertified evaluation
monotonically invalidates that copy site. Unsupported object spreads preserve
possible getter effects during the callback invocation, even when no result is
consumed. General object spread and accessor execution stay conservative.

## Callback storage through mapped `Object.fromEntries`

Synchronous callback analysis has a separate, narrow storage proof for
`Object.fromEntries(entries.map(entry => [entry.key, entry]))`. It is not a
purity-whitelist entry. The call is skipped as a callback effect only when the
source is a chunk-top `const` binding admitted by the existing plain-array
proof and, at the current prefix, resolves to one fresh dense array of fresh
ordinary data objects. Each selected key must be exactly one own static data
property with a primitive literal initializer. The source and entries must not
have been written, escaped to opaque code, or otherwise tainted before
construction.

The mapper must be a single-identifier, non-async arrow returning exactly a
dense two-element array containing `entry.key` and `entry`, in that order.
`Object.fromEntries` and the `Array` machinery must be the unshadowed standard
intrinsics, with no visible intrinsic mutation or dynamic scope. Holes,
spreads, accessors, computed fields, non-literal keys, aliases that fail the
plain-array proof, and other call shapes stay on the conservative effect path.
The argument expression is still evaluated normally. When the proof applies,
callbacks reachable through the source objects are retained on the abstract
result rather than invoked or globally published; later member calls and opaque
consumers continue through ordinary callback-effect analysis.
The abstract result conservatively retains the source array identity as a
route to its entry aliases. Since Object.fromEntries stores the mapped entry
objects by reference, later writes through the result can taint the original
entry graph and disable another construction proof.

This relies on the standard Array iterator and `Array.prototype.map` reading
the dense own data elements and storing each mapper result without invoking
its contents, followed by `Object.fromEntries` applying `ToPropertyKey` only
to the primitive selected field and storing the paired object value. The
prefix taint checks reject prior source/property writes and opaque calls;
visible writes through aliases and `Object`/`Reflect` mutation helpers also
disable the proof. A11 supplies integrity for unmodified built-in prototypes
and intrinsics outside the analyzed chunk. The paired CLI controls live in
`from_entries_callback_storage_test`: construction alone preserves output,
while an early consumer, opaque consumer, mutated key, or changed array species
keeps the callback effects visible.

## Callback timing for a fresh frozen record

Synchronous callback analysis recognizes a direct call to the unshadowed
standard `Object.freeze` with exactly one fresh ordinary object literal. The
existing execution index must prove static own data properties, with no spread,
computed key, accessor, or `__proto__` setter. Whole-chunk dynamic scope or a
visible write to `Object` or the global object disables the proof. The call
still evaluates its argument and every nested effect in order; only the
speculative callback execution by `Object.freeze` is omitted. The abstract
return is the same fresh object, retaining any callback stored in a field for
later access or publication.

Under A11, `Object.freeze` applies `SetIntegrityLevel` to the fresh ordinary
object. Its own-key and descriptor operations do not invoke the values stored
in data properties. The proof says nothing about freezing a Proxy, an escaped
object, or an uncertain argument. `fresh_freeze_callback_timing_test` checks a
split accepted after the proof, retained callback invocation, and early reads
through both an effectful argument and a Proxy trap.

## `PURE_STATIC_PROPS` — disposal symbols

`Symbol.dispose` and `Symbol.asyncDispose` are well-known Symbol values, just
like `Symbol.iterator`. ECMA-262 §20.4.2.1 and §20.4.2.3 specify each as a
non-writable, non-configurable own data property of the intrinsic `Symbol`
constructor. Under A11, reading either property executes no user code and
returns a primitive Symbol suitable for a computed class method key. A
shadowed `Symbol` receiver still fails the whitelist check. See the
[Symbol constructor specification](https://tc39.es/ecma262/multipage/fundamental-objects.html#sec-properties-of-the-symbol-constructor).

## `PURE_STATIC_FUNCTION_REFS` — reads vs calls

This table whitelists static-property _reads_ of function-valued built-ins
(`const define = Object.defineProperty;`). Reading such a property fires no
getter per ECMA-262, so the read itself is pure. The **call** of the
resolved value remains subject to the `PURE_STATIC_CALLS` contract above:
call shapes that mutate, fire iterators, fire user getters, or invoke proxy
traps must not be added to `PURE_STATIC_CALLS` regardless of whether the
read appears here.

Every entry lands with both directions pinned:

- a positive `static_function_ref_*_alias_is_pure` test (`Receiver.method`
  classifies as `Pure`), and
- a negative `static_function_ref_*_calls_remain_unknown` test
  (`Receiver.method(args)` classifies as `Unknown`).

The pairing prevents a future maintainer from misreading a read-pure entry
as call-pure and promoting it into `PURE_STATIC_CALLS`.

## Declared purity — spec trust contracts

The spec format admits optional per-member `purity:` annotations. With
`purity: "pure"` the validator treats calls to the bound Ident as `Pure`
regardless of the function body; with `purity: "pure_new"` it treats
`new BoundIdent(...)` as `Pure` when every constructor argument is pure.
The validator does **not** re-verify the body or constructor — an incorrect
annotation produces a buggy debundle exactly as an incorrect spec selector
does. Soundness shifts to the spec author.

Use an annotation when the body is too dynamic for static inference
(dynamic dispatch through a registry, dynamic property access), when it
calls vendor code outside the chunk's analyzable scope, or when the
function is opaque by construction (a host-provided callback, an FFI shim)
but is author-known to have no observable side effects.

`pure` overrides A8's shadowing fallback: a chunk that imports `Boolean`
from userland _and_ declares the local `Boolean` binding pure uses the
declared-pure path — the author asserts THIS bound value is pure, wherever
it came from. Arguments are still evaluated normally; declared purity
covers the function value, not its arguments. `pure_new` is separate from
`pure`: it covers only `new X(...)`, does not make `X(...)` pure, and still
evaluates constructor arguments normally.

`initializer_purity: "pure"` is a separate, narrower trust contract for one
selected, simple top-level variable initializer. It admits only the evaluation
of that initializer expression; it does not certify calls or construction of
the value it produces. The author must audit the complete expression, including
any synchronous callback execution, and establish that it has no observable
effects and cannot throw. Direct eager reads and unresolved callback-body reads
remain graph dependencies, so this annotation does not assert that callbacks
are deferred or skip their ordering constraints. The materializer rejects this
annotation for imports, functions, classes, destructuring declarations,
declarations without an initializer, and ambiguous multiple initialized sites.

When the annotated binding is selected in an analyzed defining chunk and
exported, the same author-trusted `pure_new` contract applies to each
importer's local binding. The export mapping is resolved after structural
selectors; source-matched classes need no minified export-name assertion.

`pure_members: [<prop>, …]` is a third trust contract on the same member,
extending "calls of the bound Ident are pure" to "calls of
`<binding>.<prop>(args)` for each listed property are pure when args
evaluate pure". The intended shape is a vendor namespace binding — a
star-import or renamed binding standing in for a vendor module such as
React — whose member function values are author-known side-effect-free even
though the analyzer cannot inspect them.

```yaml
- name: React
  selector:
    binding: { name: b, kind: import_specifier }
  pure_members: [forwardRef, lazy, memo, createContext]
```

Only static identifier-property access fires the rule
(`<binding>.<prop>(args)` / `<binding>?.<prop>(args)`). Computed access
(`<binding>[expr](args)`), chained access (`<binding>.x.y(args)`), private
fields, and non-Ident receivers fall back to the regular classifier path.
As with `purity: pure`, the rule wins over the global-shadowing fallback,
and args are still classified independently. The annotation is
**per-member**: a sibling member without it stays subject to inferred
classification. There is no "declare a whole module pure" shorthand.

## Audited namespace calls without reentry

`no_reentry_members` suppresses only the opaque synchronous callback fallback
for a direct, stable static namespace member call with primitive literals,
`void` or `!` of a primitive literal, or uninterpolated templates. The separate
`no_reentry_record_members` assertion admits calls with at least one fresh
object literal recursively made of those values under static data keys. It
rejects spread, computed keys, accessors, methods, shorthand, `__proto__`,
identifier values, calls, functions, proxies, and arrays. A primitive-only
assertion cannot admit records. This syntax check proves only that evaluating
the argument does not itself run caller code. The record assertion must cover
every admitted record shape and provider branch. Handling the object or its
prototype must not invoke caller/importer/host callbacks or read
importer-owned lexical state before the call returns. A provider that stores
the record without inspecting it can satisfy this when its other reachable
behavior is audited. No library name is implicitly trusted. The independent
lookup, namespace identity, publication,
and mutation checks still apply, and the return value remains opaque.

## Implicit local class construction

The source-derived local-class certificate is shared by purity classification
and synchronous execution. Its class-shape gate excludes explicit constructors,
inheritance, decorators, computed keys, static fields/blocks, and initializer
expressions that might read or invoke user code. The accepted initializers are
absent values, primitive literals, `void 0` with a numeric zero literal
operand, `!` of a numeric/boolean literal, deferred function/arrow allocation,
and `new Set`/`Map`/`WeakSet`/`WeakMap` without arguments when the intrinsic
binding is unshadowed. The latter reuses the existing no-argument built-in
admission; it does not admit iterator arguments. `void` evaluates its operand
before discarding the value, so calls, property reads, computed keys, and other
non-literal operands remain excluded; see the [ECMAScript void operator](https://tc39.es/ecma262/multipage/ecmascript-language-expressions.html#sec-void-operator).
An ordinary getter's body is deferred and is not evaluated by construction.

The prepared canonical native index validates these field constructor identities
before the class certificate reaches purity or synchronous execution. It rejects
shadowed/replaced bindings, dynamic scopes and recognized writes to the native
family or global object, including mutations supplied by the existing
whole-program native write census. A11 remains the external integrity
premise; source-visible checks strengthen this composed proof without making
purity catalogue admissions unconditional under native replacement.

Purity requires an exact direct top-level `new` initializer after the class
declaration and a unique class origin in monotone whole-chunk value flow.
Unknown, rebound, or shadowed alternatives fail this check. This preserves
the temporal class initialization requirement without claiming that a function
body appearing later cannot be called earlier. Synchronous execution consumes
the same class certificate; its prefix-value check still excludes uncertain
identity. For zero-argument certified default-field construction, the
interpreter can omit the prototype/exposure guard used for explicit `this.x`
assignment: class fields use DefineField on the fresh instance, and every
admitted initializer is non-reentrant under the existing intrinsic premise.
ECMA-262's [default constructor algorithm](https://tc39.es/ecma262/multipage/ecmascript-language-functions-and-classes.html#sec-runtime-semantics-classdefinitionevaluation)
creates an ordinary instance before initializing its fields;
[InitializeInstanceElements and DefineField](https://tc39.es/ecma262/multipage/abstract-operations.html#sec-definefield)
install private fields with PrivateFieldAdd and public fields with
CreateDataPropertyOrThrow, without consulting an inherited setter.
The interpreter still tracks retained callbacks and defers later methods.
The certificate does not prove later methods or field callbacks pure.

The array-ordering analysis additionally requires a distinct heap capability.
Admitted initializers only install fresh instance state; a native field requires
an explicit fresh-allocation model under the same guarded native identity.
Only a certified construction site's own opaque heap effect and body promotion
are omitted. Callee/argument evaluation and every other call site still
contribute their effects, including another call to the same binding within
that statement. Deferred captures stay available for later calls and property
accesses. Binding initialization and class TDZ constraints remain independent.
This does not grant heap locality to arbitrary pure or synchronously complete
calls, or to other native models.
Direct `eval` or `with` anywhere in the chunk disables the certificate, using
the same dynamic-scope scan as the shared Set-iterable proof.

## Property absence for checked private constructors

Explicit private constructor models require each assigned field to be absent
from the intrinsic Object prototype. Reading a missing literal input/result
property uses the same property-specific premise. Absence is stronger than
absence of getters: an inherited data value can retain callbacks that the
current argument-only instance model does not represent. The proof does not
admit inherited data values, accessors, or standard Object prototype properties.

The canonical whole-program index retains property keys independently of
native-call family integrity. A known write to another name does not block this
premise; native call guards keep their existing family checks. Unproved member
writers, including deferred and post-await bodies, block their possible keys.
Canonical fresh-receiver certificates and eligible class constructor `this`
writes are excluded. Derived public fields have no fresh-this guarantee because
`super` can return a native object. Unknown keys, descriptor/prototype operations,
dynamic scopes and escaped native capabilities block every property. Native
capability containment includes records, callable captures and class fields;
it is separate from receiver identity. Key evidence depending on replaced
natives is widened before the property proof runs. A11 remains the external
host-integrity premise; these checks do not prove arbitrary foreign code safe.

The proof changes synchronous callback reachability only. It preserves ordinary
writer effects, class/input privacy, prefix availability, exposure and alias
checks. Failure retains the existing conservative execution path.

## Fresh local record field writes

The local wrapper proof admits a narrow mutation while keeping ordinary
top-level assignments impure. A single shared certificate verifies that a
function allocates an ordinary object with static own data fields, writes
distinct pre-existing fields from simple parameter or literal values, then
returns that local. The source statements are ordered: allocation precedes
every write, and return follows every write. Ordinary object-literal data
checks reject getters, setters, computed keys, spread, and `__proto__`;
the extra field check rejects missing or duplicate keys. Own writable fields
make each assignment independent of inherited setters and prototype lookup.
The exact body excludes aliases, unknown calls, callback invocation, and
external publication. The synchronous interpreter also requires complete
agreement with its indexed allocation, writes, and return before omitting a
speculative callback root. Failed proofs retain the regular purity and
callback-effect paths.

## Audited callback-factory exports

The analyzer also infers a narrower source-derived timing fact for a directly
exported, stable function declaration or definite top-level function initializer
that only returns a fresh zero-argument function. The outer function may have
ordinary identifier parameters, but the admitted importer call supplies exactly
one inline callback; all other parameters are omitted. The returned function
may read those parameters and its own locals, call the first callback, and use
static own data properties of fresh local objects. The proof rejects outer
execution, exterior captures, extra nested functions, dynamic property effects,
and any returned-body call or write that could introduce an unrepresented
callback root. A top-level initializer must retain a single function identity;
the fact says nothing about availability before that initializer runs in an
import cycle. This inference only defers the supplied callback on a successful
factory return. It does not claim purity, suppress eager import evaluation,
remove possible throws, or relax the author assertions below.

`chunk_export_purity.<defining chunk>.callback_factory_exports` and
`callback_factory_members` are not purity annotations. They assert that a call
to the named export or static namespace member, with one inline function or
arrow callback or a retained audited-factory result, neither invokes that
callback or other
caller-, host-, or export-provided callbacks synchronously nor publishes the
argument outside the returned fresh ordinary callable/plain-object graph. It
must not synchronously access importer-owned state through other routes, including
imported lexical cells or caller-owned objects. Provider-private effects are
allowed. The analyzer enforces the call-site shape and conservatively handles other shapes;
the author remains responsible for every provider and type branch, helper,
ordinary property access on both input and result, and prototype integrity.
The retained input and returned graph must be fresh ordinary callable/plain-object
graphs with no proxies or accessors at the call. The returned graph may expose
or invoke the captured callback later.
For a namespace-member contract, the author must audit the provider and every
caller/runtime path supplying values used by admitted callbacks, establishing
that listed factory members retain their identities and behavior through the
analyzed chunk's synchronous initialization. In particular, provider or other
namespace members and callback/ref/hook inputs must not expose an alias that
can replace a listed factory member. Direct static identifier-property
namespace call results are treated as independent only under this explicit
author assertion. The checker propagates explicit namespace provenance through
assignments, local aliases, property values, returns, and call arguments. An
unrelated call or constructor result with no such path is not treated as a
namespace alias solely because its runtime value is unknown; the author audit
asserts that values used by admitted callbacks cannot alias or replace listed
members. Unknown syntax remains conservative when it reaches a publication or
call-argument sink; unsupported destructuring results stay possible namespace
values through later local aliases. Recognized local class values and
allocations are checked against their retained aggregate static or instance
fields. A member projection checks the whole receiver, which may conservatively
taint an unrelated sibling property. Constructors, inheritance, decorators,
accessors, and unsupported class surfaces remain opaque; method bodies keep
their existing return and effect checks. A value from an unknown call or
constructor used as an actual static or dynamic write receiver remains a
possible namespace alias, including through local aliases and member
projections, and disables the contract. For call-argument checks, the checker follows namespace values in
the supplied argument expressions and excludes the callee expression, which
is not passed as an argument. The checker does not prove that arbitrary
callback parameters or unrelated call results are disjoint from the
namespace; that non-aliasing guarantee is part of the author audit.
Within a literal traversed inside an admitted inline callback, a direct static
namespace member used as a call callee is analyzed as a callee, not as a
captured namespace value. Its arguments and effects remain analyzed; computed
and chained callees stay conservative.

The author must additionally audit static member reads used as callback result
object data properties. The analyzer treats a direct one-hop
identifier-property read as a deferred member value, but does not infer whether
that value aliases the namespace or can mutate a listed factory member. The
author must establish that guarantee and audit any accessor, proxy, or callback
behavior reachable through the member value. The object-literal `__proto__`
prototype setter is not an ordinary data property and stays conservative. Only
the direct data-property shape is accepted; computed reads, chains, calls,
spreads, arrays, and other expression forms remain conservative.
Factory calls remain effectful for ordering unless a separate purity contract
applies. See <design.md> for how this contract scopes callback-flow analysis.

The separate `descriptor_copy_factory_exports` contract covers only a
single-execution direct imported call with a fresh static object literal. Its
author premise supplies the provider's descriptor-copy/return-only behavior
and the lifetime identity of the exact `Object` method slots and ordinary
target prototype. The shared raw write census rejects known contradictory
slots; opaque writes remain an author obligation, not a whole-`Object` native
integrity fact. The checked eager-read and assigned-key syntax prevents input
getters and copied setters from running during registration. Copied getters
activate on later reads, and opaque use of the result promotes its retained
graph. The provider call itself remains ordinarily effectful and throwing.

The separate `retained_callback_factory_exports` and
`retained_callback_factory_members` contracts additionally admit stable named
local callbacks and explicitly listed callback positions at exact arities.
They do not widen either fresh-only assertion. The stronger audit belongs to
each provider **and each call site**: existing callable identity, aliases and
property/prototype capabilities cannot expose additional callbacks, proxies or
accessors outside the tracked callable/capture graphs. The provider must not
mutate, invoke or publish those inputs synchronously, access importer state,
or introduce provider callbacks or cross-instance aliases in its fresh result.
The analyzer aggregates all admitted callback graphs and retains ordinary
mutation/publication/invocation checks. Recognized uncertain, rebound, exposed
or mutated values fall back conservatively. Exact shape checks and the full
author obligation are in <../SPEC.md> § Retaining existing or multiple callbacks.

## Local-effect decorator annotations

`typescript_decorate_helper` only certifies mutation locality.
`typescript_metadata_decorate_helper` also certifies metadata-only helper and
supplied-decorator behavior at eligible applications. The full shape checks,
author audit and retained constraints are in <../SPEC.md> § Local-effect
decorator annotations. Observable writes outside the target graph invalidate
locality; callback invocation or publication invalidates the stronger timing
summary. Neither annotation asserts call purity.

## `ChunkBinding::PlainData`

A chunk-top binding admits as `PlainData` when its declared value is a
plain object/array literal, and the chunk-wide write scan can prove no
post-init mutation can install an accessor on the binding's cell.
Concrete shapes admitted: `const X = <plain literal>`; `let X = <plain
literal>` / `var X = <plain literal>` with every chunk-top re-bind
being another plain literal (whole-object replacement, no in-place
mutation); the TS-enum-IIFE shape `var X = ((p) => (p.A = "a", …, p))(X
|| {})` whose IIFE produces a plain object by parameter mutation (see
`is_ts_enum_iife_init_for_binding`).

The closed local constructor-result proof can also use an exact named
primitive field from that enum object inside a fresh dense array argument.
This requires one initializer for the binding, the whole-chunk PlainData
no-write/no-escape proof, execution of that initializer before the read, and
the ordinary Object prototype integrity guard. Numeric reverse-mapping writes
can certify their inner named fields; computed or unknown read keys cannot.
The member read remains an eager dependency and is evaluated normally. Array
holes and spreads are outside this proof.

For a computed object-property key, the same initialized exact-member proof
also establishes that `ToPropertyKey` receives a primitive, so it cannot call
user code through the enum member value. The candidate additionally requires
the canonical structural facts to show no eager or lazy enum-binding rebind
outside its initializer. This removes only the coercion/reentry concern:
member access and key-expression evaluation remain ordinary eager effects.
Computed enum member names, member mutation, opaque escape, reflective writes,
accessors, and unknown receiver forms continue through conservative handling.

Why member reads on PlainData are pure:

- **No accessor channels at any program point.** The initial init is a
  syntactic plain literal — no getters/setters/methods/computed
  keys/`__proto__` — so the value carries only data properties. Every
  re-bind writes another plain-literal value, so the post-write value
  still has only data properties. Object/array spread inside the
  literal (`{...src}`, `[...src]`) is permitted: `CopyDataProperties`
  copies values via `CreateDataPropertyOrThrow` regardless of the
  source's descriptor shape — the result is plain.

- **No post-init accessor installation.** The chunk-wide write scan
  rejects any member write (`X.k = …`, `X[k] = …`), member update,
  member delete, or call to `Object.{defineProperty,defineProperties,
setPrototypeOf,assign}` /
  `Reflect.{defineProperty,set,setPrototypeOf,deleteProperty}` with
  `X` as first arg. Plain-ident writes whose RHS is not a
  plain-literal also disqualify.

- **No escaping references.** The write scan above is name-based: it
  only catches mutations spelled through the candidate's own
  identifier. An alias (`const Y = X; Object.defineProperty(Y, …)`)
  or any captured reference (call argument, array element, object
  property value) would defeat it, so the scan also treats every
  bare candidate `Ident` outside a short list of provably
  non-capturing read positions (member receiver, spread source,
  `typeof`/`!`/`void` operand, `Object.{keys,values,entries,freeze,
fromEntries}` single argument with `Object` unshadowed, the vetted
  TS-enum-IIFE argument, `export { X }`) as
  an escape and disqualifies the candidate.

  **Residual assumption for `export { X }`:** consumers in other
  chunks are assumed not to install accessors on an exported PlainData
  reference. Within the analyzed chunk, the value-flow pass follows
  direct literal identities through local assignments, function
  parameters, returns, and static object properties. A direct
  `return X` or concise `() => X` is admitted only when the literal
  identity is tracked; the scan checks aliases and disqualifies `X`
  when one is passed to an unknown consumer or used at a mutation
  site. Other return shapes remain escapes.

- **Primitive member values do not expose their receiver.** The escape scan
  distinguishes an exact static member read on a chunk-top `const` object
  from passing the object's identity. The cached initializer must be an
  ordinary object literal with an own primitive literal value at that key
  after duplicate-key replacement. Binding IDs distinguish local shadows.
  Reads of those values, including inside a formatted message, do not expose
  the receiver to a consumer. The whole-chunk mutation, alias and escape scan
  still runs: a write or an actual object escape invalidates the candidate.
  Computed keys, spreads, accessors, nonprimitive values and method results
  retain conservative escape handling. An empty value-flow result alone is
  not a primitive proof: some unsupported expressions also have no tracked
  object origin.

- **Re-bind soundness for `let`.** Reads on `X` after a re-bind see
  the new plain-literal value; reads before still saw the old
  plain-literal value. At no program point does `X` hold an
  accessor-carrying value, so member reads fire no user code regardless
  of write/read interleaving. The re-bind statement itself is
  independently classified impure (the existing `AssignOrUpdate` rule
  on `Expr::Assign`), so it keeps its `S`-edge and the debundler
  cannot move it past sequenced points.

Sound enabler for the recursive-purity claim "`x` reads on `x.k` /
`x[pure]` are pure iff `x` is bound to a plain-data shape that has no
accessor channels". Eliminates the chain-of-hints needed when a
chunk-local helper's body is just a property read on a chunk-local
config object — including the `let envConfig = {...}` / `envConfig =
{...envConfig, ...n}` pattern that the
`runtime/environment/env_config.yaml` spec hints in the private downstream repo
were a workaround for.

## `PURE_OBJECT_CALLS_ON_PLAIN_DATA`

Static `Object` methods that are Pure when called with a single
argument that is structurally a fresh plain-data object/array literal
(no accessors / methods / `__proto__` / computed keys / spread of
non-plain-data sources) OR a chunk-top binding that has admitted as
`ChunkBinding::PlainData`.

The contract is stricter than `PURE_STATIC_CALLS` because every member
here either invokes `[[Get]]` on own keys (which fires user getters /
Proxy traps on a general argument) or mutates descriptor state
(`freeze`). Restricting the argument shape to a fresh plain-data
receiver — verified syntactically at the call site — closes both
holes: no own-key access can fire a user accessor (none exist), and
the mutation in `freeze`'s case targets a value that is not aliased
through any user-observable channel before the call.

The general-arg form of these calls stays in `PURE_STATIC_FUNCTION_REFS`
(read-pure, call-unknown), matching the rule that we cannot admit `[[Get]]`
or descriptor mutation on an arbitrary receiver. New entries need the same
per-entry ECMA-262 citation as `PURE_STATIC_CALLS`, plus paired tests in
`purity/classifier_tests.rs`: a positive
`object_<prop>_on_plain_<shape>_classifies_pure` (`Object.<prop>({lit})` is
Pure) and a negative `object_<prop>_on_<non-plain>_stays_unknown`
(`Object.<prop>(somefn())` and the opaque-binding form are Unknown).
Cycle-breaking behaviour is pinned in <../e2e/object_plain_data_calls_test.rs>.

Soundness per entry:

| Method                                  | ECMA-262 operation and reason it is safe under the receiver gate                                                                                                                                                                                                                                                                                                                  |
| --------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Object.keys(O)`                        | §20.1.2.17: `ToObject(O)` needs no coercion for an object; `EnumerableOwnPropertyNames` uses `[[OwnPropertyKeys]]` and `[[GetOwnProperty]]`. Ordinary plain objects return their own keys and descriptors without user code.                                                                                                                                                      |
| `Object.values(O)`, `Object.entries(O)` | Like `keys`, then `[[Get]]` for each own key. Every key is a data property on a fresh literal or admitted PlainData binding; `PlainDataWriteScanner` rejects later accessor installation through `Object.defineProperty` or `Object.setPrototypeOf`.                                                                                                                              |
| `Object.freeze(O)`                      | §20.1.2.6: `SetIntegrityLevel` clears `[[Extensible]]` and makes own descriptors non-configurable (data properties also non-writable), without `[[Get]]` or user code. The mutated literal is fresh, or the admitted PlainData binding has no observable consumer outside the chunk.                                                                                              |
| `Object.fromEntries(I)`                 | §20.1.2.7: `I[@@iterator]()` uses the built-in iterator only for a fresh, spread-free array. Each entry must be a fresh two-element array with data properties at `[0]` and `[1]` (`is_fresh_entry_array_for_from_entries`). These gates also exclude the observable TypeError for a non-iterable argument. PlainData Object bindings are excluded because they are not iterable. |

Out of scope (not admitted; flagged for follow-up review):

- `Object.assign(target, src)` — mutates `target` AND calls
  `[[OwnPropertyKeys]]`/`[[Get]]` on `src`. The target-mutation half
  rules out the literal-arg shortcut: even with two literal args,
  `Object.assign({}, …)` returns the first arg mutated, which is
  observable only if the result is captured — but the mutation itself
  is invisible without the capture, so this is safely pure-of-result.
  Skipped to keep the rule tight; the `assign` path needs its own
  argument-count + result-shape analysis.

- `Object.getPrototypeOf(O)` / `getOwnPropertyDescriptor(O, k)` — richer
  results and argument-specific behavior remain outside the current gate.

- `Array.from(I[, mapFn])` — sound only when `mapFn` is absent (a
  `mapFn` invokes user code per element). Skipped to avoid the
  per-call argument-count gate.

## Host membership nonreentry is separate from purity

`non_reentrant_global_membership` is an explicit root/key host-integrity audit,
not a builtin whitelist. It narrows callback reentry only for literal-key `in`
probes on a direct unshadowed `globalThis` or `window`. Key coercion, aliases,
shadowed roots, effectful receivers and unlisted keys retain fallback. The
index also rejects recognized root/prototype mutations and global exposure.
Unknown computed writes on other descendant objects do not themselves identify
a root mutation. Explicit prototype keys, known identity/prototype paths and
visible stored back-aliases still reject. Disjointness of other descendants is
part of the complete author alias-integrity premise, not inferred host trust.
Source checks cannot establish the complete host or cross-chunk prototype
surface: its integrity at each probe remains a strong author premise. The
operator's existing impurity, operand reads/effects and possible throws remain;
see the complete contract in [SPEC](../SPEC.md#audited-builtin-global-membership-probes).

## Audited local constructor-factory prototype premises

`retained_record_factory` is a binding-level author assertion, not a purity or
native-integrity fact. Its bounded admission and proof-use report are specified
in [SPEC.md](../SPEC.md). The canonical fresh-record call check matters because
the execution value model can erase assignment targets or sequence prefixes.
Argument evaluation must run no user code and the stored values must not hide
an unrepresented callback. An identifier-valued record property is admitted
only when the binding is initialized and free of dynamic-scope contamination,
and its value is a primitive literal `const` (one primitive prefix value), one
source function (stored, not called: retained like an inline callback), or one
untainted literal array or plain-data object whose current slots are all
primitive. A shared primitive container holds no callback, and later writes
through any alias land on the same tracked allocation. Method definitions only
allocate functions. Template substitutions must be primitive `const`s or
checked enum reads: `ToString` of an object can call `toString` or
`Symbol.toPrimitive`, and a Symbol would throw. Spreads stay refused: copying
reads every own enumerable property, so an accessor or proxy source runs code,
and the copied keys are not visible to the constructor-read check. The read
remains part of normal eager execution, so TDZ and dependency ordering stay
observable.
Source execution remains essential: nonreentry alone would not justify dropping
a factory's eager lexical read or a class TDZ. The source-checked constructor
proof admits only the existing private return-only allocation graph.

Only exact constructor/property and input-object/missing-property pairs are
author-discharged. Missing reads require actual absence; setter-freedom would
still admit an inherited callback-valued data property omitted from the retained
instance graph. Assignments require successful ordinary own data property
creation, including the absence of inherited interceptors or nonwritable data.
The ordinary executor still evaluates the selected initializer, wrapper and
constructors. A typed certificate is minted only when the full return-only
source proof succeeds and that exact execution completes without unknown calls,
precision loss or uninitialized reads. The purity pass uses it to remove only
the matching `impure_function_call` reason at that call span. Argument and
callee effects, eager reads and every other purity reason remain. Any captured
native-integrity evidence used by the proof stays in the resulting purity
dependencies; no function-body or class-wide purity summary changes. A source
or runtime fallback does not issue the certificate and retains conservative
reachability.
