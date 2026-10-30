# Initialization contract chooser

A cycle report identifies ordering constraints. Establish the source and runtime
premises before changing an assertion. The analyzer checks admitted syntax and
recognized guards; it does not verify the complete provider or host audit. A green
gate cannot establish that an author assertion is true.

## Trace the blocker

1. Refresh the graph and read the exact cycle/cut evidence. Record the source unit,
   location, constraint kind, and called binding. Use the CLI's
   [gate queries](../../docs/cli.md#gate-queries) for the supported source-backed
   diagnostics; an inconclusive result leaves the cause unresolved.
2. Follow aliases and imports to the defining implementation. Separate eager
   effects, retained callbacks, and reads before the first `await`. Include
   helpers, provider branches, accessors, aliases, and publication paths.
3. For export-reentry constraints, inspect `export_reentry.json` beside the graph
   and verify the loader/dependency closure. For host operations, establish the
   exact object, descriptor, prototype, and intrinsic-integrity premises.

Source inference already recognizes some private callback containers and local
constructor results. Check [synchronous analysis limits](../../SPEC.md#synchronous-analysis-limits)
before adding an assertion for behavior the analyzer already proves.

## Choose the contract for the established fact

This table routes to the relevant audit. [SPEC](../../SPEC.md) owns the complete
normative definitions; read the linked definition before selecting a YAML key.
Callback and no-reentry keys below live under
`chunk_export_purity.<defining chunk>`.

| Established fact                                                                                   | Contract family and next step                                                                                                                                                                                     |
| -------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A call retains callbacks only in a fresh returned callable/plain-object graph                      | [Callback factories](#fresh-returned-callback-graphs): choose the inline-only, composable, or exact-signature contract for the actual inputs                                                                      |
| Direct imported `new` retains callbacks in a fresh instance                                        | [Retained constructors](#fresh-constructed-instances): construction and reachable-method audit                                                                                                                    |
| Registration avoids synchronous reentry but may store callbacks across calls                       | [Callback registration](#registration-and-cross-call-storage): later opaque operations may invoke every registered callback                                                                                       |
| A call with admitted primitive or inert-record arguments cannot synchronously reenter foreign code | [No-reentry calls](#calls-with-primitive-or-inert-record-arguments): audit identity and any namespace lookup; result stays opaque                                                                                 |
| An initializer, call, or construction has no observable effects                                    | [Purity scope](#purity-scope): select the exact evaluated operation                                                                                                                                               |
| Native browser ESM evaluation has no synchronous host namespace access                             | [Host behavior](../../SPEC.md#host-behavior): `host_behavior.synchronous_module_loader: none`; verify loader, closure, cycles, suspension, and vendor replacements                                                |
| A decorator only mutates inert target metadata                                                     | [Metadata effects](../../SPEC.md#local-effect-decorator-annotations): select the matching `effect:` and helper shape; audit target-method invocation and foreign reentry separately                               |
| A browser-global slot cannot expose its stored callback during synchronous initialization          | [Global publication](../../SPEC.md#deferred-browser-global-publication): `chunk_analysis_options.<chunk>.deferred_global_publications`; audit the host descriptor and all consumers                               |
| A literal-key membership probe on a direct builtin global cannot reenter                           | [Host membership](../../SPEC.md#audited-builtin-global-membership-probes): `chunk_analysis_options.<chunk>.non_reentrant_global_membership`; audit exact keys, host identity, prototypes, aliases, and intrinsics |

### Fresh returned callback graphs

**Choose:** `inline_callback_factory_exports` for one syntactic inline callback
returning a fresh ordinary callable; `callback_factory_exports` or
`callback_factory_members` for the broader single-input/composition contract;
`retained_callback_factory_exports` or `retained_callback_factory_members` for
exact audited signatures admitting existing or multiple callbacks.

**Shape and timing:** ordinary calls return a fresh ordinary callable/plain-object
graph retaining the admitted callbacks. The provider cannot invoke foreign
callbacks synchronously or publish them outside that result. Later invocation or
publication of the result can expose its callbacks and captures.

**Audit:** all admitted provider/helper/type paths, input and result graphs,
property/prototype surfaces, and access to importer-owned state. Existing named
callbacks add identity, alias, and input-surface obligations. Proxies, accessors,
class instances, existing-instance aliases, and cross-call callback storage are
outside the returned-graph premise. A helper that registers callbacks globally
needs the registration audit below even if it also returns a fresh wrapper.

Provider-private effects and argument evaluation remain normally ordered;
callback deferral does not assert purity. Full definitions:
[audited callback factories](../../SPEC.md#audited-callback-factories) and
[retaining existing or multiple callbacks](../../SPEC.md#retaining-existing-or-multiple-callbacks).

### Fresh constructed instances

**Choose:** `retained_callback_constructor_exports` for direct imported
`new Binding(...)`, with exact argument-count/callback-position signatures.
Ordinary factory assertions do not cover `new`.

**Shape and timing:** a fresh instance retains only the admitted callback graphs;
construction cannot invoke or publish them synchronously. Accessing, invoking, or
publishing the result can expose all retained callbacks and captures.

**Audit:** superclass construction, field initializers, defaults, helpers,
constructor identity, input aliases/surfaces, reachable methods, prototype and
intrinsic integrity. Shared callback storage, preexisting-instance aliases,
proxies, accessors, and externally mutable callback-bearing prototypes violate
the premise. Construction purity requires a separate audit.

Full definition: [retaining callbacks in constructed instances](../../SPEC.md#retaining-callbacks-in-constructed-instances).

### Registration and cross-call storage

**Choose:** `no_reentry_callback_exports` for a direct imported call with exact
argument-count/callback-position signatures, or
`anonymous_statements[].no_reentry_call` when the exact top-level call is
selected by `match` / `source_match` in the caller's spec. A source-selected
call may be bare or the rightmost operand of an `&&` chain; the guard retains
ordinary effects and reentry checks. The source assertion accepts a direct
identifier callee or a static member on an identifier
receiver, and its author audit covers every callable identity that can reach
the exact call site. For `no_reentry_callback_exports`, callee aliases keep
conservative timing. Callback inputs must resolve to one stable local ordinary
callable identity; retained factory-result graphs, computed or optional
members, effectful receivers, spreads, nested eager calls, and construction
keep conservative timing. Other arguments must use the admitted inert
primitive syntax.

**Shape and timing:** the audited call avoids synchronous foreign reentry, but may
store callbacks in a cross-call registry or return an opaque capability. After
the call, the analysis treats every supplied callback and capture graph as
globally reachable. Later opaque calls or protocol operations may invoke them.
The source-selected assertion itself establishes neither purity nor a result
shape; ordinary analysis remains in force and may independently infer either.

**Audit:** provider/helper/type paths, callable identity, own/prototype surfaces,
and aliases. The provider cannot synchronously invoke callbacks, mutate supplied
graphs, inspect effectful properties/prototypes, or access caller/importer-owned
lexical state. A source-selected member callee also puts its exact property
lookup under this premise; receiver and argument expressions still evaluate
normally. Provider-private effects remain possible. Registration does not
establish purity or a fresh-return/constructor retention policy.

Full definition: [callback registration without synchronous reentry](../../SPEC.md#callback-registration-without-synchronous-reentry).

### Calls with primitive or inert-record arguments

`no_reentry_exports` and `no_reentry_members` admit their checked primitive
arguments. `no_reentry_record_members` separately admits direct static member
calls with at least one fresh inert object literal; every other argument must be
an admitted primitive or fresh inert record. Audit every record shape and its
provider branch, including reads, coercions, retention, and prototypes.

All three require the callable's stable identity and behavior, audited provider
paths, and any independent namespace lookup/capability premises. They allow
provider-private effects and leave results opaque; they do not supply a
callback-retention policy.

Full definitions: [direct imported calls](../../SPEC.md#calls-without-synchronous-reentry)
and [namespace/record calls](../../SPEC.md#namespace-and-record-calls-without-synchronous-reentry).

## Runnable examples

[contract_examples.py](examples/contract_examples.py) runs the synthetic factory,
constructor, and registration examples through the real CLI. Each example pairs
an admitted call with a nearby unsupported shape or a later-use case that keeps
conservative constraints. The same recipe and inputs ship with every Debundle
skill and run under Bazel; the test harness supplies temporary directories and
compares original and emitted Node behavior for accepted cases.
Controls replace `source/main.js` in temporary copies, preserving its import
paths; run them through the recipe.

| Example                                                                                | Audited YAML                                                                                    | Boundary control                                                                                                                                                     |
| -------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [Fresh factory](examples/contract_examples/fresh_factory/source/main.js)               | [Single-callback factory](examples/contract_examples/fresh_factory/spec_config.yaml)            | [Named callback](examples/contract_examples/fresh_factory/source/controls/named_callback.js) needs the separate retained-input audit and signature                   |
| [Constructed instance](examples/contract_examples/retained_constructor/source/main.js) | [Exact constructor signature](examples/contract_examples/retained_constructor/spec_config.yaml) | [Unlisted arity](examples/contract_examples/retained_constructor/source/controls/unlisted_arity.js) keeps conservative timing                                        |
| [Persistent registry](examples/contract_examples/persistent_registry/source/main.js)   | [Registration timing](examples/contract_examples/persistent_registry/spec_config.yaml)          | [Early opaque invocation](examples/contract_examples/persistent_registry/source/controls/early_emit.js) can reach the callback before its captured value initializes |

From this guide's directory, using the adapter's engine and a Node binary:

```bash
python3 examples/contract_examples.py \
  --debundle /path/to/debundle \
  --node /path/to/node \
  --fixtures examples/contract_examples \
  --out /tmp/debundle-contract-examples
```

Read each provider and its call site before the assertion. These examples teach
shape and timing boundaries; a passing test does not prove a different provider's
body, runtime, host setup, or intrinsic integrity.

## Purity scope

- `initializer_purity: pure` applies to one selected simple top-level variable
  initializer. Audit the entire expression, including synchronous callbacks:
  it must have no observable effects and cannot throw. It does not certify
  later calls or construction of the resulting value. Eager reads and
  callback dependencies remain. Imports, functions, classes, destructuring,
  missing initializers, and ambiguous initialized sites are unsupported.
- `purity: pure` asserts call purity for one local bound function.
- `purity: pure_new` asserts purity only for `new BoundClass(...)`. A selected
  defining class's assertion follows its exports to importers. Prefer a
  structural `source_match` for a minified class.
- `chunk_export_purity.<provider>.pure_exports` and `pure_new_exports` assert
  call or construction purity for named imports from that provider.
- Member-level `pure_members` covers listed static calls through a binding.
  `fluent_exports` asserts the broader transitive derived-value contract;
  audit that complete surface before selecting it.

These are author-trusted assertions. The classifier does not verify the body.
Arguments retain their normal checks; call purity does not certify an export's
initializer or remove independently observed callback dependencies.

## Validate the decision

Record the inspected source, audited provider/runtime premises, and exact
assertion. Rerun source validation, the extraction gate, and the adapter's runtime
smoke. Confirm the intended assignment passes and inspect retained constraints:
actual eager reads, mutation, argument evaluation, and unrelated ordering still
apply. Host membership certification also leaves possible throws and operand
impurity intact.

If the premise cannot be established, retain the conservative constraints and
choose a different module boundary. A successful source match or gate alone does
not prove runtime behavior.
