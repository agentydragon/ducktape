# Abstract interpreter

One abstract interpreter over one abstract domain, driven by one declarative
table of builtin summaries. Every bespoke certificate rule in § 1 becomes a
derived instance: the interpreter evaluates the shape under the domain and the
table, and the former rule's conclusion is a projection of the resulting effect
set. The iterative substrate stays: typed facts, program points, worklist,
proof dependencies and `AnalysisAssumptions` axioms (`inference/iterative.rs`,
<../docs/inference.md>).

Precision is not the goal. The goal is one semantics, so a new bundle shape
costs a table row or an axiom, not a rule. Where a current rule's precision
cannot be reproduced by the domain, § 2.5 says so and names the author
assertion that replaces it.

Burn-down: each step in § 5 deletes the rule it replaces once the interpreter
passes that rule's contract tests unchanged. Durable content graduates to
`SPEC.md`, `docs/inference.md` and the table's own file; this plan is deleted
when § 5 is empty.

## 1. Inventory

Test labels are `//devinfra/js/debundle/e2e:<name>` unless marked `unit`.
"Proves" is the fact a consumer reads; "Premises" are what the rule checks
itself (A1–A11 and the shared native census are implicit everywhere).

### Structural facts (`facts/`, `graph/build.rs`)

| #   | Family                                     | Files                                                                                                                                                                                                                 | Proves                                                                                                                                                                                 | Premises                                                                                                 | Contract tests                                                                                                                                                                                                                                                                | Docs                                                                                                                                      |
| --- | ------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Statement fact collector                   | `facts/collector.rs`, `statement_facts.rs`, `lazy_boundary.rs`, `item_views.rs`, `analyze.rs`                                                                                                                         | Eager/lazy/first-order reads, rebinds, calls; `StatementEffectSummary` over `Binding`/`GlobalProp` cells; `dataflow_summarizable`, `cell_writes_summarizable`; top-level-await ordinal | Bail shapes: `with`, `Function(...)`, computed global key, member write through a binding, unproven call | `s_chain_dataflow_soundness`, `at_init_s_chain_dataflow`, `lemma_five_side_effect_order`, `repeated_var_loop`, `hoisted_var_declaration`                                                                                                                                      | design § Emission modes, § Shared `var` declaration sites                                                                                 |
| 2   | Global-object aliases and escape taint     | `facts/global_object.rs`                                                                                                                                                                                              | Which alias names denote the global object; taint fixpoint that clears `dataflow_summarizable`                                                                                         | Alias not declared or imported at chunk top                                                              | `soundness_at_init_value_flow`, `at_init_s_chain_dataflow`                                                                                                                                                                                                                    | design § Emission modes                                                                                                                   |
| 3   | Dynamic scope                              | `facts/dynamic_scope.rs`                                                                                                                                                                                              | Whole-chunk absence of direct `eval`/`with`; premise of every identity proof                                                                                                           | —                                                                                                        | `soundness_dynamic_scope`                                                                                                                                                                                                                                                     | design § A1                                                                                                                               |
| 4   | Value flow                                 | `facts/value_flow.rs`                                                                                                                                                                                                 | Monotone `Value` sets (`Alloc` of `Function`, `Class`, `Object`, `Set`; `Top`) for bindings, properties, returns, parameters; candidate-object reachability; call targets              | Intra-chunk, context-insensitive; `Top` joins beside known identities                                    | `soundness_at_init_value_flow`, `value_flow_sharing_scale`, `at_init_unresolved_callee`, `unknown_call_unescaped_callable`, `unknown_call_intermediate_availability`; unit `facts/value_flow_tests.rs`, `facts/value_set_tests.rs`                                            | inference § Scheduling                                                                                                                    |
| 5   | At-init call promotion and fallback        | `graph/build.rs::promote_at_init_calls`, `facts/at_init_fallback.rs`, `facts/source_unit_read_closure.rs`                                                                                                             | Promoted `EagerUse` edges from at-init callers to callee lazy reads/rebinds; fallback read closure; plain-array / `no_sync_callback_members` / member-row (`addEventListener`) pruning | Callee bodies to first definite `await`; unresolved targets widen to the owner read closure              | `at_init_promotion_fndecl_direct_read`, `at_init_promotion_nested_closure`, `at_init_cross_module_call_body_reads`, `at_init_unresolved_callee`, `lemma_three_at_init_read`, `class_callback_reachability`, `unknown_call_export_reachability`, `unknown_call_export_reentry` | design § At-init call promotion; `graph/edge.rs`                                                                                          |
| 6   | Local effects                              | `facts/local_effects.rs`, `facts/local_effect_targets.rs`                                                                                                                                                             | `effect: typescript_decorate_helper*` target-local mutation; metadata-only decorator summary                                                                                           | Author `known_effects`; exact helper call shape                                                          | `makes_decorate_call_lowering`, `metadata_decorator_timing`, `three_argument_metadata`                                                                                                                                                                                        | SPEC § Local-effect decorator annotations; purity_soundness § Local-effect decorator annotations                                          |
| 7   | Set iterable shapes                        | `facts/set_iterables.rs`                                                                                                                                                                                              | `new Set([primitives])`, const-array alias chains, `set_from_set_spread` intervals                                                                                                     | Set/Array integrity or author input; every alias reference enumerated                                    | `set_iterable_reentry`, `native_operation_input`, `purity`                                                                                                                                                                                                                    | purity_soundness § `Set` from a chunk-top const array alias; SPEC § Native-operation inputs                                               |
| 8   | Stable primitive array spreads             | `facts/array_spreads.rs`                                                                                                                                                                                              | Contiguous multi-declarator spread of primitive arrays                                                                                                                                 | Array iterator integrity or author input; producer/consumer co-location                                  | `stable_array_spread`                                                                                                                                                                                                                                                         | SPEC § Native-operation inputs (`array_spread_from_primitive_arrays`)                                                                     |
| 9   | Local class construction                   | `facts/local_class_construction.rs`                                                                                                                                                                                   | Implicit constructor with inert fields and zero-argument native collection fields                                                                                                      | Unique class value in flow; native field identity through #12                                            | `local_class_construction`, `native_call_identity_input`                                                                                                                                                                                                                      | purity_soundness § Implicit local class construction; SPEC § Synchronous analysis limits                                                  |
| 10  | Fresh receiver and fresh record            | `facts/fresh_receiver.rs`, `purity/fresh_record.rs`                                                                                                                                                                   | Ordered writes to pre-existing own data fields of one fresh literal, then return                                                                                                       | Exact body shape; no aliases, calls or publication                                                       | `local_record_factory_contract`, `fresh_returned_record_destructuring`                                                                                                                                                                                                        | purity_soundness § Fresh local record field writes, § Source-composed returned records                                                    |
| 11  | Callable certificates                      | `facts/callable_inference.rs`, `facts/semantic_inference.rs` (`StableFunction`, `StableProvider`, `NamespaceWrapper`, `ArgumentNoEscape`, `DescriptorCopyProvider`/`Call`), `facts/sync_effects/namespace_wrapper.rs` | Stable identity; namespace facade; descriptor-copy wrapper; parameter non-escape                                                                                                       | No dynamic scope or rebind; exact body shapes; Object/WeakMap/Array/Function integrity                   | `imported_callback_timing`, `retained_callback_factory`, `retained_constructor_callback`, `namespace_wrapper`, `descriptor_copy_factory`, `argument_input`, `wrapper_argument_native_integrity`                                                                               | inference; SPEC §§ Inferred returned-record construction timing, Inferred namespace-wrapper timing, Audited callback factories            |
| 12  | Native integrity and identity certificates | `facts/abstract_domain/integrity.rs` (`IntrinsicSlot`, `IntegrityFlags`, `IntrinsicIntact`); `facts/semantic_inference.rs` (`LocalClass`, `PrimitiveSetIteration`, `StablePrimitiveArraySpread`)                      | Slot integrity flags (census or exact-site author axiom); exact callee identity; fresh empty collection; getter-free own-data read                                                     | Completed census; no dynamic scope; unshadowed binding or `globalThis` qualification                     | `native_constructor_identity_order`, `empty_collection_timing`, `native_own_data_read`, `prototype_property_integrity`, `intrinsic_alias_lowering`                                                                                                                            | inference; SPEC §§ Native integrity inputs, Synchronous execution diagnostics; purity_soundness § Empty intrinsic collection construction |
| 13  | Heap content effects and frontier          | `facts/array_content_effects.rs`, `graph/build.rs` (`emit_heap_content_order`, `emit_cell_order_events`, `OneShotEagerAllocations`, `HeapContentRules`, `CellOrderBarrier`)                                           | `ArrayContents`/`SetContents` cell reads, writes, allocations; one-shot births; opaque barriers; RAW/WAW/WAR edges                                                                     | Value-flow identities; synchronous-call completeness; certified construction sites                       | `local_array_push_order`, `stable_array_spread`, `at_init_s_chain_dataflow`, `graph_provenance`; unit `graph/build.rs::array_heap_frontier_tests`                                                                                                                             | design § Emission modes                                                                                                                   |
| 14  | S-chain emission                           | `graph/build.rs::emit_s_chain`                                                                                                                                                                                        | Strict adjacent-impure chain or dataflow-aware cell edges                                                                                                                              | #1, #24 verdicts; `trusted_dataflow_summaries`                                                           | `lemma_five_side_effect_order`, `s_chain_dataflow_soundness`, `runtime_tdz_on_imported_class`                                                                                                                                                                                 | design § S — the side-effect order graph                                                                                                  |

### Synchronous execution (`facts/sync_effects/`)

| #   | Family                                                                    | Files                                                                                                                                          | Proves                                                                                                                                                                                                                                                                                                     | Premises                                                                                                  | Contract tests                                                                                                                                                                                                                                                                                                                  | Docs                                                                                                                                                                    |
| --- | ------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 15  | Execution index                                                           | `execution_index.rs`; `execution_index/callback_arguments.rs`, `execution_index/prototype_properties.rs`                                       | Callables, roots, writes, source order; declaration/write census; callback-argument evidence; `%Object.prototype%` write census                                                                                                                                                                            | Source-only; precedes author-input validation                                                             | `synchronous_precision_report`, `prototype_property_integrity`, `wrapper_argument_native_integrity`                                                                                                                                                                                                                             | SPEC § Synchronous analysis limits; purity_soundness §§ Source callback arguments and unreachable native writes, Property absence                                       |
| 16  | Native write census and key domains                                       | `execution_index.rs` (`NativeWriteCensus`, `NativePointEffects`, `PropertyKeyDomain`, `ConditionalIntrinsicWrite`, `NativeIntegrityRootRules`) | Mutated `NativeIntrinsic` families read as slot flags; exact-slot known writes; finite-string / Symbol / unknown key domains; closed-write fixed point across chunks                                                                                                                                       | A11 outside analyzed source                                                                               | `native_constructor_identity_order`, `native_operation_input`, `native_call_identity_input`, `empty_collection_timing`, `intrinsic_array_iteration`                                                                                                                                                                             | SPEC § Inferred native property-key domains; purity_soundness § Property-key domains in native writes; plans/native_receiver_identity.md                                |
| 17  | Receiver flow                                                             | `execution_index.rs` (`ReceiverFlowRules`, `ReceiverFlowClosure`, `NativeReceiverRoot`)                                                        | Captured ordinary-object descendants                                                                                                                                                                                                                                                                       | Author `captured_value_inputs`                                                                            | `captured_value_input`                                                                                                                                                                                                                                                                                                          | SPEC § Captured ordinary-object values                                                                                                                                  |
| 18  | Namespace privacy, global membership, deferred publication, fresh `Error` | `execution_index.rs` (`NamespacePrivacy`, `GlobalMembershipProbe`, `DeferredGlobalPublication`, `FreshErrorWriteClaim`)                        | Namespace value never escapes except through listed members; literal-key `in` on an unshadowed root; one guarded global write; `Error(...)` sequence writes                                                                                                                                                | Author `namespace_independent_members`, `non_reentrant_global_membership`, `deferred_global_publications` | `global_membership_reentry`, `native_browser_reentry`, `imported_callback_timing`, `retained_callback_factory`                                                                                                                                                                                                                  | SPEC §§ Deferred browser-global publication, Audited builtin-global membership probes; design § Namespace capability privacy                                            |
| 19  | Interpreter                                                               | `interpreter.rs`, `runtime_inference.rs`, `runtime_heap.rs`, `runtime_models.rs`, `runtime_callbacks.rs`                                       | Per-prefix `ExecutionState`; `Outcome` (effects, returns, `complete`, executed owners); evaluate/invoke/call/member-write/native transfers; callback promotion (raw, deferred, returned, possible); bounded fallback; `forEach`/`filter`/`map`; local class construction; `Object.assign`                  | Callable coverage to first definite `await`; budgets 64 depth / 256 invocations / 64 pool                 | `synchronous_precision_report`, `opaque_callback_scale`, `no_reentry_callback`, `call_registration_contract`, `imported_callback_timing`, `intrinsic_array_iteration`, `map_entry_callbacks`, `from_entries_callback_storage`, `optional_call_timing`, `reentry_free_operations`, `export_reentry_report`, `imported_primitive` | SPEC §§ Synchronous analysis limits, Synchronous execution diagnostics, Native collection callback effects; design § At-init call promotion; debug/runtime_transfers.md |
| 20  | Value model                                                               | `value_model.rs`                                                                                                                               | `AbstractValue` (a `Value` or one of 11 callable/heap carriers), `ValueExpr`, `Values`, interned `Captured` contexts, object shapes in ECMAScript own-key order                                                                                                                                            | —                                                                                                         | all of #19                                                                                                                                                                                                                                                                                                                      | inference § Scheduling                                                                                                                                                  |
| 21  | Builtin summary rows                                                      | `facts/abstract_domain/builtins.rs`                                                                                                            | Eighteen rows (`IntrinsicOp`): `new Map/Set/WeakMap/WeakSet()`, `new FinalizationRegistry(f)`, `Math.{round,floor,ceil,trunc,abs,sign}`, `Object.{getOwnPropertyDescriptors,getOwnPropertyNames,defineProperties,freeze}`, `RegExp`, `Symbol`/`Symbol.for`; `Row { coerces, allocates, mutates, returns }` | Unshadowed intrinsic; `requires` slot integrity; input shape                                              | `native_call_summary`, `math_round_timing`, `fresh_freeze_callback_timing`, `empty_collection_timing`, `own_property_names`, `own_descriptor_inspection`                                                                                                                                                                        | purity_soundness § Guarded native call summaries; SPEC § Inferred FinalizationRegistry callback retention                                                               |
| 22  | Own-property projections                                                  | `property_inference.rs`                                                                                                                        | Source origin → static shape → prefix state → names / descriptor projection; application through `defineProperties`                                                                                                                                                                                        | Untainted exact origin; own-key set equals indexed shape                                                  | `own_property_names`, `own_descriptor_inspection`, `namespace_wrapper`                                                                                                                                                                                                                                                          | SPEC § Ordinary own-property inspection; purity_soundness § Ordinary own-property inspection and descriptor application                                                 |

### Purity (`purity/`, `cross_module_*.rs`)

| #   | Family                            | Files                                                                                                 | Proves                                                                                                                                                          | Premises                                                              | Contract tests                                                                                                                                | Docs                                                                             |
| --- | --------------------------------- | ----------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| 23  | Whitelists (landed, step 4a)      | `facts/abstract_domain/builtins.rs` rows and `DATA_PROPERTIES`; `purity/native_rows.rs` projects them | Rows with `assumed_intact`; purity admits a row when its listed steps are silent for the syntactic operands                                                     | ECMA-262 citation per row; receiver unshadowed; A11                   | `purity`, `object_plain_data_calls`, `proxy_constructor_purity`; unit `purity/classifier_tests.rs`                                            | purity_soundness § Admission rule: builtin summary rows                          |
| 24  | Expression classifier             | `purity/classifier.rs`, `purity_type.rs`                                                              | `Purity` with `PurityRule` reasons: coercing operators, `ToPropertyKey`, iteration, destructuring, spreads, tagged templates, `new` shapes, member reads        | Syntactic; result-primitive gate for coercion                         | `purity`, `initializer_purity`, `export_default_purity`, `dependency_graph_purity_reason`, `impure_call_reason_growth`, `repeated_var_purity` | purity_soundness § Declared purity — spec trust contracts                        |
| 25  | Code graph and PlainData          | `purity/code_graph.rs`, `facts/abstract_domain/heap.rs` (step 4b), `ts_enum_iife.rs`                  | `ChunkBinding` kinds; PlainData as heap `escape`/`taint` projection; TS enum IIFE; exact enum member reads and computed keys                                    | Name-based write scan plus value-flow alias check                     | `object_plain_data_calls`, `plain_data_escape_scale`, `soundness_plain_data_escape`                                                           | purity_soundness § `ChunkBinding::PlainData`; SPEC § Synchronous analysis limits |
| 26  | Value classes                     | `facts/abstract_domain/primitive.rs` (`collect_binding_prims`), `heap.rs`, `purity/fluent.rs`         | Primitive types of chunk-top `const` and stable Number `var` bindings (projected to result-primitive and Symbol classes); fluent and plain-array alias closures | Whole-chunk write/escape gates in #25; written or shadowed intrinsics | `purity`, `captured_value_input`                                                                                                              | purity_soundness § Immutable Symbol computed keys                                |
| 27  | Recursive and cross-module purity | `purity/effect_inference.rs`, `cross_module_purity.rs`                                                | Bottom-up function effects to closure across modules; pinned exports as axioms                                                                                  | Resolved import graph; absent export stays `unknown_call`             | `cross_module_purity_scale`, `pure_members`, `purity`                                                                                         | inference § Scheduling                                                           |
| 28  | Cross-module primitive values     | `program_analysis.rs`, `cross_module_purity.rs::resolve_export_values`                                | Exported `Value` sets projected onto importers through the import resolver                                                                                      | Provider ESM without CJS surface, `eval`, `with`                      | `imported_primitive`                                                                                                                          | purity_soundness § Imported primitive coercion timing                            |
| 29  | Redundant hint probes (consumer)  | `purity/redundant_hints.rs`                                                                           | Diagnostics over #23–#27                                                                                                                                        | —                                                                     | `purity_hint_diagnostics`                                                                                                                     | purity_soundness § Redundant purity hints                                        |

### Admission and author inputs

| #   | Family             | Files                                                        | Proves                                | Premises                              | Contract tests    | Docs                                         |
| --- | ------------------ | ------------------------------------------------------------ | ------------------------------------- | ------------------------------------- | ----------------- | -------------------------------------------- |
| 30  | Chunk admission    | `chunk_analysis/chunk_admission.rs`, `chunk_analysis/mod.rs` | A1/A3/A5 shapes rejected; A2 TLA bail | —                                     | `chunk_admission` | design § A1–A11, § Coverage gaps             |
| 31  | Author assumptions | `analysis_inputs.rs::AnalysisAssumptions` (30 fields)        | Axioms consumed by #5–#27             | Author audit; `audited_source_sha256` | § 4               | design § Conditionally-correct optimizations |

Consumers unchanged by this plan: the realizability gate (`gate.rs`,
`realizability/`), quotient and edge roles (`graph/edge.rs`), reports.

## 2. The abstract domain

Defined once, in the `facts/abstract_domain/` package (`value.rs`,
`primitive.rs`, `heap.rs`, `effect.rs`). The directory is not
`facts/abstract/`: `abstract` is a reserved word in Rust. Value flow, the
interpreter's `AbstractValue`, purity's binding classes and cross-module export
values share `value.rs`'s `Value`.

### 2.1 Values

```text
Value   ::= Prim(kind, Const(literal) | Any)
          | Alloc(site, kind, mult)            mult ∈ {One, Many}
          | Intrinsic(path)                     e.g. %Set%, %Object.freeze%, %Array.prototype%
          | Callable(body)                      body ∈ {Known(fn), Native(op), Unknown}
          | Foreign(summary)                    imported capability token (returned record, deferred callbacks)
          | Unavailable                         TDZ read: evaluation throws before a value
          | Top
kind    ::= Object | Array | Set | Map | WeakMap | WeakSet | Function | Class | Instance(class)
          | Promise | RegExp | Registry | Proxy | Error
```

A value set is a finite join of these. `Intrinsic(path)` carries no integrity
itself; integrity is a flag answered per slot (`facts/abstract_domain/integrity.rs`). `Const` holds
the literal needed by current rules: string/number/boolean/null/bigint
literals, `Symbol.for` keys, enum member values. Two distinct constants join to
`Any` of the same kind; distinct primitive kinds join to their union.

Landed with step 3: `Value ::= Alloc(kind, site) | Prim(kinds) | Unavailable |
Top`. `Prim` holds the set of primitive types a value can have, so primitives
join by union rather than to `Top`, keeping a stable `var`'s Number-or-
`undefined` type and a `Number | BigInt` operator result. A local function body
is `Alloc(Function, site)`. `primitive.rs` evaluates operator results and
expressions over a binding scope; builtin rows return `Prim(kinds)`. `Const`,
`Intrinsic`, `Callable(Native | Unknown)` and `Foreign` arrive with the step
that first reads them (string constants with 5d's own-property names; callable
and foreign values with 5b and 5c, which fold the interpreter carriers
`Closure`, `ClassMembers`, `Instance`, `CallbackFactory`,
`InlineCallbackFactory`, `DeferredCallbacks`, `OwnDescriptorMap`,
`ForeignDeferredRecord`, `ObjectAssign`, `ArrayIntrinsic` and `GlobalObject`
into `Value`). Multiplicity is a heap property and arrives with step 6.

### 2.2 Heap

```text
Heap    ::= site → ObjectState
ObjectState ::= { props: key → ValueSet,        key ∈ FiniteString | Symbol(site|wellknown) | UnknownKey
                  dense: Known(len) | Unknown,  arrays only
                  proto: Intrinsic(path) | Alloc(site) | Null | Unknown,
                  escape: Private | Published(owners) | Global,
                  integrity: Extensible | Frozen,
                  taint: Clean | Tainted }
```

`UnknownKey` absorbs: a write with an unknown key sets `taint = Tainted` and
`props` to `Top` for every key. `escape` is monotone `Private → Published →
Global`; `Published(owners)` names the source units that can reach the
allocation (today's `callback_source_units`, `exported_runtime_values`,
`global_escaped_callbacks`). `Many` multiplicity means one site stands for
several runtime objects; property maps then join across instances.

Landed with step 4b: `facts/abstract_domain/heap.rs` holds `escape ::=
Private | Published` and `taint ::= Clean | Tainted` for chunk-top literal
initializers (and TS enum IIFE and derived-array initializers), solved
flow-insensitively before purity from value flow and one whole-chunk scan;
`PlainData` is its projection. `props`, `dense`, `proto`, `integrity`,
`Published(owners)`/`Global` and per-prefix state arrive with the
interpreter-derived heap (4c, 5a, 6).

### 2.3 Effects

```text
Cell    ::= Binding(id) | GlobalProp(name) | Prop(site, key) | Contents(site) | IntrinsicSlot(path)
Effect  ::= Read(Cell) | Write(Cell) | Alloc(site) | Invoke(CallableSet) | Coerce(ValueSet, op)
          | Havoc(reach)  reach ⊆ sites ∪ bindings reachable from the call's inputs and globals
          | Throw
```

`Coerce(v, op)` for `op ∈ {ToPrimitive, ToPropertyKey, ToString, ToNumber,
[[Get]], [[HasProperty]], Iterate}` resolves to nothing when every value in
`v` is `Prim`, to `Invoke(unknown)` when any value is an allocation with an
unknown prototype or `Tainted`, and to nothing when the allocation is a
`Clean` ordinary object whose prototype is an intact intrinsic and the key is
own data. This single resolution rule replaces #24's `CoercingOperator`,
`ToPropertyKeyCoercion`, `IterationProtocol`, `DestructuringPattern` reasons,
#19's `CallKind::ImplicitCoercion`/`ArraySpread`/`ObjectSpread` paths and
#16's key-domain refinement.

`Invoke(set)` with a `Known` body is expanded by the interpreter under budget;
`Unknown` or over-budget becomes `Havoc(reach)`. `Havoc` is what the gate
reads as an opaque barrier (#13 `CellOrderBarrier::AllPriorEvents`) and what
purity reads as `UnknownCall`.

### 2.4 Lattice and widening

- Program state at a program point is one `(Env, Heap)`; paths join, never
  split (today's `prefix_values` + `prefix_object_properties`). No
  path-sensitivity.
- Values: finite height by construction (`Const → Any`, primitive types by
  union; callable sets widen to `Unknown` past `MAX_SYNCHRONOUS_CALLBACK_POOL`).
- Heap: `props` keyed maps join pointwise; `UnknownKey` absorbs; `escape` and
  `integrity` monotone; `mult` goes `One → Many` on a second birth of the same
  site in one prefix (loops, repeated invocations). This is #13's
  `OneShotEagerAllocations` and SPEC's "one source origin can represent
  multiple runtime allocations".
- Callables: one summary per `(body, abstract argument shapes)`; a second
  distinct shape for the same body widens the arguments to `Top` and
  re-summarizes once (today's `InvocationSignature` memo plus budgets
  `MAX_SYNCHRONOUS_INVOCATION_DEPTH = 64`, `MAX_SYNCHRONOUS_INVOCATIONS = 256`).
  Budget exhaustion is a widening to `Havoc`, recorded as today's
  `precision_losses`.
- Loops: one pass with `Many` multiplicity and joined state; no fixpoint over
  loop bodies.
- Statement boundary: the settled state after statement _n_ is the input of
  statement _n+1_ and is never rewritten (inference § Dependency regions). A
  statement's summary is `(effects, result)` over its input state.

### 2.5 Current precision: kept, derived, or replaced by an axiom

| Rule (§ 1)                                                                   | Relies on                                                 | In the domain                                                                                                                                                                                                                                                                                                     |
| ---------------------------------------------------------------------------- | --------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| #1 cell summaries, #2 taint                                                  | Syntactic `Binding`/`GlobalProp` cells; alias taint       | Derived: `Read/Write(Binding                                                                                                                                                                                                                                                                                      | GlobalProp)`; a global alias as a value is `Alloc(global)`with`escape = Global`, so reads through it are `Prop(global, …)` cells |
| #4 value flow                                                                | Origin sets per binding/property/return                   | Derived: `Value` sets; `NativeSet` origin becomes `Alloc(site, Set)` with `proto = %Set.prototype%`                                                                                                                                                                                                               |
| #5 promotion                                                                 | Callee lazy reads to first `await`; fallback read closure | Derived: `Invoke(Known)` expansion; `Havoc(reach)` carries the read closure. Kept.                                                                                                                                                                                                                                |
| #7 Set from primitive array, #8 array spread                                 | Every alias reference enumerated; interval co-location    | Derived from `escape = Private` and `Iterate` resolution on a `Clean` dense array. Interval co-location stays a graph edge (#13).                                                                                                                                                                                 |
| #9 local class, #10 fresh record                                             | Exact body shapes                                         | Derived: interpreting the constructor/factory body yields `Alloc(One)` with known props and `escape = Private`                                                                                                                                                                                                    |
| #11 callback factory / returned record / namespace wrapper / descriptor copy | Exact body shapes plus native slots                       | Derived for local bodies; imported providers keep `Foreign(summary)` tokens whose summary rows are axioms (§ 4)                                                                                                                                                                                                   |
| #12 native integrity, call identity                                          | Family-level census; unshadowed binding                   | Landed in step 2 as slot flags (`facts/abstract_domain/integrity.rs`). Identity is `Env(binding) = Intrinsic(path)` plus `IntrinsicSlot(path)` integrity                                                                                                                                                          |
| #16 key domains                                                              | Finite-string / Symbol key refinement of native writes    | Derived: `Write(Prop(intrinsic, key))` with `key` from the value lattice                                                                                                                                                                                                                                          |
| #17 local receiver identity                                                  | Reaching definitions with kills                           | **Dropped**: the domain joins paths. A receiver with both a fresh and a global-alias definition is `Top` → `Havoc`. Replacement: `captured_value_inputs` axiom on the binding (§ 4), or a `Private` initializer that dominates syntactically (straight-line prefix, which the joined state already distinguishes) |
| #18 namespace privacy                                                        | Whole-chunk flow of one namespace value                   | Derived: `escape` of the namespace allocation; listed members read as `Prop(ns, member)`                                                                                                                                                                                                                          |
| #19 callback pool, fallback, array iteration                                 | Budgets, interned contexts, bulk effect merge             | Kept as the interpreter core                                                                                                                                                                                                                                                                                      |
| #21 native models, #22 own-property projections                              | Per-operation input/return/heap arms                      | Table rows (§ 3)                                                                                                                                                                                                                                                                                                  |
| #23 whitelists, #24 classifier                                               | Per-operation purity tables; result-primitive gate        | Table rows; `Pure ⇔ effects ∩ {Write(observable), Invoke, Havoc} = ∅`. A row's own `throws` (`OnValue`, unmet type checks) excludes admission, as the whitelists did; throws of operand evaluation stay outside purity                                                                                            |
| #25 PlainData                                                                | Name-based write scan + alias escape scan                 | Derived: `escape = Private`, `taint = Clean`, props all `Prim`                                                                                                                                                                                                                                                    |
| #26 fluent / plain-array / Symbol-const / stable numeric                     | Finite positive closures                                  | Derived: `Prim(Const)` tracking and `Alloc(Array)` with `Prim` elements; fluent stays an axiom (`fluent_exports`)                                                                                                                                                                                                 |
| #27 recursive purity                                                         | Bottom-up effects over call obligations                   | Kept: callable summaries are the same worklist facts                                                                                                                                                                                                                                                              |
| #28 imported primitives                                                      | Cross-module primitive closure                            | Derived: exported `Value` sets projected onto importers through the existing resolver                                                                                                                                                                                                                             |
| #17 `set_from_set_spread` interval, #8 co-location                           | Source-order interval of certified declarators            | Kept as a graph co-location edge; the interpreter only certifies the values                                                                                                                                                                                                                                       |

## 3. Builtin summary table

One file, `facts/abstract_domain/builtins.rs`, a `const` slice of rows keyed
by intrinsic operation. The interpreter consults it at every `Invoke(Native(op))`
and every protocol step (`Iterate`, `[[Get]]`, …) that lands on an intrinsic.
Nothing else in the crate names a builtin.

### 3.1 Schema

```text
Row {
  op:        IntrinsicOp,                    // %Set% [[Construct]], %Object.freeze% [[Call]], %Array.prototype.push% [[Call]] …
  cite:      &str,                            // ECMA-262 section anchor
  arity:     RangeInclusive<usize>,           // admitted argument counts; spreads never admitted
  coerces:   &[(ArgSel, CoerceOp)],           // which inputs go through ToPrimitive/ToPropertyKey/Iterate/[[Get]]…
  invokes:   &[ArgSel],                       // arguments called synchronously (callbacks)
  allocates: Option<(Kind, Multiplicity)>,    // fresh result allocation
  mutates:   &[(ArgSel, Mutation)],           // Props(keys) | Integrity | Contents | Proto
  returns:   Return,                          // Prim(kind) | Arg(i) | Fresh | Receiver | Unknown
  throws:    Throws,                          // Never | OnCoercion | Always(kind)
  requires:  &[IntrinsicSlot],                // slots whose integrity flag must be Intact or AuthorAsserted
}
ArgSel ::= This | Arg(i) | Rest(i) | Elements(Arg(i)) | Entries(Arg(i))
```

Landed with steps 1 and 4a: `Row { op, invocation, cite, arity, coerces,
invokes, allocates: Option<Kind>, mutates: &[Mutation], returns, throws,
assumed_intact, requires: &[IntrinsicSlot] }` (slot-level `requires` from
step 2; empty outside the census) with `Invocation ::= Call | Construct |
Tag`, `ArgSel ::= Arg(i) | Rest(i)`, `Mutation ::= Integrity |
DefineProperties | FreshOwnDescriptorsOf | FreshOwnKeysOf | FreshRetains`,
`Return ::= Unknown | Prim(kinds) | Arg(i) | Fresh` (`Prim` from step 3), `Throws ::= Never |
UnlessObjects | UnlessCallable | OnValue`, plus `DATA_PROPERTIES` for static
reads. Per-element iteration steps are `CoerceOp::{EntryGet,
EntryKeyToPropertyKey}` rather than `Elements`/`Entries` selectors.
`Multiplicity` and `Invoke`-bearing rows arrive with the step that first
reads them. `assumed_intact` is purity's
A11 identity premise; it goes once purity reads integrity flags.

`coerces` is where user code can hide: a row with `coerces = []` and
`invokes = []` fires no user code for any inputs; a row that coerces `Arg(0)`
fires no user code only when the interpreter proves `Arg(0)` is `Prim` or a
`Clean` ordinary object with intact prototype (§ 2.3). Since step 4a purity
evaluates this column against syntactic operand shapes
(`purity/native_rows.rs`); 4c replaces the shapes with the argument
lattice (4b only made the `PlainData` operand a heap-state projection).

### 3.2 Worked rows

| op                                         | cite                                           | arity | coerces                                                                       | invokes                                          | allocates                                | mutates                                | returns                | throws                                           | requires                                                                                         |
| ------------------------------------------ | ---------------------------------------------- | ----- | ----------------------------------------------------------------------------- | ------------------------------------------------ | ---------------------------------------- | -------------------------------------- | ---------------------- | ------------------------------------------------ | ------------------------------------------------------------------------------------------------ |
| `%Symbol%` call                            | `sec-symbol-description`                       | 0..=1 | `Arg(0)`: ToString                                                            | —                                                | —                                        | —                                      | `Prim(Symbol)`         | OnCoercion                                       | `%Symbol%`                                                                                       |
| `%Symbol.for%`                             | `sec-symbol.for`                               | 1     | `Arg(0)`: ToString                                                            | —                                                | —                                        | — (agent-local registry; unobservable) | `Prim(Symbol)`         | OnCoercion                                       | `%Symbol%`, `%Symbol.for%`                                                                       |
| `%Set%` construct, no iterable             | `sec-set-iterable`                             | 0     | —                                                                             | —                                                | `(Set, One)`                             | —                                      | `Fresh`                | Never                                            | `%Set%`                                                                                          |
| `%Set%` construct, iterable                | `sec-set-iterable`                             | 1     | `Arg(0)`: Iterate; `Elements(Arg(0))`: none (SameValueZero)                   | — (via Iterate)                                  | `(Set, One)`                             | `Fresh`: Contents                      | `Fresh`                | OnCoercion                                       | `%Set%`, `%Set.prototype.add%`, `%Array.prototype[@@iterator]%`, `%ArrayIteratorPrototype%.next` |
| `%Map%` construct, iterable                | `sec-map-iterable`                             | 1     | `Arg(0)`: Iterate; `Entries(Arg(0))`: [[Get]] `0`,`1`                         | —                                                | `(Map, One)`                             | `Fresh`: Contents                      | `Fresh`                | OnCoercion                                       | `%Map%`, `%Map.prototype.set%`, Array iterator slots as above                                    |
| `%WeakMap%`/`%WeakSet%` construct          | `sec-weakmap-iterable`, `sec-weakset-iterable` | 0..=1 | `Arg(0)`: Iterate (when present)                                              | —                                                | `(WeakMap, One)`                         | `Fresh`: Contents                      | `Fresh`                | OnCoercion; Always(TypeError) for primitive keys | `%WeakMap%`, `%WeakMap.prototype.set%`, Array iterator slots                                     |
| `%Object.freeze%`                          | `sec-object.freeze`                            | 1     | `Arg(0)`: [[OwnPropertyKeys]], [[DefineOwnProperty]]                          | —                                                | —                                        | `Arg(0)`: Integrity                    | `Arg(0)`               | OnCoercion                                       | `%Object%`, `%Object.freeze%`                                                                    |
| `%Object.defineProperty%`                  | `sec-object.defineproperty`                    | 3     | `Arg(1)`: ToPropertyKey; `Arg(2)`: [[Get]] of descriptor fields               | —                                                | —                                        | `Arg(0)`: Props(key)                   | `Arg(0)`               | OnCoercion                                       | `%Object%`, `%Object.defineProperty%`                                                            |
| `%Object.defineProperties%`                | `sec-object.defineproperties`                  | 2     | `Arg(1)`: [[OwnPropertyKeys]], [[Get]] per key and descriptor field           | —                                                | —                                        | `Arg(0)`: Props(keys of `Arg(1)`)      | `Arg(0)`               | OnCoercion                                       | `%Object%`, `%Object.defineProperties%`                                                          |
| `%Object.entries%`                         | `sec-object.entries`                           | 1     | `Arg(0)`: [[OwnPropertyKeys]], [[Get]] per key                                | —                                                | `(Array, One)` of fresh pairs            | —                                      | `Fresh`                | OnCoercion                                       | `%Object%`, `%Object.entries%`                                                                   |
| `%Object.fromEntries%`                     | `sec-object.fromentries`                       | 1     | `Arg(0)`: Iterate; `Entries(Arg(0))`: [[Get]] `0`,`1`, ToPropertyKey on `[0]` | —                                                | `(Object, One)`                          | `Fresh`: Props                         | `Fresh`                | OnCoercion                                       | `%Object%`, `%Object.fromEntries%`, Array iterator slots                                         |
| `%Array.prototype.push%`                   | `sec-array.prototype.push`                     | 0..   | `This`: [[Get]] `length`, [[Set]] indices                                     | —                                                | —                                        | `This`: Contents, Props(`length`)      | `Prim(Number)`         | OnCoercion                                       | `%Array.prototype.push%`                                                                         |
| array spread `[...x]`                      | `sec-runtime-semantics-arrayaccumulation`      | —     | `x`: Iterate                                                                  | —                                                | (part of enclosing literal)              | —                                      | —                      | OnCoercion                                       | `%Array.prototype[@@iterator]%`, `%ArrayIteratorPrototype%.next` when `x` is an `Alloc(Array)`   |
| `%Function.prototype.call%`                | `sec-function.prototype.call`                  | 0..   | —                                                                             | `This`                                           | —                                        | —                                      | result of `This`       | as callee                                        | `%Function.prototype.call%`                                                                      |
| `%Promise.resolve%`                        | `sec-promise.resolve`                          | 0..=1 | `Arg(0)`: [[Get]] `then` when object; `This`: [[Get]] `constructor`           | `then` of `Arg(0)` if thenable, deferred via job | `(Promise, One)`                         | —                                      | `Fresh`                | OnCoercion                                       | `%Promise%`, `%Promise.resolve%`; host job hooks (`reentry_free_operations` axiom, § 4)          |
| `%String.raw%` tag                         | `sec-string.raw`, `sec-gettemplateobject`      | —     | each substitution: ToString                                                   | —                                                | — (template object cached by the engine) | —                                      | `Prim(String)`         | OnCoercion                                       | `%String%`, `%String.raw%`                                                                       |
| `%Math.round%` family                      | `sec-math.round` …                             | 1     | `Arg(0)`: ToNumber                                                            | —                                                | —                                        | —                                      | `Prim(Number)`         | OnCoercion                                       | `%Math%`, `%Math.round%` …                                                                       |
| `%Array.prototype.forEach%`/`map`/`filter` | `sec-array.prototype.foreach` …                | 1..=2 | `This`: [[Get]] `length`, indices                                             | `Arg(0)` per element                             | `map`/`filter`: `(Array, One)`           | —                                      | `Fresh` or `undefined` | as callback                                      | `%Array.prototype.forEach%` …, `%Array%` (species)                                               |
| `%FinalizationRegistry%` construct         | `sec-finalization-registry-constructor`        | 1     | —                                                                             | — (retains `Arg(0)`)                             | `(Registry, One)`                        | `Fresh`: Props(callback)               | `Fresh`                | Always(TypeError) if not callable                | `%FinalizationRegistry%`                                                                         |

Rows reproduce today's conclusions as consequences, not as special cases:
`Symbol.for("x")` is pure because `coerces` resolves to nothing on a
`Prim(Const)` and `mutates` is empty (purity_soundness § Source-selected
native-operation inputs); `new Set([1,2])` is pure because `Iterate` on a
`Clean` dense `Alloc(Array)` of `Prim` resolves to nothing and the required
slots are intact; `Object.freeze(fresh)` keeps its callbacks because `returns
= Arg(0)` and `mutates` only touches `Integrity`; `push` invalidates the
array's dense shape because `mutates` names `Contents`.

### 3.3 Integrity flags from the census

Landed in step 2: `facts/abstract_domain/integrity.rs`, SPEC § Native
integrity inputs and inference § Shared inference. `%Object.prototype%[key]`
keeps its property-specific census rather than inheriting the `Object` family
(the deviation is stated at `IntegrityFlags::object_prototype_property`).

## 4. Author assertions as axioms

Every `AnalysisAssumptions` field is one of six axiom forms over the same
domain. The interpreter never special-cases an assumption; it reads an axiom
where it would otherwise read a census flag, a value, or a callable summary.

| Axiom form                       | Meaning                                                                                      | `AnalysisAssumptions` fields                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| -------------------------------- | -------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `IntrinsicIntact(slots, scope)`  | `integrity(slot) = AuthorAsserted` at the selected site(s)                                   | `native_operation_inputs` (`symbol`, `symbol_for`, `promise_resolve`, `set_from_primitive_array`, `set_from_set_spread`, `object_own_data_function_read`, `array_spread_from_primitive_arrays`), `native_call_identity_inputs` (`object_freeze`, `set_empty_private_field`), `non_reentrant_global_membership` (host `[[HasProperty]]` on a root), `retained_record_factories` (prototype absence / own-data writes), `host_behavior` (`reentry_free_operations`: host job hooks for `%Promise.resolve%` and `AsyncLocalStorage`; `reentry_free_builtin_modules`: builtin module leaves)                          |
| `ValueIs(binding, Value)`        | Seeds `Env(binding)` with an abstract value the source cannot prove                          | `captured_value_inputs` (`Alloc(site, Object)` with `proto = Unknown`, `escape = Published`, not an intrinsic)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `CallableSummary(callable, Row)` | A § 3.1 row for a non-native callable; the interpreter uses it instead of expanding the body | `declared_pure`, `declared_pure_new`, `declared_pure_members`, `fluent_bindings`, imported `pure_exports`; `no_reentry_exports`, `no_reentry_members`, `no_reentry_record_members`, `no_reentry_callback_exports`, `no_reentry_statement_calls`; `inline_callback_factories`, `callback_factory_members`, `retained_callback_factories`, `retained_callback_factory_members`, `retained_callback_constructors`; `descriptor_copy_factory` providers; `namespace_independent_members`; `known_effects` (decorate helpers); `no_sync_callback_members`; `argument_inputs` (one parameter's `mutates`/escape column) |
| `CellUnobserved(cell, until)`    | A write to `cell` is not read by any init-time reader before `until`                         | `deferred_global_publications`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `EffectsDisjoint(unitA, unitB)`  | The two source units' effect sets commute; the gate drops the `Sequenced` edge between them  | `commuting_statements` (sibling PR; not yet landed)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `SummaryTrusted(unit)`           | The syntactic summary stands in for the interpreter's for this unit                          | `declared_pure_initializers`, `trusted_dataflow_summaries` (chunk-wide)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |

The row form for author summaries is the same struct as a builtin row with
`cite` replaced by the spec location and `audited_source_sha256`. A
`no_reentry_*` contract is a row with `invokes = []` and `returns = Unknown`;
a `callback_factory_*` contract is `invokes = []`, `returns = Fresh` with
`Props(callback) ← Arg(i)`; `pure` is `invokes = [] ∧ mutates = [] ∧ coerces =
[]`. The current per-contract admission checks (argument shapes, arity,
spread rejection) become the row's `arity` and `coerces` columns evaluated
against the call's abstract arguments. Proof dependencies stay typed:
consuming an axiom records its `ProofDependencies` reference exactly as
`context.assume` does today.

## 5. Migration order

Each step is one PR or a short native GitHub stack. A step lands only when
the rule's contract tests pass unchanged on the interpreter path and the
bespoke rule is deleted in the same change. Every step reruns the § 6
measurements.

Canonical replays (hours at `-c opt`, both corpora) are required only for the
steps that change what the gates can prove: 2, 5a–5d and 6. Steps 4, 7
and 8 are covered by the public controls and the e2e suite.

Pipeline order: `facts/analyze.rs::analyze_chunk_with_policy_and_sync` runs
purity (`ChunkCodeGraph`) → heap-content effects → the interpreter
(`annotate_synchronous_calls`), and the interpreter consumes the purity graph
and statement facts. The vendor-prune path (`analyze_vendor_prune_chunk`)
never runs the interpreter. A step that derives from the interpreter a fact
purity or heap-content effects consume lands after 4c and 6, or owns that
reorder and measures its cost.

### Step 0 — baseline

Done 2026-10-29: <../perf/abstract_interpreter_step0_baseline.md> (public
controls, both canonical inputs on the pinned and the `devel` engine). The
canonical inputs carried one `native_receiver_certificates` site in total (a
Tana vendor write; none on Claude). Step 2 deleted #17's path-sensitive
receiver rule with no replacement axiom: other census writes already mutate
the global object on both inputs, so the site's discharge changed no edge,
atomic group or loss there.

### Step 4 — purity as empty effect set

- `Purity::Pure ⇔` the statement's effect set has no `Write` to an observable
  cell, no `Invoke`, no `Havoc`. `PurityRule` reasons become projections of
  the offending effect (`Coerce` → `CoercingOperator`, `Havoc` → `UnknownCall`,
  …) so `dependency_graph.json` wire shape is unchanged. PlainData (#25) is
  `escape = Private ∧ taint = Clean ∧ props ⊆ Prim`; `native_rows.rs`'s
  syntactic operand resolution becomes the `Coerce` resolution rule. Delete
  `plain_data.rs`, `ts_enum_iife.rs` (the IIFE is interpreted), most of
  `classifier.rs` (including the namespace-facade shape and the
  result-primitive / safe-key predicates it took over from the deleted
  `builtin_calls.rs`); keep `purity_type.rs` as the projection and
  `effect_inference.rs` as the cross-module worklist over summaries.
  Step 3's `collect_binding_prims` (now in `primitive.rs`) becomes the
  interpreter's `Prim` binding values, and `heap.rs`'s name-based plain-array
  closure becomes `Alloc(Array)` with `Prim` elements.
- Stack: 4a whitelists → rows (landed); 4b PlainData → heap state (landed:
  `plain_data.rs` deleted, scan moved to `heap.rs` with per-allocation
  state, still before purity); 4c classifier → projection, which also moves
  the heap state into the interpreter once purity consumes its effects.
- Tests: `purity`, `object_plain_data_calls`, `proxy_constructor_purity`,
  `initializer_purity`, `export_default_purity`, `pure_members`,
  `cross_module_purity_scale`, `dependency_graph_purity_reason`,
  `impure_call_reason_growth`, `plain_data_escape_scale`,
  `soundness_plain_data_escape`, `repeated_var_purity`,
  `purity_hint_diagnostics`; unit `purity/classifier_tests.rs` and
  `graph_purity_tests.rs` are rewritten against effect sets or deleted where
  they pin table membership.
- SPEC/docs: purity_soundness.md shrinks to § Admission rule (now "how to add a
  row"), § Declared purity and the redundant-hint section; per-operation
  sections delete with their rules.
- Soundness: the whitelist contract "fires no user code on any argument type"
  is `coerces = [] ∧ invokes = []`; the primitive-arg and plain-data gates are
  the `Coerce` resolution rule. A row admitting more than its old table entry
  is the only way to weaken, so review is row-by-row.
- Risk: high in volume, low in semantics. The classifier is 1,194 lines of
  cases; the e2e suite, not the unit tests, is the contract.

### Step 5 — native-operation and callable certificates

- 5a collections: `set_iterables.rs`, `array_spreads.rs` → derived from
  `Iterate` resolution on `Clean` dense arrays; the interval co-location check
  stays in `graph/build.rs`. Tests `set_iterable_reentry`,
  `stable_array_spread`, `native_operation_input`. Depends on 4b, 4c and 6:
  purity and heap-content effects consume these certificates before the
  interpreter runs (§ 5 pipeline order); 4b's `escape`/`taint` state is
  whole-chunk and has no `dense`; and a flow-insensitive
  derivation from `ChunkValueFlow` loses per-prefix precision, since
  `array_spreads.rs` accepts producers mutated or escaping after the spread.
- 5b local construction: `local_class_construction.rs`, `fresh_receiver.rs`,
  `fresh_record.rs` → interpreting the constructor/factory body. Tests
  `local_class_construction`, `local_record_factory_contract`,
  `fresh_returned_record_destructuring`. Depends on 4b, 4c and 6, like 5a:
  purity reads `LocalClassConstructionProof` (`new` admission in
  `classifier.rs`, `cross_module_purity.rs`), the `FreshRecordProof` that
  `ChunkCodeGraph` builds, and value flow's `fresh_returned_record_keys`
  (destructuring admission); heap-content effects read `LocalClassHeapProof`.
  All run before the interpreter, and on the vendor-prune path without it.
  The derived fact, `Alloc(One)` with known props and `escape = Private`, is
  heap state 4b only has for chunk-top initializers, without `props`; the
  interpreter's per-allocation state arrives with 4c and 6. `fresh_receiver.rs` is read only by the
  interpreter but needs the same state: an enum IIFE adds new keys to its
  `{}` receiver, so setter freedom rests on the receiver's escape and taint,
  not on fields it already owns.
- 5c callable summaries: `callable_inference.rs`, `namespace_wrapper.rs`,
  `SemanticKey::{StableFunction, StableProvider, NamespaceWrapper,
ArgumentNoEscape, DescriptorCopy*}` → the
  interpreter summarizes the exported body once; imported providers carry the
  summary as `Foreign(summary)` through `imported_callables`. Tests
  `imported_callback_timing`, `retained_callback_factory`,
  `retained_constructor_callback`, `namespace_wrapper`,
  `descriptor_copy_factory`, `argument_input`. Landed:
  `facts/abstract_domain/foreign_call.rs` evaluates exported bodies whose
  whole effect set is allocation and publishes `foreign_call` through
  `imported_callables` (`foreign_call_summary`); a parameterless body
  returning a fresh method/getter literal is the returned-record projection
  (`ReturnedRecord` folded), and one that directly returns a closure over its
  first parameter is the inferred callback factory (`CallbackFactory` folded). The other certificates above fill in where it
  does not apply; folding them, and summarizing returned objects' methods,
  remain.
- 5d own-property projections: `property_inference.rs` → `props` of a `Clean`
  `Alloc(One)` in ECMAScript own-key order. Tests `own_property_names`,
  `own_descriptor_inspection`.
- After 5d `semantic_inference.rs` holds only the axiom entry points and is
  renamed `facts/abstract_domain/axioms.rs`.
- SPEC: §§ Inferred returned-record construction timing, Inferred
  namespace-wrapper timing, Ordinary own-property inspection, and the
  source-derived half of § Audited callback factories become one § Derived
  callable summaries stating the summary schema and the budgets.
- Soundness: a derived summary is the interpreter's effect set over `Top`
  arguments; it is weaker than or equal to each hand-written certificate
  because the certificates were sufficient conditions for the same effect set
  being empty.
- Risk: high. These rules carry the private corpus' current acceptance; a
  precision regression shows up as a red canonical gate. Mitigation: 5a–5d
  land only after the private corpus replays accept the same assignments
  (debug/runtime_transfers.md's paired-measurement protocol).

### Step 6 — heap frontier from effect sets

- `array_content_effects.rs` and `HeapContentEffects` are deleted;
  `emit_heap_content_order` reads `Read/Write(Contents(site))`, `Alloc(site)`
  with `mult = One`, and `Havoc` directly from statement summaries.
  `OneShotEagerAllocations` is the `One` multiplicity; `CellOrderBarrier`
  stays.
- Tests: `local_array_push_order`, `stable_array_spread`,
  `at_init_s_chain_dataflow`, `graph_provenance`, `s_chain_dataflow_soundness`,
  `lemma_five_side_effect_order`; unit `array_heap_frontier_tests` stays.
- SPEC/docs: design § Emission modes' array-content paragraphs become a
  reference to the `Contents` cell.
- Soundness: the frontier algorithm is unchanged; only its input cells change
  source. An allocation the interpreter cannot see is `Havoc`, which is the
  existing barrier.
- Risk: medium. Edge counts must not grow (§ 6).

### Step 7 — statement cell summaries and taint

- `StatementEffectSummary`, `dataflow_summarizable`, `cell_writes_summarizable`
  and `global_object.rs` taint become projections of the interpreter's effect
  set (`Binding`/`GlobalProp` cells; a bail shape is a `Havoc`). `collector.rs`
  keeps only syntax collection (declarations, lazy boundaries, TLA).
- Tests: `s_chain_dataflow_soundness`, `soundness_at_init_value_flow`,
  `at_init_s_chain_dataflow`, `repeated_var_loop`, `hoisted_var_declaration`.
- Soundness: every current bail shape produces `Havoc` under the domain
  (`with` and direct `eval` are already dynamic-scope; `Function(...)` is an
  `Invoke(Unknown)`; computed global keys are `UnknownKey` on the global
  allocation).
- Risk: medium. `trusted_dataflow_summaries` must keep its exact meaning:
  `SummaryTrusted` replaces `Havoc` from unproven calls by the syntactic cells
  only, never `Havoc` from `UnknownKey`.

### Step 8 — promotion and fallback

- Remaining: `safe_plain_array_at_init_fallback_sources` becomes `Clean`
  array resolution after 4c. Its callbacks must be evaluated against the
  elements' state (`props ⊆ Prim`), not just a `Clean` array: today it admits
  a root only when the callbacks use elements by identity, strict equality or
  truthiness, or when 4b's heap state proves the elements primitive. The rule
  lost the two Tana sites in `index-DI2GynTv.js` that need element state:
  `lV` (tool objects read through `n.name` and `n.systemNodeId`, lines
  69844–69850) and `eet` (command objects, line 109391).
- `promote_at_init_calls` already consumes complete synchronous summaries and
  takes the syntactic reach (`at_init_unresolved_sources`, closed by
  `source_unit_read_closure.rs`) only for incomplete ones. An incomplete
  summary has no `Havoc(reach)` to hand over: the interpreter did not evaluate
  the unsupported operation, so the syntactic reach stays its reach.
- Tests: the #5 set. Removing the rule outright grew Tana's `eager_use` edges
  by 1,353 (Claude unchanged).

### Step 9 — `EffectsDisjoint` and gate consumption

- Gate consumption of `commuting_statements` lands before this plan's steps,
  against the current edge reasons: it removes a `Sequenced` edge only when at
  least one endpoint's reason is opaque, and rejects an entry contradicting a
  derived conflict. This step ports that check onto effect sets: the axiom
  removes the `Sequenced` edge when the summarized effect sets are disjoint and
  the author asserts the unsummarized remainder is. The gate reads effect sets,
  not purity bits, for this decision.
- Tests: new controls — two units with a shared `Contents` cell must keep
  their edge despite the assertion.

### Step 10 — graduation

- `docs/inference.md` describes the interpreter, the domain and the table;
  `SPEC.md` § Synchronous analysis limits onward lists axiom forms and the
  summary schema; `purity_soundness.md` is the row-authoring rule. Delete this
  plan.

## 6. Scale and performance

Sizing premise (step 0, `devel` engine 4377a57bae, RBE executor): Tana
3,146,382 edges, 1,121 s wall, 5.13 GiB peak; Claude 7,366,298 edges over 76
graphs, 87 s, 9.62 GiB. The pinned engine ecb4a734 does the same inputs in
2,163,669 edges / 255 s / 3.67 GiB (Tana, accepted) and 6,123,098 edges /
43.5 s / 4.40 GiB (Claude). The older 106 s / 3.75 GiB figure in
<../perf/large_web_corpus_baseline.md> is a different engine and spec and is
not comparable.

### 6.1 Measure before and after every step

Per <../AGENTS.md> § Profiling: `-c opt` binary, pre-staged inputs,
`perf_wrapper.sh`; keep `perf_record_stderr.txt`, `perf_report_flat_symbols.txt`
and the children report. Record:

- Wall, user CPU, peak RSS: three alternating pairs per control. The budget is
  cumulative against the step 0 baseline, not per step: at step 10 neither wall
  nor peak RSS may exceed the baseline on any control or canonical input. A
  single step may regress by at most 10%, and its PR names the later step that
  recovers it.
- Public controls: `opaque_callback_scale`, `value_flow_sharing_scale`,
  `cross_module_purity_scale`, `plain_data_escape_scale`,
  `conflict_diagnostics_scale`, `perf/gen_synth_corpus.py` (10k statements).
- Private canonical inputs: gate verdict, atomic-group count, per-reason edge
  counts in `dependency_graph.json`, `precision_losses` count by reason. On
  `devel` both gates reject today (the pinned engine accepts Tana), so "verdict
  unchanged" is vacuous; the invariant is that edge counts, atomic-group counts
  and losses never grow at any step, and the public e2e suite passes unchanged.
- Flat profiles on the public controls (step 0) are dominated by `memcmp`,
  `Atom::cmp`, `malloc`/`free` and `statement_for_source_unit_ordinal`; the
  `BTreeSet<Id>` / `BTreeSet<RuntimeKey>` clones seen in earlier replay
  prototypes are not in either top 20. Re-profile before optimizing either.

### 6.2 What keeps the interpreter per-statement

The current code already summarizes at these points; the interpreter keeps
every one of them:

- One settled `ExecutionState` per prefix; statement _n+1_ consumes statement
  _n_'s state and never rewrites it (`runtime_inference.rs`; inference
  § Dependency regions).
- Invocation memo per `InvocationSignature` with `read_final` dependencies, no
  parent replay (`interpreter.rs::invocation_memos`; runtime_transfers.md
  § Terminal transfers).
- Budgets `MAX_SYNCHRONOUS_INVOCATION_DEPTH`, `MAX_SYNCHRONOUS_INVOCATIONS`,
  `MAX_SYNCHRONOUS_CALLBACK_POOL` widening to `Havoc` with recorded losses.
- Interned `Captured` contexts and callback sets (`value_model.rs`), batch
  discovery of exports and arguments, bulk sorted effect-set merges
  (debug/2026_10_24_callback_analysis_performance.md).
- Persistent `OrdMap` state so per-prefix snapshots share structure.
- Heap frontier reset at `CellOrderBarrier::HeapFrontier` so barriers stay
  linear in the live frontier (`array_heap_frontier_tests::repeated_opaque_heap_barriers_keep_edges_linear_in_the_live_frontier`).
- `SourceUnitReadClosure` shared SCC summaries for fallback reach.
- Cross-module summaries: one worklist over `(body, abstract args)` facts
  (`effect_inference.rs`, `cross_module_purity.rs`); importers consume
  summaries, never provider bodies.

New widening the domain adds: constants to `Any` after one distinct join;
`One → Many` on repeated birth; argument shapes to `Top` after one distinct
re-summary per body; `UnknownKey` absorbing a property map. Each is a
finite-height guarantee, not a heuristic, so the worklist terminates without
budgets; budgets remain for cost.

### 6.3 Risks

1. Step 5 precision regressions on the private corpus (red canonical gates).
   The hand-written certificates encode exactly the shapes that corpus needed;
   the domain must reproduce each as a consequence. Mitigation: replay-gated
   landing, one certificate family per PR.
2. Heap-state size. Property maps per allocation site across thousands of
   statements, with persistent snapshots, can exceed today's
   `prefix_object_properties`. Mitigation: `escape = Global` drops the map to
   `Top`; measure RSS per step.
3. Loss of #17's reaching-definition precision (§ 2.5). The rule is deleted;
   a receiver it would discharge now keeps its census roots. Once the census no
   longer mutates the global object for other reasons, such a site needs an
   axiom, and `captured_value_inputs` cannot express it yet: it admits only a
   top-level binding initialized by a member read, while the one Tana site is a
   function-local `var` with a fresh initializer and a later global-slot
   reassignment.

## 7. Not in scope

- No cross-version or multi-spec reasoning (design § Mission; AGENTS § Spec
  authoring).
- No runtime execution or runtime checks; the interpreter is static and its
  conclusions are gated the same way (design § Why a static gate, not a runtime
  check).
- No weakening of the soundness contract (AGENTS § Soundness over
  completeness): every step is accepted only when its conclusions are a subset
  of the replaced rule's on the contract tests and the corpus replays. New
  precision is not a goal and is not added.
- No change to the gate algorithm, quotient, emission or selector resolution.
- A11 stays an external author obligation; the census and the table narrow
  what A11 must cover, they do not discharge it.

## 8. Doc drift found while inventorying

Fix during the step that touches the area; listed so they are not rediscovered.

- `docs/inference.md` opens with "`inference.rs` provides a dependency
  worklist"; the worklist is `inference/iterative.rs`, `inference.rs` only
  defines `Judgment` and re-exports.
