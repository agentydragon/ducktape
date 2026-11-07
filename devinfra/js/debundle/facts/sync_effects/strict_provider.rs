//! Isolated provider consumer of shared value, heap and effect transfers.

use super::super::allocation_transfer::{
    self, AllocationContext, AllocationJudgment, AllocationValue, KeyedCollectionOperationResult,
    LiteralHeap, LiteralLayout, WritablePrefixState,
};
use super::super::descriptor_transfer::{
    self, AppliedAccessor, DescriptorCall, DescriptorContext, DescriptorHeap, OwnDescriptorMapState,
};
use super::super::effect_coverage::{
    EffectCoverageFragment, NativeCalleeSelection, OperationSupportOperation,
    OperationSupportReceipt,
};
use super::super::local_operations::{DeferredCallableCreation, LocalBinding, LocalCallee};
use super::super::local_transfer::{
    self, EvaluatedCallee, LocalContext, LocalFrame, LocalValueResult, SelectedValue,
};
use super::super::own_data_transfer::OwnDataContext;
use super::super::property_inference::{
    OwnPropertyProjection, OwnPropertyProof, OwnPropertyRequest, infer_own_property_projection,
};
use super::super::value_model::{
    AbstractValue, CallSite, Context, DenseArray, OwnPropertyShape, PrivateFieldId, ProviderInput,
    ProviderScopeId, SymbolicInputId, ValueExpr, Values,
};
use super::{Call, CallKind, ExecutionIndex, native_call_identity_key};
use crate::analysis_inputs::{
    ForeignInvocation, ForeignNativeOperation, ForeignReturn, ParameterHeapRead, ProofDependencies,
    RecordInputProperty, RecordInputShape,
};
use crate::analysis_provenance::InferenceRule;
use crate::execution_evidence::OrderedFieldReadRefusal;
use crate::facts::abstract_domain::builtins::{
    self, IntrinsicOp, Invocation, NativeBodyEffects, Return, Row,
};
use crate::facts::abstract_domain::foreign_call::DerivedForeignCall;
use crate::facts::abstract_domain::integrity::Integrity;
use crate::facts::abstract_domain::value::{Kind, Value};
use crate::facts::semantic_inference::{NativePremise, names_intrinsic, native_premise};
use analysis_model::PrimKinds;
use imbl::OrdMap;
use std::collections::{BTreeMap, BTreeSet};
use swc_common::Span;
use swc_ecma_ast::Id;

struct ProviderContext<'a> {
    index: &'a ExecutionIndex,
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
    properties: OrdMap<u32, BTreeMap<Option<String>, Values>>,
    private_properties: OrdMap<u32, BTreeMap<PrivateFieldId, Values>>,
    arrays: OrdMap<u32, DenseArray>,
    writable_prefix: OrdMap<u32, WritablePrefixState>,
    descriptor_maps: OrdMap<CallSite, OwnDescriptorMapState>,
    applied_accessors: OrdMap<u32, BTreeMap<String, AppliedAccessor>>,
    revision: u64,
    /// Simple parameters and source-proved initialized var captures enter the
    /// shared body kernel; other projections retain allocation-only effects.
    body_effect_mode: bool,
    /// Static admission for the narrower private-birth support proof. Actual
    /// operation support still checks the current frame at every access.
    provider_local_birth_candidate: bool,
    body_effect_bindings: BTreeSet<Id>,
    input_shape: Option<RecordInputShape>,
    parameter_reads: BTreeSet<ParameterHeapRead>,
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
    fn binding(&mut self, binding: &Id) -> Option<AllocationValue> {
        allocation_transfer::closed_binding(&self.bindings, binding, self.scope)
    }
    fn child(&mut self, expression: &ValueExpr) -> Option<AllocationValue> {
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
            properties: &mut self.properties,
            private_properties: Some(&mut self.private_properties),
            arrays: &mut self.arrays,
            revision: &mut self.revision,
            writable_prefix: self.body_effect_mode.then_some(&mut self.writable_prefix),
        }
    }
    fn keyed_collection_new(
        &mut self,
        callee: &ValueExpr,
        arguments: &[ValueExpr],
        site: CallSite,
    ) -> Option<KeyedCollectionOperationResult> {
        self.construct_empty_collection(callee, arguments, site)
    }
}

impl ProviderContext<'_> {
    fn descriptor_heap(&mut self) -> DescriptorHeap<'_> {
        DescriptorHeap {
            maps: &mut self.descriptor_maps,
            properties: &mut self.properties,
            accessors: &mut self.applied_accessors,
            revision: &mut self.revision,
        }
    }

    fn retained_object_values(&self, object: u32) -> Option<Values> {
        let mut retained = Values::new();
        for (property, values) in self.properties.get(&object)? {
            if let Some(accessor) = property
                .as_ref()
                .and_then(|property| self.applied_accessors.get(&object)?.get(property))
            {
                retained.extend(accessor.getter.iter().chain(&accessor.setter).cloned());
            } else {
                retained.extend(values.iter().cloned());
            }
        }
        Some(retained)
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
                source_object: *object,
                source_values: values,
                shape: self.ordinary_record_shape(*object)?,
                properties: self.properties.get(object)?,
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
        let result = allocation_transfer::keyed_collection_new(row, site, dependencies.clone())?;
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

impl OwnDataContext for ProviderContext<'_> {
    fn ordinary_record_shape(&self, allocation: u32) -> Option<&OwnPropertyShape> {
        self.index.ordinary_record_shape(allocation)
    }
    fn object_properties_are_settled(&self, _allocation: u32) -> bool {
        // This isolated query refuses external mutation and publication.
        true
    }
    fn instance_properties_are_settled(&self, _class: u32) -> bool {
        // Provider queries have no checked runtime instance-prefix witness.
        false
    }
    fn property_values(&self, allocation: u32, property: &str) -> Option<&Values> {
        self.properties
            .get(&allocation)?
            .get(&Some(property.to_owned()))
    }
    fn property_is_accessor(&self, allocation: u32, property: &str) -> bool {
        self.applied_accessors
            .get(&allocation)
            .is_some_and(|properties| properties.contains_key(property))
    }
    fn property_is_writable(&self, allocation: u32, property: &str) -> bool {
        self.body_effect_mode
            && matches!(self.writable_prefix.get(&allocation),
                Some(WritablePrefixState::Proven(properties)) if properties.contains(property))
    }
    fn first_generation_dense_array_length(&self, allocation: u32) -> Option<u32> {
        if !self.index.array_elements.contains_key(&allocation)
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
    fn own_properties(&self, object: u32) -> Option<&BTreeMap<Option<String>, Values>> {
        self.properties.get(&object)
    }
    fn descriptor_map(&self, site: CallSite) -> Option<&OwnDescriptorMapState> {
        self.descriptor_maps.get(&site)
    }
    fn has_applied_accessors(&self, object: u32) -> bool {
        self.applied_accessors.contains_key(&object)
    }
    fn target_is_extensible(&self, object: u32) -> bool {
        matches!(
            self.writable_prefix.get(&object),
            Some(WritablePrefixState::Proven(_))
        )
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
        Some(LocalValueResult {
            allocation: AllocationValue::opaque(Values::from([AbstractValue::SymbolicInput(
                SymbolicInputId {
                    scope: self.scope,
                    input: ProviderInput::Property {
                        argument: 0,
                        property: ordinal,
                    },
                },
            )])),
            effects,
            selection: None,
            postcondition: None,
        })
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
        origin: u32,
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
                home_object,
            )));
        }
        self.closure_captures.insert(callable, captures);
        Some(LocalValueResult {
            allocation: AllocationValue {
                values: Values::from([AbstractValue::Value(Value::Alloc(
                    Kind::Function,
                    callable,
                ))]),
                judgment: AllocationJudgment::CoveredAllocationOnly(Default::default()),
            },
            effects: if self.body_effect_mode {
                creation.effect_coverage()
            } else {
                EffectCoverageFragment::default()
            },
            selection: None,
            postcondition: None,
        })
    }
    fn assign_capture(
        &mut self,
        _frame: &mut LocalFrame,
        _binding: &Id,
        _selected: &SelectedValue,
        _span: Span,
    ) -> Option<LocalValueResult> {
        None
    }
    fn read_binding(&mut self, binding: &Id, frame: &mut LocalFrame) -> Option<LocalValueResult> {
        // The frame starts with scoped arguments and checked private var
        // captures. Other entries come from the covered local operations.
        let values = frame.environment.get(binding)?.clone();
        let effects = if self.body_effect_mode
            && frame.initialized_bindings.contains(binding)
            && !values.is_empty()
            && !values.contains(&AbstractValue::Value(Value::Unavailable))
        {
            EffectCoverageFragment::no_effects()
        } else {
            EffectCoverageFragment::default()
        };
        Some(LocalValueResult {
            allocation: AllocationValue {
                values,
                judgment: AllocationJudgment::CoveredAllocationOnly(Default::default()),
            },
            effects,
            selection: None,
            postcondition: None,
        })
    }
    fn call(
        &mut self,
        _frame: &mut LocalFrame,
        invocation: ForeignInvocation,
        callee: &EvaluatedCallee,
        arguments: &[local_transfer::SelectedValue],
        span: Span,
    ) -> Option<LocalValueResult> {
        if invocation != ForeignInvocation::Call {
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
        let descriptor_effects = descriptor_transfer::native_call_effects(
            self,
            DescriptorCall {
                row,
                site: span.into(),
                arguments: &arguments,
                projection: projection.as_ref(),
                target_postcondition: target_postcondition.as_deref(),
            },
            &dependencies,
        );
        let borrowed_read = descriptor_effects
            .as_ref()
            .is_some_and(|effects| !effects.parameter_reads.is_empty());
        let mut postcondition = None;
        let (values, effects) = if let Some(descriptor_effects) = descriptor_effects {
            self.parameter_reads
                .extend(descriptor_effects.parameter_reads);
            postcondition = descriptor_effects.result;
            // Concrete cells belong to the isolated fresh heap. Symbolic input
            // cells were retained above for caller-side instantiation.
            let values = match row.op {
                IntrinsicOp::ObjectGetOwnPropertyDescriptors => {
                    let OwnPropertyProjection::Descriptors(projection) = projection?.projection
                    else {
                        return None;
                    };
                    Values::from([self.descriptor_heap().capture(span.into(), projection)])
                }
                IntrinsicOp::ObjectGetOwnPropertyNames => {
                    let OwnPropertyProjection::Names(projection) = projection?.projection else {
                        return None;
                    };
                    let object = CallSite::from(span).allocation_id();
                    AllocationContext::heap(self).dense_array(
                        object,
                        projection
                            .names
                            .into_iter()
                            .map(|_| {
                                Values::from([AbstractValue::Value(Value::Prim(PrimKinds::STRING))])
                            })
                            .collect(),
                    );
                    Values::from([AbstractValue::Value(Value::Alloc(Kind::Object, object))])
                }
                IntrinsicOp::ObjectDefineProperties => {
                    let application = descriptor_transfer::application(
                        self,
                        &arguments[0],
                        &arguments[1],
                        target_postcondition.as_deref(),
                    )?;
                    AllocationContext::heap(self).invalidate_writable(application.target);
                    if !self
                        .descriptor_heap()
                        .apply(application.target, application.map_site)
                    {
                        return None;
                    }
                    arguments[0].clone()
                }
                IntrinsicOp::ObjectAssign => {
                    let assignment = descriptor_transfer::assignment(
                        self,
                        &arguments[0],
                        &arguments[1],
                        target_postcondition.as_deref(),
                    )?;
                    AllocationContext::heap(self).invalidate_writable(assignment.target);
                    self.descriptor_heap().assign(&assignment);
                    arguments[0].clone()
                }
                _ => return None,
            };
            (values, descriptor_effects.coverage)
        } else {
            let effects =
                allocation_transfer::primitive_native_call_effects(row, &arguments, &dependencies)?;
            let Return::Prim(kinds) = row.returns else {
                return None;
            };
            (
                Values::from([AbstractValue::Value(Value::Prim(kinds))]),
                effects,
            )
        };
        self.native_operations
            .insert(ForeignNativeOperation::for_row(row, Invocation::Call)?);
        self.native_derivation_rules.extend(
            selection
                .premise
                .inference_use
                .derivation_rules
                .iter()
                .copied(),
        );
        Some(LocalValueResult {
            allocation: AllocationValue {
                values,
                judgment: if borrowed_read {
                    AllocationJudgment::Opaque
                } else {
                    AllocationJudgment::CoveredAllocationOnly(dependencies)
                },
            },
            effects,
            selection: None,
            postcondition: postcondition.map(|proof| proof.after_mutation(self.revision)),
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

    fn keyed_collection_new(
        &mut self,
        _frame: &mut LocalFrame,
        callee: &ValueExpr,
        arguments: &[ValueExpr],
        site: CallSite,
    ) -> Option<KeyedCollectionOperationResult> {
        self.construct_empty_collection(callee, arguments, site)
    }
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
    let mut visited_objects = BTreeSet::new();
    let mut visited_closures = BTreeSet::new();
    let mut visited_descriptor_maps = BTreeSet::new();
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
            AbstractValue::Value(Value::Alloc(Kind::Function, callable)) => {
                if !visited_closures.insert(callable) {
                    continue;
                }
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

/// The canonical source collector retained whole-body syntax coverage before
/// value lowering. Direct returns and ordered bodies share their transfers
/// with application execution; refusal never retries the legacy AST evaluator.
fn execute_provider_body<'a>(
    index: &'a ExecutionIndex,
    callable: u32,
    shadowed: &BTreeSet<&'static str>,
    input_shape: Option<RecordInputShape>,
) -> Option<ProviderExecution<'a>> {
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
                && body.captured_writes().is_empty()
        });
    let provider_local_birth_syntax_candidate = body_effect_candidate
        && body.calls.is_empty()
        && local_body.is_some_and(LocalBody::provider_local_birth_candidate);
    let closure_projection = if closure_mode {
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
            reads
                .iter()
                .all(|binding| index.initialized_provider_var_capture(binding, callable))
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
            !index.initialized_provider_var_capture(binding, callable)
        } else {
            !index.private_var_binding(binding)
        };
        if uncovered {
            return None;
        }
        let value = if let Some((kinds, dependencies)) =
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
    let mut context = ProviderContext {
        input_shape,
        parameter_reads: BTreeSet::new(),
        index,
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
        properties: OrdMap::new(),
        private_properties: OrdMap::new(),
        arrays: OrdMap::new(),
        writable_prefix: OrdMap::new(),
        descriptor_maps: OrdMap::new(),
        applied_accessors: OrdMap::new(),
        revision: 0,
        body_effect_mode,
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
    let mut body_effects = None;
    let result = if let Some(expression) = body.body_facts.value_flow().direct_allocation_return()
        && !closure_mode
        && !body_effect_mode
    {
        allocation_transfer::transfer(&mut context, expression)?
    } else {
        let mut frame = LocalFrame::new(std::mem::take(&mut context.bindings));
        if body_effect_mode {
            frame.parameter_inputs = Some(parameter_inputs);
            frame.initialized_bindings.extend(captures.iter().cloned());
        }
        let transfer = local_transfer::execute(&mut context, local_body?, &mut frame)?;
        if body_effect_mode {
            let mut effects = transfer.effects.clone();
            if provider_local_birth_candidate
                && matches!(
                    &transfer.allocation,
                    AllocationJudgment::CoveredAllocationOnly(_)
                )
                && frame.thrown.is_none()
                && !frame.returns.is_empty()
                && !frame
                    .returns
                    .contains(&AbstractValue::Value(Value::Unavailable))
                && !frame.returns.contains(&AbstractValue::Value(Value::Top))
            {
                // Sealing relies on the same complete closed effect transfer
                // consumed by callers; local birth membership alone is not a
                // body-level no-publication proof.
                let _ = effects.seal_provider_local_birth_support(
                    &frame.fresh_records,
                    &frame.borrowed_records,
                );
            }
            body_effects = Some(effects);
        }
        context.final_environment = Some(frame.environment.clone());
        AllocationValue {
            values: frame.returns,
            judgment: transfer.allocation,
        }
    };
    if let Some(effects) = &mut body_effects {
        effects.depend_on(&capture_dependencies);
    }
    Some(ProviderExecution {
        context,
        scope,
        captures,
        result,
        body_effects,
        capture_dependencies,
    })
}

struct ProviderExecution<'a> {
    context: ProviderContext<'a>,
    scope: ProviderScopeId,
    captures: Vec<Id>,
    result: AllocationValue,
    body_effects: Option<EffectCoverageFragment>,
    capture_dependencies: ProofDependencies,
}

pub(super) fn derive_conditional_body(
    index: &ExecutionIndex,
    callable: u32,
    shadowed: &BTreeSet<&'static str>,
    input: RecordInputShape,
) -> Option<crate::facts::abstract_domain::foreign_call::DerivedConditionalBody> {
    let body = index.callables.get(&callable)?;
    if body.params.len() != 1 {
        return None;
    }
    let execution = execute_provider_body(index, callable, shadowed, Some(input.clone()))?;
    // Provider module cells need a qualified footprint, independent of their
    // initialization/value proof. This query only instantiates parameter cells.
    if !execution.captures.is_empty() {
        return None;
    }
    let effects = execution.body_effects?;
    // Provider-local shapes have no checked birth/escape correspondence to
    // the caller, so this conditional body cannot discharge them.
    if effects.has_unresolved_provider_operation_support() {
        return None;
    }
    let dependencies = effects.closed_effect_dependencies()?;
    Some(
        crate::facts::abstract_domain::foreign_call::DerivedConditionalBody {
            input,
            reads: execution.context.parameter_reads,
            native_operations: execution.context.native_operations,
            dependencies,
            effects,
        },
    )
}

pub(super) fn derive(
    index: &ExecutionIndex,
    callable: u32,
    shadowed: &BTreeSet<&'static str>,
) -> Option<DerivedForeignCall> {
    let body = index.callables.get(&callable)?;
    let ProviderExecution {
        context,
        scope,
        captures,
        result,
        mut body_effects,
        capture_dependencies,
    } = execute_provider_body(index, callable, shadowed, None)?;
    let AllocationJudgment::CoveredAllocationOnly(mut dependencies) = result.judgment else {
        return None;
    };
    // Kind facts can affect returned graph retention, including deferred
    // closure captures. Guard both projections even when no read runs now.
    dependencies.extend(&capture_dependencies);
    if let Some(effects) = &mut body_effects {
        effects.depend_on(&capture_dependencies);
    }
    // Native operation identity is conditional on the exact census proof;
    // transport every consumed row dependency to the summary's importer.
    let mut returns = BTreeSet::new();
    let mut retained_arguments = BTreeSet::new();
    let mut retained_provider_captures = BTreeSet::new();
    let closure = match result.values.iter().collect::<Vec<_>>().as_slice() {
        [AbstractValue::Value(Value::Alloc(Kind::Function, callable))] => {
            let span = index.callable_spans.get(callable)?;
            Some(span.lo.0..span.hi.0)
        }
        _ => None,
    };
    let record = if body.params.is_empty() {
        match result.values.iter().collect::<Vec<_>>().as_slice() {
            [AbstractValue::Value(Value::Alloc(Kind::Object, allocation))] => {
                index.method_record_members.get(allocation).cloned()
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
        calls: &[],
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
        properties: OrdMap::new(),
        private_properties: OrdMap::new(),
        arrays: OrdMap::new(),
        writable_prefix: OrdMap::new(),
        descriptor_maps: OrdMap::new(),
        applied_accessors: OrdMap::new(),
        revision: 0,
        input_shape: None,
        parameter_reads: BTreeSet::new(),
        body_effect_mode: true,
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
    let mut frame = LocalFrame::constructor(body);
    let transfer =
        local_transfer::execute_constructor(&mut context, body, parameter_inputs, &mut frame)?;
    if !frame
        .returns
        .iter()
        .all(|value| matches!(value, AbstractValue::Value(Value::Prim(_))))
    {
        return None;
    }
    let AllocationJudgment::CoveredAllocationOnly(dependencies) = transfer.allocation else {
        return None;
    };
    let mut retained_arguments = BTreeSet::new();
    let mut pending_values = vec![AbstractValue::Value(Value::Alloc(Kind::Object, class))];
    let mut visited_objects = BTreeSet::new();
    let mut visited_closures = BTreeSet::new();
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
                if let Some(properties) = context.properties.get(&allocation) {
                    pending_values.extend(properties.values().flatten().cloned());
                }
                if let Some(properties) = context.private_properties.get(&allocation) {
                    pending_values.extend(properties.values().flatten().cloned());
                }
            }
            AbstractValue::Value(Value::Alloc(Kind::Function, callable)) => {
                if visited_closures.insert(callable) {
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
        body_effects: Some(transfer.effects),
        record: None,
        closure: None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::facts::StableArraySpreadShapes;
    use crate::facts::item_views::top_level_item_views;
    use swc_ecma_ast::{Decl, Expr, FnDecl, ModuleItem, Stmt};
    use swc_ecma_visit::{Visit, VisitWith};

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
            Vec::new(),
            Vec::new(),
            Default::default(),
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
            Vec::new(),
            Vec::new(),
            Default::default(),
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
                        .is_some_and(|effects| effects.has_closed_effects()),
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
                assert!(effects.has_closed_effects(), "{source}: {effects:#?}");
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
                summary
                    .body_effects
                    .as_ref()
                    .is_some_and(|effects| !effects.has_closed_effects()),
                "{source}"
            );
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
                Vec::new(),
                Vec::new(),
                Default::default(),
                Default::default(),
            );
            let caller = js_ast::parse_js_module("caller.js", "import { make } from './provider.js'; const wrapped = make({ name: 'report', create() { return later(); }, get schema() { return later(); } }); var later = () => 42; console.log(wrapped.schema, wrapped.create());").unwrap().module;
            let caller_views = top_level_item_views(&caller.body);
            let caller_prepared = super::super::prepare_callable_capabilities(
                &caller_views,
                StableArraySpreadShapes::default(),
                BTreeSet::new(),
                Vec::new(),
                Vec::new(),
                Default::default(),
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
            let execution =
                execute_provider_body(&prepared.index, *key, &prepared.shadowed, Some(input))
                    .unwrap_or_else(|| {
                        panic!("canonical body: {body:#?}; native selections: {native:#?}")
                    });
            assert!(matches!(
                execution.result.judgment,
                AllocationJudgment::Opaque
            ));
            let effects = execution.body_effects.expect("complete operation query");
            assert!(effects.has_closed_effects(), "{effects:#?}");
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
                assert!(effects.has_closed_effects(), "{source}");
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
                    .is_some_and(|effects| effects.has_closed_effects()),
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
                        .is_some_and(|effects| effects.has_closed_effects()),
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
                    .is_some_and(|effects| effects.has_closed_effects()),
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
                        .is_none_or(|effects| !effects.has_closed_effects()),
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
                            .is_some_and(|effects| effects.has_closed_effects()),
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
                        .is_some_and(|effects| effects.has_closed_effects())
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
                    Vec::new(),
                    Vec::new(),
                    Default::default(),
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
                        .is_some_and(|effects| effects.has_closed_effects()),
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
                    Vec::new(),
                    Vec::new(),
                    Default::default(),
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
