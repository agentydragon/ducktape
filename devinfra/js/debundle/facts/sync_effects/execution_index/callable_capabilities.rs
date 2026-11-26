//! Callable-capability inference over the prepared execution index.

use super::super::strict_provider;
use super::native_write_census::{ArraySpreadProtocolIntegrity, NativeWriteCensus};
use super::{CallKind, ExecutionIndex, SemanticShapeInputs};
use analysis_domain::integrity::{IntegrityFlags, IntrinsicSlot, PrototypeWrites};
use analysis_domain::item_views::TopLevelItemView;
use analysis_vocabulary::execution_evidence::SourceLocator;
use analysis_vocabulary::inputs::{
    AnalysisInputs, BundleGlobalNames, HostBehaviorKey, InferredSetConstruction, NativeInputKind,
    NativeIntegrity, NativeIntrinsic, ProgramPremiseKey, ProofDependencies,
    ResolvedBindingValueInput, ResolvedNativeOperationInput, SelectedBindingValueInput,
    SelectedNativeOperationInput,
};
use analysis_vocabulary::report_schema::NativeMutationWitness;
use analysis_vocabulary::value::{Kind, Value};
use purity::callable_inference::{
    CallableInferenceInputs, FunctionBindingFacts, NamespaceWrapperCandidateFacts,
};
use purity::semantic_inference::{SemanticFact, SemanticKey};
use purity::{LocalClassConstructionShapes, SetIterableShapes, StableArraySpreadShapes};
use source_identity::SourceUnitOrdinal;
use std::collections::{BTreeMap, BTreeSet};
use std::ops::Range;
use std::sync::Arc;
use swc_ecma_ast::*;
use value_flow::ChunkValueFlow;
use value_model::{CallSite, MemberKey, ValueExpr};

/// An imported one-argument call supplies only the first parameter. The
/// returned closure can therefore retain that callback and omitted parameters,
/// but it must not capture another source/host capability that the abstract
/// returned callback graph would omit when the result is invoked later. The
/// factory call itself is a derived foreign call: it runs no other code.
pub(super) fn deferred_callback_closure(
    index: &ExecutionIndex,
    outer: u32,
    closure: &Range<u32>,
) -> bool {
    let (Some(outer), Some(inner)) = (
        index.callables.get(&outer),
        index.callables.get(&closure.start),
    ) else {
        return false;
    };
    let Some(callback) = outer
        .params
        .first()
        .and_then(|param| param.binding.as_ref())
    else {
        return false;
    };
    // The factory must return the closure literal directly. A body that binds
    // it first keeps opaque timing; author source policies
    // (`source_export_policy` fixtures) are tested against such a provider.
    if !outer.declared_locals.is_empty()
        || inner.opaque
        || inner.generator
        || inner.implicit_capture
        || index.async_callables.contains(&closure.start)
        || !inner.params.is_empty()
        || inner.dynamic_property_effect
        || !inner.member_writes.is_empty()
        || !inner.member_value_writes.is_empty()
        || !inner.callback_result_writes.is_empty()
        || index
            .callables
            .range((closure.start + 1)..closure.end)
            .next()
            .is_some()
    {
        return false;
    }
    let allowed: BTreeSet<_> = outer
        .params
        .iter()
        .filter_map(|param| param.binding.clone())
        .chain(inner.declared_locals.iter().cloned())
        .collect();
    let local_receivers: BTreeSet<_> = allowed
        .iter()
        .filter(|id| *id != callback)
        .cloned()
        .collect();
    let has_fresh_own_data_property = |value: &ValueExpr, property: &str| {
        matches!(value, ValueExpr::ObjectLiteral(object, fields)
            if index.plain_data_objects.contains(object)
                && fields.iter().any(|(name, _)| name.as_deref() == Some(property)))
    };
    inner.reads.is_subset(&allowed)
        && inner.rebinds.is_subset(&local_receivers)
        && inner
            .local_assignments
            .iter()
            .all(|(id, _)| local_receivers.contains(id))
        && inner
            .calls
            .iter()
            .all(|call| match (&call.kind, &call.callee) {
                (CallKind::Call, ValueExpr::Binding(id)) => id == callback && !call.has_spread,
                (
                    CallKind::LocalMemberRead,
                    ValueExpr::Member(receiver, Some(property), MemberKey::Static, _),
                ) => {
                    match receiver.as_ref() {
                        ValueExpr::Binding(id) if local_receivers.contains(id) => {
                            inner.local_assignments.iter().any(|(assigned, value)| {
                                assigned == id && has_fresh_own_data_property(value, property)
                            }) && inner
                                .local_assignments
                                .iter()
                                .filter(|(assigned, _)| assigned == id)
                                .all(|(_, value)| has_fresh_own_data_property(value, property))
                        }
                        // ValueExpr reduces an assignment expression to its RHS.
                        // The indexed member receiver is still this fresh literal.
                        value @ ValueExpr::ObjectLiteral(..) => {
                            has_fresh_own_data_property(value, property)
                        }
                        _ => false,
                    }
                }
                _ => false,
            })
}

/// A derived foreign call's fresh record result, with each member's provider
/// captures for the report.
pub struct InferredReturnedRecord {
    pub members: Vec<(String, u32, BTreeSet<String>)>,
    pub inference: analysis_vocabulary::provenance::InferenceUse,
}

pub struct InferredNamespaceWrapper {
    pub function_start: u32,
    pub inference: analysis_vocabulary::provenance::InferenceUse,
    pub dependencies: ProofDependencies,
}

pub struct InferredCallableCapabilities {
    /// Exported local → derived call summary (`foreign_call`).
    pub foreign_calls: BTreeMap<String, InferredForeignCall>,
    /// Keyed-collection families whose global constructor slot this module's
    /// census leaves intact.
    pub intact_collections: BTreeSet<NativeIntrinsic>,
    pub stable_providers: BTreeSet<String>,
    pub namespace_wrappers: BTreeMap<String, InferredNamespaceWrapper>,
}

pub struct InferredForeignCall {
    pub derived: crate::foreign_call::DerivedForeignCall,
    pub declaration_start: u32,
    pub declaration: value_flow::CallableDeclaration,
    pub dependencies: ProofDependencies,
    pub record: Option<InferredReturnedRecord>,
    /// The returned closure defers its first argument (`deferred_callback_closure`).
    pub callback_factory: bool,
}

impl InferredForeignCall {
    pub fn permits_foreign_summary(&self) -> bool {
        self.declaration.permits_foreign_summary(
            self.derived.retained_provider_captures.is_empty()
                && self.derived.provider_cell_reads.is_empty(),
        )
    }
}

/// Prepared once for callable inference and the whole-program native census.
/// Value flow remains demand-driven even when every module contributes writes.
pub struct PreparedCallableCapabilities {
    pub(in super::super) index: ExecutionIndex,
    pub(super) exported_locals: BTreeSet<String>,
    pub(super) flow: Option<ChunkValueFlow>,
}

impl PreparedCallableCapabilities {
    /// A sufficient whole-source namespace privacy receipt, covering only
    /// these exact canonical IDs. No value-flow or ordered execution fact is
    /// consumed. Callers must independently check canonical dynamic scope,
    /// provider stability/closure and each requested member contract.
    pub fn source_private_namespace_family(&self) -> Option<&BTreeMap<Id, BTreeSet<String>>> {
        let privacy = &self.index.namespace_privacy;
        (!privacy.members.is_empty()
            && !privacy.invalid
            && self.index.dynamic_scope_owners.is_empty())
        .then_some(&privacy.members)
    }

    /// A negative check for an exact audited imported call. This reads the
    /// already prepared callable index; unresolved calls remain the author's
    /// responsibility rather than becoming inferred absence of effects.
    pub fn known_external_write_for_local(&self, local: &str) -> Option<String> {
        let index = &self.index;
        let binding = index
            .hoisted_bindings
            .keys()
            .chain(index.top_assignments.iter().flatten().map(|(id, _)| id))
            .find(|id| id.0.as_ref() == local)?;
        let start = direct_callable_binding(index, binding, &mut BTreeSet::new())?;
        let mut pending = vec![start];
        let mut visited = BTreeSet::new();
        while let Some(key) = pending.pop() {
            if !visited.insert(key) {
                continue;
            }
            let callable = &index.callables[&key];
            let known_nonfresh = |receiver: &Id| {
                // A helper parameter or closure capture can still point to
                // the wrapper allocated by its caller. Only an identified
                // module/global binding is a definite contradiction here.
                !callable.declared_locals.contains(receiver)
                    && !callable.pattern_locals.contains(receiver)
                    && !callable
                        .params
                        .iter()
                        .any(|param| param.bindings.contains(receiver))
                    && ((index.global_object_names.contains(receiver.0.as_ref())
                        && !index.callables.values().any(|scope| {
                            scope.declared_locals.contains(receiver)
                                || scope.pattern_locals.contains(receiver)
                                || scope
                                    .params
                                    .iter()
                                    .any(|param| param.bindings.contains(receiver))
                        }))
                        || index.imported_bindings.contains(receiver)
                        || index.hoisted_bindings.contains_key(receiver)
                        || index.top_const_declarations.contains(receiver)
                        || index
                            .top_assignments
                            .iter()
                            .flatten()
                            .any(|(id, _)| id == receiver))
            };
            for (receiver, property) in &callable.member_writes {
                if known_nonfresh(receiver) {
                    return Some(format!("property write {}.{property}", receiver.0));
                }
            }
            for receiver in &callable.dynamic_property_receivers {
                if let ValueExpr::Binding(receiver) = receiver
                    && known_nonfresh(receiver)
                {
                    return Some(format!("computed property write on {}", receiver.0));
                }
            }
            for binding in &callable.rebinds {
                if known_nonfresh(binding) {
                    return Some(format!("outer binding write {}", binding.0));
                }
            }
            for call in &callable.calls {
                if call.kind != CallKind::Call {
                    continue;
                }
                let ValueExpr::Binding(callee) = &call.callee else {
                    continue;
                };
                if let Some(&helper) = callable.local_functions.get(callee) {
                    pending.push(helper);
                } else if let Some(helper) =
                    direct_callable_binding(index, callee, &mut BTreeSet::new())
                {
                    pending.push(helper);
                }
            }
        }
        None
    }

    /// Called with the conservative completed all-source census before any
    /// structural flow. A later refined census cannot add roots retroactively.
    pub fn inferred_set_constructions(
        &self,
        integrity: &NativeIntegrity,
    ) -> BTreeMap<u32, InferredSetConstruction> {
        // Keep the early proof independent of the late index flags. A11 gives
        // original slot identities; only this completed all-source census (or
        // its validated program premise) may preserve them. Default flags are
        // open/Unknown and no author exact-site axiom is supplied here.
        let dependencies = ProofDependencies {
            value_inputs: integrity.consumed_value_inputs.clone(),
            host_behavior: integrity.consumed_host_behavior.clone(),
            no_accessor_install_callables: if integrity
                .consumed_host_behavior
                .contains(&HostBehaviorKey::PredefinedGlobals)
            {
                self.index
                    .census_globals
                    .bundle
                    .no_accessor_install_callables
                    .clone()
            } else {
                BTreeSet::new()
            },
            program_premises: if integrity.never_written_overrides.is_empty() {
                BTreeSet::new()
            } else {
                BTreeSet::from([ProgramPremiseKey::IntrinsicsNeverWritten])
            },
            ..ProofDependencies::default()
        };
        let flags = IntegrityFlags::new(
            self.index.dynamic_scope_owners.is_empty(),
            integrity.mutated.clone(),
            &integrity.witnesses,
            dependencies,
            BTreeMap::new(),
            PrototypeWrites::new(
                integrity
                    .prototype_mutations
                    .iter()
                    .map(|write| write.properties.as_ref()),
            ),
        )
        .with_never_written(integrity.never_written.clone());
        self.index.infer_set_constructions(&flags)
    }

    pub fn value_input_errors(&self) -> &[String] {
        &self.index.value_input_errors
    }

    pub fn resolved_value_inputs(&self) -> Vec<ResolvedBindingValueInput> {
        self.index.resolved_value_inputs.clone()
    }

    pub fn native_operation_input_errors(&self) -> &[String] {
        &self.index.native_operation_input_errors
    }

    pub fn resolved_native_operation_inputs(&self) -> Vec<ResolvedNativeOperationInput> {
        self.index.resolved_native_operation_inputs.clone()
    }

    pub fn known_global_native_operation_mutation_owner(
        &self,
        operation: NativeInputKind,
    ) -> Option<SourceUnitOrdinal> {
        self.index
            .known_global_native_operation_mutation(operation)
            .map(|(_, owner)| owner)
    }

    pub fn known_object_freeze_mutation_owner(&self) -> Option<SourceUnitOrdinal> {
        self.index.known_object_freeze_mutation()
    }

    pub fn known_descriptor_copy_mutation_owner(
        &self,
        assigned_keys: &BTreeSet<String>,
    ) -> Option<SourceUnitOrdinal> {
        self.index.known_descriptor_copy_mutation(assigned_keys)
    }

    pub fn known_set_protocol_mutation_owner(&self) -> Option<SourceUnitOrdinal> {
        self.index.known_set_protocol_mutation()
    }

    pub fn known_array_iterator_protocol_mutation_owner_in(
        &self,
        integrity: &ArraySpreadProtocolIntegrity,
    ) -> Option<SourceUnitOrdinal> {
        self.index
            .known_array_iterator_protocol_mutation_with(integrity)
    }

    pub fn locate_value_inputs(&mut self, source: Option<SourceLocator<'_>>) {
        let locate = |span| source?.span(span);
        for input in &mut self.index.resolved_value_inputs {
            input.evidence.declaration_location = locate(input.declaration_span);
            input.evidence.initializer_location = locate(input.initializer_span);
            self.index
                .value_input_evidence
                .insert(input.evidence.reference.clone(), input.evidence.clone());
        }
        for witness in Arc::make_mut(&mut self.index.native_write_census.witnesses) {
            witness.value_input_evidence = witness
                .value_input_refs
                .iter()
                .filter_map(|reference| self.index.value_input_evidence.get(reference).cloned())
                .collect();
        }
        for write in &mut self.index.native_write_census.conditional {
            write.witness.value_input_evidence = write
                .witness
                .value_input_refs
                .iter()
                .filter_map(|reference| self.index.value_input_evidence.get(reference).cloned())
                .collect();
        }
    }

    pub fn locate_native_operation_inputs(&mut self, source: Option<SourceLocator<'_>>) {
        let locate = |span| source?.span(span);
        for input in &mut self.index.resolved_native_operation_inputs {
            input.evidence.declaration_location = locate(input.declaration_span);
            input.evidence.initializer_location = locate(input.call_span);
            self.index
                .native_operation_input_evidence
                .insert(CallSite::from(input.call_span), input.evidence.clone());
        }
    }

    pub fn locate_native_writes(&mut self, chunk_id: &str, source: Option<SourceLocator<'_>>) {
        self.index.source_chunk_id = Some(chunk_id.to_owned());
        let source_location = |span| source?.span(span);
        let locate = |witness: &mut NativeMutationWitness| {
            if witness.receiver_roots.is_empty() {
                return;
            }
            witness.chunk_id = chunk_id.to_owned();
            witness.source_location = source_location(witness.span);
            if let Some(proof) = &mut witness.unreachable_branch {
                proof.guard_location = source_location(proof.guard_span);
                proof.wrapper_call_location = source_location(proof.wrapper_call_span);
                proof.callback_location = source_location(proof.callback_span);
            }
        };
        for witness in Arc::make_mut(&mut self.index.native_write_census.witnesses) {
            locate(witness);
        }
        for write in &mut self.index.native_write_census.conditional {
            locate(&mut write.witness);
        }
        for mutation in Arc::make_mut(&mut self.index.native_write_census.prototype_mutations) {
            mutation.chunk_id = chunk_id.to_owned();
            mutation.source_location = source_location(mutation.span);
        }
    }

    pub fn native_writes(&self) -> &NativeWriteCensus {
        &self.index.native_write_census
    }

    pub fn refine_native_branches(
        &mut self,
        summaries: &BTreeMap<String, analysis_vocabulary::inputs::CallableSummary>,
    ) {
        self.index.refine_native_branches(summaries);
    }

    pub fn callback_object_arguments<'f>(
        &self,
        body: &[TopLevelItemView<'_>],
        structural_flow: impl FnOnce() -> &'f ChunkValueFlow,
    ) -> BTreeMap<String, (u32, BTreeSet<usize>)> {
        // Only source identity is complete here: callback argument coverage
        // helps resolve native writes, so no native construction is admitted.
        let arguments = |call: &InferredForeignCall| {
            super::callback_arguments::ordinary_callback_arguments(
                &self.index,
                call.declaration_start,
                call.derived.closure.as_ref()?,
            )
            .filter(|_| call.callback_factory)
        };
        infer_foreign_calls(
            &self.index,
            body,
            &self.exported_locals,
            self.flow.as_ref(),
            structural_flow,
            |_| false,
            |call| arguments(call).is_some(),
        )
        .into_iter()
        .filter_map(|(local, call)| Some((local, (call.declaration_start, arguments(&call)?))))
        .collect()
    }
}

pub(super) fn direct_callable_binding(
    index: &ExecutionIndex,
    binding: &Id,
    visited: &mut BTreeSet<Id>,
) -> Option<u32> {
    if !visited.insert(binding.clone()) || index.reassigned_bindings.contains(binding) {
        return None;
    }
    if let Some(&key) = index.hoisted_bindings.get(binding) {
        return Some(key);
    }
    let assignments = index
        .top_assignments
        .iter()
        .flatten()
        .filter(|(id, _)| id == binding)
        .map(|(_, value)| value)
        .collect::<Vec<_>>();
    match assignments.as_slice() {
        [ValueExpr::Alloc(Kind::Function, key)] if index.callables.contains_key(key) => Some(*key),
        [ValueExpr::Binding(alias)] => direct_callable_binding(index, alias, visited),
        _ => None,
    }
}

pub fn prepare_callable_capabilities(
    body: &[TopLevelItemView<'_>],
    exported_locals: BTreeSet<String>,
    selected_value_inputs: Vec<SelectedBindingValueInput>,
    selected_native_operation_inputs: Vec<SelectedNativeOperationInput>,
    predefined_globals: spec::PredefinedGlobals,
    bundle_globals: BundleGlobalNames,
    namespace_privacy_members: BTreeMap<String, BTreeSet<String>>,
) -> PreparedCallableCapabilities {
    let flow = (!selected_value_inputs.is_empty()).then(|| ChunkValueFlow::analyze(body));
    PreparedCallableCapabilities {
        index: ExecutionIndex::collect(
            body,
            &AnalysisInputs {
                selected_value_inputs,
                selected_native_operation_inputs,
                assumptions: analysis_vocabulary::inputs::AnalysisAssumptions {
                    predefined_globals,
                    bundle_globals,
                    namespace_independent_members: namespace_privacy_members,
                    ..Default::default()
                },
                ..AnalysisInputs::default()
            },
            flow.as_ref(),
            &BTreeSet::new(),
            &LocalClassConstructionShapes::default(),
        ),
        exported_locals,
        flow,
    }
}

/// Consume the same prepared facts after resolving all modules' native writes.
pub fn infer_callable_capabilities<'f>(
    body: &[TopLevelItemView<'_>],
    prepared: PreparedCallableCapabilities,
    integrity: &NativeIntegrity,
    required_stable_providers: &BTreeSet<String>,
    derive_foreign_calls: bool,
    structural_flow: impl FnOnce() -> &'f ChunkValueFlow,
) -> InferredCallableCapabilities {
    let PreparedCallableCapabilities {
        mut index,
        exported_locals,
        flow: mut prepared_flow,
    } = prepared;
    index.apply_native_integrity(integrity);
    let mut result = InferredCallableCapabilities {
        foreign_calls: BTreeMap::new(),
        intact_collections: BTreeSet::new(),
        stable_providers: BTreeSet::new(),
        namespace_wrappers: BTreeMap::new(),
    };
    let has_candidate = index.hoisted_bindings.iter().any(|(id, key)| {
        required_stable_providers.contains(id.0.as_ref())
            || (exported_locals.contains(id.0.as_ref())
                && index.namespace_wrapper_candidates.contains_key(key))
    });
    if has_candidate {
        let flow = prepared_flow
            .take()
            .unwrap_or_else(|| ChunkValueFlow::analyze(body));
        let shadowed = analysis_domain::item_views::compute_shadowed_globals(body);
        let callables = callable_inference_inputs(&index, &flow, &exported_locals);
        index.semantic_facts = Some(index.prepare_semantic_facts(
            &flow,
            &shadowed,
            SemanticShapeInputs {
                classes: LocalClassConstructionShapes::default(),
                sets: SetIterableShapes::default(),
                array_spreads: &StableArraySpreadShapes::default(),
                callables,
            },
            None,
        ));
        result.stable_providers = index
            .hoisted_bindings
            .iter()
            .filter(|(binding, _)| required_stable_providers.contains(binding.0.as_ref()))
            .filter(|(binding, function)| {
                index.semantic_facts.as_ref().is_some_and(|facts| {
                    facts
                        .judgment(&SemanticKey::StableProvider {
                            binding: (*binding).clone(),
                            function: **function,
                        })
                        .is_some()
                })
            })
            .map(|(binding, _)| binding.0.to_string())
            .collect();
        result.namespace_wrappers = infer_namespace_wrappers(&index, &exported_locals);
        prepared_flow = Some(flow);
    }
    if derive_foreign_calls {
        result.foreign_calls = infer_foreign_calls(
            &index,
            body,
            &exported_locals,
            prepared_flow.as_ref(),
            structural_flow,
            |family| {
                matches!(
                    index
                        .integrity
                        .require(&[IntrinsicSlot::Global(family)], None),
                    analysis_domain::integrity::Integrity::Intact(_)
                )
            },
            |call| call.permits_foreign_summary() || call.callback_factory,
        );
    }
    result.intact_collections = [
        NativeIntrinsic::Map,
        NativeIntrinsic::Set,
        NativeIntrinsic::WeakMap,
        NativeIntrinsic::WeakSet,
    ]
    .into_iter()
    .filter(|family| {
        matches!(
            index
                .integrity
                .require(&[IntrinsicSlot::Global(*family)], None),
            analysis_domain::integrity::Integrity::Intact(_)
        )
    })
    .collect();
    result
}

/// Exported top-level function and class declarations, and single-declarator
/// top-level function or arrow initializers, with one stable value, summarized
/// by `foreign_call`. Dynamic scope can rebind any of them. `collection_intact`
/// answers the census for an empty keyed collection's constructor; `wanted`
/// selects summaries before value flow checks their binding's stability.
pub(super) fn infer_foreign_calls<'f>(
    index: &ExecutionIndex,
    body: &[TopLevelItemView<'_>],
    exported_locals: &BTreeSet<String>,
    flow: Option<&ChunkValueFlow>,
    structural_flow: impl FnOnce() -> &'f ChunkValueFlow,
    collection_intact: impl Fn(NativeIntrinsic) -> bool,
    wanted: impl Fn(&InferredForeignCall) -> bool,
) -> BTreeMap<String, InferredForeignCall> {
    if !index.dynamic_scope_owners.is_empty() {
        return BTreeMap::new();
    }
    let flow = match flow {
        Some(flow) => flow,
        None => structural_flow(),
    };
    let shadowed = analysis_domain::item_views::compute_shadowed_globals(body);
    let candidates: Vec<_> = flow
        .callable_declarations()
        .filter(|(binding, _)| exported_locals.contains(binding.0.as_ref()))
        .filter(|(binding, _)| !index.reassigned_bindings.contains(*binding))
        .filter_map(|(binding, declaration)| {
            let declaration_start = declaration.start();
            let derived = if matches!(declaration, value_flow::CallableDeclaration::Class(_)) {
                strict_provider::derive_class(index, declaration_start, &shadowed)?
            } else {
                // All callable bodies consume one shared source-covered plan.
                // Refusal never retries another evaluator.
                strict_provider::derive(index, declaration_start, &shadowed)?
            };
            if !derived
                .native_constructions
                .iter()
                .all(|family| collection_intact(*family))
            {
                return None;
            }
            Some((
                binding,
                foreign_call(index, derived, declaration_start, declaration),
            ))
        })
        .filter(|(_, call)| wanted(call))
        .collect();
    if candidates.is_empty() {
        return BTreeMap::new();
    }
    // Binding values do not depend on the tracked candidate objects, so the
    // structural flow the purity oracle solves for this body anyway answers
    // this check.
    candidates
        .into_iter()
        .filter(|(binding, call)| {
            let values = flow.values_of_binding(binding);
            // An initializer's binding holds `undefined` (or is in its TDZ)
            // until its statement runs; afterwards it must hold only that
            // function.
            if call.declaration.is_declaration() {
                values.iter().count() == 1
            } else {
                values
                    .iter()
                    .eq([Value::Alloc(Kind::Function, call.declaration_start)])
            }
        })
        .map(|(binding, call)| (binding.0.to_string(), call))
        .collect()
}

pub(super) fn foreign_call(
    index: &ExecutionIndex,
    derived: crate::foreign_call::DerivedForeignCall,
    declaration_start: u32,
    declaration: value_flow::CallableDeclaration,
) -> InferredForeignCall {
    let mut dependencies = derived.dependencies.clone();
    for family in &derived.native_constructions {
        dependencies.extend(
            &index
                .integrity
                .require(&[IntrinsicSlot::Global(*family)], None)
                .dependencies()
                .expect("derivation checked the census for every construction"),
        );
    }
    let record = derived
        .record
        .as_ref()
        .map(|members| returned_record(index, members, &derived, &dependencies));
    let callback_factory = derived.closure.as_ref().is_some_and(|closure| {
        derived.returns
            == BTreeSet::from([analysis_vocabulary::inputs::ForeignReturn::FreshAllocation])
            && deferred_callback_closure(index, declaration_start, closure)
    });
    InferredForeignCall {
        declaration_start,
        declaration,
        derived,
        dependencies,
        record,
        callback_factory,
    }
}

/// Report projection of a derived record: each member's free provider
/// bindings, and the rules the derivation used.
pub(super) fn returned_record(
    index: &ExecutionIndex,
    members: &[crate::foreign_call::RecordMember],
    derived: &crate::foreign_call::DerivedForeignCall,
    dependencies: &ProofDependencies,
) -> InferredReturnedRecord {
    use analysis_vocabulary::provenance::{InferenceRule, InferenceUse};
    let members = members
        .iter()
        .map(|member| {
            let callable = &index.callables[&member.function_start];
            let parameters: BTreeSet<_> = callable
                .params
                .iter()
                .filter_map(|param| param.binding.as_ref())
                .collect();
            let captures = callable
                .reads
                .difference(&callable.declared_locals)
                .filter(|binding| !parameters.contains(binding))
                .map(|binding| binding.0.to_string())
                .collect();
            (member.name.clone(), member.function_start, captures)
        })
        .collect();
    let mut derivation_rules =
        BTreeSet::from([InferenceRule::ReturnedRecord, InferenceRule::StableFunction]);
    if !derived.native_constructions.is_empty() {
        derivation_rules.extend([
            InferenceRule::EmptyCollectionConstruction,
            InferenceRule::NativeCallIdentity,
            InferenceRule::NativeIntegrity,
        ]);
    }
    InferredReturnedRecord {
        members,
        inference: InferenceUse {
            conclusion: InferenceRule::ReturnedRecord,
            derivation_rules,
            value_input_refs: dependencies.value_inputs.clone(),
            descriptor_copy_author_refs: BTreeSet::new(),
            argument_input_refs: dependencies.argument_inputs.clone(),
            native_operation_input_refs: dependencies.native_operation_inputs.clone(),
            imported_primitive_array_spread_input_refs: dependencies
                .imported_primitive_array_spread_inputs
                .clone(),
        },
    }
}

pub(super) fn namespace_wrapper_inputs(
    index: &ExecutionIndex,
    exported_locals: &BTreeSet<String>,
) -> BTreeMap<u32, NamespaceWrapperCandidateFacts> {
    let mut inferred = BTreeMap::new();
    for (binding, &function_start) in &index.hoisted_bindings {
        if !exported_locals.contains(binding.0.as_ref()) {
            continue;
        }
        let Some(candidate) = index.namespace_wrapper_candidates.get(&function_start) else {
            continue;
        };
        let Some([cache_declaration]) = index
            .binding_declaration_writes
            .get(&candidate.cache)
            .map(Vec::as_slice)
        else {
            continue;
        };
        let Some(ValueExpr::New(weak_map, args, _)) = &cache_declaration.initializer else {
            continue;
        };
        let ValueExpr::Binding(weak_map) = weak_map.as_ref() else {
            continue;
        };
        if !cache_declaration.direct_top_level
            || !cache_declaration.simple_identifier
            || !index.top_const_declarations.contains(&candidate.cache)
            || index.exported_bindings.contains(&candidate.cache)
            || index.reassigned_bindings.contains(&candidate.cache)
            || !args.is_empty()
            || weak_map.0.as_ref() != "WeakMap"
            || index
                .binding_use_bounds
                .get(&candidate.cache)
                .is_none_or(|(first, last)| {
                    *first < function_start || *last >= candidate.function_end
                })
        {
            continue;
        }
        inferred.insert(
            function_start,
            NamespaceWrapperCandidateFacts {
                object: candidate.object.clone(),
                weak_map: weak_map.clone(),
            },
        );
    }
    inferred
}

/// Retain canonical candidates without granting their semantic conclusions.
pub(super) fn callable_inference_inputs(
    index: &ExecutionIndex,
    flow: &ChunkValueFlow,
    exported_locals: &BTreeSet<String>,
) -> CallableInferenceInputs {
    let mut inputs = CallableInferenceInputs {
        namespace_wrappers: namespace_wrapper_inputs(index, exported_locals),
        ..CallableInferenceInputs::default()
    };
    let rebound: BTreeSet<_> = index
        .top_assignments
        .iter()
        .flatten()
        .map(|(binding, _)| binding.clone())
        .chain(
            index
                .callables
                .values()
                .flat_map(|callable| callable.rebinds.iter().cloned()),
        )
        .collect();
    for binding in index
        .hoisted_bindings
        .keys()
        .filter(|binding| exported_locals.contains(binding.0.as_ref()))
    {
        inputs.functions.insert(
            binding.clone(),
            FunctionBindingFacts {
                values: flow.values_of_binding(binding),
                rebound: rebound.contains(binding),
            },
        );
    }
    inputs
}

pub(super) fn infer_namespace_wrappers(
    index: &ExecutionIndex,
    exported_locals: &BTreeSet<String>,
) -> BTreeMap<String, InferredNamespaceWrapper> {
    index
        .hoisted_bindings
        .iter()
        .filter_map(|(binding, &function)| {
            if !exported_locals.contains(binding.0.as_ref()) {
                return None;
            }
            let semantic_facts = index.semantic_facts.as_ref()?;
            let key = SemanticKey::NamespaceWrapper {
                binding: binding.clone(),
                function,
            };
            let judgment = semantic_facts.judgment(&key)?;
            let SemanticFact::NamespaceWrapper(function) = judgment.value() else {
                return None;
            };
            Some((
                binding.0.to_string(),
                InferredNamespaceWrapper {
                    function_start: *function,
                    inference: semantic_facts.inference_use(&key)?,
                    dependencies: judgment.dependencies().clone(),
                },
            ))
        })
        .collect()
}

#[cfg(test)]
mod preflow_set_protocol_tests {
    use super::super::native_write_census::{
        apply_intrinsics_never_written, resolve_native_integrity,
    };
    use super::*;
    use analysis_vocabulary::inputs::InferredSetOperation;
    use raw_js_test_support::parse;

    // This tests the pre-flow prerequisite seam, which the CLI only exposes
    // after flow and execution. It uses the real source/native preparation.
    fn proofs(source: &str) -> BTreeMap<u32, InferredSetConstruction> {
        proofs_with_premises(source, &BTreeSet::new())
    }

    fn proofs_with_premises(
        source: &str,
        families: &BTreeSet<NativeIntrinsic>,
    ) -> BTreeMap<u32, InferredSetConstruction> {
        let module = parse(source);
        let views = analysis_domain::item_views::top_level_item_views(&module.body);
        let prepared = prepare_callable_capabilities(
            &views,
            BTreeSet::new(),
            Vec::new(),
            Vec::new(),
            spec::PredefinedGlobals::Unknown,
            BundleGlobalNames::default(),
            BTreeMap::new(),
        );
        assert!(prepared.flow.is_none());
        assert!(
            prepared
                .index
                .infer_set_constructions(&IntegrityFlags::default())
                .is_empty()
        );
        let mut census = resolve_native_integrity([prepared.native_writes()]);
        apply_intrinsics_never_written(&mut census, families)
            .expect("premise is not source-refuted");
        let proofs = prepared.inferred_set_constructions(&census);
        assert!(prepared.flow.is_none());
        proofs
    }

    #[test]
    fn full_primitive_set_spread_protocol_is_proved_before_flow() {
        let proofs = proofs(
            r#"console.log("before");
var seed = new Set(["a", "b"]), sibling = new Set(["x"]), copied = new Set([...seed, "c"]);
console.log("after", copied.size);"#,
        );
        assert_eq!(proofs.len(), 3);
        let spread = proofs
            .values()
            .find(|proof| proof.binding.0 == "copied")
            .unwrap();
        let InferredSetOperation::ProtectedSpread { interval, .. } = &spread.operation else {
            panic!("spread needs a protected source interval")
        };
        assert_eq!(
            interval
                .interval_bindings
                .iter()
                .map(|id| id.0.as_ref())
                .collect::<Vec<_>>(),
            ["seed", "sibling", "copied"]
        );
        assert!(spread.dependencies.native_operation_inputs.is_empty());
    }

    #[test]
    fn source_shape_never_substitutes_for_protocol_integrity() {
        for prefix in [
            "Set.prototype.add = function () {};",
            "Set.prototype[Symbol.iterator] = function () {};",
            "Object.getPrototypeOf(new Set()[Symbol.iterator]()).next = function () {};",
            "Array.prototype[Symbol.iterator] = function () {};",
        ] {
            let source =
                format!("{prefix} var seed = new Set(['a']), copied = new Set([...seed, 'b']);");
            assert!(
                !proofs(&source)
                    .values()
                    .any(|proof| proof.binding.0 == "copied"),
                "{prefix}"
            );
        }
    }

    #[test]
    fn preflow_protocol_consumes_the_existing_program_premise() {
        let source = "globalThis[unknownKey] = 1; var seed = new Set(['a']), sibling = new Set(['x']), copied = new Set([...seed, 'c']);";
        assert!(
            !proofs(source)
                .values()
                .any(|proof| proof.binding.0 == "copied")
        );
        let proven = proofs_with_premises(
            source,
            &BTreeSet::from([NativeIntrinsic::Set, NativeIntrinsic::Array]),
        );
        let copied = proven
            .values()
            .find(|proof| proof.binding.0 == "copied")
            .expect("all protocol slots inherit the validated Set/Array families");
        assert!(
            copied
                .dependencies
                .program_premises
                .contains(&ProgramPremiseKey::IntrinsicsNeverWritten)
        );
        assert!(copied.dependencies.native_operation_inputs.is_empty());
    }

    #[test]
    fn source_interval_requires_unique_stable_bindings_and_inert_siblings() {
        for source in [
            "var seed = new Set(['a']); var copied = new Set([...seed, 'c']);",
            "var seed = new Set(['a']), seed = new Set(['b']), copied = new Set([...seed, 'c']);",
            "var seed = new Set(['a']), sibling = mutate(seed), copied = new Set([...seed, 'c']);",
            "var seed = new Set(['a']), copied = new Set([...seed, 'c']); seed = new Set(['b']);",
            "var seed = new Set(['a']), copied = new Set([...seed, callback]);",
        ] {
            assert!(
                !proofs(source)
                    .values()
                    .any(|proof| proof.binding.0 == "copied"),
                "{source}"
            );
        }
    }
}
