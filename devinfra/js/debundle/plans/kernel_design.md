# Kernel design

The Wave 2 interfaces of <convergence_landing.md>: the five narrow context
traits, the `World` type and its join, the one continuation driver, and the
completion ledger as the only coverage authority. Slices L2a–L2d implement it;
the file is deleted when L2c lands and its contracts graduate to `SPEC.md` and
`docs/design.md`.

Evidence references (`path:line`) point into `facts/sync_effects/` at the
#14552 head `839549c0f4` (tag `debundle-reference-14552`), the reference
implementation being ported. Adapter
abbreviations: **App** `LocalExecutionContext` (`interpreter.rs:2997`, trait
impl `:3755-6504`), **PA** `ProviderExecutionContext`
(`interpreter/provider_activation.rs:22`, impl `:550-1031`), **SP** strict
`ProviderContext` (`strict_provider.rs:74`, impl `:458-962`).

## Traits

The kernel (`kernel/continuation.rs`, `kernel/driver.rs`) owns everything
three adapters would otherwise re-implement: lexical cells and regions,
catch bindings, closure creation, captured-cell stores, array spread,
primitive-array markers, and the TDZ rule. Adapters implement only the five
traits below; nothing else reaches the kernel.

```rust
pub trait BindingContext {
    fn cell_policy(&self) -> CellPolicy;                 // which bindings get World cells
    fn cell_effect(&self, binding: &Id) -> Option<EffectCell>;
    fn module_binding_read(&mut self, binding: &Id, frame: &LocalFrame) -> Option<ModuleRead>;
    fn module_binding_store(&mut self, binding: &Id, selected: &SelectedValue) -> StoreOutcome;
    fn receiver_value(&mut self, frame: &LocalFrame) -> Option<Values>;
    fn named_self_binding(&mut self, binding: &Id, frame: &LocalFrame) -> Option<EffectCoverageFragment>;
    fn module_prefix_receiver(&self, binding: &Id, receivers: &Values) -> bool;
}
pub trait CallContext {
    fn call(&mut self, frame: &mut LocalFrame, request: &LocalCallRequest) -> Family<LocalValueResult>;
    fn read_call_callee(&mut self, ...) -> Option<LocalValueResult>;
    fn imported_member_callee(&mut self, ...) -> Option<(Values, LocalValueResult)>;
    fn function_call_method_callee(&mut self, ...) -> Option<(FunctionCallMethodSelection, EffectCoverageFragment)>;
    fn function_apply_arguments(&mut self, ...) -> Option<FunctionApplyArguments>;
    fn constructor_phase(&mut self, phase: ConstructorPhase);
}
pub trait HeapContext {
    fn world(&self) -> &World;
    fn world_mut(&mut self) -> &mut World;
    fn heap_resolver(&self) -> SourceHeapResolver;
    fn native_iterable(&mut self, use_: NativeIterableUse, operand: &Values) -> Option<NativeIterableReceipt>;
    fn constructor_prefix_is_unexposed(&self, class: ClassDefinitionId) -> bool;
    fn constructor_assignment_absence(&mut self, class: ClassDefinitionId, property: &str) -> Option<ProofDependencies>;
    fn object_spread_copy_support(&mut self, ...) -> bool;
    fn symbolic_field_read(&mut self, receivers: &Values, property: &str) -> Option<LocalValueResult>;
}
pub trait NativeContext {
    fn native_callee(&mut self, ...) -> Option<(NativeCalleeSelection, EffectCoverageFragment)>;
    fn array_method_callee(&mut self, ...) -> Result<Option<(ArrayMethodSelection, EffectCoverageFragment)>, OrderedCallAdmissionRefusal>;
    fn native_field_read(&mut self, ...) -> Option<LocalValueResult>;
    fn keyed_collection_new(&mut self, ...) -> Option<KeyedCollectionOperationResult>;
    fn set_construction(&mut self, ...) -> Option<LocalValueResult>;
    fn plan(&mut self, ...) -> Option<CheckedNativePlan>;
    fn commit(&mut self, commit: NativeOperationCommit);
}
pub trait ControlContext {
    fn unproved(&mut self, frame: &mut LocalFrame, what: Unproved) -> Disposition;
    fn publish_throw(&mut self, values: &Values);
    fn skipped_region(&mut self, ...) -> bool;
    fn operation_support_receipt(&self, ...) -> Option<OperationSupportReceipt>;
    fn record_operation_support(&mut self, receipt: OperationSupportReceipt);
    fn fork_policy(&self) -> ForkPolicy;                 // Refuse | Explore
}
```

`Unproved` enumerates field read, field write, operator, optional call,
control flow, binding targets and refused object spread; `Disposition` is
`Summarize | Refuse`. There is no effect sink: the kernel puts property reads
and writes into the ledger it returns (§ Observations are returned), and the
provider's scope filter applies where its ledger is adopted (the policy behind
`strict_provider.rs:545-546`).

### Observations are returned

Two kinds of state cross the kernel boundary and they move differently:

- **Stores** (heap, lexical cells, object surface, descriptors) are mutated in
  place through `&mut World`. A missed store write shows up as a wrong value.
- **Observations** (effects, completion, proof dependencies, inference uses,
  native integrity and prototype checks, operation-support receipts) are only
  ever **returned** and composed, never recorded into a context. A missed
  observation would silently claim purity, so the shape makes it a type error:
  a branch with nothing to return does not compile, and a dropped value is a
  `#[must_use]` error. `EffectCoverageFragment`, `TransferLedger`,
  `Completion` and `Joined` carry `#[must_use]`.

Consequences: `ControlContext::{record_property_read, record_property_write,
publish_throw}` do not exist; `TransferLedger.dependencies` is kept and
replaces the `proof_dependencies: RefCell` (L2a dropped it as reader-less; the
`RefCell` is the reader). The nine `RefCell` ledgers in `ExecutionState`
(`interpreter.rs`, `native_*_checks`, `*_uses`, `proof_dependencies`,
`record_factory_prototype_premises`) become returned values that ride the
ledger upward; they are not relocated as a struct of `RefCell`s. A join
consumes the whole family of alternatives as one value (`Family` or `Vec`), so
an alternative cannot be left out of a join by omission.

`OwnDataContext` collapses into `HeapContext::world` plus `heap_resolver`;
every other method is an inherent `World` query. `AllocationContext`,
`ArrayContext`, `DescriptorContext` and `NativeOperationContext` are
deleted; `allocation_transfer`, `array_transfer`, `descriptor_transfer` and
`native_operation` take `&mut (impl HeapContext + ControlContext)` or
`NativeContext`.

### Kernel rules that replace adapter choices

- **Closure value**: one `AbstractValue::Closure(HeapAddress,
LexicalCaptures)`. Provider identity is the `HeapModuleScope` inside the
  address. `ProviderClosure`, `InstantiatedProviderClosure` and SP's
  `closure_captures` side map are deleted.
- **Array spread**: the kernel requires the canonical `IterateSpread` source
  operation site and a `native_iterable` receipt. `PrimitiveOnly` yields are
  accepted as an array of unknown primitive contents with no user-code
  invocation; a provider summary needing exact slots refuses at sealing.
- **TDZ**: reading an uninitialized cell is `Completion::Abrupt` with a
  ReferenceError payload plus the recorded binding read.
- **Regions**: the kernel allocates and restores cells; the only refusal is
  `has_unscoped_lexical_bindings`.
- **Forks**: a `Step` fork is refused by scalar consumers and explored by the
  two adapters, per `fork_policy`.

### Inventory

Reference `LocalContext` (`local_transfer.rs:701-1072`, 59 methods) mapped to
the target. **Kernel** = semantics live in the kernel; **World** = derivable
from `World` or the index, no trait method; **Adapter** = stays policy, named
trait; **Delete**.

| Reference method                  | App     | PA      | SP      | Target                                             |
| --------------------------------- | ------- | ------- | ------- | -------------------------------------------------- |
| `diagnostic` (cfg test)           | 3757    | –       | –       | Delete                                             |
| `constructor_instance_address`    | default | default | default | World                                              |
| `receiver_value`                  | 3761    | default | default | Adapter: Binding                                   |
| `constructor_phase`               | 3767    | –       | –       | Adapter: Call                                      |
| `constructor_prefix_is_unexposed` | 3777    | –       | –       | Adapter: Heap                                      |
| `constructor_assignment_absence`  | 3781    | –       | –       | Adapter: Heap                                      |
| `native_callee`                   | 4056    | –       | 513     | Adapter: Native                                    |
| `imported_member_callee`          | 4256    | –       | –       | Adapter: Call                                      |
| `array_method_callee`             | 4298    | –       | –       | Adapter: Native                                    |
| `function_call_method_callee`     | 4090    | 551     | –       | Adapter: Call                                      |
| `function_apply_arguments`        | 4171    | 594     | –       | Adapter: Call                                      |
| `rest_parameter_allocation`       | 4219    | 633     | –       | World (scope)                                      |
| `array_creation_started`          | 4418    | 649     | –       | Kernel                                             |
| `array_created`                   | 4432    | 664     | –       | Kernel                                             |
| `array_allocation_is_fresh`       | 4438    | default | default | Kernel                                             |
| `array_spread`                    | 3886    | 671     | default | Kernel; adapter `native_iterable`                  |
| `primitive_array_created`         | 3934    | default | –       | Kernel                                             |
| `instantiate_parameter_bindings`  | 4490    | 718     | –       | Kernel; adapter `cell_policy`                      |
| `named_self_binding`              | 4522    | –       | –       | Adapter: Binding                                   |
| `declarative_binding_operation`   | 4587    | 791     | 575     | Kernel; adapter `cell_effect`                      |
| `instantiate_bindings`            | 4667    | 725     | –       | Kernel                                             |
| `enter_region_bindings`           | 4747    | 732     | default | Kernel                                             |
| `leave_region_bindings`           | 4818    | 747     | –       | Kernel                                             |
| `local_binding_stored`            | 4868    | 782     | –       | Kernel                                             |
| `read_binding`                    | 5303    | 811     | 718     | Kernel (cells, TDZ); adapter `module_binding_read` |
| `read_call_callee`                | 5550    | default | default | Adapter: Call                                      |
| `module_prefix_receiver`          | 4979    | default | default | Adapter: Binding                                   |
| `selection_prerequisites`         | 4982    | 715     | default | World (`settled_dependencies`)                     |
| `closure`                         | 4985    | 872     | 587     | Kernel                                             |
| `assign_capture`                  | 5071    | 847     | 672     | Kernel (cells); adapter `module_binding_store`     |
| `call`                            | 5585    | 934     | 765     | Adapter: Call                                      |
| `literal_layout`                  | 5992    | 949     | 888     | World (index)                                      |
| `exact_object_spread_copy`        | 5995    | –       | –       | World (index)                                      |
| `object_spread_copy_support`      | 6004    | –       | –       | Adapter: Heap                                      |
| `refused_object_spread`           | 6035    | default | default | Adapter: Control (`Unproved`)                      |
| `incomplete_object_spread`        | 6063    | default | default | Kernel                                             |
| `object_spread_dependencies`      | 6080    | default | default | World                                              |
| `literal_property_kind`           | 5982    | 952     | 891     | World (index)                                      |
| `heap` (`LiteralHeap`)            | 6083    | 955     | 898     | Delete (L1b)                                       |
| `record_property_read`            | 6086    | 958     | 545     | Returned in the ledger (§ Observations)            |
| `record_property_write`           | 6112    | 963     | 546     | Returned in the ledger (§ Observations)            |
| `create_class_data_property`      | 6117    | –       | –       | Kernel                                             |
| `operation_support_receipt`       | 6091    | 968     | 547     | Adapter: Control                                   |
| `record_operation_support`        | 6106    | default | default | Adapter: Control                                   |
| `native_field_read`               | 6132    | –       | –       | Adapter: Native                                    |
| `symbolic_field_read`             | –       | –       | 459     | Adapter: Heap                                      |
| `unproved_field_read`             | 6219    | 981     | 901     | Adapter: Control (`Unproved`)                      |
| `unproved_binding_targets`        | 6318    | –       | –       | Adapter: Control (`Unproved`)                      |
| `unproved_field_write`            | 6355    | 993     | 914     | Adapter: Control (`Unproved`)                      |
| `unproved_operator`               | 6385    | 1002    | default | Adapter: Control (`Unproved`)                      |
| `unproved_optional_call`          | 6398    | default | default | Adapter: Control (`Unproved`)                      |
| `publish_throw`                   | 6411    | 1028    | 924     | Adapter: Control                                   |
| `skipped_region`                  | 6418    | 1008    | 928     | Adapter: Control                                   |
| `instantiate_catch_binding`       | 6451    | 761     | –       | Kernel                                             |
| `leave_catch_binding`             | 6479    | 768     | –       | Kernel                                             |
| `unproved_control_flow`           | 6487    | 1005    | default | Adapter: Control (`Unproved`)                      |
| `abandon_remainder`               | default | –       | –       | Kernel                                             |
| `keyed_collection_new`            | 3813    | –       | 942     | Adapter: Native                                    |
| `set_construction`                | 3944    | –       | –       | Adapter: Native                                    |

`OwnDataContext` (`own_data_transfer.rs:19-93`, six impls) and
`AllocationContext` (`allocation_transfer.rs:795-911`, twelve methods
duplicating the table above) have no rows: they are deleted whole.

## World

```rust
pub struct World {
    pub scope: HeapModuleScope,        // address admission and effect-sink isolation
    pub cells: LexicalCells,           // arena, ids never reused
    pub active_cells: OrdMap<Id, LexicalCellId>,
    pub module: ModuleBindings,        // prefix_values, prefix_callable_selections, initialized_bindings, top_env, top_captures
    pub surface: ObjectSurfaceStore,
    pub descriptors: DescriptorHeap,   // own_descriptor_maps, descriptor_copy_results, prefix_private_properties
    pub arrays: DenseArrays,           // dense_arrays, primitive_arrays, unknown_array_contents, accumulated_values, array_intrinsic_mutated
    pub native: NativeRetained,        // prefix_native_retained_references + may_be_unknown, tainted_native_sets, tainted_functions, revoked_audited_results, uncertain_native_execution
    pub classes: ClassState,           // class_superclasses, tainted_classes, escaped_class_capabilities, first_class_members_publications, checked_constructor_inputs, private_constructor_result_owners, active_construction
    pub publication: Publication,      // global_escaped_callbacks, exported_runtime_values, first_callback_publication_owners, returned_callback_owners, accepted_deferred_global_writes, frozen_namespace_calls, parameter_initialized_objects
    pub reachability: ReachabilityState,
    pub fallback: FallbackRetention,   // fallback_retained_values, fallback_retained_cells
    pub revision: Revision,            // prefix, reachability
}
```

One `World` serves both adapters: the application's `SemanticWorldData`
(`interpreter.rs:1073-1119`) and the strict provider's six private copies
(`strict_provider.rs:90-96`) are the same type with a different `scope`.

**Copy-on-write.** `World` lives behind one `Arc` with `make_mut`; maps are
`imbl::OrdMap`; `LexicalCells` keeps its shared id allocator so sibling forks
never collide. A fork at a `Step` branch or a `Family::fork` clones the handle,
not the contents.

**Identity.** `HeapAddress` and `LexicalCellId` are value identities. No
`Deref` from `World` or any adapter to a field store (today
`interpreter.rs:1121-1133`, `strict_provider.rs:120-132`); callers name
`world.surface`. `Arc::ptr_eq` is a fast path inside `same_contents` only;
derived `PartialEq` is the answer. Interner ids (`CaptureInterner`,
`CaptureIdentityArena`) are `ExecutionState`, compared only within one
`Executor` run; `same_run_arenas` is a run-id check.

### Join

```rust
pub struct Joined { pub world: World, pub widened: bool, pub refused: Option<JoinRefusal> }
pub enum JoinRefusal { EmptySet, DivergentActiveCells, DivergentScope }
impl World { pub fn join(worlds: &[&World]) -> Joined }
```

- Strict fast path: all worlds `same_contents` → first world, `widened:
false`.
- Conservative fallback: each field type implements `Join`; the per-field
  joins now in `interpreter/conservative_world_join.rs:426-1551` are their
  bodies. `widened` is the OR of field widenings; `reachability.incomplete`
  ORs.
- Refusal never panics. `refused` is set for divergent `active_cells` or
  scopes; the returned world is the pointwise join of the remaining fields
  with `reachability.incomplete = true`. The caller in `alternatives/` maps
  `refused` to the remainder fallback (`RequiresRemainderFallback`), never to
  an up-front fork refusal: branches run so that their divergence is known.
  This removes the panics at `interpreter/transfer_alternatives.rs:657` and
  `:688`.

Of the joins at the reference head, `conservative_world_join` and its
helpers, `transfer_alternatives::join_execution_states` (`:1168`) with its
`join_native_retained_reference_maps` and `join_callback_fallback`, and
`object_surface::join_attributes` (`:1125`) fold into `World::join` and one
`ExecutionState::join` in `alternatives/`. Three stay because they join
other lattices: `EffectCoverageFragment::join_alternatives`
(`effect_coverage.rs:1913`, covered iff every path covers),
`transfer_alternatives::join_outcomes` (`:807`, the `Outcome` join with
`join_own_property_store_receipts`), and the receiver-flow join in
`execution_index.rs:4765`.

## Completion

`TransferLedger` (`completion.rs:255`) is the only coverage authority.
`Outcome.complete` (`interpreter.rs:238`) is deleted; its readers become:

| Reader kind                                                                                                                                             | Reads                                                                                                                       |
| ------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| "may I certify / seal / retire a barrier" (`interpreter.rs:5743, 6279, 9488, 10113, 15442-15451, 15704-15736, 17137, 19518, 20240, 21976, 24318-24884`) | `ledger.has_closed_effects() && ledger.effects.precision_losses.is_empty()`; `unknown_call` is a `Havoc` cell in the ledger |
| child completeness passed upward (`:2564, 2786, 2912, 20925, 22240`; `source_continuation.rs:447-481`; `provider_activation.rs:151, 262, 347`)          | `child.ledger.closed_effect_dependencies()`; parents already compose the child ledger                                       |
| setters (`:624, 766, 3613, 5416, 5950, 6489, 15684, 19286, 20177`; `transfer_alternatives.rs:646-692`; `provider_transfer.rs:92`)                       | deleted; each site composes `EffectCoverageFragment::default()` where it does not already                                   |
| `join_outcomes` AND (`transfer_alternatives.rs:873`)                                                                                                    | `EffectCoverageFragment::join_alternatives` yields `Unknown` if any path is                                                 |
| export (`interpreter.rs:24375`, `transfer_alternatives.rs:974`)                                                                                         | the one writer of `SynchronousCalls.complete`, computed as `ledger.has_closed_effects()`                                    |

`SynchronousCalls.complete` (`statement_facts.rs:286`) stays as a projection
for `binding_effect_coverage.authority` and the saved codec; the codec change
rides L2c behind the engine hash.

Precondition for the deletion (first commit of L2c): a test that a refused
body leaves the ledger open, and a line-by-line check that `finish_refused`
composes an unknown fragment, since `adopt_local_transfer(completed = false)`
(`interpreter.rs:~600`) composes only the prefix ledger.

## Driver

```rust
pub enum ForkPolicy { Refuse, Explore }

pub fn drive<C: BindingContext + CallContext + HeapContext + NativeContext + ControlContext>(
    ctx: &mut C, body: &LocalBody, mut continuation: LocalContinuation,
) -> Family<(LocalFrame, LocalExecutionAttempt)> {
    loop {
        match continuation.poll(ctx, body) {
            LocalSuspension::Call(request) => match ctx.call(&mut continuation.frame, &request) {
                Family::One(result) => continuation.resume_call(ctx, result),
                family => return family.bind(|leaf| {
                    let mut caller = continuation.clone();
                    caller.resume_call(ctx, leaf);
                    drive(ctx, body, caller)
                }),
            },
            LocalSuspension::Fork(children) => match ctx.fork_policy() {
                ForkPolicy::Refuse => continuation.refuse_branching(),
                ForkPolicy::Explore => return Family::fork(children).bind(|child| drive(ctx, body, child)),
            },
            LocalSuspension::Complete(attempt) => return Family::One((continuation.frame, attempt)),
        }
    }
}
```

`drive` replaces the five loops at the reference head
(`local_transfer.rs:3600`, `local_transfer/continuation.rs:525`,
`strict_provider.rs:1239`, `interpreter/provider_activation.rs:165`,
`interpreter/source_continuation.rs:69`). `Family` carries one `World`
snapshot per leaf and is joined by `alternatives/` at the resume point.
Scalar consumers (the retained-closure proof query, allocation-only
expression evaluation) refuse forks; the application and provider adapters
explore them. "Any refused leaf refuses the family" is the caller's
`Family::all_completed()` check, not a driver variant.

**Provider execution** is one `adapters/provider.rs` adapter. Its `World` is
the caller's fork when the provider body runs inside a caller transfer, or a
fresh `World { scope: provider }` when sealing a summary; `scope` alone
decides address admission, effect-sink isolation and the pooled-region
refusal in `operation_support_receipt`. Summary substitution
(`interpreter/provider_graph.rs:35`) stays as the fast path tried before the
adapter runs. The flat lane `invoke_with_cells` (`interpreter.rs:19539`) is
deleted for bodies with an ordered plan.

## Slices

Each slice exceeds the ~3k-line target of the slice contract and says so.

**L2a — traits, continuation, driver, provider executor** (needs L1a, L1b).
`local_transfer/continuation.rs` → `kernel/continuation.rs`; `LocalFrame`,
`LocalActivation`, `LocalExecutionAttempt`, `LocalCallRequest`,
`RemainderDisposition` → `kernel/frame.rs`; the five traits in
`kernel/context.rs`; `drive` in `kernel/driver.rs`. Each adapter's trait impl
splits into five in place. Deletes `LocalContext`, `AllocationContext` and
the `LocalValueContext` bridge (`local_transfer.rs:1074-1400`), the five
driver loops, `invoke_with_cells` for ordered-plan bodies, the legacy
`ValueExpr::Member` arm for covered reads, `ProviderClosure`, the cfg(test)
`diagnostic`. Tests: `foreign_call_summary`, `local_class_construction`,
`source_continuation_tests.rs` (moves with the continuation), strict-provider
unit tests. About 4.5k moved, 1.5k edited, 1k deleted.

**L2b — `world/` and `alternatives/`** (needs L2a). `SemanticWorldData`,
`LexicalCell*`, `ReachabilityState`, `ObjectSurfaceStore`, `DescriptorHeap`
→ `world/`; `conservative_world_join.rs` → `world/join.rs`;
`transfer_alternatives.rs` → `alternatives/`. SP's private fields become a
`World`. Deletes both `Deref` impls, the duplicate `join_callback_fallback`
and `join_class_member_effects`, the two join panics. Replaces the `RefCell`
ledgers in `ExecutionState` (`interpreter.rs:1796-1807`) with returned
observations per § Observations are returned, restoring
`TransferLedger.dependencies`; deletes the L2a sink methods
`record_property_{read,write}`/`publish_throw` and closes L2a's L2b-gated
tombstones in a stacked second PR. Audits interner ids. Tests:
`source_continuation`, `optional_call_timing`, join unit tests per refusal
variant. About 6k moved, 1.2k edited, 0.4k deleted.

**L2c — application adapter and `complete`** (needs L2b).
`LocalExecutionContext` and helpers → `adapters/application.rs`;
`execute_owner` (`interpreter.rs:23729`) → `roots/{operands,body,assignment,
finalize}.rs`; `execute_call_transfer_inner` (`:20966`) → operands-supplied
mode in `roots/call_operands.rs`, self-evaluating mode in `legacy/` with a
`CLEANUP` naming `evaluate_legacy_compat` (`:18349`) as last caller. Deletes
`Outcome.complete` per § Completion after its precondition test. Each
`roots/` phase is a function over the `World` owners it needs (`&mut
world.publication, &world.module, ...`, split borrows), not `&mut self` on
`Executor`; what a phase may mutate is its signature. Each phase returns its
observations (§ Observations are returned). Tests: effect-translation
positives with `swapped_pairs > 0` and Node parity;
`synchronous_precision_report_test` asserts the report projection. About
3.5k moved, 1.8k edited, 0.6k deleted.

**L2d — un-nest the interpreter** (needs L2a; parallel to L2b). Removes
`#[path = "interpreter.rs"]` at `execution_index.rs:16684` and the nested
`#[path]`s at `interpreter.rs:5-158`; `ExecutionIndex` becomes an input
struct; `execution_index/provider_program.rs` → `provider/`. About 1.2k
edited.
