# Selector resolution: specification

What `debundle` guarantees when it places spec entities in a bundle chunk. How to
write selectors: <docs/selectors.md>. How it is implemented:
`docs/selector_resolution.md`.

## Entities

An **entity** is one thing a spec places in a chunk: a module member, a
`source_matches[].bindings[]` entry, or an `anonymous_statements[]` entry. Each
entity's selector names the places it may occupy: a top-level declaration (by the
minified binding it declares) or one top-level statement.

An entity is scoped to its module's chunk, and only that chunk's places are its
candidates. In a tree spec, each module tree names the chunk its modules are
scoped to; several trees may name the same chunk, and their modules then share
it as if authored in one tree.

## Repeated `var` declarations

Repeated declarations of the same module-scoped `var` denote one binding.
Selecting or renaming that binding applies to every declaration site, including
sites inside top-level control flow. A structural selector may identify it from
any of those sites. Its declarations must share one output module; initializers,
reads, and mutations retain their observable order. A bare redeclaration does
not reset the binding. Other duplicate declaration combinations remain unsupported.
Owner-based relational selectors inspect facts on each statement site; they do
not union those facts across all redeclarations. Binding selectors canonicalize
to the first declaration site, so relations composed with them cannot inspect
later sites' facts. Control-flow statements containing `var` move intact; all
bindings they declare must be assigned to the same destination.

## Matching

Only the shape matcher decides where a `source_match` template matches. A
template matches a place when their syntax trees are equal up to:

- **holes** — `ANYTHING`, `EXPR`, `STMT`, `STMT_LIST`, `DECLARATORS`, `SEQ_EXPRS` and
  the other hole keywords of <docs/selectors.md> match any subtree of their kind;
- **alpha-renaming** — binding and value identifiers match any identifier,
  consistently within their lexical scope.

A **free** identifier, one the template uses but never declares, is not always
alpha-renamed. In order, it is:

1. a hole keyword;
2. a **reference** to the spec entity exported under that name: by the
   template's own module if it exports one, else by the one other module that
   does. A name several other modules export makes the template `invalid`;
3. an unshadowed **global** (`Object`, `window`, `console`, …) if the chunk
   neither declares nor imports that name at top level: it matches only itself;
4. otherwise alpha-renamed.

A place matches a reference only where the free name binds, throughout the
match, the one chunk identifier the referenced entity takes, whichever selector
pins that entity. A reference to an entity whose own selector fails before the
joint solve (`no_match`, `too_broad`, `invalid`) alpha-renames.

A `source_matches[]` binding may claim a free identifier of its template
instead of a declaration (**pinning by use site**). Its entity is the top-level
declaration that identifier binds to, throughout the match; a match where it
binds no top-level declaration, or different identifiers in different scopes,
is not a place of that entity. A claimed free identifier is the entity's own
binding, never a reference or a global. A claim of free identifiers only does
not claim the matched statement.

An anonymous statement written with a bare `match:` rather than `source_match:`
skips alpha-renaming: its identifiers must equal the chunk's. Literals,
operators, member property names, object keys and tree structure are
significant. Relational selectors (`cross_ref`, `reads_member`, …) match through
facts derived from the chunk, not through templates.

An `anonymous_statements[].source_match.match` may contain a contiguous run of
top-level statements. The final statement is the only claimed place; preceding
statements constrain the match through their shape and free identifiers and may
be owned by other entities. The context form requires an anonymous final
statement. A bare `anonymous_statements[].match` contains exactly one statement.
A context window does not use a top-level `STMT_LIST` hole.

## Local selector explanations

`inspect-source` labels the parsed file's top-level statements and original
locations. `spec match-selector --explain --statements` compares one inline or
authored selector against the entire selected range using its structural
matching semantics. It reports a local match, mismatch, unsupported comparison,
or diagnostic limit. An inconclusive comparison is not a mismatch.

This operation does not establish uniqueness, enforce ownership/all-different,
or resolve references to other spec exports. Loading a selector from a spec
preserves its matching mode and claimed bindings without solving other entries.
Local match results are distinct from resolved assignment outcomes.

## Assignment

Every chunk's entities form one program. Each entity takes exactly one of its
matched places, jointly with every other entity:

- no two entities claim the same place, whichever modules or trees they come
  from;
- a relational selector holds between the places its entities take;
- a template's references agree with the places their entities take.

The answer is the **unique** assignment satisfying these. An assignment that
holds only for some solutions of the joint problem is not an answer.

## Outcomes

Every entity gets exactly one outcome, in one record format shared by `run`,
`spec validate` and `spec match-selector`:

| Outcome                                   | Meaning                                                                                                                                                                                    | Severity |
| ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------- |
| `resolved`, `resolved_by: own_selector`   | its selector alone matches one place                                                                                                                                                       | ok       |
| `resolved`, `resolved_by: own_references` | several places match its selector; only one agrees with the places of the `references` it names                                                                                            | ok       |
| `resolved`, `resolved_by: elimination`    | unique only because the named `claimers` took its other places                                                                                                                             | warning  |
| `resolved`, `resolved_by: referenced_by`  | its selector matches several places; the templates of the named `referrers`, which name it, pick one                                                                                       | warning  |
| `no_match`                                | no place satisfies its selector and constraints; a one-statement template may list up to 3 `nearest_unclaimed` statements (no entity claims them), closest first, with where each diverges | error    |
| `unsatisfiable`                           | its interacting group admits no joint assignment; optional `witness` proves an ownership conflict among a subset                                                                           | error    |
| `ambiguous`                               | several assignments exist; at most 5 of its places are listed, with `truncated` when more exist, else with `differentiators` (below)                                                       | error    |
| `conflict`                                | none of the places its selector matches agrees with where the entities its template references (`with`) resolve                                                                            | error    |
| `too_broad`                               | its selector matches more than 100 places, and it takes no further part                                                                                                                    | error    |
| `duplicate_claim`                         | resolved to a binding another entity already claims; `declaration` gives the statement declaring it (`owner`, its body index) and its keyword `kind` (`function`, `const`, `import`, ...)  | error    |
| `invalid`                                 | its selector does not parse, uses an unsupported construct, or its matches do not map to places                                                                                            | error    |
| `undecided`                               | the solver stopped before deciding it                                                                                                                                                      | error    |

An `ambiguous` outcome's `differentiators` give each listed place that has one
a stable anchor the other listed places lack: a literal, object key, class
member, member-path call, declaration kind or arity, in the place's own
statement or else in the statement adjacent to it. A place with none is not
listed there, and the report says when no place has one.

A contradiction affects only its group of interacting entities. Every member
reports `unsatisfiable`, with `group` naming the group's first entity as a
chunk-local identifier. An optional `witness` names distinct selector claims
and their complete candidate-owner union, with fewer owners than claims.
This proves an ownership conflict but need not be minimal. Each owner records
its post-split ordinal, original source statement index, and matched bindings.
Witness members participate in that conflict; other group members remain
blocked. Without a witness, the group remains infeasible and the diagnostic
states that no smaller ownership witness was found within diagnostic limits.
Independent groups resolve normally.

`run` and `spec validate` in both modes give each entity the same outcome. The
edit gate and `describe` resolve only `source_matches[]` entries and anonymous
statements, without the spec's other members, and give those entities the same
outcome except that one unique only because a relational member claimed its
other places is `ambiguous` to them, and a template reference to any other
member alpha-renames there. Only the pipeline
(`run`, `spec validate --spec`) reports `duplicate_claim`. `spec match-selector`
resolves its selector as a spec of one entity, so a selector resolved by
elimination or by its references in a spec is `ambiguous` there.

## Modes

- **Keep-going** (the default): every outcome is reported, entities with an
  error outcome stay unclaimed, and a chunk with any error outcome fails once
  all of its outcomes are reported. Every chunk is reported; the run fails
  with the first failing chunk in chunk-id order.
- **Fail-fast** (`--fail-fast`): the first error outcome stops the run. Outcomes
  come in a fixed order: every chunk's duplicate claims, then every chunk's
  resolved outcomes, chunks in chunk-id order.

Warnings never stop a run.

## Readable names and the chunk public surface

`name:` on a member and the name assigned to `source_matches[].bindings[]`
name the spec-owned binding in its logical module. If the source chunk already
exports that binding, emitted output keeps the original chunk export name and
adds the spec name as a second export of the same binding. The two ESM export
names are live aliases; existing consumers therefore remain compatible.

Named imports in other processed chunks may import the readable alias and bind
it locally under that readable name. This import-local change is conservative:
a collision with an existing or nested binding, an ambiguous alias, or an
unprocessed importer leaves the old import form unchanged. Dynamic imports and
namespace imports continue to use the original minified export names. Bindings
without a spec-assigned name are not changed by this cross-chunk pass.

## Runtime loading contract

Flat and tree specs accept `execution_runtime: generic` (the default) or
`execution_runtime: native_browser_esm`. The latter declares execution through
the browser's native ESM loader, with no synchronous host API that can obtain
the namespace of a module while that module is evaluating. It is independent
of browser-harness output configuration.

The native-browser proof still checks a complete ordinary-ESM static closure,
absence of top-level await and absence of a cycle back to the selected chunk.
CommonJS compatibility references alone do not expose that chunk's exports
through the native loader. Callbacks actually passed or published remain
tracked, including callbacks that escape through global or unknown objects.

Vendor replacement may preserve the proof when the original residual edges
over-approximate the effective dependencies. A bundled partial replacement
qualifies only when its prepared bundle is an ESM leaf with no static requests
or top-level await: generated facades lead only to that leaf, while residual
pruning can delete original edges. Bundles with external requests, raw package
swaps and full package swaps retain conservative export reentry. Boundary
renames and suppressed chunks add no dependency edges.

## Export-reentry diagnostics

Materialization reports the selected export-reentry policy and its observed
blockers in `export_reentry.json` beside the per-chunk owner graph. This is
diagnostic evidence only: it does not change the analyzer's proof or emitted
JavaScript. `Full` reports include accepted and rejected chunks; rejection-only
reports include it on rejection, while accepted dry runs remain silent. The
typed wire schema and blocker fields are described in
<docs/wire_format.md#export-reentry-diagnostics>.

## Partial vendor-swap ownership

The vendor strip pass assigns top-level items to swapped packages through
source reads and rebinds (including nested function bodies), plus writes to
referenced local values. Inferred possible call targets do not assign package
ownership: a function passed to a different invocation of the same helper is
not thereby part of this package. The strip pass rejects non-shareable items
also needed by residual exports and checks retained bindings and observable
writes before removing swapped items.

## Synchronous analysis limits

Recursion and analysis limits preserve the first-await boundary where retained
callback read coverage is complete. Possible synchronous reads and writes remain
constraints, including for private callbacks that have not escaped. Unsupported
bodies and dynamic scope retain conservative whole-body effects. Opaque-call
reachability follows those same synchronous prefixes, so a value first read
after a definite `await` does not expose its callback body through that path
during chunk initialization.
Ordinary eager reads and synchronous callback reads stay between the known
preceding and following writes of the binding, including `var` initializers
and later assignments, even when the eager reader's owner is pure.

Opaque calls holding a class or instance may invoke its constructor, field
initializers, methods, and accessors. Their synchronous read closure stops at a
definite `await` where retained callable coverage is complete; uncovered owners
and incomplete reachability retain whole-body effects and rebinds.
Defining a pure, unpublished subclass with a direct, uniquely known ordinary
class superclass reads that superclass for the eager dependency graph, but does
not by itself publish or invoke the superclass constructor or methods. Actual
construction and publication remain reachability roots; unknown or alternate
superclass values retain conservative roots.
When those effects reach a top-level class declaration with a simple binding
as its superclass, analysis uses the possible class values from the prefix at
the declaration. Later writes to that binding do not replace the superclass
captured by the class. Unsupported or uncertain heritage retains the generic
fallback.

A pure initializer that calls a uniquely known local function can retain an
unpublished callback graph in a fresh ordinary record without making that
initializer a speculative reachability root. The function must have simple
parameters, no synchronous calls, writes, dynamic scope, defaults, or other
effects, and a single return value modeled as a plain data record. Its values
may be parameters, local or inline functions, primitives, or nested records
of the same shape. Arguments must be inline functions or primitives. The call
and its eager dependencies still run normally; any later export, write, or
invocation of the result restores its callback reachability.

A direct local wrapper call may also retain callbacks in a fresh checked class
instance without publishing them. The wrapper must have one return containing
only checked local `new` allocations, simple parameters, no other calls or
effects, and a unique stable identity. Inputs must be fresh plain-data records,
inline functions, or primitives. Nested allocations must be inert; constructor
property reads must resolve to own data or safe missing properties under A11.
The initializer, wrapper, and class owners still contribute actual synchronous
effects and ordering. Their execution alone does not expose deferred methods or
make them available as host accessors. Later invocation or publication of the
result restores those capabilities. Unsupported constructor paths retain
conservative reachability.

At-init callback analysis also records possible implicit primitive conversions
from arithmetic, relational and loose-equality operators, the left operand of
`in`, numeric unary operators, updates and non-logical compound assignments.
If retained value flow proves an operand primitive, the conversion adds no
callback effect. An object or unknown operand uses the conservative synchronous
callback path, covering custom `[Symbol.toPrimitive]`, `valueOf` and `toString`
methods without assuming which conversion method runs. Strict equality,
short-circuit operators, `typeof`, logical negation and `instanceof` do not
create a ToPrimitive effect. Callback effects reachable through an operand's
retained lazy closure are included as well, covering an accessor that returns
another conversion callable. This is callback-effect analysis; it does not
classify the enclosing operator as pure.

## Audited callback factories

`chunk_export_purity.<defining chunk>.callback_factory_exports` lists named
exports whose calls are audited to defer a single callback argument.
`callback_factory_members` lists static members on an imported namespace with
the same contract. At an importer, the analyzer applies either contract only
to a call with exactly one non-spread inline function or arrow callback, or a
retained result of another audited factory. The retained input is a fresh
ordinary callable/plain-object graph, and the outer factory must preserve the
original callback inside its own fresh returned graph. Construction and other
argument shapes remain conservative. The provider must not invoke that callback or any
caller-, host-, or export-provided callback synchronously, or publish the
callback outside the returned fresh ordinary callable/plain-object graph. The
consumer may later reach or invoke the captured callback through that result.
An invocation includes synchronous reads by the callback and by callbacks it
returns synchronously. Reads after an unconditional `await` remain deferred;
unsupported callback bodies retain conservative whole-body effects.
The provider must not synchronously read or write importer-owned state through
another route, including imported lexical cells or caller-owned objects; effects
confined to provider-private state are allowed.

The author must audit every provider and type branch, helper, callback-bearing
property access on both input and result, and prototype integrity; neither
graph may contain proxies or accessors at the call. This assertion describes
callback flow only. It does not
assert that the factory call is pure or effect-free. Writes through the result
that can install opaque values, accessors, or a new prototype conservatively
restore callback effects; known primitive metadata writes retain deferral.

`chunk_export_purity.<defining chunk>.inline_callback_factory_exports` is a
narrower named-export contract. It applies only to calls with exactly one
non-spread syntactic inline function or arrow callback. The provider must
return a fresh ordinary callable containing that callback, without invoking
it or any caller-, host-, or export-provided callback synchronously or
publishing it elsewhere. Its other synchronous behavior must satisfy the
same importer-state restriction above. The author audits provider and helper
paths reachable for this admitted input shape; other overloads are unclaimed.
Calls with a named callback, a retained factory result, spread arguments, or
`new` use conservative callback timing. This contract is independent of purity
and does not grant the broader `callback_factory_exports` composition rule.

`chunk_export_purity.<defining chunk>.no_reentry_exports` asserts that direct
calls of listed named imports cannot synchronously invoke caller-, importer-,
or host-provided callbacks or read importer-owned lexical state. The author
must audit all provider paths and preserve the exported callable's identity
through synchronous initialization; a provider-side rebind or mutable wrapper
that can change its behavior is outside the assertion. Provider-private effects
remain possible, and the result stays opaque. Only a direct imported binding
call with no spread or `new` and the same checked primitive argument forms as
`no_reentry_members` below is admitted; zero arguments are allowed. Argument
expressions are still visited normally.
Aliases, uncertain arguments, and result use retain ordinary reentry checks.
The defining module still initializes in its usual order. This contract is
independent of `pure_exports` and callback-factory assertions.

`chunk_export_purity.<defining chunk>.no_reentry_members` asserts a narrower
call-timing fact for listed static namespace members: with admitted primitive
arguments, the call does not synchronously invoke caller-, importer-, or
host-provided callbacks or read importer-owned lexical state. Provider-private
effects remain possible. It neither asserts purity nor describes the return
value; the return stays opaque. The same member must also satisfy
`namespace_independent_members` so its lookup cannot itself execute foreign
code, and the namespace identity and mutation checks must pass. The checked
call shape is a direct `namespace.member(...)` with no spread or `new` and only
primitive literals, `void` or `!` of a primitive literal, or a template with no
interpolations as arguments; zero arguments are allowed. Argument expressions
are still visited normally. Caller-owned objects, callbacks, aliases, optional
or computed lookups, and changed namespace members retain ordinary reentry.

`chunk_export_purity.<defining chunk>.no_reentry_record_members` is a separate
author assertion for direct static member calls with at least one fresh object
literal argument. Every other argument must be an admitted primitive value or
another fresh object literal. The objects may recursively contain those values
under static data keys; spread, accessors, methods, shorthand, computed keys,
and `__proto__` keys are excluded. This assertion requires an independent audit
of each admitted record shape and its provider branch, including any reads,
coercions, or retention of the object and its prototype. The provider must not
synchronously invoke caller/importer/host callbacks or access importer-owned
lexical state through the record. A primitive-only `no_reentry_members` assertion
does not imply this record assertion, and neither annotation is a general
whitelist for a named library. The same independent lookup, namespace identity,
mutation, and publication checks apply; the call result remains opaque.

`chunk_export_purity.<chunk>.namespace_independent_members` maps namespace
exports to individually audited static member names. Each lookup must be stable,
run no caller/importer/host code, and expose no capability to obtain the namespace
or replace an audited factory. An ordinary own data property or an explicitly
audited inert forwarding accessor can satisfy this; a primitive result alone
cannot. The guarantee covers the member value's reachable operations, properties,
prototypes, constructors, accessors, and returned callbacks, including implicit
namespace receivers in member calls and tags. Independently supplied argument or
receiver capabilities may propagate normally; the implicit namespace receiver is
not such an exception. Hidden closure capabilities to rewrite factories are
forbidden. This summary grants neither purity nor callback deferral.

The author must identify every imported export that can alias the namespace
family and retain the provider/runtime input audit below. The checker groups all
configured family roots together. Only when every runtime occurrence is the
receiver of a direct static lookup of a listed member may namespace admission
ignore unrelated unknown values and `this`. Whole namespace uses, writes,
updates, deletes, assignment/destructuring/loop targets, computed or optional
lookups, namespace exports, dynamic scope, or incomplete member lists retain the
ordinary conservative proof. Member results may be called, chained, constructed,
or used as superclasses with their ordinary execution effects. Callback deferral
still requires `callback_factory_members`; export/reentry and early callback
invocation checks remain active. See the [source-privacy proof](https://github.com/agentydragon/ducktape/blob/devel/devinfra/js/debundle/docs/design.md#namespace-capability-privacy).

Explicit member writes nested in object or array destructuring, including nested,
default, rest, and loop-head targets, retain the same conservative namespace
mutation checks as ordinary member assignments.

For a namespace-member contract, the author must also audit the provider and all
caller/runtime paths that supply values used by admitted callbacks. The listed
factory members must keep their audited identities and behavior throughout the
analyzed chunk's synchronous initialization. In particular, provider or other
namespace members, callback/ref/hook inputs, and values returned through those
paths must not expose an alias that can replace a listed factory member.
Direct static identifier-property namespace call results are treated as
independent only under this explicit author assertion. The analyzer propagates
explicit namespace provenance through assignments, local aliases, property
values, returns, and call arguments. An unrelated call or constructor result
with no such path is not treated as a namespace alias solely because its
runtime value is unknown; the author audit asserts that values used by admitted
callbacks cannot alias or replace listed members. Unknown syntax remains
conservative at publication and call-argument sinks. Binding cycles alone do
not establish namespace provenance; namespace and
unknown-value inputs to a cycle remain conservative. A static identifier-property
optional read directly from an identifier retains the receiver's namespace
provenance and the possible undefined result. Optional calls, computed optional
reads and chained receivers remain conservative; this value-flow rule does not
assert purity or remove accessor effects.
For call-argument checks,
the analyzer follows namespace values in the supplied argument expressions
and excludes the callee expression, which is not passed as an argument. A
value from an unknown call or constructor used as a static or dynamic write
receiver remains a possible namespace alias, including through local aliases
and member projections, and disables the contract. The checker does not prove
that arbitrary callback parameters or unrelated call results are disjoint
from the namespace; that non-aliasing guarantee is part of the author audit.
Under intrinsic integrity, adjacent writes to declared own data fields of a
checked fresh base-class instance do not invalidate a namespace contract.
The allocation and assignment values must run no user code, and the instance
must remain private throughout that run. Unsupported constructors, fields,
receivers, dynamic scope, and intervening statements retain the conservative
write checks.
For that exact top-level own-field write, the same proof excludes a setter
effect from unknown-call reachability. The assignment remains an actual
top-level effect; nested callable writes and later receiver mutations retain
their usual accessor checks.
For a fresh top-level object literal that visibly contains the namespace and
has static ordinary own data properties,
the checker distinguishes the container's identity from namespace values held
in its properties. A read of a verified own property follows that property's
retained value; storing a namespace in the container still counts as
publication at a sink. An adjacent write to an existing own data property may
use the same private fresh-value proof when its value runs no user code.
Getters, setters, computed keys, spreads, `__proto__`, unknown receivers, and
unsupported property reads retain conservative namespace checks. Literals
without an explicit namespace path keep the conservative receiver rule.
Class publication tracking also follows retained static fields through a
uniquely resolved local class superclass on the constructor surface. Unknown,
imported, reassigned, or non-class bases and cycles remain conservative;
instance inheritance, static accessors, decorators, and unsupported class
surfaces remain opaque.
Adjacent static property writes to the fresh ordinary result of a direct
audited factory call with one inline callback also retain the contract. Their
values must run no user code; prototype writes and intervening statements end
the proof. Rejecting the producing namespace's factory contract also removes
this proof from its results.

Under intrinsic integrity (A11), a sequence expression assigning a direct global
`Error(...)` result to a local binding can retain the namespace contract through
immediately following static property assignments to that binding. Values must
be inert or declared local reads; `__proto__`, `stack`, and intervening
expressions end the proof. Every matching write in that owner and callable must
be proved. Calls, argument evaluation, and namespace publication through assigned
values retain their ordinary checks. Shadowed or visibly replaced `Error` and
dynamic scope remain conservative; see [design details](https://github.com/agentydragon/ducktape/blob/devel/devinfra/js/debundle/docs/design.md#conditions-on-the-input-chunk-assumptions-a1a11).

Within a literal traversed inside an admitted inline callback, a direct static
namespace member used as a call callee is analyzed as the callee expression,
not as a captured namespace value. Call arguments and effects remain analyzed;
computed and chained callees stay conservative.

Inside callback result object data properties, the analyzer also treats a
one-hop read such as `namespace.member` as a deferred member value. Only an
ordinary data property qualifies; the object-literal `__proto__` prototype
setter does not. That direct shape does not prove the member value's identity;
the author must audit its provider branches and any accessor, proxy, or
callback behavior reachable through it. Computed reads, chains, calls, spreads,
arrays, and other expression shapes remain conservative. Direct namespace
values and aliases remain rejected.

Without an assertion, source inference recognizes only immutable exported
function declarations with one simple identifier parameter. The body must
only return a zero-argument function or arrow that only calls that parameter
with no arguments. Reassigned bindings and dynamic scopes remain opaque.

`chunk_analysis_options.<chunk>.deferred_global_publications` names audited
browser-global data slots, for example `{globalThis: [inspectState]}` or
`{window: [inspectState]}`. A listed slot must be an ordinary inert own data
property, or an absent slot whose creation cannot invoke an inherited setter,
at the write. The object must permit the write. No host, importer, or source
consumer may use its stored function or class during synchronous
initialization of this chunk.
The analyzer admits only a whole top-level direct assignment of a known local
function/class, or the same `window.slot = value` assignment behind a checked
`typeof window` guard. It rejects ambiguous aliases, visible global-root
replacement, dynamic global access, additional source mentions of the slot
(including through another builtin
global alias), and visible global-object escapes. The certificate delays only
the callback-availability consequence of that exact write. RHS evaluation,
eager reads, property mutation, ordinary effects, and source-order constraints
remain. Independent exports or other publications of the same value retain
their own reentry behavior. A spec author must audit the host's property
descriptor and all consumers; source checks alone cannot establish either
host fact.
Primitive-valued operator results cannot carry the global-object identity.
The synthetic protocol call for a literal-key `in` check on a direct builtin
global root does not publish that root as a value; its possible host or proxy
effects remain in synchronous effect analysis. Dynamic keys, aliases, ordinary
calls, and visible root replacement retain the conservative publication guard.

## Local-effect decorator annotations

`effect: typescript_decorate_helper` asserts target-local mutation; it does not
assert callback timing. `effect: typescript_metadata_decorate_helper` additionally
certifies that every admitted application, including every helper branch,
intrinsic alias and supplied decorator, only writes inert target metadata.
Decorators run synchronously, but neither they nor the helper may invoke target
methods, getters, setters or constructors; publish callbacks outside the target
graph; reenter unrelated caller/host/export callbacks; or access caller cells
through hidden routes. Provider-private constant/metadata reads are allowed;
observable writes outside the target graph are not. The author audits ordinary
intact targets, prototypes, descriptors and intrinsics. Existing descriptor,
prototype and callable behavior must remain unchanged. Imported decorators need
this explicit audit; purity or import shape does not imply their behavior.

The stronger summary applies only to a whole expression statement shaped as
`helper([bare identifiers], LocalClass.prototype, literalStringKey, 1|2)`, with
a nonempty spread-free array and a key other than `"__proto__"`. Analysis must
prove exact stable local function/class identities and initialized bindings.
Helpers with non-top-level captured state, dynamic scopes and unsupported forms
keep conservative timing. Eager argument reads,
the helper's and local decorators' syntactic lazy read/rebind dependency closure,
and target-local mutation/co-location edges remain. This is not purity.

`effect: typescript_metadata_decorate_helper_with_member_decorators` adds one
static member lookup (`provider.decorator`) to the admitted decorator array.
The provider root must be an imported binding without any direct member write
or rebind in the analyzed chunk. The author additionally certifies that each
admitted provider lookup is an ordinary, stable own data property throughout
synchronous initialization: no getter, proxy, alias mutation, or hidden
callback reentry can replace or observe it. The selected member must itself
be a metadata-only decorator under the contract above. This assertion covers
all supported applications of the helper, not just a single call. Computed
keys, chained members, spreads, and local providers remain conservative.
The existing bare-identifier helper effect does not acquire member support.

`effect: typescript_metadata_decorate_helper_three_args` explicitly asserts
the same metadata-only behavior for
`helper([bare identifiers], LocalClass.prototype, literalStringKey)`.
The helper determines the descriptor without a fourth kind argument. All
helper branches and supplied decorators must satisfy the contract above;
reading and restoring an ordinary own descriptor must preserve its behavior.
For a subclass target, inherited metadata must also be ordinary inert data:
copying it may neither invoke accessors nor mutate the base class's metadata.
Stable initialized identities, dependency closures and target co-location
checks still apply. Extra arguments and unsupported decorator/key expressions
remain conservative. Earlier effects retain their existing accepted shapes.

## Vendor split-brain evidence

A partial vendor swap rejected for shared retained implementation reports both
its swapped-package ownership path and its residual liveness path. Ownership
traces distinguish dependencies of stripped exports from dependent items absorbed
into the package island. These explain the stripper's conservative classification;
they do not assert that every reported dependency executes synchronously.

## Native collection callback effects

Bounded synchronous analysis can execute a known callback for direct one-callback
`forEach`, `filter`, and `map` calls over a retained untainted dense ordinary
array. It passes actual retained element capabilities and preserves callback
reads, writes, coercions, and publication. Fresh results retain element aliases;
iteration and spread do not invoke stored functions. Unsupported sources,
mutation, intrinsic changes, and invocation limits retain conservative effects.
See [the proof and fallback conditions](https://github.com/agentydragon/ducktape/blob/devel/devinfra/js/debundle/docs/purity_soundness.md#retained-dense-array-iteration-effects).
