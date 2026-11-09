//! Isolated provider consumer of shared value, heap and effect transfers.

#[path = "strict_provider/private_heap.rs"]
mod private_heap;
use private_heap::{PrivateHeapEvidence, SealedPrivateHeap};

use super::super::allocation_transfer::{
    self, AllocationContext, AllocationJudgment, AllocationValue, ChildTransfer,
    KeyedCollectionOperationResult, LiteralHeap, LiteralLayout, WritablePrefixState,
};
use super::super::descriptor_transfer::{
    self, DescriptorCall, DescriptorContext, DescriptorHeap, OwnDescriptorMapState,
};
use super::super::effect_coverage::{
    EffectCoverageFragment, LocalBodyIdentity, NativeCalleeSelection, OperationStep,
    OperationSupportOperation, OperationSupportReceipt, SourceOperationSite,
};
use super::super::local_operations::{
    DeferredCallableCreation, LocalBinding, LocalBody, LocalCallee,
};
use super::super::local_transfer::{
    self, EvaluatedCallee, KnownThrow, LocalContext, LocalContinuation, LocalExecutionAttempt,
    LocalFrame, LocalNormal, LocalSuspension, LocalTransferResult, LocalValueResult, SelectedValue,
};
use super::super::native_operation::{
    self, CheckedNativePlan, NativeOperationCommit, NativeOperationContext,
    NativeRetainedReferences,
};
use super::super::object_surface::ObjectSurfaceStore;
use super::super::own_data_transfer::OwnDataContext;
use super::super::property_inference::{
    OwnPropertyProof, OwnPropertyRequest, infer_own_property_projection,
};
use super::super::value_model::{
    AbstractValue, CallSite, Context, DenseArray, OwnPropertyShape, PrivateFieldId, ProviderInput,
    ProviderScopeId, SymbolicInputId, ValueExpr, Values,
};
use super::{Call, CallKind, ExecutionIndex, native_call_identity_key};
use crate::analysis_inputs::{
    ConditionalResultAlternative, ConditionalResultProjection, ForeignInvocation,
    ForeignNativeOperation, ForeignReturn, ParameterHeapRead, PrimitiveCellInvariant,
    ProofDependencies, RecordInputProperty, RecordInputShape,
};
use crate::analysis_provenance::InferenceRule;
use crate::execution_evidence::{OrderedFieldReadRefusal, SkippedCallReason};
use crate::facts::abstract_domain::builtins::{
    self, IntrinsicOp, Invocation, NativeBodyEffects, Row,
};
use crate::facts::abstract_domain::foreign_call::DerivedForeignCall;
use crate::facts::abstract_domain::integrity::Integrity;
use crate::facts::abstract_domain::value::{
    ClassDefinitionId, HeapAddress, Kind, SourceHeapResolver, Value,
};
use crate::facts::semantic_inference::{NativePremise, names_intrinsic, native_premise};
use crate::facts::sync_effects::{
    AbruptAlternative, Completion, NonNormalPossibilities, TransferLedger, UnknownThrowValues,
};
use analysis_model::PrimKinds;
use imbl::OrdMap;
use source_identity::ModuleCellId;
use std::collections::{BTreeMap, BTreeSet};
use std::ops::{Deref, DerefMut};
use swc_common::Span;
use swc_ecma_ast::Id;

fn completed_provider_transfer(attempt: LocalExecutionAttempt) -> Option<LocalTransferResult> {
    match attempt {
        LocalExecutionAttempt::Completed(result) => Some(result),
        LocalExecutionAttempt::Refused { .. } => None,
    }
}

#[derive(Clone)]
struct ProviderContext<'a> {
    index: &'a ExecutionIndex,
    caller: Option<LocalBodyIdentity>,
    calls: &'a [Call],
    scope: ProviderScopeId,
    bindings: Context,
    argument_values: Values,
    closure_captures: BTreeMap<u32, Values>,
    closure_local_reads: BTreeMap<u32, BTreeSet<Id>>,
    closure_provider_reads: BTreeMap<u32, BTreeSet<Id>>,
    unsupported_closures: BTreeSet<u32>,
    final_environment: Option<Context>,
    class_mode: bool,
    local_bindings: BTreeSet<Id>,
    shadowed: BTreeSet<&'static str>,
    native_operations: BTreeSet<ForeignNativeOperation>,
    native_derivation_rules: BTreeSet<InferenceRule>,
    surface: ObjectSurfaceStore,
    private_heap: PrivateHeapEvidence,
    private_properties: OrdMap<HeapAddress, BTreeMap<PrivateFieldId, Values>>,
    arrays: OrdMap<HeapAddress, DenseArray>,
    native_retained_references: OrdMap<HeapAddress, NativeRetainedReferences>,
    descriptor_maps: OrdMap<HeapAddress, OwnDescriptorMapState>,
    revision: u64,
    /// Simple parameters and source-proved initialized var captures enter the
    /// shared body kernel; other projections retain allocation-only effects.
    body_effect_mode: bool,
    /// A query-local lexical transfer context. Existing provider inference
    /// leaves captured writes unsupported; only the post-init cell-kind query
    /// enables this path, with every input binding seeded conservatively.
    cell_transfer_mode: bool,
    cell_transfer_bindings: BTreeSet<Id>,
    module_cells: BTreeMap<Id, (ModuleCellId, PrimitiveCellInvariant)>,
    module_cell_accesses: crate::analysis_inputs::ModuleCellAccesses,
    /// Static admission for the narrower private-birth support proof. Actual
    /// operation support still checks the current frame at every access.
    provider_local_birth_candidate: bool,
    body_effect_bindings: BTreeSet<Id>,
    input_shape: Option<RecordInputShape>,
    parameter_reads: BTreeSet<ParameterHeapRead>,
}

impl Deref for ProviderContext<'_> {
    type Target = ObjectSurfaceStore;

    fn deref(&self) -> &Self::Target {
        &self.surface
    }
}

impl DerefMut for ProviderContext<'_> {
    fn deref_mut(&mut self) -> &mut Self::Target {
        &mut self.surface
    }
}

pub(super) struct PrimitiveCellTransfer {
    pub(super) cell_values: Values,
    pub(super) normal_completion: bool,
    pub(super) possible_throw: bool,
    /// The cell invariant accepts only covered, source-only transfers with no
    /// additional proof dependencies. A possible throw does not relax this.
    pub(super) allocation_covered: bool,
}

fn native_selection(
    index: &ExecutionIndex,
    call: &Call,
    shadowed: &BTreeSet<&'static str>,
    locals: &BTreeSet<Id>,
) -> Option<(&'static Row, NativePremise, Id)> {
    if call.kind != CallKind::Call || call.has_spread || !index.dynamic_scope_owners.is_empty() {
        return None;
    }
    let (row, callee) = native_call_identity_key(call.kind, &call.callee)?;
    if !(descriptor_transfer::supports_native_call(row)
        || row.body_effects == NativeBodyEffects::NoExteriorAccess)
        || !names_intrinsic(
            &callee,
            row.family()?,
            index.property_key_declarations.contains(&callee.binding)
                || locals.iter().any(|binding| binding.0 == callee.binding.0),
            shadowed,
            &index.global_object_names,
        )
    {
        return None;
    }
    let premise = native_premise(
        &index.integrity,
        row.requires,
        Some(CallSite::from(call.span).allocation_id()),
        InferenceRule::NativeCallIdentity,
    )?;
    if !premise.census {
        return None;
    }
    Some((row, premise, callee.binding))
}

impl AllocationContext for ProviderContext<'_> {
    fn source_covered(&self) -> bool {
        true
    }
    fn binding(&mut self, binding: &Id) -> ChildTransfer<AllocationValue> {
        match allocation_transfer::closed_binding(&self.bindings, binding, self.scope) {
            Some(value) => ChildTransfer::Value(value),
            None => ChildTransfer::BeforeExecution,
        }
    }
    fn child(
        &mut self,
        expression: &ValueExpr,
        _prefix_reached: bool,
    ) -> ChildTransfer<AllocationValue> {
        allocation_transfer::transfer(self, expression)
    }
    fn literal_layout(&self, allocation: u32) -> LiteralLayout {
        self.index.allocation_layout(allocation)
    }
    fn literal_property_kind(
        &self,
        allocation: u32,
        position: usize,
    ) -> super::super::value_model::LiteralPropertyKind {
        self.index.literal_property_kind(allocation, position)
    }
    fn heap(&mut self) -> LiteralHeap<'_> {
        LiteralHeap {
            surface: &mut self.surface,
            private_properties: Some(&mut self.private_properties),
            arrays: &mut self.arrays,
            revision: &mut self.revision,
            track_writable_prefix: self.body_effect_mode,
            track_spread_shape: false,
        }
    }
    fn keyed_collection_new(
        &mut self,
        callee: &ValueExpr,
        arguments: &[ValueExpr],
        site: CallSite,
    ) -> ChildTransfer<KeyedCollectionOperationResult> {
        match self.construct_empty_collection(callee, arguments, site) {
            Some(result) => ChildTransfer::Value(result),
            None => ChildTransfer::BeforeExecution,
        }
    }
}

impl ProviderContext<'_> {
    fn native_operation_plan_is_supported(&self, plan: &CheckedNativePlan) -> bool {
        let parameter_reads_supported = plan.parameter_reads.iter().all(|read| match read {
            ParameterHeapRead::ObjectShape { argument }
            | ParameterHeapRead::OwnProperty { argument, .. } => *argument == 0,
        });
        parameter_reads_supported
            && (plan.parameter_reads.is_empty() || self.input_shape.is_some())
            && !plan.heap.opaque
            && !plan.heap.observes_external_heap
    }

    fn descriptor_heap(&mut self) -> DescriptorHeap<'_> {
        DescriptorHeap {
            resolver: self.index.heap_resolver,
            maps: &mut self.descriptor_maps,
            surface: &mut self.surface,
            revision: &mut self.revision,
        }
    }

    fn retained_object_values(&self, object: HeapAddress) -> Option<Values> {
        self.surface
            .has_surface_state(object)
            .then(|| self.surface.retained_values(object))
    }

    fn own_property_projection(
        &self,
        values: &Values,
        request: OwnPropertyRequest,
    ) -> Option<OwnPropertyProof> {
        if values.len() != 1 {
            return None;
        }
        if let AbstractValue::SymbolicInput(input) = values.first()? {
            if input.scope != self.scope || input.input != ProviderInput::Argument(0) {
                return None;
            }
            return super::super::property_inference::conditional_input_projection(
                self.input_shape.as_ref()?,
                *input,
                request,
            );
        }
        let AbstractValue::Value(Value::Alloc(Kind::Object, object)) = values.first()? else {
            return None;
        };
        infer_own_property_projection(
            super::super::property_inference::OwnPropertyPrefix {
                heap_resolver: self.heap_resolver(),
                source_object: *object,
                source_values: values,
                shape: self.ordinary_record_shape(*object)?,
                properties: self.surface.own_properties(*object)?,
                tainted: !self.object_properties_are_settled(*object)
                    || self.has_applied_accessors(*object),
                literal_descriptors_intact: self.target_is_extensible(*object),
            },
            self.index.semantic_facts.as_deref(),
            super::super::property_inference::DescriptorCallbackModel::ProviderFunction,
            request,
        )
    }

    fn construct_empty_collection(
        &mut self,
        callee: &ValueExpr,
        arguments: &[ValueExpr],
        site: CallSite,
    ) -> Option<KeyedCollectionOperationResult> {
        if !arguments.is_empty() {
            return None;
        }
        let ValueExpr::Binding(binding) = callee else {
            return None;
        };
        let name = binding.0.as_ref();
        if self.shadowed.contains(name)
            || self.index.property_key_declarations.contains(binding)
            || self
                .local_bindings
                .iter()
                .any(|binding| binding.0.as_ref() == name)
        {
            return None;
        }
        let row = builtins::lookup(name, None, Invocation::Construct)?;
        let Integrity::Intact(dependencies) = self.index.integrity.require(row.requires, None)
        else {
            return None;
        };
        let result = allocation_transfer::keyed_collection_new(
            self.heap_resolver(),
            row,
            site,
            dependencies.clone(),
        )?;
        self.native_operations
            .insert(ForeignNativeOperation::for_row(row, Invocation::Construct)?);
        self.native_derivation_rules.extend([
            InferenceRule::NativeCallIdentity,
            InferenceRule::NativeIntegrity,
            InferenceRule::EmptyCollectionConstruction,
        ]);
        Some(result)
    }
}

impl NativeOperationContext for ProviderContext<'_> {
    fn native_operation_revision(&self) -> u64 {
        self.revision
    }

    fn commit_native_operation(&mut self, commit: NativeOperationCommit) {
        match commit {
            NativeOperationCommit::None => {}
            NativeOperationCommit::RetainNativeReferences { allocation, values } => {
                let mut retained = self
                    .native_retained_references
                    .get(&allocation)
                    .cloned()
                    .unwrap_or_default();
                let before = retained.values.len();
                retained.values.extend(values);
                let changed = retained.values.len() != before;
                let inserted = !self.native_retained_references.contains_key(&allocation);
                self.native_retained_references.insert(allocation, retained);
                if changed || inserted {
                    self.revision = self.revision.wrapping_add(1);
                }
            }
            NativeOperationCommit::CaptureDescriptors { site, projection } => {
                self.descriptor_heap().capture(site, projection);
            }
            NativeOperationCommit::CreateNamesArray { allocation, names } => {
                AllocationContext::heap(self).dense_array(
                    allocation,
                    names
                        .into_iter()
                        .map(|_| {
                            Values::from([AbstractValue::Value(Value::Prim(PrimKinds::STRING))])
                        })
                        .collect(),
                );
            }
            NativeOperationCommit::DefineProperties(application) => {
                AllocationContext::heap(self).invalidate_writable(application.target);
                self.descriptor_heap().apply_application(&application);
            }
            NativeOperationCommit::AssignProperties(assignment) => {
                AllocationContext::heap(self).invalidate_writable(assignment.target);
                self.descriptor_heap().assign(&assignment);
            }
        }
    }
}

impl OwnDataContext for ProviderContext<'_> {
    fn heap_resolver(&self) -> SourceHeapResolver {
        self.index.heap_resolver
    }

    fn object_surface(&self) -> &ObjectSurfaceStore {
        &self.surface
    }
    fn ordinary_record_shape(&self, allocation: HeapAddress) -> Option<&OwnPropertyShape> {
        if allocation.kind() != Some(Kind::Object) {
            return None;
        }
        let origin = self.heap_resolver().source_origin(allocation)?;
        self.index.ordinary_record_shape(origin)
    }
    fn own_properties(&self, allocation: HeapAddress) -> Option<&BTreeMap<Option<String>, Values>> {
        self.surface.own_properties(allocation)
    }
    fn object_properties_are_settled(&self, _allocation: HeapAddress) -> bool {
        // This isolated query refuses external mutation and publication.
        true
    }
    fn instance_properties_are_settled(&self, _class: ClassDefinitionId) -> bool {
        // Provider queries have no checked runtime instance-prefix witness.
        false
    }
    fn property_values(&self, allocation: HeapAddress, property: &str) -> Option<&Values> {
        self.surface.data_values(allocation, property)
    }
    fn property_is_accessor(&self, allocation: HeapAddress, property: &str) -> bool {
        self.surface.has_accessor_property(allocation, property)
    }
    fn property_is_writable(&self, allocation: HeapAddress, property: &str) -> bool {
        self.body_effect_mode
            && matches!(self.writable_prefix.get(&allocation),
                Some(WritablePrefixState::Proven(properties)) if properties.contains(property))
            && self
                .surface
                .attributes(allocation, property)
                .is_some_and(|attributes| attributes.writable == Some(true))
    }
    fn first_generation_dense_array_length(&self, allocation: HeapAddress) -> Option<u32> {
        // Source array literals use the ordinary Object lane in the value
        // model; `array_elements` remains their source-coordinate index.
        if allocation.kind() != Some(Kind::Object) {
            return None;
        }
        let origin = self.heap_resolver().source_origin(allocation)?;
        if !self.index.array_elements.contains_key(&origin)
            || !self.arrays.contains_key(&allocation)
        {
            return None;
        }
        match self.writable_prefix.get(&allocation)? {
            WritablePrefixState::DenseArray { length } => Some(*length),
            _ => None,
        }
    }
}

impl DescriptorContext for ProviderContext<'_> {
    fn heap_epoch(&self) -> u64 {
        self.revision
    }
    fn descriptor_map(&self, site: HeapAddress) -> Option<&OwnDescriptorMapState> {
        self.descriptor_maps.get(&site)
    }
    fn has_applied_accessors(&self, object: HeapAddress) -> bool {
        self.surface.has_any_accessors(object)
    }
    fn target_is_extensible(&self, object: HeapAddress) -> bool {
        OwnDataContext::extensibility_knowledge(self, object)
            == super::super::object_surface::ExtensibilityKnowledge::Extensible
    }
}

impl LocalContext for ProviderContext<'_> {
    fn symbolic_field_read(
        &mut self,
        receivers: &Values,
        property: &str,
    ) -> Option<LocalValueResult> {
        if receivers.len() != 1 {
            return None;
        }
        let AbstractValue::SymbolicInput(input) = receivers.first()? else {
            return None;
        };
        if input.scope != self.scope || input.input != ProviderInput::Argument(0) {
            return None;
        }
        let shape = self.input_shape.as_ref()?;
        let (ordinal, (_, kind)) = shape
            .properties
            .iter()
            .enumerate()
            .find(|(_, (name, _))| name.as_str() == property)?;
        if !matches!(kind, RecordInputProperty::Data) {
            return None;
        }
        self.parameter_reads.insert(ParameterHeapRead::OwnProperty {
            argument: 0,
            property: property.to_owned(),
        });
        self.parameter_reads
            .insert(ParameterHeapRead::ObjectShape { argument: 0 });
        let mut effects = EffectCoverageFragment::no_effects();
        effects.operation_support(OperationSupportReceipt::parameter_object_shape(
            OperationSupportOperation::OwnDataRead,
            0,
            &ProofDependencies::default(),
        ));
        let ledger = TransferLedger::new(
            Completion::Normal(SelectedValue::raw(Values::from([
                AbstractValue::SymbolicInput(SymbolicInputId {
                    scope: self.scope,
                    input: ProviderInput::Property {
                        argument: 0,
                        property: ordinal,
                    },
                }),
            ]))),
            effects,
            Box::default(),
            AllocationJudgment::Opaque,
        );
        if !ledger.has_closed_effects() {
            return None;
        }
        Some(LocalValueResult { ledger })
    }
    fn native_callee(
        &mut self,
        _frame: &mut LocalFrame,
        invocation: ForeignInvocation,
        _callee: &LocalCallee,
        span: Span,
    ) -> Option<(NativeCalleeSelection, EffectCoverageFragment)> {
        if invocation != ForeignInvocation::Call {
            return None;
        }
        let call = self
            .calls
            .iter()
            .find(|call| call.kind == CallKind::Call && call.span == span)?;
        let (row, premise, _) =
            native_selection(self.index, call, &self.shadowed, &self.local_bindings)?;
        let mut effects = EffectCoverageFragment::no_effects();
        effects.depend_on(&premise.dependencies);
        Some((
            NativeCalleeSelection {
                owner: call.owner,
                site: span.into(),
                row,
                premise,
                native_operation_input: None,
            },
            effects,
        ))
    }
    // Concrete cells belong to the isolated fresh heap. Conditional symbolic
    // input reads have a separate parameterized footprint; returned-value
    // retention remains an independent projection.
    fn record_property_read(&mut self, _cell: crate::facts::EffectCell) {}
    fn record_property_write(&mut self, _cell: crate::facts::EffectCell) {}
    fn operation_support_receipt(
        &self,
        frame: &LocalFrame,
        operation: OperationSupportOperation,
        origin: HeapAddress,
        dependencies: &ProofDependencies,
    ) -> Option<OperationSupportReceipt> {
        // A candidate receipt is issued only for this access, after checking
        // the active frame's birth set and excluding every borrowed origin.
        // Otherwise preserve the unresolved provider-world receipt; neither
        // path projects a provider ordinal into caller heap identity.
        if self.provider_local_birth_candidate
            && frame.fresh_records.contains(&origin)
            && !frame.borrowed_records.contains(&origin)
        {
            Some(OperationSupportReceipt::provider_local_birth_candidate(
                operation,
                origin,
                dependencies,
            ))
        } else {
            Some(OperationSupportReceipt::provider_world_object_shape(
                operation,
                origin,
                dependencies,
            ))
        }
    }
    fn declarative_binding_operation(
        &mut self,
        binding: &Id,
        _frame: &LocalFrame,
        _operation: local_transfer::BindingOperation,
    ) -> EffectCoverageFragment {
        if self.body_effect_mode && self.body_effect_bindings.contains(binding) {
            EffectCoverageFragment::no_effects()
        } else {
            EffectCoverageFragment::default()
        }
    }
    fn closure(
        &mut self,
        frame: &mut LocalFrame,
        creation: DeferredCallableCreation,
    ) -> Option<LocalValueResult> {
        let callable = creation.callable();
        assert_eq!(
            self.index.callable_creation(callable),
            Some(creation),
            "canonical callable creation kind/home-object association"
        );
        let body = self.index.callables.get(&callable)?;
        let parameters: BTreeSet<_> = body
            .params
            .iter()
            .filter_map(|parameter| parameter.binding.as_ref())
            .collect();
        let parameter_bindings: BTreeSet<_> = body
            .params
            .iter()
            .flat_map(|parameter| parameter.bindings.iter())
            .collect();
        let class_unavailable_capture = body.reads.iter().any(|binding| {
            !body.declared_locals.contains(binding)
                && !parameters.contains(binding)
                && !self.local_bindings.contains(binding)
        });
        let unavailable_capture = body.reads.iter().any(|binding| {
            body.self_binding.as_ref() != Some(binding)
                && !body.declared_locals.contains(binding)
                && !body.pattern_locals.contains(binding)
                && !parameter_bindings.contains(binding)
                && !self.local_bindings.contains(binding)
                && !frame.environment.contains_key(binding)
                && !self.index.deferred_module_reference(binding)
        });
        if self.class_mode
            && (class_unavailable_capture
                || body.generator
                || self.index.async_callables.contains(&callable))
        {
            return None;
        }
        if unavailable_capture
            || ((self.class_mode || creation.home_object().is_none())
                && (body.generator || self.index.async_callables.contains(&callable)))
        {
            self.unsupported_closures.insert(callable);
        }
        let mut captures = self.argument_values.clone();
        if body.implicit_capture && self.class_mode {
            // Method/accessor receivers are dynamic, not the constructor's
            // lexical receiver. Keep class-mode admission conservative.
            if creation.home_object().is_some() {
                return None;
            }
            captures.extend(frame.this_values.as_ref()?.iter().cloned());
        } else if body.implicit_capture && matches!(creation, DeferredCallableCreation::Arrow(_)) {
            self.unsupported_closures.insert(callable);
        }
        if let Some(home_object) = creation.home_object() {
            captures.insert(AbstractValue::Value(Value::Alloc(
                Kind::Object,
                self.heap_resolver().allocation(Kind::Object, home_object),
            )));
        }
        self.closure_captures.insert(callable, captures);
        Some(LocalValueResult {
            ledger: TransferLedger::new(
                Completion::Normal(SelectedValue::raw(Values::from([AbstractValue::Value(
                    Value::Alloc(
                        Kind::Function,
                        self.heap_resolver().allocation(Kind::Function, callable),
                    ),
                )]))),
                if self.body_effect_mode {
                    creation.effect_coverage()
                } else {
                    EffectCoverageFragment::default()
                },
                Box::default(),
                AllocationJudgment::CoveredAllocationOnly(Box::default()),
            ),
        })
    }
    fn assign_capture(
        &mut self,
        frame: &mut LocalFrame,
        binding: &Id,
        selected: &SelectedValue,
        _span: Span,
    ) -> Option<LocalValueResult> {
        if !self.cell_transfer_mode
            || !self.cell_transfer_bindings.contains(binding)
            || selected.values.is_empty()
        {
            return None;
        }
        if let Some((cell, invariant)) = self.module_cells.get(binding) {
            if selected.values.is_empty()
                || !selected.values.iter().all(|value| {
                    matches!(value, AbstractValue::Value(Value::Prim(kinds))
                    if kinds.is_only(invariant.post_init_kinds))
                })
            {
                return None;
            }
            self.module_cell_accesses.writes.insert(*cell);
            self.module_cell_accesses
                .invariants
                .insert(*cell, invariant.clone());
        }
        frame.binding_selections.remove(binding);
        frame.binding_postconditions.remove(binding);
        frame
            .environment
            .insert(binding.clone(), selected.values.clone());
        frame.initialized_bindings.insert(binding.clone());
        Some(LocalValueResult {
            ledger: TransferLedger::new(
                Completion::Normal(SelectedValue {
                    values: selected.values.clone(),
                    selection: selected.selection.clone(),
                    postcondition: selected.postcondition.clone(),
                }),
                EffectCoverageFragment::no_effects(),
                Box::default(),
                AllocationJudgment::CoveredAllocationOnly(Box::default()),
            ),
        })
    }
    fn read_binding(
        &mut self,
        binding: &Id,
        frame: &mut LocalFrame,
        _source_site: Option<&SourceOperationSite>,
    ) -> Option<LocalValueResult> {
        // The frame starts with scoped arguments and checked private var
        // captures. Other entries come from the covered local operations.
        let values = frame.environment.get(binding)?.clone();
        if values.is_empty() {
            return None;
        }
        if let Some((cell, invariant)) = self.module_cells.get(binding) {
            if !values.iter().all(|value| {
                matches!(value, AbstractValue::Value(Value::Prim(kinds))
                if kinds.is_only(invariant.post_init_kinds))
            }) {
                return None;
            }
            self.module_cell_accesses.reads.insert(*cell);
            self.module_cell_accesses
                .invariants
                .insert(*cell, invariant.clone());
        }
        let effects = if self.body_effect_mode
            && frame.initialized_bindings.contains(binding)
            && !values.is_empty()
            && !values.contains(&AbstractValue::Value(Value::Unavailable))
        {
            EffectCoverageFragment::no_effects()
        } else {
            EffectCoverageFragment::default()
        };
        let completion = if frame.initialized_bindings.contains(binding) {
            Completion::Normal(SelectedValue::raw(values))
        } else {
            Completion::OpaqueMayNormal(SelectedValue::raw(values))
        };
        Some(LocalValueResult {
            ledger: TransferLedger::new(
                completion,
                effects,
                Box::default(),
                AllocationJudgment::CoveredAllocationOnly(Box::default()),
            ),
        })
    }
    fn call(
        &mut self,
        frame: &mut LocalFrame,
        invocation: ForeignInvocation,
        callee: &EvaluatedCallee,
        arguments: &[local_transfer::SelectedValue],
        source_site: &SourceOperationSite,
        span: Span,
    ) -> Option<LocalValueResult> {
        if invocation != ForeignInvocation::Call
            || source_site.step != OperationStep::CallInvocation
            || !self.index.source_operation_sites.contains(source_site)
            || !self
                .calls
                .iter()
                .any(|call| call.kind == CallKind::Call && call.has_source_site(source_site))
        {
            return None;
        }
        let EvaluatedCallee::Native { selection, .. } = callee else {
            // Forwarding a symbolic callable does not prove its execution.
            return None;
        };
        if selection.site != CallSite::from(span) {
            return None;
        }
        let row = selection.row;
        let Integrity::Intact(protocol) =
            self.index.integrity.require(row.protocol_requires(), None)
        else {
            return None;
        };
        let mut dependencies = selection.premise.dependencies.clone();
        dependencies.extend(protocol);
        let target_postcondition = arguments
            .first()
            .and_then(|argument| argument.postcondition.clone());
        let arguments: Vec<_> = arguments
            .iter()
            .map(|argument| argument.values.clone())
            .collect();
        let projection = match row.op {
            IntrinsicOp::ObjectGetOwnPropertyNames => Some(self.own_property_projection(
                arguments.first()?,
                OwnPropertyRequest::Names {
                    native_call: selection.premise.clone(),
                },
            )?),
            IntrinsicOp::ObjectGetOwnPropertyDescriptors => Some(self.own_property_projection(
                arguments.first()?,
                OwnPropertyRequest::Descriptors {
                    native_call: selection.premise.clone(),
                },
            )?),
            _ => None,
        };
        if let Some(proof) = &projection {
            dependencies.extend(&proof.dependencies);
            self.native_derivation_rules
                .extend(proof.inference_use().derivation_rules);
        }
        let native_operation = ForeignNativeOperation::for_row(row, Invocation::Call)?;
        let plan = descriptor_transfer::native_call_plan(
            self,
            DescriptorCall {
                row,
                site: span.into(),
                arguments: &arguments,
                projection: projection.as_ref(),
                target_postcondition: target_postcondition.as_deref(),
            },
            &dependencies,
        )
        .or_else(|| {
            allocation_transfer::primitive_native_call_plan(
                row,
                &arguments,
                &dependencies,
                self.revision,
            )
        })?;
        if !self.native_operation_plan_is_supported(&plan) {
            // The plan has not committed yet. In particular, do not mutate
            // ProviderContext and then discard its untransportable heap cells.
            return None;
        }
        let private_transfer = self.private_heap.admit(self, frame, &plan).ok()?;
        let operation = native_operation::execute_native_plan(self, plan).ok()?;
        // The debt is owned only after the checked commit succeeds. A complete
        // body must seal its result/capture reachability before exporting it.
        self.private_heap = private_transfer;
        self.parameter_reads.extend(operation.parameter_reads);
        let (normal, abrupt, non_normal) = operation.ledger.completion.into_alternatives();
        if !abrupt.is_empty() || non_normal != NonNormalPossibilities::default() {
            return None;
        }
        let completion = Completion::from_alternatives(
            normal.map(|result| SelectedValue {
                values: result.values,
                selection: None,
                postcondition: result.postcondition,
            }),
            Vec::<AbruptAlternative<Box<KnownThrow>>>::new(),
            NonNormalPossibilities::default(),
        );
        self.native_operations.insert(native_operation);
        self.native_derivation_rules.extend(
            selection
                .premise
                .inference_use
                .derivation_rules
                .iter()
                .copied(),
        );
        Some(LocalValueResult {
            ledger: TransferLedger::new(
                completion,
                operation.ledger.effects,
                operation.ledger.dependencies,
                operation.ledger.allocation,
            ),
        })
    }
    fn literal_layout(&self, allocation: u32) -> LiteralLayout {
        self.index.allocation_layout(allocation)
    }
    fn literal_property_kind(
        &self,
        allocation: u32,
        position: usize,
    ) -> super::super::value_model::LiteralPropertyKind {
        self.index.literal_property_kind(allocation, position)
    }
    fn heap(&mut self) -> LiteralHeap<'_> {
        AllocationContext::heap(self)
    }
    fn unproved_field_read(
        &mut self,
        _frame: &mut LocalFrame,
        _receiver: Option<&Id>,
        _receivers: &Values,
        _property: &str,
        _span: Span,
        _refusal: OrderedFieldReadRefusal,
    ) -> Option<AllocationValue> {
        // Symbolic inputs and captures prove no descriptor or getter behavior.
        // Only the shared fresh own-data transfer can certify a field read.
        None
    }
    fn unproved_field_write(
        &mut self,
        _frame: &mut LocalFrame,
        _receivers: &Values,
        _values: &Values,
        _span: Span,
    ) {
        // The shared kernel returns None; there is no application state to
        // publish into, and this query cannot certify the body.
    }
    fn publish_throw(&mut self, _values: &Values) {
        // The shared abrupt result makes the allocation judgment opaque.
    }

    fn skipped_region(
        &mut self,
        frame: &mut LocalFrame,
        region: &SourceOperationSite,
        frontier: &SourceOperationSite,
        reason: SkippedCallReason,
    ) -> bool {
        let Some(caller) = &self.caller else {
            return false;
        };
        self.index
            .record_skipped_region(self.calls.iter(), caller, frame, region, frontier, reason)
    }

    fn keyed_collection_new(
        &mut self,
        _frame: &mut LocalFrame,
        callee: &ValueExpr,
        arguments: &[ValueExpr],
        source_site: &SourceOperationSite,
        site: CallSite,
    ) -> Option<KeyedCollectionOperationResult> {
        if source_site.step != OperationStep::CallInvocation
            || CallSite::from(source_site.span) != site
            || !self.index.source_operation_sites.contains(source_site)
            || !self
                .calls
                .iter()
                .any(|call| call.kind == CallKind::New && call.has_source_site(source_site))
        {
            return None;
        }
        self.construct_empty_collection(callee, arguments, site)
    }
}

pub(super) fn transfer_primitive_cell_initializer(
    index: &ExecutionIndex,
    shadowed: &BTreeSet<&'static str>,
    body: &LocalBody,
    binding: &Id,
    initial_cell: Values,
) -> Option<PrimitiveCellTransfer> {
    transfer_primitive_cell(index, shadowed, body, binding, initial_cell, true)
}

pub(super) fn transfer_primitive_cell_store(
    index: &ExecutionIndex,
    shadowed: &BTreeSet<&'static str>,
    body: &LocalBody,
    binding: &Id,
    current_cell: Values,
) -> Option<PrimitiveCellTransfer> {
    transfer_primitive_cell(index, shadowed, body, binding, current_cell, false)
}

fn transfer_primitive_cell(
    index: &ExecutionIndex,
    shadowed: &BTreeSet<&'static str>,
    body: &LocalBody,
    binding: &Id,
    initial_cell: Values,
    initializer: bool,
) -> Option<PrimitiveCellTransfer> {
    let scope = ProviderScopeId::fresh()?;
    let mut environment = Context::new();
    environment.insert(binding.clone(), initial_cell);

    // The transfer is for one arbitrary-reachable writer. Its other bindings
    // are unconstrained, even when the writer belongs to a nested callable or
    // is reached through callback reentry.
    let mut inputs = BTreeSet::new();
    inputs.extend(body.free_reads().iter().cloned());
    inputs.extend(body.captured_writes().iter().cloned());
    inputs.extend(body.bindings.keys().cloned());
    for input in &inputs {
        if input != binding {
            environment.insert(
                input.clone(),
                Values::from([AbstractValue::Value(Value::Top)]),
            );
        }
    }
    let mut symbolic_bindings = Context::new();
    symbolic_bindings.insert(
        binding.clone(),
        Values::from([AbstractValue::Value(Value::Top)]),
    );
    for input in &inputs {
        symbolic_bindings.insert(
            input.clone(),
            Values::from([AbstractValue::Value(Value::Top)]),
        );
    }
    let local_bindings = body.bindings.keys().cloned().collect();
    let caller = body
        .source_region_site
        .as_ref()
        .map(|site| site.body.clone())?;
    let calls = index.calls_for_local_body(&caller)?;
    let mut context = ProviderContext {
        index,
        caller: Some(caller),
        calls: &calls,
        scope,
        bindings: symbolic_bindings,
        argument_values: Values::new(),
        closure_captures: BTreeMap::new(),
        closure_local_reads: BTreeMap::new(),
        closure_provider_reads: BTreeMap::new(),
        unsupported_closures: BTreeSet::new(),
        final_environment: None,
        class_mode: false,
        local_bindings,
        shadowed: shadowed.clone(),
        native_operations: BTreeSet::new(),
        native_derivation_rules: BTreeSet::new(),
        surface: ObjectSurfaceStore::default(),
        private_heap: PrivateHeapEvidence::default(),
        private_properties: OrdMap::new(),
        arrays: OrdMap::new(),
        native_retained_references: OrdMap::new(),
        descriptor_maps: OrdMap::new(),
        revision: 0,
        body_effect_mode: false,
        cell_transfer_mode: true,
        cell_transfer_bindings: BTreeSet::from([binding.clone()]),
        module_cells: BTreeMap::new(),
        module_cell_accesses: crate::analysis_inputs::ModuleCellAccesses::default(),
        provider_local_birth_candidate: false,
        body_effect_bindings: BTreeSet::new(),
        input_shape: None,
        parameter_reads: BTreeSet::new(),
    };
    let mut frame = LocalFrame::new(environment);
    if !initializer {
        // This transfer starts at the module cell's post-initialization
        // state. Seed only the distinguished current cell; this is not a
        // caller-side initialization receipt for any provider dependency.
        frame.initialized_bindings.insert(binding.clone());
    }
    let transfer =
        completed_provider_transfer(local_transfer::execute(&mut context, body, &mut frame))?;
    let normal_completion = transfer.ledger.completion.may_normal().is_some();
    let cell_values = if initializer {
        match transfer.ledger.completion.may_normal() {
            Some(LocalNormal::Next) => Values::from([AbstractValue::UNDEFINED]),
            Some(LocalNormal::Return { selected, .. }) => selected.values.clone(),
            Some(LocalNormal::Unknown(selected)) => selected.values.clone(),
            None => Values::new(),
        }
    } else {
        frame.environment.get(binding)?.clone()
    };
    let possible_throw = !transfer.ledger.completion.is_exact_normal();
    Some(PrimitiveCellTransfer {
        cell_values,
        normal_completion,
        possible_throw,
        allocation_covered: matches!(
            &transfer.ledger.allocation,
            AllocationJudgment::CoveredAllocationOnly(dependencies) if dependencies.is_empty()
        ),
    })
}

struct ClosureCaptureProjection {
    local_reads: BTreeMap<u32, BTreeSet<Id>>,
    provider_reads: BTreeMap<u32, BTreeSet<Id>>,
}

/// A returned closure retains the values of outer locals it can reach. Nested
/// callable reads are collected from canonical callable spans so provider
/// private-var cells remain explicit retained captures after local transfer.
fn closure_capture_projection(
    index: &ExecutionIndex,
    body: &super::super::local_operations::LocalBody,
) -> Option<ClosureCaptureProjection> {
    let outer_bindings: BTreeSet<_> = body.bindings.keys().cloned().collect();
    let mut projection = ClosureCaptureProjection {
        local_reads: BTreeMap::new(),
        provider_reads: BTreeMap::new(),
    };
    for &callable in body.closure_sites() {
        let span = index.callable_spans.get(&callable)?;
        let mut local_reads = BTreeSet::new();
        let mut provider_reads = BTreeSet::new();
        let descendants: Vec<_> = index
            .callables
            .range(callable..span.hi.0)
            .filter(|(candidate, _)| index.callable_descends_from(**candidate, callable))
            .collect();
        let mut closure_bindings = BTreeSet::new();
        for (_, nested) in &descendants {
            closure_bindings.extend(
                nested
                    .params
                    .iter()
                    .flat_map(|parameter| parameter.bindings.iter().cloned()),
            );
            closure_bindings.extend(nested.declared_locals.iter().cloned());
            closure_bindings.extend(nested.pattern_locals.iter().cloned());
            closure_bindings.extend(nested.local_functions.keys().cloned());
            closure_bindings.extend(nested.self_binding.iter().cloned());
        }
        let reads = index
            .prototype_callable_reads
            .get(&callable)
            .unwrap_or(&index.callables.get(&callable)?.reads);
        for binding in reads {
            if closure_bindings.contains(binding) {
                continue;
            }
            if outer_bindings.contains(binding) {
                local_reads.insert(binding.clone());
            } else if index.private_var_binding(binding) {
                provider_reads.insert(binding.clone());
            } else if !index.deferred_module_reference(binding) {
                // No final-frame cell or proved private capture represents this
                // enclosing lexical value. Creation cannot invent its contents.
                return None;
            }
        }
        projection.local_reads.insert(callable, local_reads);
        projection.provider_reads.insert(callable, provider_reads);
    }
    Some(projection)
}

fn retain_provider_values(
    context: &ProviderContext<'_>,
    scope: ProviderScopeId,
    captures: &[Id],
    roots: impl IntoIterator<Item = AbstractValue>,
    retained_arguments: &mut BTreeSet<usize>,
    retained_provider_captures: &mut BTreeSet<String>,
) -> Option<()> {
    let mut pending: Vec<_> = roots.into_iter().collect();
    let mut visited_objects = BTreeSet::<HeapAddress>::new();
    let mut visited_closures = BTreeSet::<HeapAddress>::new();
    let mut visited_descriptor_maps = BTreeSet::<HeapAddress>::new();
    while let Some(value) = pending.pop() {
        match value {
            AbstractValue::SymbolicInput(input) if input.scope == scope => match input.input {
                ProviderInput::Argument(index) => {
                    retained_arguments.insert(index);
                }
                ProviderInput::Capture(index) => {
                    retained_provider_captures.insert(captures.get(index)?.0.to_string());
                }
                ProviderInput::Property { argument, .. }
                | ProviderInput::Getter { argument, .. }
                | ProviderInput::Setter { argument, .. } => {
                    retained_arguments.insert(argument);
                }
            },
            AbstractValue::Value(Value::Alloc(Kind::Object, allocation)) => {
                if visited_objects.insert(allocation) {
                    pending.extend(context.retained_object_values(allocation)?);
                }
            }
            AbstractValue::OwnDescriptorMap(site) => {
                if visited_descriptor_maps.insert(site) {
                    pending.extend(
                        context
                            .descriptor_maps
                            .get(&site)?
                            .retained_values
                            .iter()
                            .cloned(),
                    );
                }
            }
            AbstractValue::Value(Value::Alloc(Kind::Function, callable_address)) => {
                if !visited_closures.insert(callable_address) {
                    continue;
                }
                let callable = context.heap_resolver().source_origin(callable_address)?;
                if context.unsupported_closures.contains(&callable) {
                    return None;
                }
                pending.extend(context.closure_captures.get(&callable)?.iter().cloned());
                let environment = context.final_environment.as_ref()?;
                for binding in context
                    .closure_local_reads
                    .get(&callable)
                    .into_iter()
                    .flatten()
                    .chain(
                        context
                            .closure_provider_reads
                            .get(&callable)
                            .into_iter()
                            .flatten(),
                    )
                {
                    pending.extend(environment.get(binding)?.iter().cloned());
                }
            }
            AbstractValue::Value(Value::Alloc(kind, _))
                if kind.is_keyed_collection() || kind == Kind::RegExp => {}
            AbstractValue::Value(Value::Prim(_)) => {}
            _ => return None,
        }
    }
    Some(())
}

/// Execute each feasible path with the shared local continuation. A fork
/// clones the strict provider's query-local world after the tested prefix;
/// no prefix is replayed and no path is joined before its completion is
/// projected by the caller.
fn execute_provider_continuation_family<'a>(
    context: ProviderContext<'a>,
    body: &LocalBody,
    frame: LocalFrame,
) -> Option<Vec<(ProviderContext<'a>, LocalFrame, LocalTransferResult)>> {
    let mut pending = vec![(context, LocalContinuation::new(frame))];
    let mut completed = Vec::new();
    while let Some((mut context, mut continuation)) = pending.pop() {
        match continuation.poll(&mut context, body) {
            LocalSuspension::Call(call) => {
                let result = context.call(
                    &mut continuation.frame,
                    call.invocation,
                    &call.callee,
                    &call.arguments,
                    &call.source_site,
                    call.span,
                );
                continuation.resume_call(&mut context, result);
                pending.push((context, continuation));
            }
            LocalSuspension::Fork(children) => {
                if children.is_empty() {
                    return None;
                }
                for child in children {
                    pending.push((context.clone(), child));
                }
            }
            LocalSuspension::Complete(LocalExecutionAttempt::Completed(result)) => {
                completed.push((context, continuation.frame, result));
            }
            // A refused leaf cannot become positive by running a second
            // summary or dropping it from the family.
            LocalSuspension::Complete(LocalExecutionAttempt::Refused { .. }) => return None,
        }
    }
    (!completed.is_empty()).then_some(completed)
}

/// The canonical source collector retained whole-body syntax coverage before
/// value lowering. Direct returns and ordered bodies share their transfers
/// with application execution; refusal never retries the legacy AST evaluator.
fn execute_provider_body_family<'a>(
    index: &'a ExecutionIndex,
    callable: u32,
    shadowed: &BTreeSet<&'static str>,
    input_shape: Option<RecordInputShape>,
    module_cells: &BTreeMap<Id, (ModuleCellId, PrimitiveCellInvariant)>,
) -> Option<Vec<ProviderExecution<'a>>> {
    let body = index.callables.get(&callable)?;
    let local_body = body.body_facts.ordered_plan();
    let conditional_body = input_shape.is_some();
    if !conditional_body
        && body
            .body_facts
            .value_flow()
            .direct_allocation_return()
            .is_none()
        && !local_body.is_some_and(|body| {
            body.allocation_candidate_syntax() || body.closure_candidate_syntax()
        })
    {
        return None;
    }
    let closure_mode =
        !conditional_body && local_body.is_some_and(|body| body.closure_candidate_syntax());
    // Effects are admitted only from the complete shared body plan: using
    // allocation_return's narrowed reads here would silently omit discarded
    // expressions and other free reads from the executed body.
    let body_effect_candidate = body
        .params
        .iter()
        .all(|parameter| parameter.binding.is_some() && parameter.default.is_none())
        && !body.generator
        && !index.async_callables.contains(&callable)
        && local_body.is_some_and(|body| {
            (conditional_body
                || body.allocation_candidate_syntax()
                || body.closure_candidate_syntax())
                && (body.captured_writes().is_empty()
                    || (conditional_body
                        && body
                            .captured_writes()
                            .iter()
                            .all(|binding| module_cells.contains_key(binding))))
        });
    let provider_local_birth_syntax_candidate = body_effect_candidate
        // The source census records a member read as a possible getter call.
        // Only the shared own-data transfer can discharge that possibility;
        // actual calls and every other implicit protocol stay excluded.
        && body
            .calls
            .iter()
            .all(|call| call.kind == CallKind::LocalMemberRead)
        && local_body.is_some_and(|body| body.provider_local_birth_candidate());
    let closure_projection = if closure_mode || conditional_body {
        Some(closure_capture_projection(index, local_body?)?)
    } else {
        None
    };
    let scope = ProviderScopeId::fresh()?;
    let local_bindings: BTreeSet<_> = local_body
        .map(|body| body.bindings.keys().cloned().collect())
        .unwrap_or_default();
    let native_callee_bindings: BTreeSet<_> = body
        .calls
        .iter()
        .filter_map(|call| {
            native_selection(index, call, shadowed, &local_bindings).map(|(_, _, binding)| binding)
        })
        .collect();
    let mut bindings = Context::new();
    let mut argument_values = Values::new();
    let mut parameter_inputs = Vec::new();
    for (parameter, param) in body.params.iter().enumerate() {
        let value = AbstractValue::SymbolicInput(SymbolicInputId {
            scope,
            input: ProviderInput::Argument(parameter),
        });
        if let Some(binding) = &param.binding {
            bindings.insert(binding.clone(), Values::from([value.clone()]));
            parameter_inputs.push(SelectedValue::raw(Values::from([value.clone()])));
        }
        argument_values.insert(value);
    }
    let mut complete_reads = local_body.map(|local_body| {
        local_body
            .free_reads()
            .iter()
            .filter(|binding| {
                !native_callee_bindings.contains(*binding)
                    && (!local_body.requires_keyed_collection_new()
                        || !matches!(binding.0.as_ref(), "Map" | "Set" | "WeakMap" | "WeakSet"))
            })
            .cloned()
            .collect::<BTreeSet<_>>()
    });
    if let (Some(complete_reads), Some(projection)) = (&mut complete_reads, &closure_projection) {
        complete_reads.extend(projection.provider_reads.values().flatten().cloned());
    }
    if let Some(complete_reads) = &mut complete_reads {
        for binding in bindings.keys() {
            complete_reads.remove(binding);
        }
    }
    let body_effect_mode = body_effect_candidate
        && complete_reads.as_ref().is_some_and(|reads| {
            reads.iter().all(|binding| {
                module_cells.contains_key(binding)
                    || index.initialized_provider_var_capture(binding, callable)
            })
        });
    let provider_local_birth_candidate = body_effect_mode && provider_local_birth_syntax_candidate;
    if conditional_body && !body_effect_mode {
        return None;
    }
    let mut reads = if let Some(expression) =
        body.body_facts.value_flow().direct_allocation_return()
        && !closure_mode
        && !body_effect_mode
    {
        let mut reads = BTreeSet::new();
        expression.bindings(&mut reads);
        reads
    } else {
        complete_reads?
    };
    for binding in bindings.keys() {
        reads.remove(binding);
    }
    let captures: Vec<_> = reads.into_iter().collect();
    let mut capture_dependencies = ProofDependencies::default();
    for (capture, binding) in captures.iter().enumerate() {
        // This establishes lexical cell initialization, not value kind, heap
        // locality, immutability or privacy of anything the cell can contain.
        let uncovered = if body_effect_mode {
            !module_cells.contains_key(binding)
                && !index.initialized_provider_var_capture(binding, callable)
        } else {
            !index.private_var_binding(binding)
        };
        if uncovered {
            return None;
        }
        let value = if let Some((_, invariant)) = module_cells.get(binding) {
            AbstractValue::Value(Value::Prim(invariant.post_init_kinds))
        } else if let Some((kinds, dependencies)) =
            index.primitive_provider_capture(binding, callable)
        {
            capture_dependencies.extend(&dependencies);
            AbstractValue::Value(Value::Prim(kinds))
        } else {
            AbstractValue::SymbolicInput(SymbolicInputId {
                scope,
                input: ProviderInput::Capture(capture),
            })
        };
        bindings.insert(binding.clone(), Values::from([value]));
    }
    let cell_transfer_bindings: BTreeSet<_> = local_body
        .into_iter()
        .flat_map(|body| body.captured_writes())
        .filter(|binding| module_cells.contains_key(*binding))
        .cloned()
        .collect();
    let expected_caller = LocalBodyIdentity::Callable {
        key: callable,
        owner: body.owner,
    };
    let caller = match local_body.and_then(|body| body.source_region_site.as_ref()) {
        Some(site) if site.body == expected_caller => Some(site.body.clone()),
        Some(_) => None,
        None => Some(expected_caller),
    };
    let mut context = ProviderContext {
        input_shape,
        parameter_reads: BTreeSet::new(),
        index,
        caller,
        calls: &body.calls,
        scope,
        bindings,
        argument_values,
        closure_captures: BTreeMap::new(),
        closure_local_reads: closure_projection
            .as_ref()
            .map(|projection| projection.local_reads.clone())
            .unwrap_or_default(),
        closure_provider_reads: closure_projection
            .as_ref()
            .map(|projection| projection.provider_reads.clone())
            .unwrap_or_default(),
        unsupported_closures: BTreeSet::new(),
        final_environment: None,
        class_mode: false,
        local_bindings,
        shadowed: shadowed.clone(),
        native_operations: BTreeSet::new(),
        native_derivation_rules: BTreeSet::new(),
        surface: ObjectSurfaceStore::default(),
        private_heap: PrivateHeapEvidence::default(),
        private_properties: OrdMap::new(),
        arrays: OrdMap::new(),
        native_retained_references: OrdMap::new(),
        descriptor_maps: OrdMap::new(),
        revision: 0,
        body_effect_mode,
        cell_transfer_mode: conditional_body && !cell_transfer_bindings.is_empty(),
        cell_transfer_bindings,
        module_cells: module_cells.clone(),
        module_cell_accesses: crate::analysis_inputs::ModuleCellAccesses::default(),
        provider_local_birth_candidate,
        body_effect_bindings: if body_effect_mode {
            local_body?
                .bindings
                .iter()
                .filter_map(|(binding, kind)| {
                    matches!(
                        kind,
                        LocalBinding::Parameter | LocalBinding::Let | LocalBinding::Const
                    )
                    .then_some(binding.clone())
                })
                .collect()
        } else {
            BTreeSet::new()
        },
    };
    let paths = if let Some(expression) = body.body_facts.value_flow().direct_allocation_return()
        && !closure_mode
        && !body_effect_mode
    {
        let value = match allocation_transfer::transfer(&mut context, expression) {
            ChildTransfer::Value(value) => value,
            ChildTransfer::BeforeExecution | ChildTransfer::RemainderAfterPrefix { .. } => {
                return None;
            }
        };
        let allocation_transfer::AllocationValue { ledger } = value;
        // An isolated value expression is not a whole-body completion proof.
        // Preserve possible-normal values but force its completion to remain
        // opaque; an exact abrupt result has no successful return capability.
        let (selected, abrupt, mut non_normal) = ledger.completion.into_alternatives();
        let selected = selected?;
        let abrupt = abrupt
            .into_iter()
            .map(|alternative| match alternative {
                AbruptAlternative::Exact(()) => {
                    AbruptAlternative::Unknown(Some(UnknownThrowValues::new(Values::from([
                        AbstractValue::Value(crate::facts::abstract_domain::value::Value::Top),
                    ]))))
                }
                AbruptAlternative::Unknown(values) => AbruptAlternative::Unknown(values),
            })
            .collect();
        non_normal.may_diverge = true;
        non_normal.unresolved = true;
        let completion: Completion<SelectedValue, Box<KnownThrow>> =
            Completion::from_alternatives(Some(SelectedValue::raw(selected)), abrupt, non_normal);
        let ledger = TransferLedger::new(
            completion,
            ledger.effects,
            ledger.dependencies,
            ledger.allocation,
        );
        vec![(context, None, ledger)]
    } else if conditional_body {
        let mut frame = LocalFrame::new(std::mem::take(&mut context.bindings));
        frame.parameter_inputs = Some(parameter_inputs);
        frame.initialized_bindings.extend(captures.iter().cloned());
        execute_provider_continuation_family(context, local_body?, frame)?
            .into_iter()
            .map(|(context, frame, result)| {
                let TransferLedger {
                    completion,
                    effects,
                    dependencies,
                    allocation,
                } = result.ledger;
                let completion = completion.map_normal(|normal| match normal {
                    LocalNormal::Next => {
                        SelectedValue::raw(Values::from([AbstractValue::UNDEFINED]))
                    }
                    LocalNormal::Return { selected, .. } => *selected,
                    LocalNormal::Unknown(selected) => selected,
                });
                (
                    context,
                    Some(frame),
                    TransferLedger::new(completion, effects, dependencies, allocation),
                )
            })
            .collect()
    } else {
        let mut frame = LocalFrame::new(std::mem::take(&mut context.bindings));
        if body_effect_mode {
            frame.parameter_inputs = Some(parameter_inputs);
            frame.initialized_bindings.extend(captures.iter().cloned());
        }
        let transfer = completed_provider_transfer(local_transfer::execute(
            &mut context,
            local_body?,
            &mut frame,
        ))?;
        let TransferLedger {
            completion,
            effects,
            dependencies,
            allocation,
        } = transfer.ledger;
        let completion = completion.map_normal(|normal| match normal {
            LocalNormal::Next => SelectedValue::raw(Values::from([AbstractValue::UNDEFINED])),
            LocalNormal::Return { selected, .. } => *selected,
            LocalNormal::Unknown(selected) => selected,
        });
        let ledger = TransferLedger::new(completion, effects, dependencies, allocation);
        vec![(context, Some(frame), ledger)]
    };
    let mut executions = Vec::with_capacity(paths.len());
    for (mut context, frame, mut ledger) in paths {
        ledger.depend_on(&capture_dependencies);
        if let Some(frame) = frame {
            if body_effect_mode
                && provider_local_birth_candidate
                && ledger.is_exact_normal()
                && matches!(
                    &ledger.allocation,
                    AllocationJudgment::CoveredAllocationOnly(_)
                )
                && ledger.may_normal().is_some_and(|selected| {
                    !selected.values.is_empty()
                        && !selected
                            .values
                            .contains(&AbstractValue::Value(Value::Unavailable))
                        && !selected.values.contains(&AbstractValue::Value(Value::Top))
                })
            {
                // Sealing relies on the same exact-normal, complete effect
                // transfer consumed by callers; local birth membership alone
                // is not a body-level no-publication proof.
                let _ = ledger.seal_provider_local_birth_support(
                    &frame.fresh_records,
                    &frame.borrowed_records,
                );
            }
            context
                .private_heap
                .observe_frame(&frame, context.index.heap_resolver)
                .ok()?;
            context.final_environment = Some(frame.environment.clone());
        }
        // This isolated provider query has no retained-native-reference export
        // or visitor. Refuse the whole family rather than publishing a fresh
        // result after dropping references from any branch's internal slots.
        if !context.native_retained_references.is_empty() {
            return None;
        }
        let private_heap = context
            .private_heap
            .seal(&context, &ledger, captures.len())
            .ok()?;
        if private_heap.retained_graph.is_some() {
            ledger.allocation = AllocationJudgment::Opaque;
        }
        executions.push(ProviderExecution {
            private_heap_proof: private_heap,
            context,
            scope,
            captures: captures.clone(),
            ledger,
            capture_dependencies: capture_dependencies.clone(),
        });
    }
    (!executions.is_empty()).then_some(executions)
}

fn execute_provider_body<'a>(
    index: &'a ExecutionIndex,
    callable: u32,
    shadowed: &BTreeSet<&'static str>,
    input_shape: Option<RecordInputShape>,
    module_cells: &BTreeMap<Id, (ModuleCellId, PrimitiveCellInvariant)>,
) -> Option<ProviderExecution<'a>> {
    let mut executions =
        execute_provider_body_family(index, callable, shadowed, input_shape, module_cells)?;
    if executions.len() != 1 {
        return None;
    }
    executions.pop()
}

struct ProviderExecution<'a> {
    private_heap_proof: SealedPrivateHeap,
    context: ProviderContext<'a>,
    scope: ProviderScopeId,
    captures: Vec<Id>,
    ledger: TransferLedger<SelectedValue, Box<super::super::local_transfer::KnownThrow>>,
    capture_dependencies: ProofDependencies,
}

fn conditional_result_projection(
    values: &Values,
    scope: ProviderScopeId,
    input: &RecordInputShape,
    reads: &BTreeSet<ParameterHeapRead>,
) -> Option<ConditionalResultProjection> {
    if values.is_empty() {
        return None;
    }
    let mut alternatives = BTreeSet::new();
    for value in values {
        let alternative = match value {
            AbstractValue::SymbolicInput(symbolic) if symbolic.scope == scope => {
                match symbolic.input {
                    ProviderInput::Argument(0) => {
                        ConditionalResultAlternative::Argument { index: 0 }
                    }
                    ProviderInput::Property {
                        argument: 0,
                        property,
                    } => {
                        let (name, kind) = input.properties.iter().nth(property)?;
                        if !matches!(kind, RecordInputProperty::Data)
                            || !reads.contains(&ParameterHeapRead::ObjectShape { argument: 0 })
                            || !reads.contains(&ParameterHeapRead::OwnProperty {
                                argument: 0,
                                property: name.clone(),
                            })
                        {
                            return None;
                        }
                        ConditionalResultAlternative::Property {
                            argument: 0,
                            property: name.clone(),
                        }
                    }
                    _ => return None,
                }
            }
            AbstractValue::Value(Value::Prim(kinds))
                if !kinds.is_empty() && kinds.without(PrimKinds::ANY).is_empty() =>
            {
                ConditionalResultAlternative::Primitive { kinds: *kinds }
            }
            _ => return None,
        };
        alternatives.insert(alternative);
    }
    let projection = ConditionalResultProjection { alternatives };
    projection.is_well_formed().then_some(projection)
}

fn conditional_provider_alternative(
    execution: &ProviderExecution<'_>,
    input: &RecordInputShape,
) -> Option<crate::analysis_inputs::ConditionalProviderAlternative> {
    use crate::analysis_inputs::{ConditionalProviderCompletion, ConditionalProviderValue};

    let effects = &execution.ledger.effects;
    // Provider-local shapes have no checked birth/escape correspondence to
    // the caller, so this conditional body cannot discharge them.
    if effects.has_unresolved_provider_operation_support() {
        return None;
    }
    let mut dependencies = if execution.ledger.is_exact_normal() {
        execution.ledger.closed_effect_dependencies()?
    } else if execution.ledger.is_exact_abrupt() {
        execution.ledger.known_abrupt_dependencies()?
    } else {
        // Every feasible leaf must keep its own represented completion; an
        // opaque, divergent, or witnessless leaf is not serialized as an
        // exact provider alternative.
        return None;
    };
    dependencies.extend(&execution.capture_dependencies);
    if let AllocationJudgment::CoveredAllocationOnly(allocation_dependencies) =
        &execution.ledger.allocation
    {
        dependencies.extend(allocation_dependencies);
    }

    let (selected, throw_origin) = match &execution.ledger.completion {
        Completion::Normal(selected) => (selected, None),
        Completion::Abrupt(thrown) => {
            if thrown.origin.scope != execution.context.index.heap_resolver.scope() {
                return None;
            }
            (&thrown.value, Some(thrown.origin.site.clone()))
        }
        _ => return None,
    };
    let value = if let Some(graph) = &execution.private_heap_proof.retained_graph {
        ConditionalProviderValue::RetainedGraph(graph.clone())
    } else {
        ConditionalProviderValue::Projection(conditional_result_projection(
            &selected.values,
            execution.scope,
            input,
            &execution.context.parameter_reads,
        )?)
    };
    let completion = if let Some(origin) = throw_origin {
        ConditionalProviderCompletion::Throw { origin, value }
    } else {
        ConditionalProviderCompletion::Normal { value }
    };

    Some(crate::analysis_inputs::ConditionalProviderAlternative {
        completion,
        reads: execution.context.parameter_reads.clone(),
        native_operations: execution.context.native_operations.clone(),
        effects: effects.clone(),
        module_cells: execution.context.module_cell_accesses.clone(),
        dependencies,
    })
}

pub(super) fn derive_conditional_body(
    index: &ExecutionIndex,
    callable: u32,
    shadowed: &BTreeSet<&'static str>,
    input: RecordInputShape,
    module_cells: &BTreeMap<Id, (ModuleCellId, PrimitiveCellInvariant)>,
) -> Option<crate::facts::abstract_domain::foreign_call::DerivedConditionalBody> {
    let body = index.callables.get(&callable)?;
    if body.params.len() != 1 {
        return None;
    }
    let executions =
        execute_provider_body_family(index, callable, shadowed, Some(input.clone()), module_cells)?;
    let mut alternatives = Vec::with_capacity(executions.len());
    for execution in executions {
        // Provider module cells need a qualified footprint, independent of
        // their initialization/value proof. This query only instantiates
        // parameter cells.
        if execution
            .captures
            .iter()
            .any(|binding| !module_cells.contains_key(binding))
        {
            return None;
        }
        alternatives.push(conditional_provider_alternative(&execution, &input)?);
    }

    (!alternatives.is_empty()).then_some(
        crate::facts::abstract_domain::foreign_call::DerivedConditionalBody {
            input,
            alternatives,
        },
    )
}

/// Nonserialized correspondence produced by the current canonical program's
/// shared transfer. Retained edges alone cannot construct this capability.
#[derive(Clone)]
pub(super) struct VerifiedProviderGraph {
    pub(super) graph: crate::analysis_inputs::ProviderRetainedGraph,
    pub(super) source: super::provider_program::ProviderProgramSource,
    pub(super) callable_origin: u32,
    pub(super) slots: Vec<VerifiedProviderSlot>,
    pub(super) dependencies: ProofDependencies,
}

/// One checked, correlated provider leaf. `completion` is source-reexecuted;
/// a graph is present only when this same leaf's payload is a retained graph.
#[derive(Clone)]
pub(super) struct VerifiedProviderAlternative {
    pub(super) completion: crate::analysis_inputs::ConditionalProviderCompletion,
    pub(super) dependencies: ProofDependencies,
    pub(super) graph: Option<VerifiedProviderGraph>,
}

/// A persisted throw site is accepted only after its source-relative identity
/// resolves to the exact canonical operation site in this prepared provider.
/// Full `SourceOperationSite` membership checks the owner, body, span, ordinal,
/// step, and linked control-test identity together.
fn provider_throw_site_is_current(
    program: &super::provider_program::ProviderProgram,
    identity: &crate::analysis_inputs::ConditionalProviderIdentity,
    callable: u32,
    site: &SourceOperationSite,
) -> bool {
    let Some(source) = identity.source.as_ref() else {
        return false;
    };
    let Some(body) = program.executable.index.callables.get(&callable) else {
        return false;
    };
    let Some(callable_span) = program.executable.index.callable_spans.get(&callable) else {
        return false;
    };
    let canonical_body = LocalBodyIdentity::Callable {
        key: callable,
        owner: body.owner,
    };
    let Some(site_start) = site.span.lo.0.checked_sub(source.source_start) else {
        return false;
    };
    let Some(site_end) = site.span.hi.0.checked_sub(source.source_start) else {
        return false;
    };
    site.owner == body.owner
        && site.body == canonical_body
        && site.step == OperationStep::Throw
        && site.span.lo.0 >= callable_span.lo.0
        && site.span.hi.0 <= callable_span.hi.0
        && site_start < site_end
        && source.callable_start <= site_start
        && site_end <= source.callable_end
        && program
            .executable
            .index
            .contains_source_operation_site(site)
}

#[derive(Clone)]
pub(super) enum VerifiedProviderSlot {
    Heap {
        source_origin: u32,
        kind: Kind,
        properties: Option<Box<VerifiedProviderProperties>>,
    },
    Closure {
        source_origin: u32,
        bindings: BTreeMap<Id, usize>,
    },
    LocalCell {
        kind: LocalBinding,
        source_origin: u32,
    },
    ModuleCell,
}

#[derive(Clone)]
pub(super) struct VerifiedProviderProperties {
    pub(super) layout: super::super::object_surface::OwnPropertyLayout,
    pub(super) dependencies: ProofDependencies,
}

/// Reuse the provider adapter and shared continuation kernel; do not interpret
/// serialized graph nodes as executable syntax or birth evidence. Re-execute
/// and match every sibling before returning even one leaf, so a saved normal
/// payload cannot be paired with a throw world's graph, reads, or effects.
pub(super) fn verify_provider_family(
    program: &super::provider_program::ProviderProgram,
    transfer: &crate::analysis_inputs::ConditionalProviderTransfer,
) -> Option<Vec<VerifiedProviderAlternative>> {
    use crate::analysis_inputs::{
        ConditionalProviderAlternative, ConditionalProviderCompletion, ConditionalProviderValue,
    };

    if !transfer.is_well_formed() {
        return None;
    }
    let callable = program.resolve_current_root(&transfer.identity)?;

    if program.source.scope != program.executable.index.heap_resolver.scope()
        || transfer.alternatives.iter().any(|alternative| {
            matches!(
                &alternative.completion,
                ConditionalProviderCompletion::Throw { origin, .. }
                    if !provider_throw_site_is_current(
                        program,
                        &transfer.identity,
                        callable,
                        origin,
                    )
            )
        })
    {
        return None;
    }

    let normalize_graph = |graph: &crate::analysis_inputs::ProviderRetainedGraph| {
        let mut graph = graph.clone();
        for node in &mut graph.nodes {
            if let crate::analysis_inputs::ProviderGraphNodeKind::Closure { source_key } =
                &mut node.kind
            {
                *source_key = program.resolve_saved_callable(&transfer.identity, *source_key)?;
            }
        }
        Some(graph)
    };
    let normalize_alternative = |alternative: &ConditionalProviderAlternative| {
        let mut alternative = alternative.clone();
        let payload = match &mut alternative.completion {
            ConditionalProviderCompletion::Normal { value }
            | ConditionalProviderCompletion::Throw { value, .. } => value,
        };
        if let ConditionalProviderValue::RetainedGraph(graph) = payload {
            *graph = normalize_graph(graph)?;
        }
        Some(alternative)
    };
    let expected = transfer
        .alternatives
        .iter()
        .map(normalize_alternative)
        .collect::<Option<Vec<_>>>()?;

    let mut module_cells = BTreeMap::new();
    let mut expected_cell_invariants = BTreeMap::new();
    for alternative in &transfer.alternatives {
        for (cell, expected) in &alternative.module_cells.invariants {
            if expected_cell_invariants
                .insert(*cell, expected.clone())
                .is_some_and(|previous| previous != *expected)
            {
                return None;
            }
        }
    }
    for (binding, cell) in &program.module_cells {
        if let Some(expected) = expected_cell_invariants.get(cell) {
            let invariant = program.primitive_cell_invariant(binding)?;
            if &invariant != expected {
                return None;
            }
            module_cells.insert(binding.clone(), (*cell, invariant));
        }
    }
    if module_cells.len() != expected_cell_invariants.len() {
        return None;
    }

    let executions = execute_provider_body_family(
        &program.executable.index,
        callable,
        &program.executable.shadowed,
        Some(transfer.input.clone()),
        &module_cells,
    )?;
    if executions.len() != expected.len() {
        return None;
    }

    let mut actual = executions
        .into_iter()
        .map(|execution| {
            if execution
                .captures
                .iter()
                .any(|binding| !module_cells.contains_key(binding))
            {
                return None;
            }
            let alternative = conditional_provider_alternative(&execution, &transfer.input)?;
            Some((alternative, execution))
        })
        .collect::<Option<Vec<_>>>()?
        .into_iter()
        .map(Some)
        .collect::<Vec<_>>();
    let mut matched = Vec::with_capacity(expected.len());
    for expected_alternative in &expected {
        let position = actual.iter().position(|candidate| {
            candidate
                .as_ref()
                .is_some_and(|(alternative, _)| alternative == expected_alternative)
        })?;
        matched.push(actual[position].take()?);
    }
    if actual.iter().any(Option::is_some) {
        return None;
    }
    matched
        .into_iter()
        .zip(expected)
        .map(|((actual_alternative, execution), expected_alternative)| {
            // The whole-family equality above is the source correspondence
            // check. Keep this leaf's graph and throw origin attached to the
            // same completion and state that produced them.
            if actual_alternative != expected_alternative {
                return None;
            }
            let graph = match &expected_alternative.completion {
                ConditionalProviderCompletion::Normal {
                    value: ConditionalProviderValue::RetainedGraph(graph),
                }
                | ConditionalProviderCompletion::Throw {
                    value: ConditionalProviderValue::RetainedGraph(graph),
                    ..
                } => {
                    if execution.private_heap_proof.retained_graph.as_ref() != Some(graph) {
                        return None;
                    }
                    let selected = match &execution.ledger.completion {
                        Completion::Normal(selected) => selected,
                        Completion::Abrupt(thrown) => &thrown.value,
                        _ => return None,
                    };
                    let slots = execution.private_heap_proof.verified_slots(
                        &execution.context,
                        callable,
                        selected,
                        program,
                    )?;
                    let mut graph_dependencies = expected_alternative.dependencies.clone();
                    for slot in &slots {
                        if let VerifiedProviderSlot::Heap {
                            properties: Some(properties),
                            ..
                        } = slot
                        {
                            graph_dependencies.extend(&properties.dependencies);
                        }
                    }
                    Some(VerifiedProviderGraph {
                        graph: graph.clone(),
                        source: program.source.clone(),
                        callable_origin: program.source.source_offset(callable)?,
                        slots,
                        dependencies: graph_dependencies,
                    })
                }
                _ => None,
            };
            Some(VerifiedProviderAlternative {
                completion: expected_alternative.completion,
                dependencies: expected_alternative.dependencies,
                graph,
            })
        })
        .collect()
}

pub(super) fn derive(
    index: &ExecutionIndex,
    callable: u32,
    shadowed: &BTreeSet<&'static str>,
) -> Option<DerivedForeignCall> {
    let body = index.callables.get(&callable)?;
    let ProviderExecution {
        private_heap_proof: _,
        context,
        scope,
        captures,
        ledger,
        capture_dependencies,
    } = execute_provider_body(index, callable, shadowed, None, &BTreeMap::new())?;
    let AllocationJudgment::CoveredAllocationOnly(allocation_dependencies) = &ledger.allocation
    else {
        return None;
    };
    let result = ledger.completion.may_normal()?.clone();
    let mut dependencies = ledger.dependencies.as_ref().clone();
    dependencies.extend(allocation_dependencies);
    // Kind facts can affect returned graph retention, including deferred
    // closure captures. Guard both projections even when no read runs now.
    dependencies.extend(&capture_dependencies);
    let body_effects = if context.body_effect_mode && ledger.is_exact_normal() {
        let mut body_ledger = ledger.clone();
        body_ledger.depend_on(&dependencies);
        body_ledger
            .closed_effect_dependencies()
            .map(|effect_dependencies| {
                dependencies.extend(&effect_dependencies);
                crate::facts::abstract_domain::foreign_call::DerivedBodyEffects {
                    completion: Completion::Normal(()),
                    effects: body_ledger.effects,
                }
            })
    } else {
        None
    };
    // Native operation identity is conditional on the exact census proof;
    // transport every consumed row dependency to the summary's importer.
    let mut returns = BTreeSet::new();
    let mut retained_arguments = BTreeSet::new();
    let mut retained_provider_captures = BTreeSet::new();
    let closure = match result.values.iter().collect::<Vec<_>>().as_slice() {
        [AbstractValue::Value(Value::Alloc(Kind::Function, callable))] => {
            let callable = index.heap_resolver.source_origin(*callable)?;
            let span = index.callable_spans.get(&callable)?;
            Some(span.lo.0..span.hi.0)
        }
        _ => None,
    };
    let record = if body.params.is_empty() {
        match result.values.iter().collect::<Vec<_>>().as_slice() {
            [AbstractValue::Value(Value::Alloc(Kind::Object, allocation))] => {
                let allocation = index.heap_resolver.source_origin(*allocation)?;
                index.method_record_members.get(&allocation).cloned()
            }
            _ => None,
        }
    } else {
        None
    };
    for value in result.values {
        match value {
            AbstractValue::SymbolicInput(input) if input.scope == scope => {
                let ProviderInput::Argument(index) = input.input else {
                    // A capture itself is not a fresh return allocation. Its
                    // exact borrowed result contract has not been modeled.
                    return None;
                };
                returns.insert(ForeignReturn::Argument { index });
            }
            AbstractValue::Value(Value::Prim(_)) => {
                returns.insert(ForeignReturn::Primitive);
            }
            AbstractValue::Value(Value::Alloc(Kind::RegExp, _)) => {
                // Regex literals are fresh opaque allocations here: their
                // prototype, internal slots, methods, and lastIndex stay
                // outside the provider's retention projection.
                returns.insert(ForeignReturn::FreshAllocation);
            }
            AbstractValue::Value(Value::Alloc(kind, _)) if kind.is_keyed_collection() => {
                // Empty native construction has no user argument references
                // to traverse. Preserve its typed identity as a fresh leaf.
                returns.insert(ForeignReturn::FreshAllocation);
            }
            AbstractValue::Value(Value::Alloc(Kind::Object, allocation)) => {
                returns.insert(ForeignReturn::FreshAllocation);
                retain_provider_values(
                    &context,
                    scope,
                    &captures,
                    context.retained_object_values(allocation)?,
                    &mut retained_arguments,
                    &mut retained_provider_captures,
                )?;
            }
            AbstractValue::OwnDescriptorMap(site) => {
                returns.insert(ForeignReturn::FreshAllocation);
                retain_provider_values(
                    &context,
                    scope,
                    &captures,
                    [AbstractValue::OwnDescriptorMap(site)],
                    &mut retained_arguments,
                    &mut retained_provider_captures,
                )?;
            }
            AbstractValue::Value(Value::Alloc(Kind::Function, callable)) => {
                returns.insert(ForeignReturn::FreshAllocation);
                retain_provider_values(
                    &context,
                    scope,
                    &captures,
                    [AbstractValue::Value(Value::Alloc(Kind::Function, callable))],
                    &mut retained_arguments,
                    &mut retained_provider_captures,
                )?;
            }
            _ => return None,
        }
    }
    Some(DerivedForeignCall {
        invocation: ForeignInvocation::Call,
        returns,
        retained_arguments,
        retained_provider_captures,
        native_operations: context.native_operations,
        native_derivation_rules: context.native_derivation_rules,
        dependencies,
        body_effects,
        record,
        closure,
    })
}

/// Derive the constructor result with the same ordered allocation operations
/// used by ordinary local execution. Class syntax admission is collected once
/// in `ExecutionIndex`; a refused query never retries the private AST evaluator.
pub(super) fn derive_class(
    index: &ExecutionIndex,
    class: u32,
    shadowed: &BTreeSet<&'static str>,
) -> Option<DerivedForeignCall> {
    let body = index.class_provider_bodies.get(&class)?;
    // Use the same source-body call inventory as application constructor
    // execution. This includes actual instance-field initializers and the
    // constructor body, while excluding static initialization and methods.
    let (calls, _call_sites) = index.class_construction_call_inventory(class)?;
    let local_bindings: BTreeSet<_> = body.constructor.bindings.keys().cloned().collect();
    if body
        .initializers
        .free_reads()
        .iter()
        .chain(body.constructor.free_reads())
        .any(|binding| {
            !matches!(binding.0.as_ref(), "Map" | "Set" | "WeakMap" | "WeakSet")
                || shadowed.contains(binding.0.as_ref())
                || local_bindings.iter().any(|local| local.0 == binding.0)
        })
        || !body.initializers.captured_writes().is_empty()
        || !body.constructor.captured_writes().is_empty()
    {
        return None;
    }
    let scope = ProviderScopeId::fresh()?;
    let owner = *index.class_owners.get(&class)?;
    let caller = LocalBodyIdentity::Construction { class, owner };
    if [&body.initializers, &body.constructor]
        .into_iter()
        .filter_map(|body| body.source_region_site.as_ref())
        .any(|site| site.body != caller)
    {
        return None;
    }
    let mut parameter_inputs = Vec::new();
    let mut argument_values = Values::new();
    for (parameter, _) in body.constructor.parameters.iter().enumerate() {
        let value = AbstractValue::SymbolicInput(SymbolicInputId {
            scope,
            input: ProviderInput::Argument(parameter),
        });
        parameter_inputs.push(SelectedValue::raw(Values::from([value.clone()])));
        argument_values.insert(value);
    }
    let mut context = ProviderContext {
        index,
        caller: Some(caller),
        calls: &calls,
        scope,
        bindings: Context::new(),
        argument_values,
        closure_captures: BTreeMap::new(),
        closure_local_reads: BTreeMap::new(),
        closure_provider_reads: BTreeMap::new(),
        unsupported_closures: BTreeSet::new(),
        final_environment: None,
        class_mode: true,
        local_bindings,
        shadowed: shadowed.clone(),
        native_operations: BTreeSet::new(),
        native_derivation_rules: BTreeSet::new(),
        surface: ObjectSurfaceStore::default(),
        private_heap: PrivateHeapEvidence::default(),
        private_properties: OrdMap::new(),
        arrays: OrdMap::new(),
        native_retained_references: OrdMap::new(),
        descriptor_maps: OrdMap::new(),
        revision: 0,
        input_shape: None,
        parameter_reads: BTreeSet::new(),
        body_effect_mode: true,
        cell_transfer_mode: false,
        cell_transfer_bindings: BTreeSet::new(),
        module_cells: BTreeMap::new(),
        module_cell_accesses: crate::analysis_inputs::ModuleCellAccesses::default(),
        provider_local_birth_candidate: false,
        body_effect_bindings: body
            .constructor
            .bindings
            .iter()
            .filter_map(|(binding, kind)| {
                matches!(
                    kind,
                    LocalBinding::Parameter | LocalBinding::Let | LocalBinding::Const
                )
                .then_some(binding.clone())
            })
            .collect(),
    };
    let mut frame = LocalFrame::constructor(body, index.heap_resolver);
    let transfer = completed_provider_transfer(local_transfer::execute_constructor(
        &mut context,
        body,
        parameter_inputs,
        &mut frame,
    )?)?;
    if !transfer.ledger.is_exact_normal() {
        return None;
    }
    let constructor_return_values = match transfer.ledger.completion.may_normal()? {
        LocalNormal::Next => Values::from([AbstractValue::UNDEFINED]),
        LocalNormal::Return { selected, .. } => selected.values.clone(),
        LocalNormal::Unknown(_) => return None,
    };
    if !constructor_return_values
        .iter()
        .all(|value| matches!(value, AbstractValue::Value(Value::Prim(_))))
    {
        return None;
    }
    let AllocationJudgment::CoveredAllocationOnly(allocation_dependencies) =
        &transfer.ledger.allocation
    else {
        return None;
    };
    let mut dependencies = transfer.ledger.dependencies.as_ref().clone();
    dependencies.extend(allocation_dependencies);
    let mut body_ledger = transfer.ledger.clone();
    body_ledger.depend_on(&dependencies);
    let body_effects = body_ledger
        .closed_effect_dependencies()
        .map(|effect_dependencies| {
            dependencies.extend(&effect_dependencies);
            crate::facts::abstract_domain::foreign_call::DerivedBodyEffects {
                completion: Completion::Normal(()),
                effects: body_ledger.effects,
            }
        });
    let mut retained_arguments = BTreeSet::new();
    let class_id = index.heap_resolver.class_definition(class);
    let class_instances = index.heap_resolver.constructor_instances(class_id);
    let mut pending_values = vec![AbstractValue::Value(Value::Alloc(
        Kind::Object,
        class_instances,
    ))];
    let mut visited_objects = BTreeSet::<HeapAddress>::new();
    let mut visited_closures = BTreeSet::<HeapAddress>::new();
    while let Some(value) = pending_values.pop() {
        match value {
            AbstractValue::SymbolicInput(input) if input.scope == scope => match input.input {
                ProviderInput::Argument(index) => {
                    retained_arguments.insert(index);
                }
                ProviderInput::Capture(_) => return None,
                ProviderInput::Property { .. }
                | ProviderInput::Getter { .. }
                | ProviderInput::Setter { .. } => return None,
            },
            AbstractValue::Value(Value::Alloc(Kind::Object, allocation)) => {
                if !visited_objects.insert(allocation) {
                    continue;
                }
                pending_values.extend(context.surface.retained_values(allocation));
                if let Some(properties) = context.private_properties.get(&allocation) {
                    pending_values.extend(properties.values().flatten().cloned());
                }
            }
            AbstractValue::Value(Value::Alloc(Kind::Function, callable_address)) => {
                if visited_closures.insert(callable_address) {
                    let callable = index.heap_resolver.source_origin(callable_address)?;
                    pending_values.extend(context.closure_captures.get(&callable)?.iter().cloned());
                }
            }
            // The shared keyed-collection constructor transfer admits only
            // empty, zero-argument constructors here. Their native storage
            // has no user argument references to traverse; keep their typed
            // identity as a leaf rather than treating them as records.
            AbstractValue::Value(Value::Alloc(kind, _)) if kind.is_keyed_collection() => {}
            AbstractValue::Value(Value::Prim(_)) => {}
            _ => return None,
        }
    }
    Some(DerivedForeignCall {
        invocation: ForeignInvocation::Construct,
        returns: BTreeSet::from([ForeignReturn::FreshAllocation]),
        retained_arguments,
        retained_provider_captures: BTreeSet::new(),
        native_operations: context.native_operations,
        native_derivation_rules: context.native_derivation_rules,
        dependencies,
        body_effects,
        record: None,
        closure: None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::facts::StableArraySpreadShapes;
    use crate::facts::item_views::top_level_item_views;
    use crate::facts::sync_effects::property_inference::DescriptorAttributes;
    use swc_ecma_ast::{Decl, Expr, FnDecl, ModuleItem, Stmt};
    use swc_ecma_visit::{Visit, VisitWith};

    fn body_effects_closed(
        effects: &crate::facts::abstract_domain::foreign_call::DerivedBodyEffects,
    ) -> bool {
        effects.completion.is_exact_normal() && effects.effects.has_closed_domains()
    }

    #[derive(Default)]
    struct MakeFunctionKey(Option<u32>);

    impl Visit for MakeFunctionKey {
        fn visit_fn_decl(&mut self, function: &FnDecl) {
            if function.ident.sym == "make" {
                self.0 = Some(function.function.span.lo.0);
            }
            function.visit_children_with(self);
        }
    }

    fn query(source: &str, syntax_covered: bool) -> Option<DerivedForeignCall> {
        let module = js_ast::parse_js_module("provider.js", source)
            .unwrap()
            .module;
        let key = match &module.body[0] {
            ModuleItem::Stmt(Stmt::Decl(Decl::Fn(function))) => function.function.span.lo.0,
            ModuleItem::Stmt(Stmt::Decl(Decl::Var(variable))) => {
                let Expr::Arrow(arrow) = variable.decls[0].init.as_deref().unwrap() else {
                    panic!("arrow fixture");
                };
                arrow.span.lo.0
            }
            _ => panic!("provider fixture"),
        };
        let views = top_level_item_views(&module.body);
        let prepared = super::super::prepare_callable_capabilities(
            &views,
            StableArraySpreadShapes::default(),
            BTreeSet::from(["make".into()]),
            Default::default(),
        );
        assert_eq!(
            prepared.index.callables[&key]
                .body_facts
                .value_flow()
                .direct_allocation_return()
                .is_some(),
            syntax_covered,
            "{source}"
        );
        derive(&prepared.index, key, &BTreeSet::new())
    }

    fn derive_source(source: &str) -> Option<DerivedForeignCall> {
        let module = js_ast::parse_js_module("provider.js", source)
            .unwrap()
            .module;
        let key = match &module.body[0] {
            ModuleItem::Stmt(Stmt::Decl(Decl::Fn(function))) => function.function.span.lo.0,
            ModuleItem::Stmt(Stmt::Decl(Decl::Class(class))) => class.class.span.lo.0,
            ModuleItem::Stmt(Stmt::Decl(Decl::Var(variable))) => {
                let Expr::Arrow(arrow) = variable.decls[0].init.as_deref().unwrap() else {
                    panic!("arrow fixture");
                };
                arrow.span.lo.0
            }
            _ => panic!("provider fixture"),
        };
        let views = top_level_item_views(&module.body);
        let mut prepared = super::super::prepare_callable_capabilities(
            &views,
            StableArraySpreadShapes::default(),
            BTreeSet::from(["make".into()]),
            Default::default(),
        );
        let integrity = super::super::resolve_native_integrity([prepared.native_writes()]);
        prepared.index.apply_native_integrity(&integrity);
        if matches!(
            &module.body[0],
            ModuleItem::Stmt(Stmt::Decl(Decl::Class(_)))
        ) {
            derive_class(&prepared.index, key, &BTreeSet::new())
        } else {
            derive(&prepared.index, key, &BTreeSet::new())
        }
    }

    #[test]
    fn conditional_body_keeps_normal_and_throw_provider_leaves_correlated() {
        js_ast::with_swc_globals(|| {
            let source =
                "export function make(input) { if (input.flag) return input.yes; throw input.no; }";
            let module = js_ast::parse_js_module("provider.js", source)
                .unwrap()
                .module;
            let views = top_level_item_views(&module.body);
            let prepared = super::super::prepare_callable_capabilities(
                &views,
                StableArraySpreadShapes::default(),
                BTreeSet::from(["make".into()]),
                Default::default(),
            );
            let key = prepared
                .index
                .hoisted_bindings
                .iter()
                .find_map(|(binding, key)| (binding.0.as_ref() == "make").then_some(*key))
                .unwrap();
            let input = RecordInputShape {
                properties: ["flag", "yes", "no"]
                    .into_iter()
                    .map(|property| (property.into(), RecordInputProperty::Data))
                    .collect(),
                creation_order: ["flag", "yes", "no"]
                    .map(|property| property.to_owned())
                    .to_vec(),
            };
            let body = derive_conditional_body(
                &prepared.index,
                key,
                &prepared.shadowed,
                input,
                &BTreeMap::new(),
            )
            .expect("both exact branch completions have source-backed projections");

            assert_eq!(body.alternatives.len(), 2, "one leaf per feasible branch");
            let normal = body
                .alternatives
                .iter()
                .find(|alternative| {
                    matches!(
                        &alternative.completion,
                        crate::analysis_inputs::ConditionalProviderCompletion::Normal { .. }
                    )
                })
                .expect("normal result branch");
            let thrown = body
                .alternatives
                .iter()
                .find(|alternative| {
                    matches!(
                        &alternative.completion,
                        crate::analysis_inputs::ConditionalProviderCompletion::Throw { .. }
                    )
                })
                .expect("exact throw branch");
            assert!(normal.reads.contains(&ParameterHeapRead::OwnProperty {
                argument: 0,
                property: "yes".into(),
            }));
            assert!(!normal.reads.contains(&ParameterHeapRead::OwnProperty {
                argument: 0,
                property: "no".into(),
            }));
            assert!(thrown.reads.contains(&ParameterHeapRead::OwnProperty {
                argument: 0,
                property: "no".into(),
            }));
            assert!(!thrown.reads.contains(&ParameterHeapRead::OwnProperty {
                argument: 0,
                property: "yes".into(),
            }));
            assert!(matches!(
                &thrown.completion,
                crate::analysis_inputs::ConditionalProviderCompletion::Throw { origin, .. }
                    if origin.step == OperationStep::Throw
                        && prepared.index.contains_source_operation_site(origin)
            ));
            let crate::analysis_inputs::ConditionalProviderCompletion::Throw { origin, .. } =
                &thrown.completion
            else {
                unreachable!("the checked provider leaf is abrupt")
            };
            let mut wrong_ordinal = origin.clone();
            wrong_ordinal.ordinal = wrong_ordinal.ordinal.checked_add(1).unwrap();
            assert!(
                !prepared
                    .index
                    .contains_source_operation_site(&wrong_ordinal),
                "a throw site with a substituted ordinal is not current source evidence"
            );
        });
    }

    #[test]
    fn empty_constructor_transports_actual_closed_body_effects() {
        js_ast::with_swc_globals(|| {
            for source in [
                "class make {}",
                "class make { constructor() {} }",
                "class make { constructor() { return; } }",
                "class make { constructor() { return 1; } }",
            ] {
                let summary = derive_source(source).unwrap_or_else(|| panic!("{source}"));
                assert!(
                    summary
                        .body_effects
                        .as_ref()
                        .is_some_and(body_effects_closed),
                    "{source}: {summary:#?}"
                );
                assert_eq!(
                    summary.returns,
                    BTreeSet::from([ForeignReturn::FreshAllocation]),
                    "{source}"
                );
                assert!(summary.retained_arguments.is_empty(), "{source}");
            }
        });
    }

    #[test]
    fn constructor_field_and_parameter_operations_close_with_retention() {
        js_ast::with_swc_globals(|| {
            for (source, retained) in [
                ("class make { field = 1; }", BTreeSet::new()),
                ("class make { field; }", BTreeSet::new()),
                ("class make { #field = 1; }", BTreeSet::new()),
                ("class make { #field; }", BTreeSet::new()),
                ("class make { field = this; }", BTreeSet::new()),
                (
                    "class make { field; constructor() { this.field = this; } }",
                    BTreeSet::new(),
                ),
                ("class make { constructor(value) {} }", BTreeSet::new()),
                (
                    "class make { #field; constructor(value) { this.#field = value; } }",
                    BTreeSet::from([0]),
                ),
                (
                    "class make { constructor() { const value = 1; return value; } }",
                    BTreeSet::new(),
                ),
                (
                    "class make { #callback; #cache = new WeakMap(); constructor(callback, label) { this.#callback = callback; } invoke() { return this.#callback(); } }",
                    BTreeSet::from([0]),
                ),
                (
                    "class make { field; #field; constructor(callback) { this.#field = callback; } }",
                    BTreeSet::from([0]),
                ),
                (
                    "class make { #field; constructor(callback) { this.#field = callback; this.#field = 1; } }",
                    BTreeSet::from([0]),
                ),
                ("class make { __proto__ = 1; }", BTreeSet::new()),
                ("class make { field = 1; field = 2; }", BTreeSet::new()),
                (
                    "class make { #first; #later = (this.#first = 1); }",
                    BTreeSet::new(),
                ),
                (
                    "class make { field; constructor(value) { this.field = value; } }",
                    BTreeSet::from([0]),
                ),
                (
                    "class make { field; #field; constructor(value) { this.field = value; this.#field = 1; } }",
                    BTreeSet::from([0]),
                ),
                (
                    "class make { field; constructor(value) { this.field = value; this.field = 1; } }",
                    BTreeSet::from([0]),
                ),
                (
                    "class make { field; count; constructor(value) { let count = 0; this.field = (count = count + 1, value); this.count = count; } }",
                    BTreeSet::from([0]),
                ),
                (
                    "class make { field; later = (this.field = 1); }",
                    BTreeSet::new(),
                ),
            ] {
                let summary = derive_source(source).unwrap_or_else(|| panic!("{source}"));
                let effects = summary
                    .body_effects
                    .as_ref()
                    .expect("executed body fragment");
                assert!(body_effects_closed(effects), "{source}: {effects:#?}");
                assert_eq!(summary.retained_arguments, retained, "{source}");
                assert_eq!(
                    summary.returns,
                    BTreeSet::from([ForeignReturn::FreshAllocation]),
                    "{source}"
                );
            }
        });
    }

    #[test]
    fn unsupported_constructor_inputs_and_results_refuse_provider_projection() {
        js_ast::with_swc_globals(|| {
            for source in [
                "class make extends Base {}",
                "class make { field = captured; }",
                "class make { constructor(value) { const field = value.field; } }",
                "class make { constructor(value = unknown()) {} }",
                "class make { constructor({ value }) {} }",
                "class make { constructor() { return {}; } }",
                "class make { constructor() { return this; } }",
                "class make { #method() { return captured; } }",
                "class make { get #field() { return captured; } }",
                "class make { #first = (this.#later = 1); #later; }",
                "class make { #field = (this.#field = 1); }",
                "class make { field; #field = (this.#field = 1); }",
                "class make { field = callback; constructor(callback) {} }",
                "class make { field = value; constructor() { const value = 1; } }",
                "class make { field = unknown(); }",
                "class make { #field; constructor(value) { this.#field = value.field; } }",
                "class make { constructor(value) { this.field = value; } }",
                "class make { #field; constructor(value) { this.field = value; } }",
                "class make { field; constructor(value) { this.other = value; } }",
                "class make { later = (this.field = 1); field; }",
                "class make { set field(value) { value(); } constructor(value) { this.field = value; } }",
                "class make { field; constructor() { this.field = value; let value = 1; } }",
                "class make { field; constructor(value) { this.field = value.field; } }",
            ] {
                assert!(derive_source(source).is_none(), "{source}");
            }
        });
    }

    #[test]
    fn constructor_hoisted_var_operation_effects_stay_uncovered() {
        js_ast::with_swc_globals(|| {
            let source = "class make { constructor() { var value = 1; return value; } }";
            let summary = derive_source(source).expect("allocation projection remains separate");
            assert!(
                summary.body_effects.is_none(),
                "uncovered var operations cannot publish a closed effect sidecar: {source}"
            );
        });
    }

    #[test]
    fn native_assignment_requires_current_descriptor_authority() {
        js_ast::with_swc_globals(|| {
            let module =
                js_ast::parse_js_module("provider.js", "function make() { return { value: 1 }; }")
                    .unwrap()
                    .module;
            let views = top_level_item_views(&module.body);
            let prepared = super::super::prepare_callable_capabilities(
                &views,
                StableArraySpreadShapes::default(),
                BTreeSet::from(["make".into()]),
                Default::default(),
            );
            let key = *prepared
                .index
                .hoisted_bindings
                .iter()
                .find(|(binding, _)| binding.0.as_ref() == "make")
                .unwrap()
                .1;
            let mut execution = execute_provider_body(
                &prepared.index,
                key,
                &prepared.shadowed,
                None,
                &BTreeMap::new(),
            )
            .expect("source literal executes through the shared kernel");
            let values = execution.ledger.may_normal().unwrap().values.clone();
            let AbstractValue::Value(Value::Alloc(Kind::Object, address)) = values.first().unwrap()
            else {
                panic!("source literal result");
            };
            assert!(
                descriptor_transfer::assignment(&execution.context, &values, &values, None)
                    .is_some()
            );
            let contents = execution
                .context
                .surface
                .data_values(*address, "value")
                .unwrap()
                .clone();
            let mut readonly = DescriptorAttributes::literal(true);
            readonly.writable = Some(false);
            execution.context.surface.define_data_property(
                *address,
                "value".into(),
                &contents,
                readonly,
            );
            assert!(execution.context.ordinary_record_shape(*address).is_some());
            assert!(
                execution
                    .context
                    .surface
                    .current_own_property_projection(*address)
                    .is_none()
            );
            assert!(
                descriptor_transfer::assignment(&execution.context, &values, &values, None)
                    .is_none(),
                "literal syntax cannot replace retired current descriptor authority"
            );
            assert_eq!(
                execution.context.surface.data_values(*address, "value"),
                Some(&contents)
            );
        });
    }

    #[test]
    fn private_native_heap_requires_sealing_or_a_correlated_retained_graph() {
        js_ast::with_swc_globals(|| {
            for (source, supported, escaping) in [
                (
                    "function make(input) { const descriptors = Object.getOwnPropertyDescriptors(input); const scratch = Object.defineProperties({}, descriptors); return 0; }",
                    true,
                    false,
                ),
                (
                    "function make(input) { const source = { value: input }; const target = { value: null }; Object.assign(target, source); return input; }",
                    true,
                    false,
                ),
                (
                    "function make(input) { const descriptors = Object.getOwnPropertyDescriptors(input); return () => input; }",
                    true,
                    false,
                ),
                (
                    "function make(input) { Object.assign(input, { value: 0 }); return 0; }",
                    false,
                    false,
                ),
                (
                    "function make(input) { return Object.getOwnPropertyDescriptors(input); }",
                    true,
                    true,
                ),
                (
                    "function make(input) { const target = {}; Object.assign(target, { value: input }); return target; }",
                    true,
                    true,
                ),
                (
                    "function make(input) { const target = {}; Object.assign(target, { value: input }); return { alias: target }; }",
                    true,
                    true,
                ),
                (
                    "function make(input) { const target = {}; Object.assign(target, { value: input }); return () => target; }",
                    true,
                    true,
                ),
                (
                    "function make(input) { const target = {}; const holder = { alias: target }; Object.assign(target, { value: input }); return () => holder; }",
                    true,
                    true,
                ),
                (
                    "function make(input) { const target = {}; Object.assign(target, { value: input }); return { left: target, right: target, first: () => target, second: () => target }; }",
                    true,
                    true,
                ),
                (
                    "function make(input) { const target = {}; Object.assign(target, { value: input }); return () => external; }",
                    false,
                    false,
                ),
            ] {
                let module = js_ast::parse_js_module("private-heap.js", source)
                    .unwrap()
                    .module;
                let views = top_level_item_views(&module.body);
                let mut prepared = super::super::prepare_callable_capabilities(
                    &views,
                    StableArraySpreadShapes::default(),
                    BTreeSet::from(["make".into()]),
                    Default::default(),
                );
                let integrity = super::super::resolve_native_integrity([prepared.native_writes()]);
                prepared.index.apply_native_integrity(&integrity);
                let key = *prepared
                    .index
                    .hoisted_bindings
                    .iter()
                    .find(|(id, _)| id.0.as_ref() == "make")
                    .unwrap()
                    .1;
                let input = RecordInputShape {
                    properties: BTreeMap::from([
                        ("value".to_owned(), RecordInputProperty::Data),
                        (
                            "hook".to_owned(),
                            RecordInputProperty::Accessor {
                                getter: true,
                                setter: true,
                            },
                        ),
                    ]),
                    creation_order: vec!["value".to_owned(), "hook".to_owned()],
                };
                let execution = execute_provider_body(
                    &prepared.index,
                    key,
                    &prepared.shadowed,
                    Some(input.clone()),
                    &BTreeMap::new(),
                );
                assert_eq!(execution.is_some(), supported, "{source}");
                if let Some(execution) = execution {
                    assert_eq!(
                        execution.private_heap_proof.retained_graph.is_some(),
                        escaping,
                        "{source}"
                    );
                    if let Some(graph) = &execution.private_heap_proof.retained_graph {
                        use crate::analysis_inputs::{
                            ProviderGraphInput, ProviderGraphNodeKind, ProviderGraphRole,
                            ProviderGraphValue,
                        };
                        assert!(graph.is_well_formed(&input, &execution.context.parameter_reads));
                        assert!(matches!(
                            execution.ledger.allocation,
                            AllocationJudgment::Opaque
                        ));
                        if source.contains("getOwnPropertyDescriptors") {
                            assert!(graph.inputs().contains(&ProviderGraphInput::Getter {
                                argument: 0,
                                property: "hook".into()
                            }));
                            assert!(graph.inputs().contains(&ProviderGraphInput::Setter {
                                argument: 0,
                                property: "hook".into()
                            }));
                        }
                        if source.contains("left: target") {
                            let root = graph
                                .roots
                                .iter()
                                .find_map(|value| match value {
                                    ProviderGraphValue::Slot { slot } => Some(&graph.nodes[*slot]),
                                    _ => None,
                                })
                                .unwrap();
                            let left = root
                                .edges
                                .iter()
                                .find(|edge| {
                                    edge.role
                                        == ProviderGraphRole::OwnData {
                                            property: "left".into(),
                                        }
                                })
                                .unwrap();
                            let right = root
                                .edges
                                .iter()
                                .find(|edge| {
                                    edge.role
                                        == ProviderGraphRole::OwnData {
                                            property: "right".into(),
                                        }
                                })
                                .unwrap();
                            assert_eq!(
                                left.value, right.value,
                                "object aliases share one formal slot"
                            );
                            let cells: Vec<_> = graph
                                .nodes
                                .iter()
                                .filter(|node| {
                                    matches!(node.kind, ProviderGraphNodeKind::Closure { .. })
                                })
                                .flat_map(|node| {
                                    node.edges.iter().filter_map(|edge| {
                                        matches!(edge.role, ProviderGraphRole::ClosureCell { .. })
                                            .then_some(&edge.value)
                                    })
                                })
                                .collect();
                            assert_eq!(cells.len(), 2);
                            assert_eq!(
                                cells[0], cells[1],
                                "closures share the same lexical cell slot"
                            );
                        }
                    }
                    assert!(execution.ledger.is_exact_normal(), "{source}");
                    assert!(execution.ledger.has_closed_effects(), "{source}");
                    assert!(
                        !execution.context.native_operations.is_empty(),
                        "native commit actually executed: {source}"
                    );
                    if source.contains("getOwnPropertyDescriptors") {
                        assert!(
                            matches!(execution.ledger.allocation, AllocationJudgment::Opaque),
                            "caller-shaped contents do not become correlated fresh output: {source}"
                        );
                        assert!(
                            execution
                                .context
                                .parameter_reads
                                .contains(&ParameterHeapRead::ObjectShape { argument: 0 })
                        );
                    }
                }
            }
        });
    }

    #[test]
    fn conditional_body_can_close_while_allocation_projection_stays_opaque() {
        js_ast::with_swc_globals(|| {
            let source = "export function make(definition) { const create = definition.create; const extra = { create: (...args) => create.call(definition, ...args), call: () => create.call(definition) }; return Object.assign(Object.defineProperties({ enabled: () => true, userFacingName: () => definition.name }, Object.getOwnPropertyDescriptors(definition)), extra); }";
            let module = js_ast::parse_js_module("provider.js", source)
                .unwrap()
                .module;
            let views = top_level_item_views(&module.body);
            let mut prepared = super::super::prepare_callable_capabilities(
                &views,
                StableArraySpreadShapes::default(),
                BTreeSet::from(["make".into()]),
                Default::default(),
            );
            let caller = js_ast::parse_js_module("caller.js", "import { make } from './provider.js'; const wrapped = make({ name: 'report', create() { return later(); }, get schema() { return later(); } }); var later = () => 42; console.log(wrapped.schema, wrapped.create());").unwrap().module;
            let caller_views = top_level_item_views(&caller.body);
            let caller_prepared = super::super::prepare_callable_capabilities(
                &caller_views,
                StableArraySpreadShapes::default(),
                BTreeSet::new(),
                Default::default(),
            );
            let demands = caller_prepared.imported_record_inputs();
            assert_eq!(
                demands.len(),
                1,
                "canonical imported call must demand its actual record layout"
            );
            let input = demands.into_iter().next().unwrap().1;
            let integrity = super::super::resolve_native_integrity([
                prepared.native_writes(),
                caller_prepared.native_writes(),
            ]);
            prepared.index.apply_native_integrity(&integrity);
            let key = prepared
                .index
                .hoisted_bindings
                .iter()
                .find(|(id, _)| id.0.as_ref() == "make")
                .unwrap()
                .1;
            let body = &prepared.index.callables[key];
            let native: Vec<_> = body
                .calls
                .iter()
                .map(|call| {
                    (
                        call.span,
                        native_selection(
                            &prepared.index,
                            call,
                            &prepared.shadowed,
                            &body
                                .body_facts
                                .ordered_plan()
                                .unwrap()
                                .bindings
                                .keys()
                                .cloned()
                                .collect(),
                        )
                        .is_some(),
                    )
                })
                .collect();
            let mut execution = execute_provider_body(
                &prepared.index,
                *key,
                &prepared.shadowed,
                Some(input),
                &BTreeMap::new(),
            )
            .unwrap_or_else(|| panic!("canonical body: {body:#?}; native selections: {native:#?}"));
            assert!(matches!(
                execution.ledger.allocation,
                AllocationJudgment::Opaque
            ));
            assert!(execution.ledger.is_exact_normal());
            let graph = execution
                .private_heap_proof
                .retained_graph
                .as_ref()
                .expect("escaped native heap has a correlated graph");
            assert!(graph.nodes.iter().any(|node| matches!(
                node.kind,
                crate::analysis_inputs::ProviderGraphNodeKind::Closure { .. }
            )));
            assert!(
                graph
                    .inputs()
                    .contains(&crate::analysis_inputs::ProviderGraphInput::Getter {
                        argument: 0,
                        property: "schema".into()
                    })
            );
            let selected = execution.ledger.may_normal().unwrap();
            let proof = selected
                .postcondition
                .as_ref()
                .expect("normal shared native result carries its generation witness");
            let selected_values = selected.values.clone();
            let projection = proof
                .current_projection(&execution.context, &selected_values)
                .expect("committed native layout projects current retained payload");
            assert!(projection.layout.descriptors.contains_key("create"));
            assert!(projection.layout.descriptors.contains_key("schema"));
            let crate::facts::sync_effects::object_surface::CurrentOwnPropertyDescriptor::Data {
                values: current_create,
                ..
            } = &projection.descriptors["create"]
            else {
                panic!("Object.assign installs a data wrapper");
            };
            assert_eq!(
                current_create.len(),
                1,
                "this generation selects only the later wrapper value"
            );
            assert!(current_create.iter().all(|value| matches!(
                value,
                AbstractValue::Value(Value::Alloc(Kind::Function, _))
            )));
            let root = graph
                .roots
                .iter()
                .find_map(|value| match value {
                    crate::analysis_inputs::ProviderGraphValue::Slot { slot } => {
                        Some(&graph.nodes[*slot])
                    }
                    _ => None,
                })
                .unwrap();
            let create_edges = root
                .edges
                .iter()
                .filter(|edge| {
                    edge.role
                        == crate::analysis_inputs::ProviderGraphRole::OwnData {
                            property: "create".into(),
                        }
                })
                .collect::<Vec<_>>();
            assert_eq!(
                create_edges.len(),
                1,
                "graph property retains the current generation value"
            );
            let crate::analysis_inputs::ProviderGraphValue::Slot { slot } = &create_edges[0].value
            else {
                panic!("wrapper is a formal closure slot")
            };
            assert!(matches!(
                graph.nodes[*slot].kind,
                crate::analysis_inputs::ProviderGraphNodeKind::Closure { .. }
            ));
            assert!(
                graph
                    .inputs()
                    .contains(&crate::analysis_inputs::ProviderGraphInput::Property {
                        argument: 0,
                        property: "create".into()
                    }),
                "original callback survives through the wrapper capture"
            );
            let target = selected_values
                .iter()
                .find_map(|value| match value {
                    AbstractValue::Value(Value::Alloc(Kind::Object, address)) => Some(*address),
                    _ => None,
                })
                .unwrap();
            let pooled_create =
                &execution.context.surface.own_properties(target).unwrap()[&Some("create".into())];
            assert!(current_create.is_subset(pooled_create));
            assert!(
                pooled_create.len() > current_create.len(),
                "pooled heap keeps overwritten payloads for other represented instances"
            );
            assert!(
                execution
                    .context
                    .surface
                    .current_own_property_projection(target)
                    .is_none(),
                "generation projection never revives the retired pooled certificate"
            );
            assert!(
                proof
                    .current_projection(
                        &execution.context,
                        &Values::from([AbstractValue::UNDEFINED])
                    )
                    .is_none(),
                "a current epoch does not authenticate another selected result"
            );
            let proof = proof.clone();
            let effects = &execution.ledger.effects;
            assert!(execution.ledger.has_closed_effects(), "{effects:#?}");
            assert!(
                execution
                    .context
                    .parameter_reads
                    .contains(&ParameterHeapRead::OwnProperty {
                        argument: 0,
                        property: "create".into()
                    })
            );
            assert!(
                execution
                    .context
                    .parameter_reads
                    .contains(&ParameterHeapRead::ObjectShape { argument: 0 })
            );
            execution.context.revision = execution.context.revision.wrapping_add(1);
            assert!(
                proof
                    .current_projection(&execution.context, &selected_values)
                    .is_none(),
                "an intervening world operation expires the native generation witness"
            );
        });
    }

    #[test]
    fn zero_argument_fresh_record_has_closed_shared_body_effects() {
        js_ast::with_swc_globals(|| {
            for source in [
                "function make() { return {}; }",
                "function make() { const record = {}; return record; }",
                "function make() { const record = { first: 0, second: 0 }; record.first = 1; record.second = 2; return record; }",
            ] {
                let summary = derive_source(source).expect("bounded allocation provider");
                let effects = summary
                    .body_effects
                    .as_ref()
                    .expect("eligible body stores its shared effect fragment");
                assert!(body_effects_closed(effects), "{source}");
                assert_eq!(
                    summary.returns,
                    BTreeSet::from([ForeignReturn::FreshAllocation]),
                    "effect coverage must not replace the allocation judgment"
                );
            }
        });
    }

    #[test]
    fn simple_parameter_record_has_closed_shared_body_effects() {
        js_ast::with_swc_globals(|| {
            let summary = query("function make(value) { return { value }; }", true)
                .expect("simple-parameter allocation provider");
            assert!(
                summary
                    .body_effects
                    .as_ref()
                    .is_some_and(body_effects_closed),
                "{summary:#?}"
            );
            assert_eq!(
                summary.retained_arguments,
                BTreeSet::from([0]),
                "{summary:#?}"
            );
        });
    }

    #[test]
    fn normal_return_accounts_for_a_skipped_source_call_suffix() {
        js_ast::with_swc_globals(|| {
            let module = js_ast::parse_js_module(
                "provider.js",
                "function make(value) { const record = { value }; return record; Object.freeze(record); }",
            )
            .unwrap()
            .module;
            let key = match &module.body[0] {
                ModuleItem::Stmt(Stmt::Decl(Decl::Fn(function))) => function.function.span.lo.0,
                _ => panic!("provider fixture"),
            };
            let views = top_level_item_views(&module.body);
            let mut prepared = super::super::prepare_callable_capabilities(
                &views,
                StableArraySpreadShapes::default(),
                BTreeSet::from(["make".into()]),
                Default::default(),
            );
            let integrity = super::super::resolve_native_integrity([prepared.native_writes()]);
            prepared.index.apply_native_integrity(&integrity);
            let input = RecordInputShape {
                properties: BTreeMap::new(),
                creation_order: Vec::new(),
            };
            let execution = execute_provider_body(
                &prepared.index,
                key,
                &prepared.shadowed,
                Some(input),
                &BTreeMap::new(),
            )
            .expect("the ordered strict-provider body is supported");
            let callable = &prepared.index.callables[&key];
            let caller = LocalBodyIdentity::Callable {
                key,
                owner: callable.owner,
            };
            let expected = prepared
                .index
                .call_operation_sites(&caller, callable.calls.iter())
                .expect("all suffix calls have canonical source sites");

            assert!(!expected.is_empty(), "the fixture has a call after Return");
            assert!(execution.context.body_effect_mode);
            assert!(execution.ledger.is_exact_normal());
            assert!(execution.ledger.has_closed_effects());
            assert!(
                execution
                    .ledger
                    .effects
                    .has_call_inventory(&caller, &expected),
                "the suffix call is accounted for by the shared canonical skip proof"
            );
            assert!(
                execution.context.native_operations.is_empty(),
                "the skipped Object.freeze call was not executed"
            );
        });
    }

    #[test]
    fn deferred_factory_creation_has_body_effects_and_retains_input() {
        js_ast::with_swc_globals(|| {
            for source in [
                "function make(callback) { return () => callback(); }",
                "function make(callback) { return function () { return callback(); }; }",
            ] {
                let summary = derive_source(source).unwrap_or_else(|| panic!("{source}"));
                assert!(
                    summary
                        .body_effects
                        .as_ref()
                        .is_some_and(body_effects_closed),
                    "creation, not invocation, must be covered: {source}; {summary:#?}"
                );
                assert_eq!(summary.retained_arguments, BTreeSet::from([0]), "{source}");
                assert_eq!(
                    summary.returns,
                    BTreeSet::from([ForeignReturn::FreshAllocation]),
                    "{source}"
                );
            }
        });
    }

    #[test]
    fn parameter_reassignment_and_fresh_self_links_have_shared_effect_receipts() {
        js_ast::with_swc_globals(|| {
            let summary = query(
                "function make(value) { return (value = { value, Self: null, Child: null }, value.Self = value, value.Child = { parent: value }, value); }",
                false,
            )
            .expect("ordered private parameter store and fresh writable slots");
            assert!(
                summary
                    .body_effects
                    .as_ref()
                    .is_some_and(body_effects_closed),
                "{summary:#?}"
            );
            assert_eq!(summary.retained_arguments, BTreeSet::from([0]));
        });
    }

    #[test]
    fn unsupported_native_unknown_and_free_read_paths_never_close_body_effects() {
        js_ast::with_swc_globals(|| {
            for source in [
                "function make() { return new Map(); } Map = () => {};",
                "function make(value) { return new Map(value); }",
                "function make() { return host(); }",
                "function make() { missing; return {}; }",
            ] {
                let summary = derive_source(source);
                assert!(
                    summary
                        .as_ref()
                        .and_then(|summary| summary.body_effects.as_ref())
                        .is_none_or(|effects| !body_effects_closed(effects)),
                    "{source}"
                );
            }
        });
    }

    #[test]
    fn strict_operators_and_terminal_branches_use_the_central_query() {
        js_ast::with_swc_globals(|| {
            for (expression, retained, body_effects_covered) in [
                (
                    "{ type: input, compare: compare === void 0 ? null : compare }",
                    BTreeSet::from([0, 1]),
                    false,
                ),
                ("{ value: input !== compare }", BTreeSet::new(), true),
                ("{ value: void input }", BTreeSet::new(), true),
                ("{ value: !input }", BTreeSet::new(), true),
                (
                    "{ value: input ? null : compare }",
                    BTreeSet::from([1]),
                    false,
                ),
            ] {
                for source in [
                    format!("function make(input, compare) {{ return {expression}; }}"),
                    format!("const make = (input, compare) => ({expression});"),
                ] {
                    let summary =
                        query(&source, true).expect("shared strict transfer, no legacy evaluator");
                    assert_eq!(
                        summary.returns,
                        BTreeSet::from([ForeignReturn::FreshAllocation])
                    );
                    assert_eq!(
                        summary
                            .body_effects
                            .as_ref()
                            .is_some_and(body_effects_closed),
                        body_effects_covered,
                        "parameter body receipt did not follow shared operation coverage: {source}; {summary:#?}"
                    );
                    assert_eq!(summary.retained_arguments, retained, "{source}");
                }
            }
            for expression in [
                "void input",
                "!input",
                "input === compare",
                "input !== compare",
            ] {
                let summary = query(
                    &format!("function make(input, compare) {{ return {expression}; }}"),
                    true,
                )
                .unwrap();
                assert_eq!(summary.returns, BTreeSet::from([ForeignReturn::Primitive]));
                assert!(summary.retained_arguments.is_empty());
                assert!(
                    summary
                        .body_effects
                        .as_ref()
                        .is_some_and(body_effects_closed)
                );
            }
            // Covered syntax can still exceed the terminal-arm transfer. It
            // must decline centrally, not retry the unmigrated evaluator.
            assert!(
                query(
                    "function make(input) { return input ? { input } : {}; }",
                    true
                )
                .is_none()
            );
            assert!(query("function make(input) { return input === missing; }", true).is_none());
            assert!(query("function make(input) { return void missing; }", true).is_none());
            assert!(query("function make(input) { return !missing; }", true).is_none());
            // Syntax coverage cannot establish primitive operands or exclude
            // coercion hooks on a provider's opaque input.
            for expression in ["input == 1", "input + 1", "input < 1"] {
                assert!(
                    query(
                        &format!("function make(input) {{ return {expression}; }}"),
                        true
                    )
                    .is_none()
                );
            }
            for expression in [
                "void input()",
                "input.value === 1",
                "input() ? null : input",
                "true ? input : input()",
                "void (input(), input)",
            ] {
                assert!(
                    query(
                        &format!("function make(input) {{ return {expression}; }}"),
                        false
                    )
                    .is_none()
                );
            }
        });
    }

    #[test]
    fn function_and_const_arrow_keys_reach_the_central_query() {
        js_ast::with_swc_globals(|| {
            for source in [
                "function make(input) { return { input }; }",
                "const make = input => ({ input });",
            ] {
                let summary = query(source, true).expect("central query, without legacy fallback");
                assert_eq!(
                    summary.returns,
                    BTreeSet::from([ForeignReturn::FreshAllocation])
                );
                assert_eq!(summary.retained_arguments, BTreeSet::from([0]));
            }
        });
    }

    #[test]
    fn returned_closure_retains_capture_reached_through_final_local_heap() {
        js_ast::with_swc_globals(|| {
            for returned in ["wrapped", "{ async run() { await 0; return local; } }"] {
                let source = format!(
                    "function outer() {{ var captured = () => 42; function make() {{ const local = {{ callback: captured }}; const wrapped = () => local; return {returned}; }} }}"
                );
                let module = js_ast::parse_js_module("provider.js", &source)
                    .unwrap()
                    .module;
                let mut make = MakeFunctionKey::default();
                module.visit_with(&mut make);
                let key = make.0.expect("canonical nested make callable");
                let views = top_level_item_views(&module.body);
                let prepared = super::super::prepare_callable_capabilities(
                    &views,
                    StableArraySpreadShapes::default(),
                    BTreeSet::from(["make".into()]),
                    Default::default(),
                );
                let summary = derive(&prepared.index, key, &BTreeSet::new())
                    .expect("local transfer retains the checked provider capture");

                assert_eq!(
                    summary.returns,
                    BTreeSet::from([ForeignReturn::FreshAllocation])
                );
                assert!(
                    summary
                        .body_effects
                        .as_ref()
                        .is_some_and(body_effects_closed),
                    "callable and descriptor creation defer their bodies, including await: {summary:#?}"
                );
                assert!(summary.retained_arguments.is_empty());
                assert_eq!(
                    summary.retained_provider_captures,
                    BTreeSet::from(["captured".to_owned()]),
                    "the returned closure reaches the capture through its final local record, including after await"
                );
            }
        });
    }

    #[test]
    fn returned_closure_rejects_unrepresented_enclosing_lexical_capture() {
        js_ast::with_swc_globals(|| {
            for declaration in [
                "const enclosing = 42;",
                "const { value: enclosing } = { value: 42 };",
                "const [enclosing] = [42];",
            ] {
                let source = format!(
                    "function outer() {{ {declaration} function make(input) {{ return () => enclosing + input; }} }}"
                );
                let module = js_ast::parse_js_module("provider.js", &source)
                    .unwrap()
                    .module;
                let mut make = MakeFunctionKey::default();
                module.visit_with(&mut make);
                let key = make.0.expect("canonical nested make callable");
                let views = top_level_item_views(&module.body);
                let prepared = super::super::prepare_callable_capabilities(
                    &views,
                    StableArraySpreadShapes::default(),
                    BTreeSet::from(["make".into()]),
                    Default::default(),
                );

                assert!(
                    prepared.index.callables[&key]
                        .body_facts
                        .ordered_plan()
                        .is_some_and(|body| body.closure_candidate_syntax()),
                    "the source is admitted to the shared closure transfer"
                );
                assert!(
                    derive(&prepared.index, key, &BTreeSet::new()).is_none(),
                    "an enclosing const that is absent from the final local frame is not a deferred module binding"
                );
            }
        });
    }
}
