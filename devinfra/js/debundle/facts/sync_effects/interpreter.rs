//! Call-site execution effects distinguish function values from function bodies: returning a closure never executes it.

use super::super::value_model::{
    AbstractValue, ArrayIteration, ArrayIterationMethod, CallSite, Captured, CapturedContext,
    Context, DenseArray, FrozenNamespaceProof, ValueExpr, Values,
};
use super::native_calls::{CallInputs, CallReturn, native_call_model};
use super::{
    Call, CallKind, Callable, CallbackResultWrite, ExecutionIndex, IntrinsicRoot, LocalConstructor,
    RetainedCallbackCall,
};
use crate::analysis_inputs::AnalysisInputs;
use crate::facts::ExportReentryPolicy;
use crate::facts::fresh_receiver::fresh_mutation_callables;
use crate::facts::item_views::TopLevelItemView;
use crate::facts::local_effect_targets::MetadataDecorateCall;
use crate::facts::source_unit_read_closure::SourceUnitReadClosure;
use crate::facts::statement_facts::{
    CallbackFactoryBarrierFact, CallbackFactoryBarrierRejection, CallbackFactoryUseFact,
    StatementFacts, StatementKind, SynchronousCalls,
};
use crate::facts::value_flow::{ChunkValueFlow, ValueOrigin, Values as FlowValues};
use crate::facts::{LocalClassConstructionProof, set_iterables::SetIterableProof};
use crate::purity::ChunkCodeGraph;
use crate::reports::schema::{
    CallbackFactoryUseKind, ExecutionOperation, ExecutionPrecisionLoss,
    ExecutionPrecisionLossReason,
};
use source_identity::SourceUnitOrdinal;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;
use swc_common::{DUMMY_SP, Span, SyntaxContext};
use swc_ecma_ast::*;

fn from_flow(values: FlowValues) -> Values {
    values
        .into_iter()
        .map(|value| match value {
            ValueOrigin::Unknown => AbstractValue::Unknown,
            other => AbstractValue::Origin(other),
        })
        .collect()
}

fn unknown() -> Values {
    BTreeSet::from([AbstractValue::Unknown])
}

#[derive(Debug, Clone, Default)]
struct Outcome {
    effects: SynchronousCalls,
    returns: Values,
    complete: bool,
    executed_owners: BTreeSet<SourceUnitOrdinal>,
    unknown_call: bool,
}

impl Outcome {
    fn lose_precision(
        &mut self,
        reason: ExecutionPrecisionLossReason,
        owner: SourceUnitOrdinal,
        span: Span,
    ) {
        self.complete = false;
        let loss = ExecutionPrecisionLoss {
            reason,
            statement_ordinal: owner,
            source_location: None,
            span,
        };
        if !self.effects.precision_losses.contains(&loss) {
            self.effects.precision_losses.push(loss);
        }
    }

    fn unsupported(&mut self, operation: ExecutionOperation, owner: SourceUnitOrdinal, span: Span) {
        self.lose_precision(
            ExecutionPrecisionLossReason::UnsupportedOperation { operation },
            owner,
            span,
        );
    }

    fn inherit_completeness(&mut self, complete: bool, losses: Vec<ExecutionPrecisionLoss>) {
        self.complete &= complete;
        self.inherit_precision_losses(losses);
    }

    fn inherit_precision_losses(&mut self, losses: Vec<ExecutionPrecisionLoss>) {
        for loss in losses {
            if !self.effects.precision_losses.contains(&loss) {
                self.effects.precision_losses.push(loss);
            }
        }
    }
}

#[derive(Debug, Clone, Default)]
struct ReachabilityState {
    unknown_call_seen: bool,
    incomplete: bool,
    revision: u64,
    expanded: BTreeSet<SourceUnitOrdinal>,
    owners: BTreeSet<SourceUnitOrdinal>,
}

/// Callback roots retained by a fallback stay escaped for the rest of the
/// chunk. Cache their synchronous read closure once; initialization timing is
/// checked against the current prefix when the summary is applied.
#[derive(Default)]
struct CallbackFallbackSummary {
    expanded: BTreeSet<SourceUnitOrdinal>,
    incomplete_expanded: bool,
    reads: BTreeSet<Id>,
    rebinds: BTreeSet<Id>,
    read_additions: Vec<Id>,
    rebind_additions: Vec<Id>,
}

type InvocationSignature = (
    u32,
    Vec<Values>,
    Option<CapturedContext>,
    bool,
    u64,
    u64,
    Option<SourceUnitOrdinal>,
);

type ActiveInvocation = (u32, Option<SourceUnitOrdinal>);

// Callable cycles can keep changing their argument/capture snapshots and thus
// evade the state-specific memoization key. Bound recursive analysis separately
// from that cache so a finite source program cannot consume the Rust stack.
const MAX_SYNCHRONOUS_INVOCATION_DEPTH: usize = 64;
// Count actual expansions, including those whose changing prefix state defeats
// memoization. A cache-size limit alone does not bound a branching call tree.
const MAX_SYNCHRONOUS_INVOCATIONS: usize = 256;
// Giving every callback the entire pool as an argument creates a quadratic
// value cross product, even without recursion. Summarize large pools before
// manufacturing capture snapshots that retain that cross product.
const MAX_SYNCHRONOUS_CALLBACK_POOL: usize = 64;

struct Executor<'a> {
    index: &'a ExecutionIndex,
    graph: &'a ChunkCodeGraph,
    facts: &'a [StatementFacts],
    flow: &'a ChunkValueFlow,
    set_iterable_proof: &'a SetIterableProof,
    shadowed: &'a BTreeSet<&'static str>,
    imported_primitives: &'a BTreeSet<String>,
    /// Stable own primitive fields, admitted using canonical rebind facts.
    enum_data_members: BTreeMap<Id, BTreeSet<String>>,
    top_bindings: BTreeSet<Id>,
    binding_owner: BTreeMap<Id, SourceUnitOrdinal>,
    source_unit_read_closure: SourceUnitReadClosure,
    synchronous_owner_adjacency: Vec<Vec<SourceUnitOrdinal>>,
    synchronous_owner_reads: Vec<Option<&'a BTreeSet<Id>>>,
    class_member_effects: BTreeMap<SourceUnitOrdinal, (BTreeSet<Id>, BTreeSet<Id>)>,
    class_superclasses: BTreeMap<u32, Values>,
    active_class_member_promotions: BTreeSet<u32>,
    exported_owners: BTreeSet<SourceUnitOrdinal>,
    prior_reachability_roots: Option<BTreeSet<SourceUnitOrdinal>>,
    current_top_owner: Option<SourceUnitOrdinal>,
    call_site_owners: BTreeMap<CallSite, (SourceUnitOrdinal, Span)>,
    proof_uses: Vec<CallbackFactoryUseFact>,
    /// Owners executed while building a certified unpublished instance graph.
    /// Their synchronous effects still run; execution alone does not expose
    /// the wrapper or the classes' deferred callable members to the host.
    private_constructor_result_owners: BTreeSet<SourceUnitOrdinal>,
    prior_effect_owners: BTreeSet<SourceUnitOrdinal>,
    accepted_deferred_global_writes: BTreeSet<SourceUnitOrdinal>,
    frozen_namespace_calls: BTreeSet<CallSite>,
    reachability: ReachabilityState,
    mutable_captures: BTreeSet<Id>,
    prefix_values: Context,
    initialized_bindings: BTreeSet<Id>,
    var_bindings: BTreeSet<Id>,
    changed_member_roots: BTreeSet<Id>,
    global_escaped_callbacks: Values,
    prefix_object_properties: BTreeMap<u32, BTreeMap<Option<String>, Values>>,
    dense_arrays: BTreeMap<u32, DenseArray>,
    ordinary_spread_copies: BTreeSet<u32>,
    array_iteration_depth: usize,
    array_iteration_protected_objects: BTreeSet<u32>,
    array_iteration_unsupported: bool,
    array_intrinsic_mutated: bool,
    tainted_objects: BTreeSet<u32>,
    tainted_functions: BTreeSet<u32>,
    tainted_classes: BTreeSet<u32>,
    escaped_class_capabilities: BTreeSet<u32>,
    exported_runtime_values: Values,
    export_reentry_enabled: bool,
    fresh_mutation_callables: BTreeSet<u32>,
    prefix_revision: u64,
    invocation_depth: usize,
    invocation_count: usize,
    callback_fallback: CallbackFallbackSummary,
    fallback_outcome: Outcome,
    fallback_callback_sizes: Option<(usize, usize)>,
    fallback_local_owners: BTreeSet<SourceUnitOrdinal>,
    /// Returned callable source closures are static within one top-level
    /// initialization prefix. Incomplete reachability requires a fresh raw
    /// closure even when the precise prefix was already summarized.
    returned_callback_owners: BTreeSet<(bool, SourceUnitOrdinal)>,
    capture_contexts: BTreeMap<Arc<Context>, usize>,
    callback_sets: BTreeMap<Arc<Values>, usize>,
    active: BTreeSet<ActiveInvocation>,
    cache: BTreeMap<InvocationSignature, Outcome>,
}

#[derive(Clone, Copy)]
struct ValidatedMetadataDecoration {
    target_owner: SourceUnitOrdinal,
}

impl Executor<'_> {
    /// The abstract callback-factory value also represents namespace members
    /// and aliases. Only a checked direct imported binding identifies the
    /// summary without changing value or cache semantics.
    fn record_callback_factory_use(
        &mut self,
        imported_local: &Id,
        kind: CallbackFactoryUseKind,
        lexical_statement_ordinal: SourceUnitOrdinal,
        span: Span,
    ) {
        let Some(summary) = self.index.callback_factory_summaries.get(imported_local) else {
            return;
        };
        let proof_use = CallbackFactoryUseFact {
            imported_local: imported_local.0.to_string(),
            evidence: summary
                .callback_factory_evidence()
                .iter()
                .cloned()
                .collect(),
            kind,
            lexical_statement_ordinal,
            source_location: None,
            span,
        };
        if !self.proof_uses.contains(&proof_use) {
            self.proof_uses.push(proof_use);
        }
    }

    fn closed_enum_member_read(&self, value: &ValueExpr) -> bool {
        let ValueExpr::Member(receiver, Some(property), _) = value else {
            return false;
        };
        let ValueExpr::Binding(binding) = receiver.as_ref() else {
            return false;
        };
        self.initialized_bindings.contains(binding)
            && self
                .enum_data_members
                .get(binding)
                .is_some_and(|keys| keys.contains(property))
    }

    fn ordinary_object_prototype_intact(&self) -> bool {
        self.index.dynamic_scope_owners.is_empty()
            && !self
                .index
                .mutated_intrinsic_roots
                .iter()
                .any(|root| matches!(root, IntrinsicRoot::GlobalObject | IntrinsicRoot::Object))
    }

    /// A successful model lookup certifies the operation after its inputs
    /// have evaluated. Both return values and call timing use these same
    /// identity, mutation, and input guards; neither infers purity.
    fn resolve_native_call(
        &self,
        kind: CallKind,
        callee: &ValueExpr,
        expressions: &[ValueExpr],
        args: &[Values],
    ) -> Option<CallReturn> {
        let (binding, member) = match callee {
            ValueExpr::Binding(binding) => (binding, None),
            ValueExpr::Member(receiver, Some(member), true) => {
                let ValueExpr::Binding(binding) = receiver.as_ref() else {
                    return None;
                };
                (binding, Some(member.as_str()))
            }
            _ => return None,
        };
        let model = native_call_model(binding.0.as_ref(), member, kind)?;
        if self.flow.is_declared(binding)
            || self.shadowed.contains(binding.0.as_ref())
            || !self.index.dynamic_scope_owners.is_empty()
            || self
                .index
                .mutated_intrinsic_roots
                .iter()
                .any(|root| *root == IntrinsicRoot::GlobalObject || *root == model.intrinsic)
        {
            return None;
        }
        let admitted = match model.inputs {
            CallInputs::Primitives { min, max } => {
                (min..=max).contains(&args.len())
                    && args.iter().all(|values| {
                        !values.is_empty()
                            && values.iter().all(|value| {
                                matches!(
                                    value,
                                    AbstractValue::NonCallable | AbstractValue::Undefined
                                )
                            })
                    })
            }
            CallInputs::FreshDataRecord => {
                matches!(expressions, [ValueExpr::ObjectLiteral(object, _)]
                    if self.index.plain_data_objects.contains(object))
            }
        };
        admitted.then_some(model.returns)
    }

    /// Author-asserted timing for two exact operations in the pinned Bun host.
    /// This does not supply a return value or a purity/throw summary.
    fn host_call_does_not_reenter(&self, call: &Call) -> bool {
        if call.has_spread || !call.args.is_empty() || !self.index.dynamic_scope_owners.is_empty() {
            return false;
        }
        match (call.kind, &call.callee) {
            (CallKind::New, ValueExpr::Binding(binding)) => self
                .index
                .host_async_local_storage_imports
                .contains(binding),
            (CallKind::Call, ValueExpr::Member(receiver, Some(member), true))
                if call.no_reentry_candidate && member == "resolve" =>
            {
                let ValueExpr::Binding(binding) = receiver.as_ref() else {
                    return false;
                };
                self.index.host_promise_resolve
                    && binding.0.as_ref() == "Promise"
                    && !self.flow.is_declared(binding)
                    && !self.shadowed.contains("Promise")
                    && !self
                        .index
                        .mutated_intrinsic_roots
                        .contains(&IntrinsicRoot::Promise)
                    && !self
                        .index
                        .mutated_intrinsic_roots
                        .contains(&IntrinsicRoot::GlobalObject)
            }
            _ => false,
        }
    }

    fn native_return_values(returns: CallReturn, args: &[Values], may_throw: bool) -> Values {
        let mut values = match returns {
            CallReturn::Unknown => unknown(),
            CallReturn::Primitive => BTreeSet::from([AbstractValue::NonCallable]),
            CallReturn::Argument(index) => args.get(index).cloned().unwrap_or_else(unknown),
        };
        if may_throw {
            values.insert(AbstractValue::Unavailable);
        }
        values
    }

    fn standard_object_prototype_property(property: &str) -> bool {
        matches!(
            property,
            "constructor"
                | "__proto__"
                | "__defineGetter__"
                | "__defineSetter__"
                | "__lookupGetter__"
                | "__lookupSetter__"
                | "hasOwnProperty"
                | "isPrototypeOf"
                | "propertyIsEnumerable"
                | "toLocaleString"
                | "toString"
                | "valueOf"
        )
    }

    /// A certified private constructor may read a missing field on its fresh
    /// plain-data input. Keep this precision within that constructor call:
    /// other paths can join a fresh object with a live imported or published
    /// value, and their ordinary member-read fallback must remain conservative.
    /// A11 also excludes an inherited user getter unless a visible mutation
    /// has invalidated the ordinary Object.prototype assumption.
    fn absent_plain_data_property(&self, object: u32, property: &str) -> bool {
        if self.private_constructor_result_owners.is_empty()
            || Self::standard_object_prototype_property(property)
            || !self.index.plain_data_objects.contains(&object)
            || self.tainted_objects.contains(&object)
            || !self.ordinary_object_prototype_intact()
        {
            return false;
        }
        self.prefix_object_properties
            .get(&object)
            .is_some_and(|properties| {
                !properties.contains_key(&None)
                    && !properties.contains_key(&Some(property.to_owned()))
            })
    }

    fn accepts_deferred_global_write(&mut self, owner: SourceUnitOrdinal, env: &Context) -> bool {
        if !self.index.deferred_global_certificates.contains(&owner) {
            return false;
        }
        let Some(candidate) = &self.index.deferred_global_candidates[owner.0] else {
            return false;
        };
        let values = self.evaluate(&candidate.value, env, env);
        matches!(
            values.iter().collect::<Vec<_>>().as_slice(),
            [AbstractValue::Closure(_, _)]
                | [AbstractValue::Origin(
                    ValueOrigin::Function(_) | ValueOrigin::Class(_)
                )]
        )
    }
    fn ordinary_data_properties(
        &self,
        values: &Values,
    ) -> Option<BTreeMap<Option<String>, Values>> {
        let [AbstractValue::Origin(ValueOrigin::Object(object))] =
            values.iter().collect::<Vec<_>>().as_slice()
        else {
            return None;
        };
        if self.tainted_objects.contains(object)
            || (!self.index.plain_data_objects.contains(object)
                && !self.ordinary_spread_copies.contains(object))
        {
            return None;
        }
        let properties = self.prefix_object_properties.get(object)?;
        if properties.contains_key(&None) {
            return None;
        }
        Some(properties.clone())
    }

    fn array_intrinsics_intact(&self) -> bool {
        let array = ("Array".into(), SyntaxContext::empty());
        !self.array_intrinsic_mutated
            && !self.flow.is_declared(&array)
            && !self.shadowed.contains("Array")
            && self.index.dynamic_scope_owners.is_empty()
            && !self.index.mutated_intrinsic_roots.iter().any(|root| {
                matches!(
                    root,
                    IntrinsicRoot::GlobalObject | IntrinsicRoot::Array | IntrinsicRoot::Object
                )
            })
    }

    fn dense_source(&self, values: &Values) -> Option<(u32, DenseArray)> {
        let [AbstractValue::Origin(ValueOrigin::Object(object))] =
            values.iter().collect::<Vec<_>>().as_slice()
        else {
            return None;
        };
        if !self.array_intrinsics_intact() || self.tainted_objects.contains(object) {
            return None;
        }
        Some((*object, self.dense_arrays.get(object)?.clone()))
    }

    fn retain_dense_array(&mut self, object: u32, slots: Vec<Values>) {
        let before = self.prefix_object_properties.get(&object).cloned();
        let dense = self
            .dense_arrays
            .entry(object)
            .or_insert_with(|| DenseArray {
                slots: slots.clone(),
            });
        let length = dense.slots.len().max(slots.len());
        dense
            .slots
            .resize_with(length, || BTreeSet::from([AbstractValue::Undefined]));
        for (index, current) in dense.slots.iter_mut().enumerate() {
            if let Some(values) = slots.get(index) {
                current.extend(values.iter().cloned());
            } else {
                current.insert(AbstractValue::Undefined);
            }
        }
        let properties = self.prefix_object_properties.entry(object).or_default();
        properties
            .entry(Some("length".into()))
            .or_default()
            .insert(AbstractValue::NonCallable);
        for (index, values) in dense.slots.iter().enumerate() {
            properties
                .entry(Some(index.to_string()))
                .or_default()
                .extend(values.iter().cloned());
            properties
                .entry(None)
                .or_default()
                .extend(values.iter().cloned());
        }
        if before.as_ref() != Some(properties) {
            self.prefix_revision = self.prefix_revision.wrapping_add(1);
        }
    }

    fn evaluate_array(&mut self, object: u32, env: &Context, captures: &Context) -> Values {
        let elements = self.index.array_elements[&object].clone();
        let mut slots = Vec::new();
        let mut containment = Values::new();
        let mut dense = true;
        let mut may_throw = false;
        let mut can_complete = true;
        for element in elements {
            if !can_complete {
                break;
            }
            let Some(element) = element else {
                dense = false;
                continue;
            };
            let mut values = self.evaluate(&element.value, env, captures);
            may_throw |= values.remove(&AbstractValue::Unavailable);
            if values.is_empty() {
                can_complete = false;
                continue;
            }
            containment.extend(values.iter().cloned());
            if element.spread {
                if let Some((_, source)) = self.dense_source(&values) {
                    slots.extend(source.slots);
                } else {
                    dense = false;
                }
            } else {
                slots.push(values);
            }
        }
        if !can_complete {
            // Do not publish this array origin or its retained callbacks when
            // evaluation can only throw before the literal is constructed.
            return if may_throw {
                BTreeSet::from([AbstractValue::Unavailable])
            } else {
                Values::new()
            };
        }
        self.prefix_object_properties
            .entry(object)
            .or_default()
            .entry(None)
            .or_default()
            .extend(containment);
        if dense {
            self.retain_dense_array(object, slots);
        } else {
            self.tainted_objects.insert(object);
        }
        let mut result = BTreeSet::from([AbstractValue::Origin(ValueOrigin::Object(object))]);
        if may_throw {
            result.insert(AbstractValue::Unavailable);
        }
        result
    }

    fn array_iteration_plan(
        &mut self,
        span: CallSite,
        env: &Context,
        captures: &Context,
    ) -> Option<(
        ArrayIteration,
        u32,
        DenseArray,
        u32,
        Option<CapturedContext>,
    )> {
        let proof = self.index.array_iterations.get(&span)?.clone();
        proof.result?;
        let values = self.evaluate(&proof.source, env, captures);
        let (source, dense) = self.dense_source(&values)?;
        if dense.slots.len() > MAX_SYNCHRONOUS_INVOCATIONS {
            return None;
        }
        for values in &dense.slots {
            if values.is_empty()
                || values.iter().any(|value| match value {
                    AbstractValue::Origin(ValueOrigin::Object(object)) => {
                        (!self.index.plain_data_objects.contains(object)
                            && !self.ordinary_spread_copies.contains(object))
                            || self.tainted_objects.contains(object)
                    }
                    AbstractValue::Origin(ValueOrigin::Function(key))
                    | AbstractValue::Closure(key, _) => self.tainted_functions.contains(key),
                    AbstractValue::NonCallable | AbstractValue::Undefined => false,
                    _ => true,
                })
            {
                return None;
            }
        }
        let callback = self.evaluate(&proof.callback, env, captures);
        let (key, captured) = match callback.iter().collect::<Vec<_>>().as_slice() {
            [AbstractValue::Origin(ValueOrigin::Function(key))] => (*key, None),
            [AbstractValue::Closure(key, captured)] => (*key, Some(captured.clone())),
            _ => return None,
        };
        let callable = self.index.callables.get(&key)?;
        if callable.generator
            || self.index.async_callables.contains(&key)
            || callable.params.iter().any(|param| param.binding.is_none())
        {
            return None;
        }
        Some((proof, source, dense, key, captured))
    }

    fn merge_array_effects(&mut self, outcome: &mut Outcome, nested: Outcome) {
        self.apply_rebind_effects(&nested.effects.rebinds);
        outcome.effects.reads.extend(nested.effects.reads);
        outcome.effects.rebinds.extend(nested.effects.rebinds);
        outcome
            .effects
            .uninitialized_reads
            .extend(nested.effects.uninitialized_reads);
        outcome.executed_owners.extend(nested.executed_owners);
        outcome.unknown_call |= nested.unknown_call;
        outcome.inherit_completeness(nested.complete, nested.effects.precision_losses);
    }

    fn execute_array_iteration(
        &mut self,
        span: CallSite,
        env: &Context,
        captures: &Context,
        outcome: &mut Outcome,
    ) -> bool {
        let Some((proof, source, dense, key, captured)) =
            self.array_iteration_plan(span, env, captures)
        else {
            return false;
        };
        let source_values = BTreeSet::from([AbstractValue::Origin(ValueOrigin::Object(source))]);
        let original_callbacks = self.callback_values(&source_values);
        self.array_iteration_protected_objects
            .extend(self.reachable_objects(&source_values));
        let mut slots = Vec::new();
        for values in &dense.slots {
            let previous_unsupported = self.array_iteration_unsupported;
            self.array_iteration_unsupported = false;
            self.array_iteration_depth += 1;
            let nested = self.invoke(
                key,
                vec![
                    values.clone(),
                    BTreeSet::from([AbstractValue::NonCallable]),
                    source_values.clone(),
                ],
                captured.clone(),
                false,
            );
            self.array_iteration_depth -= 1;
            let unsupported = self.array_iteration_unsupported;
            self.array_iteration_unsupported |= previous_unsupported;
            let returned = nested.returns.clone();
            self.merge_array_effects(outcome, nested);
            let current_source = self.evaluate(&proof.source, env, captures);
            let valid = !unsupported && self.dense_source(&current_source).is_some_and(|(object, current)| object == source && current.slots == dense.slots)
                && dense.slots.iter().flatten().all(|value| !matches!(value, AbstractValue::Origin(ValueOrigin::Object(object)) if self.tainted_objects.contains(object)))
                && dense.slots.iter().flatten().all(|value| !matches!(value, AbstractValue::Origin(ValueOrigin::Function(key)) | AbstractValue::Closure(key, _) if self.tainted_functions.contains(key)));
            let valid = valid
                && (proof.method != ArrayIterationMethod::Map
                    || returned.iter().all(|value| match value {
                        AbstractValue::NonCallable
                        | AbstractValue::Undefined
                        | AbstractValue::Origin(ValueOrigin::Function(_))
                        | AbstractValue::Closure(_, _) => true,
                        AbstractValue::Origin(ValueOrigin::Object(object)) => {
                            !self.tainted_objects.contains(object)
                                && (self.index.plain_data_objects.contains(object)
                                    || self.ordinary_spread_copies.contains(object)
                                    || self.dense_arrays.contains_key(object))
                        }
                        _ => false,
                    }));
            if !valid {
                let callback_owner = self.index.callables[&key].owner;
                self.promote_lazy_owner(
                    callback_owner,
                    self.current_top_owner.unwrap_or(callback_owner),
                    outcome,
                );
                self.tainted_objects.insert(proof.result.unwrap());
                self.prefix_object_properties
                    .entry(proof.result.unwrap())
                    .or_default()
                    .entry(None)
                    .or_default()
                    .extend(original_callbacks.iter().cloned());
                self.invoke_possible_callbacks(&[original_callbacks], false, outcome);
                return false;
            }
            slots.push(if proof.method == ArrayIterationMethod::Map {
                returned
            } else {
                values.clone()
            });
        }
        if proof.method != ArrayIterationMethod::ForEach {
            self.retain_dense_array(proof.result.unwrap(), slots);
        }
        true
    }

    fn is_proven_map_entry_array(
        &self,
        callee: &ValueExpr,
        args: &[ValueExpr],
        env: &Context,
        captures: &Context,
        owner: SourceUnitOrdinal,
    ) -> bool {
        let ValueExpr::Binding(id) = callee else {
            return false;
        };
        id.0.as_ref() == "Map"
            && !self.flow.is_declared(id)
            && !self.shadowed.contains("Map")
            && !env.contains_key(id)
            && !captures.contains_key(id)
            && !self.index.reassigned_map
            && self.index.dynamic_scope_owners.is_empty()
            && !self.index.dynamic_scope_owners.contains(&owner)
            && matches!(args, [ValueExpr::ObjectLiteral(span, _)]
                if self.index.map_entry_arrays.contains(span))
    }

    fn proven_from_entries_callbacks(
        &mut self,
        call_site: CallSite,
        env: &Context,
        captures: &Context,
    ) -> Option<Values> {
        let proof = self
            .index
            .from_entries_map_calls
            .get(&call_site)
            .or_else(|| self.index.mapped_array_map_calls.get(&call_site))?
            .clone();
        let array_intrinsic = ("Array".into(), SyntaxContext::empty());
        if !self.index.dynamic_scope_owners.is_empty()
            || self.flow.is_declared(&proof.object_intrinsic)
            || self.shadowed.contains("Object")
            || self.flow.is_declared(&array_intrinsic)
            || self.shadowed.contains("Array")
            || self.index.mutated_intrinsic_roots.iter().any(|root| {
                matches!(
                    root,
                    IntrinsicRoot::GlobalObject | IntrinsicRoot::Object | IntrinsicRoot::Array
                )
            })
        {
            return None;
        }
        let ValueExpr::Binding(source_id) = &proof.source else {
            return None;
        };
        // This path deliberately accepts only a chunk-top plain-array binding.
        // Local parameters, aliases from opaque calls, and synthetic map results
        // stay on the ordinary unknown-call path.
        if !self.top_bindings.contains(source_id)
            || !self
                .index
                .plain_array_bindings
                .contains(source_id.0.as_ref())
        {
            return None;
        }
        let source_values = self.evaluate(&proof.source, env, captures);
        let [AbstractValue::Origin(ValueOrigin::Object(array))] =
            source_values.iter().collect::<Vec<_>>().as_slice()
        else {
            return None;
        };
        if self.tainted_objects.contains(array) {
            return None;
        }
        let count = *self.index.dense_array_literals.get(array)?;
        let mut expected_entries = Values::new();
        for index in 0..count {
            let slot = self.flow.values_of_property(*array, &index.to_string())?;
            if slot.iter().next().is_none()
                || slot.iter().any(|origin| origin == ValueOrigin::Unknown)
            {
                return None;
            }
            for origin in slot {
                let ValueOrigin::Object(entry) = origin else {
                    return None;
                };
                if !self.index.plain_data_objects.contains(&entry)
                    || !self
                        .index
                        .primitive_data_properties
                        .contains(&(entry, proof.key.clone()))
                    || self.tainted_objects.contains(&entry)
                {
                    return None;
                }
                let key_values = self
                    .prefix_object_properties
                    .get(&entry)
                    .and_then(|properties| properties.get(&Some(proof.key.clone())))?;
                if key_values.is_empty()
                    || key_values.iter().any(|value| {
                        !matches!(value, AbstractValue::NonCallable | AbstractValue::Undefined)
                    })
                {
                    return None;
                }
                expected_entries.insert(AbstractValue::Origin(ValueOrigin::Object(entry)));
            }
        }
        let actual_entries = self
            .prefix_object_properties
            .get(array)
            .and_then(|properties| properties.get(&None))?;
        if actual_entries != &expected_entries {
            return None;
        }
        let mut retained_values = self.callback_values(&source_values);
        // Retain the source identity so returned registry aliases taint entries.
        retained_values.extend(source_values);
        Some(retained_values)
    }

    fn callbacks_in_from_entries_source(
        &mut self,
        call_site: CallSite,
        env: &Context,
        captures: &Context,
    ) -> Option<Values> {
        let proof = self
            .index
            .from_entries_map_calls
            .get(&call_site)
            .or_else(|| self.index.mapped_array_map_calls.get(&call_site))?
            .clone();
        let source_values = self.evaluate(&proof.source, env, captures);
        Some(self.callback_values(&source_values))
    }

    fn validated_metadata_decoration(
        &mut self,
        application: &MetadataDecorateCall,
        env: &Context,
        captures: &Context,
    ) -> Option<ValidatedMetadataDecoration> {
        if !self.index.dynamic_scope_owners.is_empty()
            || !self.initialized_bindings.contains(&application.helper)
            || !self.initialized_bindings.contains(&application.target)
        {
            return None;
        }
        let helper = self.evaluate(
            &ValueExpr::Binding(application.helper.clone()),
            env,
            captures,
        );
        let target = self.evaluate(
            &ValueExpr::Binding(application.target.clone()),
            env,
            captures,
        );
        if helper.len() != 1 || target.len() != 1 {
            return None;
        }
        let helper_key = match helper.first() {
            Some(AbstractValue::Origin(ValueOrigin::Function(key))) => *key,
            Some(AbstractValue::Closure(key, captured)) if captured.values.is_empty() => *key,
            _ => return None,
        };
        let Some(AbstractValue::Origin(ValueOrigin::Class(target_key))) = target.first() else {
            return None;
        };
        if application.stable_origins(self.flow) != Some((helper_key, *target_key)) {
            return None;
        }
        if application.member_decorators.iter().any(|root| {
            !self.index.imported_bindings.contains(root) || self.changed_member_roots.contains(root)
        }) {
            return None;
        }
        Some(ValidatedMetadataDecoration {
            target_owner: *self.binding_owner.get(&application.target)?,
        })
    }

    fn summarize_metadata_decoration(
        &mut self,
        application: &MetadataDecorateCall,
        _validated: ValidatedMetadataDecoration,
        owner: SourceUnitOrdinal,
        outcome: &mut Outcome,
    ) {
        // The author certifies the helper's dispatch and every decorator's
        // metadata-only behavior, not purity. Preserve all static dependencies
        // of their bodies without manufacturing calls into the target methods.
        for binding in std::iter::once(&application.helper)
            .chain(&application.decorators)
            .chain(&application.member_decorators)
        {
            if let Some(&binding_owner) = self.binding_owner.get(binding) {
                let (reads, rebinds) = self
                    .source_unit_read_closure
                    .lazy_effects_for_source_unit(binding_owner);
                self.promote_lazy_effects(reads, rebinds, owner, outcome);
                outcome.executed_owners.insert(binding_owner);
            }
        }
        self.apply_rebind_effects(&outcome.effects.rebinds);
    }

    fn all_owners(&self) -> impl Iterator<Item = SourceUnitOrdinal> + '_ {
        self.facts.iter().map(|fact| fact.ordinal)
    }

    fn promote_lazy_owner(
        &self,
        owner: SourceUnitOrdinal,
        effect_owner: SourceUnitOrdinal,
        outcome: &mut Outcome,
    ) {
        let fact = &self.facts[owner.0];
        let prefix_reads = self.index.owner_callable_prefix_reads[owner.0].as_ref();
        let reads = prefix_reads
            .filter(|reads| {
                // Without complete coverage, retain the original whole-body
                // fallback rather than dropping a possible setter read.
                !self.reachability.incomplete
                    && !self.index.dynamic_scope_owners.contains(&owner)
                    && !self.index.unindexed_accessor_owners.contains(&owner)
                    && fact.reads.first_order_lazy.is_subset(reads)
            })
            .unwrap_or(&fact.reads.lazy);
        // The per-Callable rebind index does not yet cover every update and
        // destructuring target. Keep the complete raw set until it does.
        self.promote_lazy_effects(reads, &fact.rebinds.lazy, effect_owner, outcome);
    }

    fn promote_lazy_effects(
        &self,
        reads: &BTreeSet<Id>,
        rebinds: &BTreeSet<Id>,
        effect_owner: SourceUnitOrdinal,
        outcome: &mut Outcome,
    ) {
        // Large fallback summaries are already sorted. Building and merging
        // their sets in bulk avoids a tree lookup for every repeated read.
        // Small per-owner additions still use insertion into the larger set.
        if reads.len() >= 64 && reads.len().saturating_mul(2) >= outcome.effects.reads.len() {
            let mut uninitialized = reads
                .difference(&self.initialized_bindings)
                .filter(|id| self.top_bindings.contains(*id))
                .cloned()
                .collect();
            outcome
                .effects
                .uninitialized_reads
                .append(&mut uninitialized);
            let mut promoted = reads
                .intersection(&self.top_bindings)
                .cloned()
                .map(|id| (id, effect_owner))
                .collect();
            outcome.effects.reads.append(&mut promoted);
        } else {
            for id in reads.iter().filter(|id| self.top_bindings.contains(*id)) {
                outcome.effects.reads.insert((id.clone(), effect_owner));
                if !self.initialized_bindings.contains(id) {
                    outcome.effects.uninitialized_reads.insert(id.clone());
                }
            }
        }
        if rebinds.len() >= 64 && rebinds.len().saturating_mul(2) >= outcome.effects.rebinds.len() {
            let mut promoted = rebinds
                .intersection(&self.top_bindings)
                .cloned()
                .map(|id| (id, effect_owner))
                .collect();
            outcome.effects.rebinds.append(&mut promoted);
        } else {
            outcome.effects.rebinds.extend(
                rebinds
                    .iter()
                    .filter(|id| self.top_bindings.contains(*id))
                    .cloned()
                    .map(|id| (id, effect_owner)),
            );
        }
    }

    fn roots_for_owner(&self, owner: SourceUnitOrdinal) -> BTreeSet<SourceUnitOrdinal> {
        let mut roots = BTreeSet::from([owner]);
        if let Some(fact) = self.facts.get(owner.0) {
            roots.extend(self.source_unit_read_closure.source_units_for_bindings(
                fact.reads.eager.iter().chain(fact.rebinds.eager.iter()),
            ));
        }
        roots
    }

    fn private_frozen_namespace(&self, fact: &StatementFacts) -> Option<FrozenNamespaceProof> {
        if !matches!(fact.kind, StatementKind::VarDecl)
            || !fact.purity.is_pure()
            || fact.declared.len() != 1
            || !self.index.const_call_owners.contains(&fact.ordinal)
            || self.exported_owners.contains(&fact.ordinal)
            || self.reachability.incomplete
            || !self.index.dynamic_scope_owners.is_empty()
            || self.index.opaque_top.contains(&fact.ordinal)
            || !self.index.top_property_writes[fact.ordinal.0].is_empty()
            || !self.index.top_member_value_writes[fact.ordinal.0].is_empty()
            || !self.index.top_callback_result_writes[fact.ordinal.0].is_empty()
            || !self.index.top_exported_values[fact.ordinal.0].is_empty()
            || self.shadowed.contains("Object")
            || self.shadowed.contains("Symbol")
            || self.index.mutated_intrinsic_roots.iter().any(|root| {
                matches!(
                    root,
                    IntrinsicRoot::GlobalObject | IntrinsicRoot::Object | IntrinsicRoot::Symbol
                )
            })
        {
            return None;
        }
        let [(binding, ValueExpr::Call(freeze_callee, freeze_args, freeze_site))] =
            self.index.top_assignments[fact.ordinal.0].as_slice()
        else {
            return None;
        };
        if !fact.declared.contains(binding)
            || self.index.exported_bindings.contains(binding)
            || !self.index.top_definite_initializers[fact.ordinal.0].contains(binding)
        {
            return None;
        }
        let ValueExpr::Member(freeze_receiver, Some(freeze_method), true) = freeze_callee.as_ref()
        else {
            return None;
        };
        let ValueExpr::Binding(object_intrinsic) = freeze_receiver.as_ref() else {
            return None;
        };
        if freeze_method != "freeze" || object_intrinsic.0.as_ref() != "Object" {
            return None;
        }
        if self.flow.is_declared(object_intrinsic) {
            return None;
        }
        let [ValueExpr::Call(define_callee, define_args, define_site)] = freeze_args.as_slice()
        else {
            return None;
        };
        if !matches!(define_callee.as_ref(),
            ValueExpr::Member(receiver, Some(method), true)
                if method == "defineProperty"
                    && matches!(receiver.as_ref(), ValueExpr::Binding(id) if id == object_intrinsic))
        {
            return None;
        }
        let [
            ValueExpr::ObjectLiteral(object, fields),
            ValueExpr::Member(symbol_receiver, Some(tag), true),
            ValueExpr::ObjectLiteral(descriptor, descriptor_fields),
        ] = define_args.as_slice()
        else {
            return None;
        };
        let ValueExpr::Binding(symbol) = symbol_receiver.as_ref() else {
            return None;
        };
        if symbol.0.as_ref() != "Symbol"
            || tag != "toStringTag"
            || self.flow.is_declared(symbol)
            || !self.index.null_proto_data_objects.contains(object)
            || !self.index.plain_data_objects.contains(descriptor)
            || !matches!(descriptor_fields.as_slice(), [(Some(name), ValueExpr::NonCallable)] if name == "value")
            || !self
                .index
                .primitive_data_properties
                .contains(&(*descriptor, "value".to_owned()))
        {
            return None;
        }
        let mut stored_bindings = BTreeSet::new();
        let mut names = BTreeSet::new();
        for (name, value) in fields {
            let Some(name) = name else {
                if self.index.null_proto_data_objects.contains(object)
                    && !names.contains("__proto__")
                    && matches!(value, ValueExpr::NonCallable)
                {
                    names.insert("__proto__".to_owned());
                    continue;
                }
                return None;
            };
            if !names.insert(name.clone()) {
                return None;
            }
            if name == "__proto__" {
                if !self.index.null_proto_data_objects.contains(object)
                    || !matches!(value, ValueExpr::NonCallable)
                {
                    return None;
                }
            } else {
                match value {
                    ValueExpr::Origin(ValueOrigin::Function(_)) => {}
                    ValueExpr::Binding(id) => {
                        if !self.prefix_values.get(id).is_some_and(|values| {
                            !values.is_empty() && !values.contains(&AbstractValue::Unavailable)
                        }) {
                            return None;
                        }
                        stored_bindings.insert(id.clone());
                    }
                    ValueExpr::NonCallable
                        if self
                            .index
                            .primitive_data_properties
                            .contains(&(*object, name.clone())) => {}
                    _ => return None,
                }
            }
        }
        let mut calls = self.index.top_calls[fact.ordinal.0].iter();
        if !calls.all(|call| {
            !call.has_spread
                && (call.kind == CallKind::Call
                    && (CallSite::from(call.span) == *freeze_site
                        || CallSite::from(call.span) == *define_site)
                    || call.kind == CallKind::LocalMemberRead
                        && matches!(&call.callee,
                            ValueExpr::Member(receiver, Some(property), true)
                                if property == "toStringTag"
                                    && matches!(receiver.as_ref(), ValueExpr::Binding(id) if id == symbol)))
        }) {
            return None;
        }
        Some(FrozenNamespaceProof {
            freeze_site: *freeze_site,
            define_site: *define_site,
            stored_bindings,
        })
    }

    fn fact_roots(
        &self,
        fact: &StatementFacts,
        metadata_target: Option<SourceUnitOrdinal>,
        private_constructor_result_owners: &BTreeSet<SourceUnitOrdinal>,
        frozen_namespace: Option<&FrozenNamespaceProof>,
        closed_callback_factory_result: bool,
    ) -> BTreeSet<SourceUnitOrdinal> {
        let mut roots = BTreeSet::new();
        // Storing a known callable in an author-certified inert global data
        // slot does not make that callable executable during this prefix.
        // Its ordinary eager read and all graph ordering edges remain intact.
        if self.accepted_deferred_global_writes.contains(&fact.ordinal) {
            return roots;
        }
        // Export lists link bindings; evaluating one does not expose a value
        // whose declaration has not run yet.
        if self.index.linkage_owners.contains(&fact.ordinal) {
            return roots;
        }
        // The full metadata timing proof covers this whole statement. Its
        // local mutation still contributes graph edges, but the call does not
        // publish an executable capability through the target or its result.
        // Rooting the statement would traverse its eager target read in the
        // synchronous owner closure and undo the target filter below.
        if metadata_target.is_none()
            && (self.exported_owners.contains(&fact.ordinal)
                || !((fact.purity.is_pure()
                    && (fact.declares_direct_function
                        || matches!(fact.kind, StatementKind::ClassDecl)
                        || closed_callback_factory_result
                        || self.closed_local_record_factory_result(fact)))
                    || frozen_namespace.is_some()
                    || !private_constructor_result_owners.is_empty()))
        {
            roots.insert(fact.ordinal);
        }
        // A pure class definition evaluates its superclass value, but merely
        // reading a known ordinary class does not run that class's constructor
        // or methods. The eager read remains in StatementFacts for the owner
        // graph. Calls/static initializers and actual capability publication
        // still enter reachability through their own executor paths.
        let inert_heritage = self.inert_class_heritage_binding(fact);
        roots.extend(
            self.source_unit_read_closure
                .source_units_for_bindings(
                    fact.reads
                        .eager
                        .iter()
                        .filter(|id| {
                            Some(*id) != inert_heritage
                                && !frozen_namespace
                                    .is_some_and(|proof| proof.stored_bindings.contains(*id))
                        })
                        .chain(fact.rebinds.eager.iter()),
                )
                .into_iter()
                .filter(|owner| {
                    Some(*owner) != metadata_target
                        && !private_constructor_result_owners.contains(owner)
                }),
        );
        roots
    }

    /// A validated factory retains the inline callback only in its fresh
    /// returned graph. Creating that graph does not publish the callback;
    /// later calls, writes, and exports still flow through the value model.
    fn closed_callback_factory_result(&self, fact: &StatementFacts) -> Option<(Id, Span)> {
        if !matches!(fact.kind, StatementKind::VarDecl)
            || fact.declared.len() != 1
            || self.index.opaque_top.contains(&fact.ordinal)
        {
            return None;
        }
        let [(binding, ValueExpr::Call(callee, args, site))] =
            self.index.top_assignments[fact.ordinal.0].as_slice()
        else {
            return None;
        };
        let [call] = self.index.top_calls[fact.ordinal.0].as_slice() else {
            return None;
        };
        let ValueExpr::Binding(factory) = callee.as_ref() else {
            return None;
        };
        (self.index.callback_factory_imports.contains(factory)
            && matches!(
                args.as_slice(),
                [ValueExpr::Origin(ValueOrigin::Function(_))]
            )
            && fact.declared.contains(binding)
            && self.index.top_definite_initializers[fact.ordinal.0].contains(binding)
            && call.kind == CallKind::Call
            && !call.has_spread
            && CallSite::from(call.span) == *site)
            .then(|| (factory.clone(), call.span))
    }

    /// A return-only local function can put its arguments into a fresh
    /// ordinary record without publishing or calling them. The initializer
    /// still runs normally; only its result owner's speculative root is
    /// omitted until a later read, call, write, or export exposes that value.
    fn closed_local_record_factory_result(&self, fact: &StatementFacts) -> bool {
        if !matches!(fact.kind, StatementKind::VarDecl)
            || !fact.purity.is_pure()
            || fact.declared.len() != 1
            || self.reachability.incomplete
            || !self.index.dynamic_scope_owners.is_empty()
            || self.index.opaque_top.contains(&fact.ordinal)
            || !self.index.top_property_writes[fact.ordinal.0].is_empty()
            || !self.index.top_member_value_writes[fact.ordinal.0].is_empty()
            || !self.index.top_callback_result_writes[fact.ordinal.0].is_empty()
            || !self.index.top_exported_values[fact.ordinal.0].is_empty()
        {
            return false;
        }
        let [(binding, ValueExpr::Call(callee, args, site))] =
            self.index.top_assignments[fact.ordinal.0].as_slice()
        else {
            return false;
        };
        let [call] = self.index.top_calls[fact.ordinal.0].as_slice() else {
            return false;
        };
        let ValueExpr::Binding(factory) = callee.as_ref() else {
            return false;
        };
        let Some(&key) = self.index.hoisted_bindings.get(factory) else {
            return false;
        };
        if !fact.declared.contains(binding)
            || !self.index.top_definite_initializers[fact.ordinal.0].contains(binding)
            || self
                .flow
                .values_of_binding(factory)
                .iter()
                .ne([ValueOrigin::Function(key)])
            || call.kind != CallKind::Call
            || call.has_spread
            || CallSite::from(call.span) != *site
            || !args.iter().all(|arg| {
                matches!(
                    arg,
                    ValueExpr::Origin(ValueOrigin::Function(_)) | ValueExpr::NonCallable
                )
            })
        {
            return false;
        }
        let Some(callable) = self.index.callables.get(&key) else {
            return false;
        };
        if self.fresh_local_record_receiver(key, callable).is_some() {
            return true;
        }
        if callable.opaque
            || callable.generator
            || self.index.async_callables.contains(&key)
            || callable.may_fall_through
            || !callable.calls.is_empty()
            || !callable.rebinds.is_empty()
            || !callable.member_writes.is_empty()
            || !callable.member_value_writes.is_empty()
            || !callable.callback_result_writes.is_empty()
            || callable.dynamic_property_effect
            || !callable.escaped_bindings.is_empty()
            || !callable.namespace_escape_values.is_empty()
            || callable
                .params
                .iter()
                .any(|param| param.binding.is_none() || param.default.is_some())
            || callable.local_assignments.iter().any(|(id, value)| {
                !callable.local_functions.contains(id)
                    || !matches!(value, ValueExpr::Origin(ValueOrigin::Function(_)))
            })
            || callable.returns.len() != 1
            || !matches!(callable.returns[0], ValueExpr::ObjectLiteral(..))
        {
            return false;
        }
        let allowed: BTreeSet<_> = callable
            .params
            .iter()
            .filter_map(|param| param.binding.clone())
            .chain(callable.local_functions.iter().cloned())
            .collect();
        callable.reads.is_subset(&allowed)
            && self.closed_local_record_value(&callable.returns[0], &allowed)
    }

    /// Check that the shared ordered record certificate is fully represented
    /// by the execution index before treating its writes as private own-data
    /// writes. Incomplete index coverage retains ordinary promotion.
    fn fresh_local_record_receiver(&self, key: u32, callable: &Callable) -> Option<u32> {
        let proof = self.graph.fresh_record_proof(key)?;
        let allowed: BTreeSet<_> = callable
            .params
            .iter()
            .filter_map(|param| param.binding.clone())
            .chain(std::iter::once(proof.local.clone()))
            .collect();
        if callable.opaque
            || callable.generator
            || self.index.async_callables.contains(&key)
            || callable.may_fall_through
            || !callable.calls.is_empty()
            || !callable.rebinds.is_empty()
            || callable.dynamic_property_effect
            || !callable.reads.is_subset(&allowed)
            || callable.local_assignments.len() != 1
            || !matches!(&callable.local_assignments[0], (id, ValueExpr::ObjectLiteral(object, _))
                if id == &proof.local && object == &proof.object)
            || callable.returns.len() != 1
            || !matches!(&callable.returns[0], ValueExpr::Binding(id) if id == &proof.local)
            || callable.escaped_bindings != BTreeSet::from([proof.local.clone()])
            || !self.index.plain_data_objects.contains(&proof.object)
            || callable.callback_result_writes.len() != proof.write_spans.len()
            || callable.member_value_writes.len() != proof.write_spans.len()
            || callable.member_writes.len() != proof.write_spans.len()
            || callable.callback_result_writes.iter().any(|write| {
                !write.simple_assignment
                    || !write.data_read_operands
                    || !matches!(&write.receiver, ValueExpr::Binding(id) if id == &proof.local)
                    || write.property.is_none()
            })
            || callable
                .member_value_writes
                .iter()
                .any(|(id, property, _)| id != &proof.local || property.is_none())
            || callable
                .member_writes
                .iter()
                .any(|(id, _)| id != &proof.local)
        {
            return None;
        }
        Some(proof.object)
    }

    fn closed_local_record_value(&self, value: &ValueExpr, allowed: &BTreeSet<Id>) -> bool {
        match value {
            ValueExpr::ObjectLiteral(key, properties) => {
                self.index.plain_data_objects.contains(key)
                    && properties.iter().all(|(name, value)| {
                        name.is_some() && self.closed_local_record_value(value, allowed)
                    })
            }
            ValueExpr::Binding(id) => allowed.contains(id),
            ValueExpr::Origin(ValueOrigin::Function(_))
            | ValueExpr::NonCallable
            | ValueExpr::Undefined => true,
            _ => false,
        }
    }

    /// A locally checked constructor can retain callbacks in a fresh instance
    /// without publishing them. This certifies the wrapper's whole return
    /// expression before its source owner enters the reachability roots; the
    /// ordinary executor still runs the wrapper and constructor afterward.
    fn closed_local_constructor_factory_result(
        &self,
        fact: &StatementFacts,
    ) -> Option<BTreeSet<SourceUnitOrdinal>> {
        if !matches!(fact.kind, StatementKind::VarDecl)
            || fact.declared.len() != 1
            || self.reachability.incomplete
            || self.exported_owners.contains(&fact.ordinal)
            || !self.ordinary_object_prototype_intact()
            || self.index.opaque_top.contains(&fact.ordinal)
            || !self.index.top_property_writes[fact.ordinal.0].is_empty()
            || !self.index.top_member_value_writes[fact.ordinal.0].is_empty()
            || !self.index.top_callback_result_writes[fact.ordinal.0].is_empty()
            || !self.index.top_exported_values[fact.ordinal.0].is_empty()
        {
            return None;
        }
        let [(binding, ValueExpr::Call(callee, args, site))] =
            self.index.top_assignments[fact.ordinal.0].as_slice()
        else {
            return None;
        };
        // Proven enum own-field reads are retained in the normal top-level
        // executor and eager facts, but cannot invoke a getter or callback.
        let calls: Vec<_> = self.index.top_calls[fact.ordinal.0]
            .iter()
            .filter(|call| {
                call.kind != CallKind::LocalMemberRead
                    || !self.closed_enum_member_read(&call.callee)
            })
            .collect();
        let [call] = calls.as_slice() else {
            return None;
        };
        let ValueExpr::Binding(factory) = callee.as_ref() else {
            return None;
        };
        let &key = self.index.hoisted_bindings.get(factory)?;
        if !fact.declared.contains(binding)
            || !self.index.top_definite_initializers[fact.ordinal.0].contains(binding)
            || self.index.exported_bindings.contains(binding)
            || self.index.exported_bindings.contains(factory)
            || self
                .global_escaped_callbacks
                .contains(&AbstractValue::Origin(ValueOrigin::Function(key)))
            || self
                .flow
                .values_of_binding(factory)
                .iter()
                .ne([ValueOrigin::Function(key)])
            || !self.prefix_values.get(factory).is_some_and(|values| {
                values.len() == 1
                    && values.contains(&AbstractValue::Origin(ValueOrigin::Function(key)))
            })
            || call.kind != CallKind::Call
            || call.has_spread
            || CallSite::from(call.span) != *site
            || !args.iter().all(|arg| self.closed_constructor_input(arg))
        {
            return None;
        }
        let callable = self.index.callables.get(&key)?;
        if callable.opaque
            || callable.generator
            || self.index.async_callables.contains(&key)
            || callable.may_fall_through
            || !callable.rebinds.is_empty()
            || !callable.local_assignments.is_empty()
            || !callable.member_writes.is_empty()
            || !callable.member_value_writes.is_empty()
            || !callable.callback_result_writes.is_empty()
            || callable.dynamic_property_effect
            || !callable.escaped_bindings.is_empty()
            || !callable.namespace_escape_values.is_empty()
            || callable.returns.len() != 1
            || callable.params.len() != args.len()
            || callable
                .params
                .iter()
                .any(|param| param.binding.is_none() || param.default.is_some())
        {
            return None;
        }
        let params: BTreeMap<_, _> = callable
            .params
            .iter()
            .zip(args)
            .map(|(param, arg)| (param.binding.clone().unwrap(), arg))
            .collect();
        let mut class_reads = BTreeSet::new();
        let mut allocations = BTreeMap::new();
        let checked_return = self.checked_constructor_return(
            &callable.returns[0],
            &params,
            false,
            &mut class_reads,
            &mut allocations,
        );
        if !checked_return {
            return None;
        }
        let allowed_reads: BTreeSet<_> = params.keys().cloned().chain(class_reads).collect();
        if !callable.reads.is_subset(&allowed_reads) {
            return None;
        }
        let mut indexed_allocations = BTreeMap::new();
        for nested in &callable.calls {
            let ValueExpr::Binding(class) = &nested.callee else {
                return None;
            };
            let class_key = self.stable_local_class_key(class)?;
            if nested.kind != CallKind::New || nested.has_spread {
                return None;
            }
            *indexed_allocations
                .entry((class_key, nested.args.len()))
                .or_insert(0) += 1;
        }
        if allocations != indexed_allocations {
            return None;
        }
        let mut owners = BTreeSet::from([callable.owner]);
        for (class_key, _) in allocations.keys() {
            owners.insert(*self.index.class_owners.get(class_key)?);
        }
        Some(owners)
    }

    fn closed_constructor_input(&self, value: &ValueExpr) -> bool {
        match value {
            ValueExpr::ObjectLiteral(key, properties) => {
                if let Some(elements) = self.index.array_elements.get(key) {
                    return self.index.dense_array_literals.contains_key(key)
                        && elements.iter().all(|element| {
                            element.as_ref().is_some_and(|element| {
                                !element.spread && self.closed_constructor_input(&element.value)
                            })
                        });
                }
                self.index.plain_data_objects.contains(key)
                    && properties
                        .iter()
                        .all(|(name, value)| name.is_some() && self.closed_constructor_input(value))
            }
            ValueExpr::Member(..) => self.closed_enum_member_read(value),
            ValueExpr::Primitive(operands) => operands
                .iter()
                .all(|operand| matches!(operand, ValueExpr::NonCallable | ValueExpr::Undefined)),
            ValueExpr::Origin(ValueOrigin::Function(_))
            | ValueExpr::NonCallable
            | ValueExpr::Undefined => true,
            _ => false,
        }
    }

    fn stable_local_class_key(&self, binding: &Id) -> Option<u32> {
        let values = self.prefix_values.get(binding)?;
        let [AbstractValue::Origin(ValueOrigin::Class(key))] =
            values.iter().collect::<Vec<_>>().as_slice()
        else {
            return None;
        };
        (self.initialized_bindings.contains(binding)
            && self.index.class_bindings.get(key) == Some(binding))
        .then_some(*key)
    }

    fn checked_constructor_return(
        &self,
        value: &ValueExpr,
        params: &BTreeMap<Id, &ValueExpr>,
        nested: bool,
        class_reads: &mut BTreeSet<Id>,
        allocations: &mut BTreeMap<(u32, usize), usize>,
    ) -> bool {
        let ValueExpr::New(callee, args, _) = value else {
            return false;
        };
        let ValueExpr::Binding(class) = callee.as_ref() else {
            return false;
        };
        let Some(key) = self.stable_local_class_key(class) else {
            return false;
        };
        let Some(constructor) = self.eligible_local_constructor(key, args.len()) else {
            return false;
        };
        if nested && !constructor.inert_allocation {
            return false;
        }
        class_reads.insert(class.clone());
        *allocations.entry((key, args.len())).or_insert(0) += 1;
        for arg in args {
            match arg {
                ValueExpr::New(..) => {
                    if !self.checked_constructor_return(arg, params, true, class_reads, allocations)
                    {
                        return false;
                    }
                }
                ValueExpr::Binding(binding) => {
                    if !params
                        .get(binding)
                        .is_some_and(|value| self.closed_constructor_input(value))
                    {
                        return false;
                    }
                }
                other if self.closed_constructor_input(other) => {}
                _ => return false,
            }
        }
        for (index, property) in &constructor.property_reads {
            let Some(arg) = args.get(*index) else {
                return false;
            };
            let source = match arg {
                ValueExpr::Binding(binding) => {
                    let Some(value) = params.get(binding) else {
                        return false;
                    };
                    *value
                }
                other => other,
            };
            let ValueExpr::ObjectLiteral(object, properties) = source else {
                return false;
            };
            if !self.index.plain_data_objects.contains(object)
                || (!properties
                    .iter()
                    .any(|(name, _)| name.as_deref() == Some(property))
                    && Self::standard_object_prototype_property(property))
            {
                return false;
            }
        }
        true
    }

    fn inert_class_heritage_binding<'a>(&'a self, fact: &StatementFacts) -> Option<&'a Id> {
        if !matches!(fact.kind, StatementKind::ClassDecl)
            || !fact.purity.is_pure()
            || self.exported_owners.contains(&fact.ordinal)
            || self.reachability.incomplete
        {
            return None;
        }
        let (class_key, _) = self.index.class_bindings.iter().find(|(key, binding)| {
            self.index.class_owners.get(key) == Some(&fact.ordinal)
                && fact.declared.contains(binding)
        })?;
        let ValueExpr::Binding(base) = self
            .index
            .class_values
            .get(class_key)?
            .super_class
            .as_ref()?
        else {
            return None;
        };
        let values = self.prefix_values.get(base)?;
        let Some(AbstractValue::Origin(ValueOrigin::Class(base_key))) = values.first() else {
            return None;
        };
        let base_owner = *self.index.class_owners.get(base_key)?;
        if !self.initialized_bindings.contains(base)
            || !fact.reads.eager.contains(base)
            || self.index.class_bindings.get(base_key) != Some(base)
            || base_owner.0 >= fact.ordinal.0
            || !self.facts[base_owner.0].purity.is_pure()
            || self.tainted_classes.contains(base_key)
            || self.escaped_class_capabilities.contains(base_key)
            || values.len() != 1
        {
            return None;
        }
        Some(base)
    }

    fn extend_reachable_owners(
        &mut self,
        roots: impl IntoIterator<Item = SourceUnitOrdinal>,
    ) -> bool {
        let before = self.reachability.owners.len();
        // A reachable callback can read only its synchronous prefix while the
        // chunk is initializing. In particular, a binding used after a
        // definite await cannot make the body of its future value executable
        // during an opaque call at this prefix.
        let reached = SourceUnitReadClosure::extend_synchronous_source_units(
            &self.synchronous_owner_adjacency,
            roots,
            &mut self.reachability.expanded,
        );
        self.reachability.owners.extend(reached.iter().copied());
        let changed = self.reachability.owners.len() != before;
        if changed {
            self.reachability.revision = self.reachability.revision.wrapping_add(1);
        }
        if reached
            .iter()
            .any(|owner| self.index.dynamic_scope_owners.contains(owner))
        {
            self.mark_reachability_incomplete();
        }
        changed
    }

    fn mark_reachability_incomplete(&mut self) {
        if !self.reachability.incomplete {
            self.reachability.incomplete = true;
            self.reachability.revision = self.reachability.revision.wrapping_add(1);
        }
        // Incomplete reachability always falls back to the whole chunk.
        self.prior_reachability_roots = None;
    }

    fn note_unknown_execution(&mut self, owner: SourceUnitOrdinal) {
        // Exports expose live capabilities only to code that can reenter the
        // module. Builtin iteration without such reentry remains precise.
        if self.export_reentry_enabled {
            let mut exported = self.exported_runtime_values.clone();
            exported.extend(self.index.exported_bindings.iter().flat_map(|binding| {
                self.prefix_values
                    .get(binding)
                    .into_iter()
                    .flatten()
                    .cloned()
            }));
            self.expose_class_capabilities(&exported);
            self.taint_objects(&exported);
        }
        let mut changed = !self.reachability.unknown_call_seen;
        self.reachability.unknown_call_seen = true;
        let roots = self
            .prior_reachability_roots
            .take()
            .into_iter()
            .flatten()
            .chain(self.roots_for_owner(owner));
        changed |= self.extend_reachable_owners(roots);
        if changed {
            self.reachability.revision = self.reachability.revision.wrapping_add(1);
        }
    }

    fn note_unknown_call(&mut self, call: &Call) {
        self.note_unknown_execution(call.owner);
        if matches!(&call.callee, ValueExpr::Binding(id) if id.0.as_ref() == "eval") {
            self.mark_reachability_incomplete();
        }
    }

    fn record_executed_owner(&mut self, owner: SourceUnitOrdinal) {
        if self.private_constructor_result_owners.contains(&owner) {
            return;
        }
        if self.reachability.unknown_call_seen {
            self.extend_reachable_owners([owner]);
        } else if let Some(roots) = &mut self.prior_reachability_roots {
            roots.insert(owner);
        }
    }

    fn promote_property_writes(
        &self,
        writes: &[(Option<Id>, Option<String>)],
        owner: SourceUnitOrdinal,
        outcome: &mut Outcome,
    ) {
        if writes.is_empty() {
            return;
        }
        let unknown_call = outcome.unknown_call
            || self.reachability.unknown_call_seen
            || self.fallback_outcome.unknown_call;
        if self.index.setter_owners.is_empty() && !unknown_call {
            return;
        }
        let mut possible_owners = outcome.executed_owners.clone();
        possible_owners.insert(owner);
        possible_owners.extend(
            self.prior_effect_owners
                .iter()
                .copied()
                .filter(|candidate| !self.accepted_deferred_global_writes.contains(candidate)),
        );
        for (receiver, _) in writes {
            if let Some(receiver) = receiver
                && let Some(binding_owner) = self.binding_owner.get(receiver)
            {
                possible_owners.insert(*binding_owner);
            }
        }
        let unknown_receiver = writes.iter().any(|(receiver, _)| receiver.is_none());
        if unknown_call {
            possible_owners.extend(
                self.reachability
                    .owners
                    .iter()
                    .copied()
                    .filter(|candidate| self.may_be_available_accessor_owner(*candidate)),
            );
            if self.reachability.incomplete {
                possible_owners.extend(self.all_owners());
            }
        } else if unknown_receiver {
            possible_owners.extend(
                self.all_owners()
                    .filter(|candidate| self.may_be_available_accessor_owner(*candidate)),
            );
        }
        let mut accessor_owners = BTreeSet::new();
        if unknown_call {
            accessor_owners.extend(possible_owners);
        } else {
            for (_, property) in writes {
                if let Some(property) = property {
                    accessor_owners.extend(
                        self.index
                            .setter_owners
                            .get(&Some(property.clone()))
                            .into_iter()
                            .flatten()
                            .copied(),
                    );
                    accessor_owners.extend(
                        self.index
                            .setter_owners
                            .get(&None)
                            .into_iter()
                            .flatten()
                            .copied(),
                    );
                } else {
                    accessor_owners.extend(
                        self.index
                            .setter_owners
                            .values()
                            .flat_map(|owners| owners.iter().copied()),
                    );
                }
            }
            accessor_owners = accessor_owners
                .intersection(&possible_owners)
                .copied()
                .collect();
        }
        if !accessor_owners.is_empty() {
            let effect_owner = if unknown_call {
                self.current_top_owner.unwrap_or(owner)
            } else {
                owner
            };
            for accessor_owner in accessor_owners {
                self.promote_lazy_owner(accessor_owner, effect_owner, outcome);
            }
            outcome.unsupported(ExecutionOperation::PropertyWrite, owner, DUMMY_SP);
        }
    }

    fn may_be_available_accessor_owner(&self, candidate: SourceUnitOrdinal) -> bool {
        if self.reachability.incomplete {
            // Direct eval and `with` can manufacture references to lexical
            // values outside the statically known read closure.
            return true;
        }
        let Some(current) = self.current_top_owner else {
            return true;
        };
        // Reachability follows lexical reads, including declarations whose
        // initializer has not executed. Such a future let/const/class value
        // cannot yet have been installed as a setter. Function declarations
        // are hoisted, and a later var declaration can name a value assigned
        // earlier, so retain both. Directly executed owners are added by the
        // caller independently of this broad reachability filter.
        candidate.0 <= current.0
            || self.index.hoisted_function_owners.contains(&candidate)
            || !self.facts[candidate.0].var_declared.is_empty()
    }

    fn safe_object_assign_target(&self, call: &Call, id: &Id) -> bool {
        if call.kind == CallKind::New
            || !call.plain_object_sources
            || !matches!(call.args.first(), Some(ValueExpr::Binding(target)) if target == id)
        {
            return false;
        }
        let ValueExpr::Member(object, Some(property), _) = &call.callee else {
            return false;
        };
        matches!(object.as_ref(), ValueExpr::Binding(object_id)
            if object_id.0.as_ref() == "Object"
                && !self.flow.is_declared(object_id)
                && !self.shadowed.contains("Object")
                && property == "assign")
    }

    fn stable_fresh_function_target(&self, key: u32, callable: &Callable, id: &Id) -> bool {
        callable.local_functions.contains(id)
            && !callable.rebinds.contains(id)
            && !callable.literal_captures.contains(id)
            && !callable.escaped_bindings.contains(id)
            && !callable.dynamic_property_effect
            && callable
                .member_writes
                .iter()
                .all(|(receiver, property)| receiver != id || property != "__proto__")
            && !callable.local_assignments.iter().any(|(target, value)| {
                if target == id {
                    return false;
                }
                let mut mentioned = BTreeSet::new();
                value.bindings(&mut mentioned);
                mentioned.contains(id)
            })
            && !self
                .index
                .callables
                .iter()
                .any(|(other, nested)| *other != key && nested.reads.contains(id))
            && !callable.calls.iter().any(|call| {
                let mut mentioned = BTreeSet::new();
                call.callee.bindings(&mut mentioned);
                for arg in &call.args {
                    arg.bindings(&mut mentioned);
                }
                mentioned.contains(id) && !self.safe_object_assign_target(call, id)
            })
    }

    fn callback_values(&self, values: &Values) -> Values {
        let mut callbacks = Values::new();
        let mut pending: Vec<_> = values.iter().cloned().collect();
        let mut visited_objects = BTreeSet::new();
        let mut expanded_escaped = false;
        while let Some(value) = pending.pop() {
            match &value {
                AbstractValue::Origin(ValueOrigin::Function(_)) | AbstractValue::Closure(_, _) => {
                    // Capture snapshots form a DAG: multiple bindings can
                    // contain the same closure. Expand each closure once,
                    // rather than revisit it along every path through captures.
                    if callbacks.insert(value.clone())
                        && let AbstractValue::Closure(_, captured) = &value
                    {
                        pending.extend(
                            captured
                                .values
                                .values()
                                .flat_map(|values| values.iter().cloned()),
                        );
                    }
                }
                AbstractValue::Origin(ValueOrigin::Class(key))
                | AbstractValue::ClassMembers(key) => {
                    callbacks.insert(AbstractValue::ClassMembers(*key));
                }
                AbstractValue::Instance(key, captured) => {
                    callbacks.insert(AbstractValue::ClassMembers(*key));
                    if callbacks.insert(value.clone()) {
                        pending.extend(captured.values.iter().cloned());
                    }
                }
                AbstractValue::ForeignDeferredRecord => {
                    callbacks.insert(value.clone());
                }
                AbstractValue::DeferredCallbacks(captured) => {
                    if callbacks.insert(value.clone()) {
                        pending.extend(captured.values.iter().cloned());
                    }
                }
                AbstractValue::Origin(ValueOrigin::Object(object)) => {
                    if visited_objects.insert(*object)
                        && let Some(properties) = self.prefix_object_properties.get(object)
                    {
                        pending.extend(
                            properties
                                .values()
                                .flat_map(|values| values.iter().cloned()),
                        );
                    }
                }
                AbstractValue::GlobalObject | AbstractValue::Unknown if !expanded_escaped => {
                    // Unknown external data cannot expose an unescaped local
                    // callback. Preserve tracked escapes; exports are added
                    // separately at opaque call sites, and known object
                    // identities are expanded through their property maps.
                    expanded_escaped = true;
                    pending.extend(
                        self.global_escaped_callbacks
                            .iter()
                            .filter(|value| !matches!(value, AbstractValue::Unknown))
                            .cloned(),
                    );
                }
                _ => {}
            }
        }
        callbacks
    }

    fn retained_factory_callbacks(
        &self,
        site: CallSite,
        expressions: &[ValueExpr],
        args: &[Values],
    ) -> Option<Values> {
        self.retained_callbacks(
            &self.index.retained_callback_calls.get(&site)?.call,
            expressions,
            args,
        )
    }

    fn retained_constructor_callbacks(
        &self,
        site: CallSite,
        expressions: &[ValueExpr],
        args: &[Values],
    ) -> Option<Values> {
        self.retained_callbacks(
            self.index.retained_constructor_calls.get(&site)?,
            expressions,
            args,
        )
    }

    fn no_reentry_callbacks(
        &self,
        site: CallSite,
        expressions: &[ValueExpr],
        args: &[Values],
    ) -> Option<Values> {
        let contract = self.index.no_reentry_callback_calls.get(&site)?;
        // This contract admits ordinary local callables only. An opaque result
        // or a returned callback graph does not establish that input shape.
        let signatures: Vec<_> = contract
            .signatures
            .iter()
            .filter(|signature| {
                signature.callback_arguments.iter().all(|position| {
                    args[*position].iter().all(|value| {
                        matches!(
                            value,
                            AbstractValue::Origin(ValueOrigin::Function(_))
                                | AbstractValue::Closure(_, _)
                        )
                    })
                })
            })
            .cloned()
            .collect();
        let contract = RetainedCallbackCall {
            namespace_member: None,
            signatures,
            argument_count: expressions.len(),
            callback_argument_exprs: BTreeMap::new(),
        };
        let callbacks = self.retained_callbacks(&contract, expressions, args)?;
        // Independently exported aliases and already exposed callbacks keep
        // ordinary conservative timing; the provider audit cannot hide them.
        if !callbacks.is_disjoint(&self.export_callback_values()) {
            return None;
        }
        Some(callbacks)
    }

    fn retained_callbacks(
        &self,
        contract: &RetainedCallbackCall,
        expressions: &[ValueExpr],
        args: &[Values],
    ) -> Option<Values> {
        if let Some((namespace, member)) = &contract.namespace_member
            && !self
                .index
                .audited_factory_members
                .get(namespace)
                .is_some_and(|members| members.contains(member))
        {
            return None;
        }
        for signature in &contract.signatures {
            let mut retained = Values::new();
            let admitted = signature.callback_arguments.iter().all(|position| {
                let expression = &expressions[*position];
                // The whole-chunk flow distinguishes a single stable function
                // identity from a rebound cell, including later writes.
                if let ValueExpr::Binding(binding) = expression
                    && (self.facts.iter().any(|fact| {
                        fact.rebinds.eager.contains(binding) || fact.rebinds.lazy.contains(binding)
                    }) || self.flow.values_of_binding(binding).iter().count() > 1)
                {
                    return false;
                }
                let values = &args[*position];
                if values.is_empty()
                    || !values.iter().all(|value| match value {
                        AbstractValue::Origin(ValueOrigin::Function(key))
                        | AbstractValue::Closure(key, _) => {
                            self.index.callables.contains_key(key)
                                && !self.tainted_functions.contains(key)
                        }
                        AbstractValue::DeferredCallbacks(_) => true,
                        _ => false,
                    })
                {
                    return false;
                }
                retained.extend(self.callback_values(values));
                true
            });
            if !admitted {
                continue;
            }
            let functions: BTreeSet<_> = retained
                .iter()
                .filter_map(|value| match value {
                    AbstractValue::Origin(ValueOrigin::Function(key))
                    | AbstractValue::Closure(key, _) => Some(*key),
                    _ => None,
                })
                .collect();
            let mutating_receiver = |expression: &ValueExpr| {
                let mut root = expression;
                while let ValueExpr::Member(receiver, _, _) = root {
                    root = receiver;
                }
                matches!(root, ValueExpr::Binding(binding) if self.flow.values_of_binding(binding).iter()
                    .any(|origin| matches!(origin, ValueOrigin::Function(key) if functions.contains(&key))))
            };
            // Existing callable own/prototype surfaces are outside the fresh
            // input proof. Reject recognized writes through all local aliases,
            // even if they occur after this call or in a lazy callable.
            let mutated = self
                .index
                .intrinsic_mutation_receivers
                .iter()
                .any(mutating_receiver)
                || self
                    .index
                    .top_callback_result_writes
                    .iter()
                    .flatten()
                    .chain(
                        self.index
                            .callables
                            .values()
                            .flat_map(|callable| &callable.callback_result_writes),
                    )
                    .any(|write| mutating_receiver(&write.receiver));
            if mutated
                || retained
                    .iter()
                    .any(|value| self.global_escaped_callbacks.contains(value))
            {
                continue;
            }
            return Some(retained);
        }
        None
    }

    fn fresh_primitive_write_receivers(
        &mut self,
        callable: &Callable,
        env: &Context,
    ) -> BTreeSet<u32> {
        let writes = &callable.callback_result_writes;
        if self.array_iteration_depth == 0
            || self.array_iteration_unsupported
            || !self.array_intrinsics_intact()
            || callable.opaque
            || writes.is_empty()
            || writes.len() != callable.member_value_writes.len()
            || writes.len()
                != callable.member_writes.len() + callable.dynamic_property_receivers.len()
            || callable
                .calls
                .iter()
                .any(|call| call.kind != CallKind::LocalMemberRead)
        {
            return BTreeSet::new();
        }
        let mut objects = BTreeSet::new();
        for write in writes {
            if !write.simple_assignment
                || !write.data_read_operands
                || !matches!(write.receiver, ValueExpr::Binding(_))
            {
                return BTreeSet::new();
            }
            for expression in [&write.property_key, &write.value] {
                let values = self.evaluate(expression, env, env);
                if values.is_empty()
                    || !values.iter().all(|value| {
                        matches!(value, AbstractValue::NonCallable | AbstractValue::Undefined)
                    })
                {
                    return BTreeSet::new();
                }
            }
            let receivers = self.evaluate(&write.receiver, env, env);
            let [AbstractValue::Origin(ValueOrigin::Object(object))] =
                receivers.iter().collect::<Vec<_>>().as_slice()
            else {
                return BTreeSet::new();
            };
            if self.tainted_objects.contains(object)
                || self.array_iteration_protected_objects.contains(object)
                || (!self.index.plain_data_objects.contains(object)
                    && !self.ordinary_spread_copies.contains(object))
                || self.dense_arrays.contains_key(object)
            {
                return BTreeSet::new();
            }
            objects.insert(*object);
        }
        objects
    }

    fn apply_member_value_writes(
        &mut self,
        writes: &[(Id, Option<String>, ValueExpr)],
        env: &Context,
        captures: &Context,
        certified_data_receivers: &BTreeSet<u32>,
        deferred_global_write: bool,
    ) {
        for (receiver, property, expression) in writes {
            let receivers = self.evaluate(&ValueExpr::Binding(receiver.clone()), env, captures);
            let values = self.evaluate(expression, env, captures);
            for receiver in receivers {
                match receiver {
                    AbstractValue::GlobalObject | AbstractValue::ArrayIntrinsic => {
                        if deferred_global_write && receiver == AbstractValue::GlobalObject {
                            continue;
                        }
                        self.array_intrinsic_mutated |= receiver == AbstractValue::ArrayIntrinsic;
                        self.taint_objects(&values);
                        let callbacks = self.callback_values(&values);
                        let before = self.global_escaped_callbacks.len();
                        self.global_escaped_callbacks.extend(callbacks);
                        if self.global_escaped_callbacks.len() != before {
                            self.prefix_revision = self.prefix_revision.wrapping_add(1);
                        }
                    }
                    AbstractValue::Origin(ValueOrigin::Object(object)) => {
                        if !certified_data_receivers.contains(&object) {
                            self.tainted_objects.insert(object);
                        }
                        let properties = self.prefix_object_properties.entry(object).or_default();
                        let values_for_property = properties.entry(property.clone()).or_default();
                        let before = values_for_property.len();
                        values_for_property.extend(values.iter().cloned());
                        if values_for_property.len() != before {
                            self.prefix_revision = self.prefix_revision.wrapping_add(1);
                        }
                    }
                    AbstractValue::Instance(key, captured) => {
                        self.tainted_classes.insert(key);
                        self.taint_objects(&captured.values);
                        self.taint_objects(&values);
                        let callbacks = self.callback_values(&values);
                        if !callbacks.is_subset(&self.global_escaped_callbacks) {
                            self.global_escaped_callbacks.extend(callbacks);
                            self.prefix_revision = self.prefix_revision.wrapping_add(1);
                        }
                    }
                    AbstractValue::Unknown
                    | AbstractValue::DeferredCallbacks(_)
                    | AbstractValue::ForeignDeferredRecord => {
                        self.taint_objects(&values);
                        // A write through an untracked object may publish the
                        // callback through that object before it is passed to
                        // opaque code. Retain it as an escaped callback root.
                        let callbacks = self.callback_values(&values);
                        let before = self.global_escaped_callbacks.len();
                        self.global_escaped_callbacks.extend(callbacks);
                        if self.global_escaped_callbacks.len() != before {
                            self.prefix_revision = self.prefix_revision.wrapping_add(1);
                        }
                    }
                    _ => {}
                }
            }
        }
    }

    fn promote_callback_result_writes(
        &mut self,
        writes: &[CallbackResultWrite],
        env: &Context,
        captures: &Context,
        owner: SourceUnitOrdinal,
        outcome: &mut Outcome,
        certified_data_receivers: &BTreeSet<u32>,
    ) {
        for write in writes {
            let receivers = self.evaluate(&write.receiver, env, captures);
            let uncertified = receivers.iter().filter(|value| !matches!(value,
                AbstractValue::Origin(ValueOrigin::Object(object)) if certified_data_receivers.contains(object))).cloned().collect();
            self.taint_objects(&uncertified);
            let mut callbacks = self.callback_values(&receivers);
            let mut root = &write.receiver;
            while let ValueExpr::Member(receiver, _, _) = root {
                root = receiver;
            }
            let root_values = self.evaluate(root, env, captures);
            let callable_surface = root_values.iter().any(|value| {
                matches!(
                    value,
                    AbstractValue::Origin(ValueOrigin::Function(_)) | AbstractValue::Closure(_, _)
                )
            });
            if !callable_surface
                && !callbacks
                    .iter()
                    .any(|value| matches!(value, AbstractValue::DeferredCallbacks(_)))
            {
                continue;
            }
            callbacks.extend(self.callback_values(&root_values));
            if write
                .property
                .as_deref()
                .is_some_and(|property| property != "__proto__")
                && matches!(write.value, ValueExpr::NonCallable | ValueExpr::Undefined)
            {
                continue;
            }
            // Callable own/prototype fields are not modeled as an ordinary
            // object graph. Installing an opaque value there, or in a retained
            // factory result, can add callbacks/accessors. Conservatively
            // include their effects now, before a later opaque consumer or
            // member projection can execute an otherwise untracked callback.
            outcome.unsupported(ExecutionOperation::PropertyWrite, owner, DUMMY_SP);
            outcome.unknown_call = true;
            self.note_unknown_execution(owner);
            let values = self.evaluate(&write.value, env, captures);
            self.promote_deferred_callbacks(&values, outcome);
            self.invoke_possible_callbacks(&[callbacks, values], false, outcome);
        }
    }

    fn intern_captures(&mut self, captures: Context) -> CapturedContext {
        if let Some((values, &identity)) = self.capture_contexts.get_key_value(&captures) {
            return CapturedContext::with_identity(identity, Arc::clone(values));
        }
        let identity = self.capture_contexts.len();
        let values = Arc::new(captures);
        self.capture_contexts.insert(Arc::clone(&values), identity);
        CapturedContext::with_identity(identity, values)
    }

    fn intern_callback_set(&mut self, values: Values) -> Captured<Values> {
        if let Some((values, &identity)) = self.callback_sets.get_key_value(&values) {
            return Captured::with_identity(identity, Arc::clone(values));
        }
        let identity = self.callback_sets.len();
        let values = Arc::new(values);
        self.callback_sets.insert(Arc::clone(&values), identity);
        Captured::with_identity(identity, values)
    }

    fn deferred_callbacks(&mut self, values: Values) -> AbstractValue {
        AbstractValue::DeferredCallbacks(self.intern_callback_set(values))
    }

    fn taint_objects(&mut self, values: &Values) {
        let mut pending: Vec<_> = values.iter().cloned().collect();
        let mut visited = BTreeSet::new();
        while let Some(value) = pending.pop() {
            if !visited.insert(value.clone()) {
                continue;
            }
            match value {
                AbstractValue::ArrayIntrinsic => {
                    self.array_intrinsic_mutated = true;
                }
                AbstractValue::Origin(ValueOrigin::Object(object))
                    if self.tainted_objects.insert(object) =>
                {
                    if let Some(properties) = self.prefix_object_properties.get(&object) {
                        pending.extend(
                            properties
                                .values()
                                .flat_map(|values| values.iter().cloned()),
                        );
                    }
                }
                AbstractValue::Instance(_, captured)
                | AbstractValue::DeferredCallbacks(captured) => {
                    pending.extend(captured.values.iter().cloned());
                }
                AbstractValue::Origin(ValueOrigin::Function(key)) => {
                    self.tainted_functions.insert(key);
                }
                AbstractValue::Closure(key, captured) => {
                    self.tainted_functions.insert(key);
                    pending.extend(
                        captured
                            .values
                            .values()
                            .flat_map(|values| values.iter().cloned()),
                    );
                }
                _ => {}
            }
        }
    }

    fn eligible_local_constructor(&self, key: u32, arg_count: usize) -> Option<&LocalConstructor> {
        let constructor = self.index.local_constructors.get(&key)?;
        let binding = self.index.class_bindings.get(&key)?;
        // A constructorless, fieldless class only allocates an instance for a
        // direct zero-argument `new`. Even if its class object escaped, a
        // modified prototype or deferred method cannot run during allocation.
        // Any unknown/reassigned callee alternative still takes the ordinary
        // opaque path in execute_call. Other constructors need the class and
        // prototype to remain unexposed to avoid setters and method reentry.
        let inert_allocation = constructor.inert_allocation && arg_count == 0;
        let checked_default_fields = constructor.checked_default_fields && arg_count == 0;
        if constructor.checked_default_fields && arg_count != 0 {
            return None;
        }
        if !inert_allocation
            && !checked_default_fields
            && (self.index.other_class_uses.contains(binding)
                || (self.export_reentry_enabled && self.index.exported_bindings.contains(binding))
                || self.tainted_classes.contains(&key)
                || !self.ordinary_object_prototype_intact()
                || self
                    .global_escaped_callbacks
                    .contains(&AbstractValue::ClassMembers(key)))
        {
            return None;
        }
        if constructor
            .binding_reads
            .iter()
            .any(|id| !self.flow.is_declared(id))
        {
            return None;
        }
        Some(constructor)
    }

    fn construct_local_class(
        &mut self,
        key: u32,
        args: &[Values],
    ) -> Option<(Outcome, AbstractValue)> {
        let constructor = self.eligible_local_constructor(key, args.len())?.clone();
        if self.export_reentry_enabled {
            let exported: Values = self
                .index
                .exported_bindings
                .iter()
                .flat_map(|id| self.prefix_values.get(id).into_iter().flatten().cloned())
                .collect();
            self.taint_objects(&exported);
        }
        for (index, _) in &constructor.property_reads {
            let values = args.get(*index)?;
            if values.is_empty()
                || !values.iter().all(|value| {
                    matches!(value,
                    AbstractValue::Origin(ValueOrigin::Object(object))
                        if self.index.plain_data_objects.contains(object)
                            && !self.tainted_objects.contains(object))
                })
            {
                return None;
            }
        }
        let outcome = constructor.key.map_or_else(
            || Outcome {
                complete: true,
                ..Outcome::default()
            },
            |constructor_key| self.invoke(constructor_key, args.to_vec(), None, true),
        );
        let values: Values = args.iter().flatten().cloned().collect();
        let instance = AbstractValue::Instance(key, self.intern_callback_set(values));
        Some((outcome, instance))
    }

    /// The foreign wrapper can invoke its captured callbacks and any values
    /// they return synchronously. It cannot resume them past an await.
    fn reachable_objects(&self, values: &Values) -> BTreeSet<u32> {
        let mut objects = BTreeSet::new();
        let mut pending: Vec<_> = values.iter().cloned().collect();
        let mut visited = BTreeSet::new();
        while let Some(value) = pending.pop() {
            if !visited.insert(value.clone()) {
                continue;
            }
            match value {
                AbstractValue::Origin(ValueOrigin::Object(object)) => {
                    objects.insert(object);
                    if let Some(properties) = self.prefix_object_properties.get(&object) {
                        pending.extend(properties.values().flatten().cloned());
                    }
                }
                AbstractValue::Instance(_, captured)
                | AbstractValue::DeferredCallbacks(captured) => {
                    pending.extend(captured.values.iter().cloned())
                }
                AbstractValue::Closure(_, captured) => {
                    pending.extend(captured.values.values().flatten().cloned())
                }
                _ => {}
            }
        }
        objects
    }

    fn expose_class_capabilities(&mut self, values: &Values) {
        let mut pending: Vec<_> = values.iter().cloned().collect();
        let mut visited = BTreeSet::new();
        while let Some(value) = pending.pop() {
            if !visited.insert(value.clone()) {
                continue;
            }
            match value {
                AbstractValue::Origin(ValueOrigin::Class(key))
                | AbstractValue::ClassMembers(key) => {
                    self.escaped_class_capabilities.insert(key);
                }
                AbstractValue::Instance(key, captured) => {
                    self.escaped_class_capabilities.insert(key);
                    pending.extend(captured.values.iter().cloned());
                }
                AbstractValue::Origin(ValueOrigin::Object(object)) => {
                    if let Some(properties) = self.prefix_object_properties.get(&object) {
                        pending.extend(properties.values().flatten().cloned());
                    }
                }
                AbstractValue::DeferredCallbacks(captured) => {
                    pending.extend(captured.values.iter().cloned())
                }
                AbstractValue::Closure(_, captured) => {
                    pending.extend(captured.values.values().flatten().cloned())
                }
                _ => {}
            }
        }
    }

    fn default_class_has_instance(&self, values: &Values) -> bool {
        let [AbstractValue::Origin(ValueOrigin::Class(key))] =
            values.iter().collect::<Vec<_>>().as_slice()
        else {
            return false;
        };
        self.index
            .class_values
            .get(key)
            .is_some_and(|class| class.default_has_instance)
            && !self.tainted_classes.contains(key)
            && !self.escaped_class_capabilities.contains(key)
            && !self
                .global_escaped_callbacks
                .contains(&AbstractValue::ClassMembers(*key))
            && self.index.class_bindings.get(key).is_some_and(|binding| {
                !self.index.other_class_uses.contains(binding)
                    && !self.index.exported_bindings.contains(binding)
            })
    }

    fn ordinary_prototype_chain(&self, values: &Values) -> bool {
        !values.is_empty()
            && values.iter().all(|value| match value {
                AbstractValue::NonCallable | AbstractValue::Undefined => true,
                AbstractValue::Origin(ValueOrigin::Object(object)) => {
                    !self.tainted_objects.contains(object)
                        && self.array_intrinsics_intact()
                        && (self.index.plain_data_objects.contains(object)
                            || self.ordinary_spread_copies.contains(object)
                            || self.dense_arrays.contains_key(object))
                }
                _ => false,
            })
    }

    fn contains_dense_array(&self, values: &Values) -> bool {
        self.reachable_objects(values)
            .iter()
            .any(|object| self.dense_arrays.contains_key(object))
    }

    fn promote_raw_callbacks(&mut self, values: &Values, outcome: &mut Outcome) {
        let effect_owner = self.current_top_owner.unwrap();
        let mut owners = BTreeSet::new();
        for callback in self.callback_values(values) {
            let owner = match callback {
                AbstractValue::ForeignDeferredRecord => {
                    self.promote_foreign_record(outcome);
                    None
                }
                AbstractValue::Origin(ValueOrigin::Function(key))
                | AbstractValue::Closure(key, _) => self
                    .index
                    .callables
                    .get(&key)
                    .map(|callable| callable.owner),
                AbstractValue::ClassMembers(key) => self
                    .flow
                    .callable_source_unit
                    .get(&ValueOrigin::Class(key))
                    .or_else(|| self.index.class_owners.get(&key))
                    .copied(),
                _ => None,
            };
            owners.extend(owner);
        }
        if self.reachability.incomplete {
            for owner in owners {
                let (reads, rebinds) = self
                    .source_unit_read_closure
                    .lazy_effects_for_source_unit(owner);
                let reads = reads.clone();
                let rebinds = rebinds.clone();
                let referenced = reads
                    .iter()
                    .flat_map(|id| self.prefix_values.get(id).into_iter().flatten().cloned())
                    .collect();
                self.taint_objects(&referenced);
                self.promote_lazy_effects(&reads, &rebinds, effect_owner, outcome);
            }
            return;
        }
        let reached = SourceUnitReadClosure::extend_synchronous_source_units(
            &self.synchronous_owner_adjacency,
            owners,
            &mut BTreeSet::new(),
        );
        for owner in reached {
            let fact = &self.facts[owner.0];
            // This coverage-checked union includes every retained callable's
            // synchronous prefix, including returned callbacks and getters.
            // Bodies past a definite await cannot run during this protocol.
            let reads = self.synchronous_owner_reads[owner.0]
                .unwrap_or(&fact.reads.lazy)
                .clone();
            let rebinds = fact.rebinds.lazy.clone();
            let referenced = reads
                .iter()
                .flat_map(|id| self.prefix_values.get(id).into_iter().flatten().cloned())
                .collect();
            self.taint_objects(&referenced);
            // Write coverage is not complete: retain all lazy rebinds.
            self.promote_lazy_effects(&reads, &rebinds, effect_owner, outcome);
        }
    }

    fn promote_deferred_callbacks(&mut self, captured: &Values, outcome: &mut Outcome) {
        let effect_owner = self.current_top_owner.unwrap();
        let mut roots = BTreeSet::new();
        for callback in self.callback_values(captured) {
            let key = match callback {
                AbstractValue::Origin(ValueOrigin::Function(key))
                | AbstractValue::Closure(key, _) => key,
                AbstractValue::ClassMembers(key) => {
                    self.promote_class_members(key, outcome);
                    continue;
                }
                _ => continue,
            };
            if let Some(callable) = self.index.callables.get(&key) {
                roots.insert(callable.owner);
            }
        }
        if self.reachability.incomplete {
            for owner in roots {
                let (reads, rebinds) = self
                    .source_unit_read_closure
                    .lazy_effects_for_source_unit(owner);
                self.promote_lazy_effects(reads, rebinds, effect_owner, outcome);
            }
        } else {
            let reached = SourceUnitReadClosure::extend_synchronous_source_units(
                &self.synchronous_owner_adjacency,
                roots,
                &mut BTreeSet::new(),
            );
            for owner in reached {
                let fact = &self.facts[owner.0];
                let reads = self.synchronous_owner_reads[owner.0].unwrap_or(&fact.reads.lazy);
                // Retain complete rebinds: prefix coverage currently applies
                // only to reads, not every update or destructuring write.
                self.promote_lazy_effects(reads, &fact.rebinds.lazy, effect_owner, outcome);
            }
        }
    }

    fn promote_returned_callbacks(&mut self, returned: &Values) {
        let callbacks: Values = self
            .callback_values(returned)
            .into_iter()
            .filter(|callback| {
                match callback {
                    AbstractValue::Origin(ValueOrigin::Function(key))
                    | AbstractValue::Closure(key, _) => {
                        self.index.callables.get(key).is_some_and(|callable| {
                            self.returned_callback_owners
                                .insert((self.reachability.incomplete, callable.owner))
                        })
                    }
                    // Keep class capability/superclass checks on their existing
                    // path. Other aggregate values have already been expanded by
                    // callback_values and are not callable roots themselves.
                    AbstractValue::ClassMembers(_) => true,
                    _ => false,
                }
            })
            .collect();
        if callbacks.is_empty() {
            return;
        }
        let mut outcome = std::mem::take(&mut self.fallback_outcome);
        let rebinds_before = outcome.effects.rebinds.len();
        outcome.unsupported(
            ExecutionOperation::ReturnedCallback,
            self.current_top_owner
                .expect("returned callback has a top owner"),
            DUMMY_SP,
        );
        outcome.unknown_call = true;
        self.promote_deferred_callbacks(&callbacks, &mut outcome);
        // Class/superclass capability checks can invoke code and populate the
        // shared accumulator reentrantly. Preserve those contributions before
        // restoring the outer summary, including roots marked cached there.
        let mut reentrant = std::mem::take(&mut self.fallback_outcome);
        outcome.effects.reads.append(&mut reentrant.effects.reads);
        outcome
            .effects
            .rebinds
            .append(&mut reentrant.effects.rebinds);
        outcome
            .effects
            .uninitialized_reads
            .append(&mut reentrant.effects.uninitialized_reads);
        outcome
            .executed_owners
            .append(&mut reentrant.executed_owners);
        outcome.unknown_call |= reentrant.unknown_call;
        outcome.inherit_precision_losses(reentrant.effects.precision_losses);
        if outcome.effects.rebinds.len() != rebinds_before {
            self.apply_rebind_effects(&outcome.effects.rebinds);
        }
        // These effects belong to the active top-level prefix, regardless of
        // the lexical callee. Merge them once there, rather than copying the
        // same large read set through each cached nested invocation.
        self.fallback_outcome = outcome;
    }

    /// Opaque code holding a class or its prototype can retrieve and run any
    /// method, getter, field callback, or constructor synchronously. Cached
    /// owner traversal includes those bodies and their synchronous callees,
    /// while stopping reads after a definite await. Owners without complete
    /// callable coverage retain their whole lazy read set.
    fn promote_class_members(&mut self, key: u32, outcome: &mut Outcome) {
        if !self.active_class_member_promotions.insert(key) {
            return;
        }
        let owner = self
            .flow
            .callable_source_unit
            .get(&ValueOrigin::Class(key))
            .copied()
            .or_else(|| self.index.class_owners.get(&key).copied());
        let Some(owner) = owner else {
            self.active_class_member_promotions.remove(&key);
            return;
        };
        let effect_owner = self.current_top_owner.unwrap_or(owner);
        // Class expressions may have only their containing statement owner,
        // while their constructor and methods still contribute lazy reads to
        // that owner's summary.
        if self.reachability.incomplete {
            let (reads, rebinds) = self
                .source_unit_read_closure
                .lazy_effects_for_source_unit(owner);
            self.promote_lazy_effects(reads, rebinds, effect_owner, outcome);
        } else {
            if !self.class_member_effects.contains_key(&owner) {
                let reached = SourceUnitReadClosure::extend_synchronous_source_units(
                    &self.synchronous_owner_adjacency,
                    [owner],
                    &mut BTreeSet::new(),
                );
                let mut reads = BTreeSet::new();
                let mut rebinds = BTreeSet::new();
                for reached_owner in reached {
                    let fact = &self.facts[reached_owner.0];
                    reads.extend(
                        self.synchronous_owner_reads[reached_owner.0]
                            .unwrap_or(&fact.reads.lazy)
                            .iter()
                            .cloned(),
                    );
                    // Prefix coverage is only proved for reads. Preserve all
                    // lazy writes, including destructuring and updates.
                    rebinds.extend(fact.rebinds.lazy.iter().cloned());
                }
                self.class_member_effects.insert(owner, (reads, rebinds));
            }
            let (reads, rebinds) = &self.class_member_effects[&owner];
            self.promote_lazy_effects(reads, rebinds, effect_owner, outcome);
        }
        self.promote_captured_superclass(key, owner, outcome);
        // The opaque caller may invoke a method that changes a top-level
        // binding. Subsequent prefix calls must include its new possible
        // value, just as they do after an ordinary callback invocation.
        self.apply_rebind_effects(&outcome.effects.rebinds);
        self.active_class_member_promotions.remove(&key);
    }

    fn promote_captured_superclass(
        &mut self,
        key: u32,
        owner: SourceUnitOrdinal,
        outcome: &mut Outcome,
    ) {
        let Some(class) = self.index.class_values.get(&key) else {
            return;
        };
        if class.super_class.is_none() {
            return;
        }
        let values = self
            .class_superclasses
            .get(&key)
            .cloned()
            .unwrap_or_else(unknown);
        let mut has_unknown = values.is_empty();
        for value in values {
            let (function, captured) = match value {
                AbstractValue::Origin(ValueOrigin::Class(base))
                | AbstractValue::ClassMembers(base) => {
                    self.promote_class_members(base, outcome);
                    continue;
                }
                AbstractValue::Origin(ValueOrigin::Function(function)) => (function, None),
                AbstractValue::Closure(function, captured) => (function, Some(captured)),
                AbstractValue::Undefined
                | AbstractValue::NonCallable
                | AbstractValue::Unavailable => continue,
                _ => {
                    has_unknown = true;
                    continue;
                }
            };
            let count = self
                .index
                .callables
                .get(&function)
                .map_or(0, |callable| callable.params.len());
            let nested = self.invoke(function, vec![unknown(); count], captured, false);
            self.apply_rebind_effects(&nested.effects.rebinds);
            outcome.effects.reads.extend(nested.effects.reads);
            outcome.effects.rebinds.extend(nested.effects.rebinds);
            outcome
                .effects
                .uninitialized_reads
                .extend(nested.effects.uninitialized_reads);
            outcome.executed_owners.extend(nested.executed_owners);
            outcome.unknown_call |= nested.unknown_call;
            outcome.inherit_completeness(nested.complete, nested.effects.precision_losses);
        }
        if has_unknown {
            outcome.unsupported(ExecutionOperation::SuperclassConstruction, owner, DUMMY_SP);
            outcome.unknown_call = true;
            let effect_owner = self.current_top_owner.unwrap_or(owner);
            self.note_unknown_execution(effect_owner);
            self.invoke_possible_callbacks(&[unknown()], false, outcome);
        }
    }

    fn closure_captures(&mut self, key: u32, captures: &Context) -> CapturedContext {
        let Some(callable) = self.index.callables.get(&key) else {
            return self.intern_captures(Context::new());
        };
        let free_bindings = callable.reads.iter().chain(callable.rebinds.iter());
        let mut captured = Context::new();
        for id in free_bindings {
            if self.top_bindings.contains(id) {
                continue;
            }
            if let Some(values) = captures.get(id) {
                captured.insert(
                    id.clone(),
                    if self.mutable_captures.contains(id) || captured.len() >= 64 {
                        unknown()
                    } else {
                        values.clone()
                    },
                );
            }
        }
        self.intern_captures(captured)
    }

    fn available_export_callbacks(&self) -> Values {
        if !self.export_reentry_enabled {
            return Values::new();
        }
        self.export_callback_values()
    }

    fn export_callback_values(&self) -> Values {
        // Discover the whole export surface in one traversal. Exported aliases,
        // unknown values, and shared objects must not each re-expand the same
        // escaped callback/capture graph independently.
        let mut values = Values::new();
        for binding in &self.index.exported_bindings {
            if let Some(exported) = self.prefix_values.get(binding) {
                values.extend(exported.iter().cloned());
            }
        }
        values.extend(
            self.index
                .exported_hoisted_functions
                .iter()
                .copied()
                .map(|key| AbstractValue::Origin(ValueOrigin::Function(key))),
        );
        values.extend(self.exported_runtime_values.iter().cloned());
        self.callback_values(&values)
    }

    fn invoke_possible_callbacks(
        &mut self,
        args: &[Values],
        no_sync_arguments: bool,
        outcome: &mut Outcome,
    ) {
        let mut callbacks = self.global_escaped_callbacks.clone();
        callbacks.extend(self.available_export_callbacks());
        // A callback-argument exemption does not certify foreign getters or
        // direct live-cell captures reachable through a returned record.
        let supplied: Values = args.iter().flatten().cloned().collect();
        let supplied_callbacks = self.callback_values(&supplied);
        if callbacks.contains(&AbstractValue::ForeignDeferredRecord)
            || supplied_callbacks.contains(&AbstractValue::ForeignDeferredRecord)
        {
            self.promote_foreign_record(outcome);
        }
        if !no_sync_arguments {
            callbacks.extend(supplied_callbacks);
        }
        // Activate the foreign pool before any callback-size fallback, which
        // otherwise only expands callables with local source owners.
        callbacks.remove(&AbstractValue::ForeignDeferredRecord);
        // Unknown host code may invoke any callback it received with another
        // currently available callback, so each formal gets the conservative
        // callback set plus a non-callable/unknown possibility.
        let mut possible_argument = callbacks.clone();
        possible_argument.insert(AbstractValue::Unknown);
        let args_for = |key: u32, index: &ExecutionIndex| {
            let count = index
                .callables
                .get(&key)
                .map_or(0, |callable| callable.params.len());
            vec![possible_argument.clone(); count]
        };
        let reentrant_pool = callbacks.iter().any(|callback| {
            let key = match callback {
                AbstractValue::Origin(ValueOrigin::Function(key))
                | AbstractValue::Closure(key, _) => *key,
                _ => return false,
            };
            self.active.contains(&(key, self.current_top_owner))
        });
        for callback in callbacks.clone() {
            if reentrant_pool
                || callbacks.len() > MAX_SYNCHRONOUS_CALLBACK_POOL
                || self.invocation_count >= MAX_SYNCHRONOUS_INVOCATIONS
                || self.invocation_depth >= MAX_SYNCHRONOUS_INVOCATION_DEPTH
            {
                // A recursive member already falls back to a closure over the
                // entire pool in its arguments. Summarize that pool once rather
                // than enumerate permutations of callbacks around the cycle.
                // The same batch fallback bounds exhausted acyclic expansions.
                let reason = if reentrant_pool {
                    ExecutionPrecisionLossReason::RecursiveInvocation
                } else if callbacks.len() > MAX_SYNCHRONOUS_CALLBACK_POOL {
                    ExecutionPrecisionLossReason::CallbackPoolLimit {
                        limit: MAX_SYNCHRONOUS_CALLBACK_POOL,
                    }
                } else if self.invocation_count >= MAX_SYNCHRONOUS_INVOCATIONS {
                    ExecutionPrecisionLossReason::InvocationExpansionLimit {
                        limit: MAX_SYNCHRONOUS_INVOCATIONS,
                    }
                } else {
                    ExecutionPrecisionLossReason::InvocationDepthLimit {
                        limit: MAX_SYNCHRONOUS_INVOCATION_DEPTH,
                    }
                };
                let nested = self.bounded_invocation_fallback(
                    self.current_top_owner
                        .expect("callback call has a top owner"),
                    std::slice::from_ref(&callbacks),
                    &Context::new(),
                    reason,
                );
                self.apply_rebind_effects(&nested.effects.rebinds);
                outcome.effects.reads.extend(nested.effects.reads);
                outcome.effects.rebinds.extend(nested.effects.rebinds);
                outcome
                    .effects
                    .uninitialized_reads
                    .extend(nested.effects.uninitialized_reads);
                outcome.executed_owners.extend(nested.executed_owners);
                outcome.unknown_call |= nested.unknown_call;
                outcome.inherit_completeness(nested.complete, nested.effects.precision_losses);
                break;
            }
            let nested = match callback {
                AbstractValue::ClassMembers(key) => {
                    self.promote_class_members(key, outcome);
                    continue;
                }
                AbstractValue::DeferredCallbacks(captured) => {
                    self.promote_deferred_callbacks(&captured.values, outcome);
                    continue;
                }
                AbstractValue::Origin(ValueOrigin::Function(key)) => {
                    self.invoke(key, args_for(key, self.index), None, false)
                }
                AbstractValue::Closure(key, captured) => {
                    self.invoke(key, args_for(key, self.index), Some(captured), false)
                }
                _ => continue,
            };
            // Foreign code can retain and invoke a callback's synchronous
            // return value as well. This includes a descriptor getter that
            // returns another callable; stopping at the getter loses the
            // returned callable's importer reads. Prefix coverage preserves
            // the ordinary boundary at an unconditional await.
            self.promote_returned_callbacks(&nested.returns);
            self.apply_rebind_effects(&nested.effects.rebinds);
            outcome.effects.reads.extend(nested.effects.reads);
            outcome.effects.rebinds.extend(nested.effects.rebinds);
            outcome
                .effects
                .uninitialized_reads
                .extend(nested.effects.uninitialized_reads);
            outcome.executed_owners.extend(nested.executed_owners);
            outcome.unknown_call |= nested.unknown_call;
            outcome.inherit_completeness(nested.complete, nested.effects.precision_losses);
        }
        // An opaque callee can retain any callback passed to it and invoke it
        // during a later prefix event.
        let before = self.global_escaped_callbacks.len();
        let values = args.iter().flatten().cloned().collect();
        self.taint_objects(&values);
        self.global_escaped_callbacks
            .extend(self.callback_values(&values));
        if self.global_escaped_callbacks.len() != before {
            self.prefix_revision = self.prefix_revision.wrapping_add(1);
        }
    }

    fn apply_rebind_effects(&mut self, rebinds: &BTreeSet<(Id, SourceUnitOrdinal)>) {
        for (id, _) in rebinds {
            if !self.top_bindings.contains(id) {
                continue;
            }
            let changed = self
                .prefix_values
                .entry(id.clone())
                .or_default()
                .insert(AbstractValue::Unknown);
            if changed {
                self.prefix_revision = self.prefix_revision.wrapping_add(1);
            }
        }
    }

    fn bounded_invocation_fallback(
        &mut self,
        owner: SourceUnitOrdinal,
        args: &[Values],
        captured: &Context,
        reason: ExecutionPrecisionLossReason,
    ) -> Outcome {
        let effect_owner = self.current_top_owner.unwrap_or(owner);
        if self.array_iteration_depth > 0 {
            self.array_iteration_unsupported = true;
        }
        // Within one source prefix initialization is fixed. Keep a monotone
        // summary so each reachable owner's lazy effects are promoted once,
        // even when recursion or changing abstract arguments hit the bound
        // repeatedly. Reusing earlier fallback roots only adds constraints.
        let mut outcome = std::mem::take(&mut self.fallback_outcome);
        let rebinds_before = outcome.effects.rebinds.len();
        outcome.lose_precision(reason.clone(), owner, DUMMY_SP);
        outcome.unknown_call = true;
        outcome.executed_owners.insert(owner);

        // The recursive summary is no longer being expanded. Preserve reads
        // from this callable and callbacks actually present in its arguments,
        // captures, exports, or escaped globals. Do not turn ordinary
        // recursion into a whole-chunk reachability root.
        let mut possible_values: Values = args.iter().flatten().cloned().collect();
        possible_values.extend(captured.values().flatten().cloned());
        possible_values.extend(self.global_escaped_callbacks.iter().cloned());
        possible_values.extend(self.available_export_callbacks());
        let callbacks = self.callback_values(&possible_values);
        if callbacks.contains(&AbstractValue::ForeignDeferredRecord) {
            self.promote_foreign_record(&mut outcome);
        }
        let mut roots = BTreeSet::new();
        for callback in &callbacks {
            let key = match callback {
                AbstractValue::Origin(ValueOrigin::Function(key))
                | AbstractValue::Closure(key, _) => Some(*key),
                _ => None,
            };
            if let Some(callback_owner) =
                key.and_then(|key| self.index.callables.get(&key).map(|c| c.owner))
            {
                roots.insert(callback_owner);
            }
            if let AbstractValue::ClassMembers(key) = callback
                && let Some(&owner) = self
                    .flow
                    .callable_source_unit
                    .get(&ValueOrigin::Class(*key))
            {
                roots.insert(owner);
            }
        }
        self.extend_reachable_owners(roots.iter().copied().chain([owner]));
        let mut reached = SourceUnitReadClosure::extend_synchronous_source_units(
            &self.synchronous_owner_adjacency,
            roots.iter().copied(),
            &mut self.callback_fallback.expanded,
        );
        if self.reachability.incomplete {
            // Dynamic lexical scope defeats the normal binding-read closure.
            // Revisit even previously expanded owners: their cached prefix
            // effects may have omitted reads that eval or with can reach.
            if !self.callback_fallback.incomplete_expanded {
                self.callback_fallback.incomplete_expanded = true;
                for fact in self.facts {
                    self.callback_fallback.expanded.insert(fact.ordinal);
                    reached.push(fact.ordinal);
                }
            }
        }
        for reached_owner in reached {
            let fact = &self.facts[reached_owner.0];
            let reads = if self.reachability.incomplete {
                &fact.reads.lazy
            } else {
                self.synchronous_owner_reads[reached_owner.0].unwrap_or(&fact.reads.lazy)
            };
            for id in reads.iter().filter(|id| self.top_bindings.contains(*id)) {
                if self.callback_fallback.reads.insert(id.clone()) {
                    self.callback_fallback.read_additions.push(id.clone());
                }
            }
            for id in fact
                .rebinds
                .lazy
                .iter()
                .filter(|id| self.top_bindings.contains(*id))
            {
                if self.callback_fallback.rebinds.insert(id.clone()) {
                    self.callback_fallback.rebind_additions.push(id.clone());
                }
            }
        }
        let callback_sizes = (
            self.callback_fallback.reads.len(),
            self.callback_fallback.rebinds.len(),
        );
        if let Some((reads_before, rebinds_before)) = self.fallback_callback_sizes {
            // New escaped roots can appear during a statement. Apply only
            // their newly discovered IDs, rather than repeatedly reapplying
            // the entire growing closure at the same initialization prefix.
            let reads = self.callback_fallback.read_additions[reads_before..]
                .iter()
                .cloned()
                .collect();
            let rebinds = self.callback_fallback.rebind_additions[rebinds_before..]
                .iter()
                .cloned()
                .collect();
            self.promote_lazy_effects(&reads, &rebinds, effect_owner, &mut outcome);
        } else {
            self.promote_lazy_effects(
                &self.callback_fallback.reads,
                &self.callback_fallback.rebinds,
                effect_owner,
                &mut outcome,
            );
        }
        self.fallback_callback_sizes = Some(callback_sizes);
        // A directly truncated callable need not escape. Follow its same
        // synchronous prefix graph without publishing private roots into the
        // persistent callback cache. Expand each local owner once per source
        // statement; earlier promotions remain in fallback_outcome.
        if !self.callback_fallback.expanded.contains(&owner) {
            let reached = SourceUnitReadClosure::extend_synchronous_source_units(
                &self.synchronous_owner_adjacency,
                [owner],
                &mut self.fallback_local_owners,
            );
            for reached_owner in reached {
                let fact = &self.facts[reached_owner.0];
                let reads = self.synchronous_owner_reads[reached_owner.0]
                    .unwrap_or(&fact.reads.lazy)
                    .difference(&self.callback_fallback.reads)
                    .cloned()
                    .collect();
                let rebinds = fact
                    .rebinds
                    .lazy
                    .difference(&self.callback_fallback.rebinds)
                    .cloned()
                    .collect();
                self.promote_lazy_effects(&reads, &rebinds, effect_owner, &mut outcome);
            }
        }

        // The truncated body may have passed one of these callback values to
        // opaque code. Retain only this proven escape set for later calls.
        let before = self.global_escaped_callbacks.len();
        self.global_escaped_callbacks.extend(callbacks);
        if self.global_escaped_callbacks.len() != before {
            self.prefix_revision = self.prefix_revision.wrapping_add(1);
        }
        if outcome.effects.rebinds.len() != rebinds_before {
            self.apply_rebind_effects(&outcome.effects.rebinds);
        }
        // Fallback effects are attributed to the active top-level statement,
        // not the lexical callee. Accumulate them there once instead of copying
        // and re-merging the whole summary through every nested invocation.
        self.fallback_outcome = outcome;
        let mut result = Outcome {
            returns: unknown(),
            unknown_call: true,
            executed_owners: BTreeSet::from([owner]),
            ..Outcome::default()
        };
        result.lose_precision(reason, owner, DUMMY_SP);
        result
    }

    fn source_record_factory(&self, callee: &ValueExpr, args: &[ValueExpr]) -> bool {
        args.is_empty()
            && matches!(callee, ValueExpr::Binding(id)
            if self.index.returned_record_imports.contains(id)
                && self.index.collection_intrinsic_is_unmodified("Map"))
    }

    fn promote_foreign_record(&mut self, outcome: &mut Outcome) {
        // Foreign bodies may read importer cells through a direct ESM capture,
        // without calling an exported callback. Preserve all such obligations.
        // This deliberately buys precision only at construction, not member use.
        self.promote_lazy_effects(
            &self.top_bindings,
            &BTreeSet::new(),
            self.current_top_owner
                .expect("foreign record use has a top owner"),
            outcome,
        );
        outcome.unknown_call = true;
        outcome.lose_precision(
            ExecutionPrecisionLossReason::UnknownCallTarget,
            self.current_top_owner
                .expect("foreign record use has a top owner"),
            DUMMY_SP,
        );
    }

    fn evaluate(&mut self, expr: &ValueExpr, env: &Context, captures: &Context) -> Values {
        match expr {
            ValueExpr::Binding(id) => {
                if self.index.callback_factory_imports.contains(id) {
                    return BTreeSet::from([AbstractValue::CallbackFactory]);
                }
                if self.index.inline_callback_factory_imports.contains(id) {
                    return BTreeSet::from([AbstractValue::InlineCallbackFactory]);
                }
                if self.top_bindings.contains(id) {
                    if let Some(prefix_values) = self.prefix_values.get(id) {
                        let mut values = prefix_values.clone();
                        if !self.initialized_bindings.contains(id) {
                            values.insert(if self.var_bindings.contains(id) {
                                AbstractValue::Undefined
                            } else {
                                AbstractValue::Unavailable
                            });
                        }
                        values
                    } else if self.var_bindings.contains(id) {
                        BTreeSet::from([AbstractValue::Undefined])
                    } else {
                        // Whole-chunk flow includes future initializers. A
                        // missing prefix value for a local binding is TDZ.
                        BTreeSet::from([AbstractValue::Unavailable])
                    }
                } else if let Some(values) = env.get(id) {
                    values.clone()
                } else if self.index.global_object_names.contains(id.0.as_ref())
                    && !self.flow.is_declared(id)
                    && !self.shadowed.contains(id.0.as_ref())
                {
                    BTreeSet::from([AbstractValue::GlobalObject])
                } else {
                    let values = from_flow(self.flow.values_of_binding(id));
                    if values.is_empty() { unknown() } else { values }
                }
            }
            ValueExpr::Origin(ValueOrigin::Function(key)) => {
                BTreeSet::from([AbstractValue::Closure(
                    *key,
                    self.closure_captures(*key, captures),
                )])
            }
            ValueExpr::Origin(origin) => BTreeSet::from([AbstractValue::Origin(*origin)]),
            ValueExpr::ObjectLiteral(key, properties) => {
                if self.index.exact_object_spread_copies.contains(key)
                    && let [(None, source)] = properties.as_slice()
                {
                    let source = self.evaluate(source, env, captures);
                    if let Some(properties) = self.ordinary_data_properties(&source) {
                        let previously_evaluated = self.ordinary_spread_copies.contains(key);
                        let current = self.prefix_object_properties.entry(*key).or_default();
                        let mut changed = false;
                        if previously_evaluated {
                            for (property, values) in current.iter_mut() {
                                if !properties.contains_key(property) {
                                    changed |= values.insert(AbstractValue::Undefined);
                                }
                            }
                        }
                        for (property, values) in properties {
                            let new_property = !current.contains_key(&property);
                            let slot = current.entry(property).or_default();
                            let before = slot.len();
                            if previously_evaluated && new_property {
                                slot.insert(AbstractValue::Undefined);
                            }
                            slot.extend(values);
                            changed |= slot.len() != before;
                        }
                        if changed {
                            self.prefix_revision = self.prefix_revision.wrapping_add(1);
                        }
                        self.ordinary_spread_copies.insert(*key);
                        return BTreeSet::from([AbstractValue::Origin(ValueOrigin::Object(*key))]);
                    }
                    self.tainted_objects.insert(*key);
                    if self.array_iteration_depth > 0 {
                        self.array_iteration_unsupported = true;
                    }
                }
                if self.index.array_elements.contains_key(key) {
                    return self.evaluate_array(*key, env, captures);
                }
                let mut evaluated = BTreeMap::<Option<String>, Values>::new();
                let mut may_throw = false;
                let mut can_complete = true;
                for (property, expression) in properties {
                    let mut values = self.evaluate(expression, env, captures);
                    if values.remove(&AbstractValue::Unavailable) {
                        may_throw = true;
                        can_complete &= !values.is_empty();
                    }
                    evaluated
                        .entry(property.clone())
                        .or_default()
                        .extend(values);
                }
                if can_complete {
                    let current = self.prefix_object_properties.entry(*key).or_default();
                    let mut changed = false;
                    for (property, values) in evaluated {
                        let current_values = current.entry(property).or_default();
                        let before = current_values.len();
                        current_values.extend(values);
                        changed |= current_values.len() != before;
                    }
                    if changed {
                        self.prefix_revision = self.prefix_revision.wrapping_add(1);
                    }
                }
                let mut result = Values::new();
                if can_complete {
                    result.insert(AbstractValue::Origin(ValueOrigin::Object(*key)));
                }
                if may_throw || !can_complete {
                    result.insert(AbstractValue::Unavailable);
                }
                result
            }
            ValueExpr::Undefined => BTreeSet::from([AbstractValue::Undefined]),
            ValueExpr::NonCallable => BTreeSet::from([AbstractValue::NonCallable]),
            ValueExpr::Member(object, property, is_static_identifier) => {
                if let ValueExpr::Binding(id) = object.as_ref()
                    && *is_static_identifier
                    && property.as_ref().is_some_and(|property| {
                        self.index
                            .callback_factory_members
                            .get(id)
                            .is_some_and(|members| members.contains(property))
                    })
                {
                    return BTreeSet::from([AbstractValue::CallbackFactory]);
                }
                if matches!(object.as_ref(), ValueExpr::Binding(id) if id.0.as_ref() == "Object" && !self.flow.is_declared(id))
                    && property.as_deref() == Some("assign")
                    && !self.shadowed.contains("Object")
                {
                    return BTreeSet::from([AbstractValue::ObjectAssign]);
                }
                let objects = self.evaluate(object, env, captures);
                let mut values = Values::new();
                for object in objects {
                    match object {
                        AbstractValue::ForeignDeferredRecord => {
                            values.insert(AbstractValue::ForeignDeferredRecord);
                        }
                        AbstractValue::ArrayIntrinsic => {
                            values.insert(AbstractValue::ArrayIntrinsic);
                        }
                        AbstractValue::GlobalObject => {
                            values.extend(self.global_escaped_callbacks.iter().cloned());
                            values.insert(AbstractValue::Unknown);
                        }
                        AbstractValue::Unavailable => {
                            values.insert(AbstractValue::Unavailable);
                        }
                        AbstractValue::DeferredCallbacks(captured) => {
                            values.insert(AbstractValue::DeferredCallbacks(captured));
                        }
                        AbstractValue::Origin(ValueOrigin::Class(key))
                        | AbstractValue::ClassMembers(key) => {
                            values.insert(AbstractValue::ClassMembers(key));
                        }
                        AbstractValue::Instance(key, captured) => {
                            if matches!(property.as_deref(), Some("constructor" | "__proto__")) {
                                self.tainted_classes.insert(key);
                            }
                            values.insert(AbstractValue::Instance(key, captured));
                        }
                        AbstractValue::Origin(ValueOrigin::Object(object_id)) => {
                            if self.dense_arrays.contains_key(&object_id)
                                && (property.is_none()
                                    || matches!(
                                        property.as_deref(),
                                        Some("constructor" | "__proto__")
                                    ))
                            {
                                values.insert(AbstractValue::ArrayIntrinsic);
                            }

                            if let Some(properties) = self.prefix_object_properties.get(&object_id)
                            {
                                if let Some(property) = property {
                                    if let Some(found) = properties.get(&Some(property.clone())) {
                                        values.extend(found.iter().cloned());
                                    } else if self.absent_plain_data_property(object_id, property) {
                                        values.insert(AbstractValue::Undefined);
                                    } else {
                                        values.insert(AbstractValue::Unknown);
                                    }
                                    values.extend(
                                        properties
                                            .get(&None)
                                            .into_iter()
                                            .flat_map(|found| found.iter().cloned()),
                                    );
                                } else {
                                    values.extend(
                                        properties.values().flat_map(|found| found.iter().cloned()),
                                    );
                                }
                            } else if let Some(property) = property {
                                if let Some(found) =
                                    self.flow.values_of_property(object_id, property)
                                {
                                    values.extend(from_flow(found.clone()));
                                } else {
                                    values.insert(AbstractValue::Unknown);
                                }
                            } else {
                                values.insert(AbstractValue::Unknown);
                            }
                        }
                        _ => {
                            values.insert(AbstractValue::Unknown);
                        }
                    }
                }
                values
            }
            ValueExpr::Call(callee, args, span) => {
                if self.frozen_namespace_calls.contains(span) {
                    return self.evaluate(&args[0], env, captures);
                }
                if let Some((proof, _, _, _, _)) = self.array_iteration_plan(*span, env, captures) {
                    return if proof.method == ArrayIterationMethod::ForEach {
                        BTreeSet::from([AbstractValue::Undefined])
                    } else {
                        BTreeSet::from([AbstractValue::Origin(ValueOrigin::Object(
                            proof.result.unwrap(),
                        ))])
                    };
                }
                let targets = self.evaluate(callee, env, captures);
                if targets.len() == 1 && targets.contains(&AbstractValue::Unavailable) {
                    return BTreeSet::from([AbstractValue::Unavailable]);
                }
                let inline_callback = matches!(
                    args.as_slice(),
                    [ValueExpr::Origin(ValueOrigin::Function(_))]
                );
                let expressions = args;
                let mut args: Vec<Values> = args
                    .iter()
                    .map(|arg| self.evaluate(arg, env, captures))
                    .collect();
                let args_may_throw = args
                    .iter()
                    .any(|values| values.contains(&AbstractValue::Unavailable));
                if args
                    .iter()
                    .any(|values| values.len() == 1 && values.contains(&AbstractValue::Unavailable))
                {
                    return BTreeSet::from([AbstractValue::Unavailable]);
                }
                for values in &mut args {
                    values.remove(&AbstractValue::Unavailable);
                }
                if self.source_record_factory(callee, expressions) {
                    return BTreeSet::from([AbstractValue::ForeignDeferredRecord]);
                }
                if let Some(returns) =
                    self.resolve_native_call(CallKind::Call, callee, expressions, &args)
                {
                    return Self::native_return_values(returns, &args, args_may_throw);
                }
                if let Some(callbacks) = self.retained_factory_callbacks(*span, expressions, &args)
                {
                    let result = self.deferred_callbacks(callbacks);
                    return if args_may_throw {
                        BTreeSet::from([result, AbstractValue::Unavailable])
                    } else {
                        BTreeSet::from([result])
                    };
                }
                if let Some(callbacks) = self.proven_from_entries_callbacks(*span, env, captures) {
                    return BTreeSet::from([self.deferred_callbacks(callbacks)]);
                }
                let deferred_callback = matches!(args.as_slice(), [values] if !values.is_empty()
                    && values.iter().all(|value| matches!(value, AbstractValue::DeferredCallbacks(_))));
                let mut returns = Values::new();
                let no_targets = targets.is_empty();
                let may_throw = args_may_throw
                    || no_targets
                    || targets.iter().any(|target| {
                        matches!(
                            target,
                            AbstractValue::Unavailable
                                | AbstractValue::Undefined
                                | AbstractValue::NonCallable
                        )
                    });
                for target in targets {
                    match target {
                        AbstractValue::CallbackFactory if inline_callback || deferred_callback => {
                            if let ValueExpr::Binding(imported_local) = callee.as_ref()
                                && let Some(&(owner, call_span)) = self.call_site_owners.get(span)
                            {
                                self.record_callback_factory_use(
                                    imported_local,
                                    CallbackFactoryUseKind::ReturnShape,
                                    owner,
                                    call_span,
                                );
                            }
                            let callbacks = self.callback_values(&args[0]);
                            returns.insert(self.deferred_callbacks(callbacks));
                        }
                        AbstractValue::InlineCallbackFactory if inline_callback => {
                            let callbacks = self.callback_values(&args[0]);
                            returns.insert(self.deferred_callbacks(callbacks));
                        }
                        AbstractValue::DeferredCallbacks(captured) => {
                            returns.insert(AbstractValue::DeferredCallbacks(captured));
                            returns.insert(AbstractValue::Unknown);
                        }
                        AbstractValue::Unavailable
                        | AbstractValue::Undefined
                        | AbstractValue::NonCallable => {}
                        AbstractValue::Origin(ValueOrigin::Function(key)) => {
                            returns.extend(self.invoke(key, args.clone(), None, false).returns);
                        }
                        AbstractValue::Closure(key, captured) => {
                            returns.extend(
                                self.invoke(key, args.clone(), Some(captured), false)
                                    .returns,
                            );
                        }
                        AbstractValue::ObjectAssign => {
                            returns.extend(args.first().cloned().unwrap_or_else(unknown));
                        }
                        AbstractValue::Unknown => {
                            returns.insert(AbstractValue::Unknown);
                        }
                        _ => {
                            returns.insert(AbstractValue::Unknown);
                        }
                    }
                }
                if returns.is_empty() && may_throw {
                    return BTreeSet::from([AbstractValue::Unavailable]);
                }
                if may_throw {
                    returns.insert(AbstractValue::Unavailable);
                }
                returns
            }
            ValueExpr::New(callee, args, span) => {
                let targets = self.evaluate(callee, env, captures);
                let evaluated_args: Vec<_> = args
                    .iter()
                    .map(|arg| self.evaluate(arg, env, captures))
                    .collect();
                let mut successful_args = evaluated_args.clone();
                let mut args_may_throw = targets.contains(&AbstractValue::Unavailable);
                for values in &mut successful_args {
                    args_may_throw |= values.remove(&AbstractValue::Unavailable);
                }
                if let Some(returns) =
                    self.resolve_native_call(CallKind::New, callee, args, &successful_args)
                {
                    return Self::native_return_values(returns, &successful_args, args_may_throw);
                }
                if self.index.retained_constructor_calls.contains_key(span) {
                    if targets.len() == 1 && targets.contains(&AbstractValue::Unavailable) {
                        return BTreeSet::from([AbstractValue::Unavailable]);
                    }
                    if evaluated_args.iter().any(|values| {
                        values.len() == 1 && values.contains(&AbstractValue::Unavailable)
                    }) {
                        return BTreeSet::from([AbstractValue::Unavailable]);
                    }
                    let mut successful_args = evaluated_args.clone();
                    let mut may_throw = false;
                    for values in &mut successful_args {
                        may_throw |= values.remove(&AbstractValue::Unavailable);
                    }
                    if let Some(callbacks) =
                        self.retained_constructor_callbacks(*span, args, &successful_args)
                    {
                        let result = self.deferred_callbacks(callbacks);
                        return if may_throw {
                            BTreeSet::from([result, AbstractValue::Unavailable])
                        } else {
                            BTreeSet::from([result])
                        };
                    }
                }
                let mut results = Values::new();
                if self.current_top_owner.is_some_and(|owner| {
                    self.is_proven_map_entry_array(callee, args, env, captures, owner)
                }) {
                    let mut can_complete = true;
                    let mut may_throw = false;
                    let mut successful_args = Values::new();
                    for values in &evaluated_args {
                        if values.contains(&AbstractValue::Unavailable) {
                            may_throw = true;
                            can_complete &= values.len() > 1;
                        }
                        successful_args.extend(
                            values
                                .iter()
                                .filter(|value| **value != AbstractValue::Unavailable)
                                .cloned(),
                        );
                    }
                    if can_complete {
                        let callbacks = self.callback_values(&successful_args);
                        results.insert(self.deferred_callbacks(callbacks));
                    }
                    if may_throw || !can_complete {
                        results.insert(AbstractValue::Unavailable);
                    }
                    return results;
                }
                for target in targets {
                    match target {
                        AbstractValue::Origin(ValueOrigin::Class(key)) => {
                            if let Some((_, instance)) =
                                self.construct_local_class(key, &evaluated_args)
                            {
                                results.insert(instance);
                            } else {
                                results.insert(AbstractValue::Unknown);
                            }
                        }
                        AbstractValue::Unavailable => {
                            results.insert(AbstractValue::Unavailable);
                        }
                        _ => {
                            results.insert(AbstractValue::Unknown);
                        }
                    }
                }
                if results.is_empty() {
                    unknown()
                } else {
                    results
                }
            }
            ValueExpr::Primitive(operands) => {
                let mut can_complete = true;
                let mut may_throw = false;
                for operand in operands {
                    let mut values = self.evaluate(operand, env, captures);
                    if values.remove(&AbstractValue::Unavailable) {
                        may_throw = true;
                    }
                    if values.is_empty() {
                        can_complete = false;
                        break;
                    }
                }
                let mut results = Values::new();
                if can_complete {
                    results.insert(AbstractValue::NonCallable);
                }
                if may_throw || !can_complete {
                    results.insert(AbstractValue::Unavailable);
                }
                results
            }
            ValueExpr::Alternatives(options) => options
                .iter()
                .flat_map(|option| self.evaluate(option, env, captures))
                .collect(),
            ValueExpr::Unknown => unknown(),
        }
    }

    fn invoke(
        &mut self,
        key: u32,
        args: Vec<Values>,
        captured: Option<CapturedContext>,
        fresh_checked_constructor: bool,
    ) -> Outcome {
        let Some((callable_owner, callable_opaque, uses_arguments)) =
            self.index.callables.get(&key).map(|callable| {
                (
                    callable.owner,
                    callable.opaque,
                    callable.reads.iter().any(|id| id.0.as_ref() == "arguments"),
                )
            })
        else {
            return Outcome::default();
        };
        let unsupported_inputs = uses_arguments
            || self.index.callables[&key]
                .params
                .iter()
                .any(|parameter| parameter.binding.is_none());
        // Implicit arguments can alias any actual input even without a
        // corresponding formal parameter. Invalidate input capabilities before
        // consulting cached outcomes, including calls outside array iteration.
        if unsupported_inputs {
            for values in &args {
                self.taint_objects(values);
            }
            if self.array_iteration_depth > 0 {
                self.array_iteration_unsupported = true;
            }
        }
        let empty_context = Context::new();
        let captured_context = captured
            .as_ref()
            .map_or(&empty_context, |captured| captured.values.as_ref());
        self.record_executed_owner(callable_owner);
        if callable_opaque {
            self.note_unknown_execution(callable_owner);
        }
        let signature = (
            key,
            args.clone(),
            captured.clone(),
            fresh_checked_constructor,
            self.reachability.revision,
            self.prefix_revision,
            self.current_top_owner,
        );
        if self.array_iteration_depth == 0
            && let Some(cached) = self.cache.get(&signature)
        {
            return cached.clone();
        }
        let active_invocation = (key, self.current_top_owner);
        if self.active.contains(&active_invocation)
            || self.invocation_depth >= MAX_SYNCHRONOUS_INVOCATION_DEPTH
            || self.invocation_count >= MAX_SYNCHRONOUS_INVOCATIONS
        {
            let reason = if self.active.contains(&active_invocation) {
                ExecutionPrecisionLossReason::RecursiveInvocation
            } else if self.invocation_depth >= MAX_SYNCHRONOUS_INVOCATION_DEPTH {
                ExecutionPrecisionLossReason::InvocationDepthLimit {
                    limit: MAX_SYNCHRONOUS_INVOCATION_DEPTH,
                }
            } else {
                ExecutionPrecisionLossReason::InvocationExpansionLimit {
                    limit: MAX_SYNCHRONOUS_INVOCATIONS,
                }
            };
            return self.bounded_invocation_fallback(
                callable_owner,
                &args,
                captured_context,
                reason,
            );
        }
        self.active.insert(active_invocation);
        self.invocation_depth += 1;
        self.invocation_count += 1;
        let Some(callable) = self.index.callables.get(&key) else {
            self.active.remove(&active_invocation);
            self.invocation_depth -= 1;
            return Outcome::default();
        };
        let mut env = captured_context.clone();
        for (index, param) in callable.params.iter().enumerate() {
            let mut values = args
                .get(index)
                .cloned()
                .unwrap_or_else(|| BTreeSet::from([AbstractValue::Undefined]));
            if let Some(default) = &param.default
                && values.contains(&AbstractValue::Undefined)
            {
                values.remove(&AbstractValue::Undefined);
                values.extend(self.evaluate(default, &env, &env));
            }
            if let Some(binding) = &param.binding {
                env.insert(binding.clone(), values);
            }
        }
        let unsupported_before = self.array_iteration_unsupported;
        if self.array_iteration_depth > 0 {
            if callable.reads.iter().any(|id| id.0.as_ref() == "arguments") {
                self.array_iteration_unsupported = true;
            }
            if let Some(reads) = self.index.callable_member_reads.get(&key) {
                for (receiver, property) in reads.clone() {
                    let values = self.evaluate(&receiver, &env, &env);
                    if property.is_none()
                        || values.is_empty()
                        || values.iter().any(|value| match value {
                            AbstractValue::NonCallable | AbstractValue::Undefined => false,
                            AbstractValue::Origin(ValueOrigin::Object(object)) => {
                                (!self.index.plain_data_objects.contains(object)
                                    && !self.ordinary_spread_copies.contains(object)
                                    && !self.dense_arrays.contains_key(object))
                                    || self.tainted_objects.contains(object)
                                    || !self.prefix_object_properties.get(object).is_some_and(
                                        |properties| properties.contains_key(&property),
                                    )
                            }
                            _ => true,
                        })
                    {
                        self.array_iteration_unsupported = true;
                    }
                }
            }
        }
        for (id, value) in &callable.local_assignments {
            let values = self.evaluate(value, &env, &env);
            env.entry(id.clone()).or_default().extend(values);
        }
        let mut outcome = Outcome {
            complete: true,
            ..Outcome::default()
        };
        outcome.executed_owners.insert(callable.owner);
        if callable.generator {
            // Generator parameter initializers run at call time, although
            // the body waits for the first iteration. Their effects are not
            // separated in the index, so retain the conservative path.
            outcome.unsupported(
                ExecutionOperation::GeneratorParameters,
                callable.owner,
                DUMMY_SP,
            );
            self.active.remove(&active_invocation);
            self.invocation_depth -= 1;
            self.cache.insert(signature, outcome.clone());
            return outcome;
        }
        if callable.opaque {
            outcome.unsupported(
                ExecutionOperation::OpaqueExecution,
                callable.owner,
                DUMMY_SP,
            );
        }
        for id in &callable.reads {
            if self.top_bindings.contains(id) {
                outcome.effects.reads.insert((id.clone(), callable.owner));
                if !self.initialized_bindings.contains(id) {
                    outcome.effects.uninitialized_reads.insert(id.clone());
                }
            }
        }
        for id in &callable.rebinds {
            if self.top_bindings.contains(id) {
                outcome.effects.rebinds.insert((id.clone(), callable.owner));
            }
        }
        // The visitor stores value-bearing member writes separately from call
        // nodes. Make same-owner callback publication visible before nested
        // opaque calls; if source order put a write afterward, this only adds
        // a conservative callback possibility.
        let mut certified_data_receivers = self.fresh_primitive_write_receivers(callable, &env);
        let fresh_record_receiver = self.fresh_local_record_receiver(key, callable);
        if let Some(object) = fresh_record_receiver {
            certified_data_receivers.insert(object);
        }
        self.apply_member_value_writes(
            &callable.member_value_writes,
            &env,
            &env,
            &certified_data_receivers,
            false,
        );
        // local_constructor checked every assignment as a static property
        // write to a fresh `this`, with a safe right-hand value and no
        // prototype getter collision. That receiver cannot publish a stored
        // callback or invoke a setter. The generic lanes represent `this` as
        // unknown and would otherwise promote lazy callback bodies after any
        // earlier opaque call.
        if !fresh_checked_constructor {
            self.promote_callback_result_writes(
                &callable.callback_result_writes,
                &env,
                &env,
                callable.owner,
                &mut outcome,
                &certified_data_receivers,
            );
        }
        for call in &callable.calls {
            self.execute_call(call, &env, &env, &mut outcome);
        }
        if !certified_data_receivers.is_empty()
            && (self.array_iteration_unsupported
                || !self.array_intrinsics_intact()
                || certified_data_receivers
                    .iter()
                    .any(|object| self.tainted_objects.contains(object)))
        {
            self.tainted_objects
                .extend(certified_data_receivers.iter().copied());
            certified_data_receivers.clear();
        }
        for expr in &callable.returns {
            outcome.returns.extend(self.evaluate(expr, &env, &env));
        }
        if callable.may_fall_through {
            outcome.returns.insert(AbstractValue::Undefined);
        }

        let mut writes: Vec<_> = callable
            .member_writes
            .iter()
            .filter(|(receiver, _)| !self.stable_fresh_function_target(key, callable, receiver))
            .map(|(receiver, property)| (Some(receiver.clone()), Some(property.clone())))
            .collect();
        if callable.dynamic_property_effect {
            writes.push((None, None));
        }
        if !fresh_checked_constructor
            && !self.fresh_mutation_callables.contains(&key)
            && certified_data_receivers.is_empty()
        {
            self.promote_property_writes(&writes, callable.owner, &mut outcome);
        }
        if unsupported_inputs
            || (self.array_iteration_depth > 0
                && self.array_iteration_unsupported
                && !unsupported_before)
        {
            let fact = &self.facts[callable.owner.0];
            self.promote_lazy_effects(
                &fact.reads.lazy,
                &fact.rebinds.lazy,
                self.current_top_owner.unwrap_or(callable.owner),
                &mut outcome,
            );
            outcome.unsupported(
                if unsupported_inputs {
                    ExecutionOperation::CallableInputs
                } else {
                    ExecutionOperation::ArrayIteration
                },
                callable.owner,
                DUMMY_SP,
            );
        }
        self.active.remove(&active_invocation);
        self.invocation_depth -= 1;
        self.cache.insert(signature, outcome.clone());
        outcome
    }

    fn execute_call(
        &mut self,
        call: &Call,
        env: &Context,
        captures: &Context,
        outcome: &mut Outcome,
    ) {
        if call.kind == CallKind::Call
            && self
                .frozen_namespace_calls
                .contains(&CallSite::from(call.span))
        {
            return;
        }
        if matches!(
            call.kind,
            CallKind::InProtocol
                | CallKind::InstanceOfProtocol
                | CallKind::DeepDestructuringProtocol
        ) {
            let mut operands: Vec<Values> = call
                .args
                .iter()
                .map(|argument| self.evaluate(argument, env, captures))
                .collect();
            if call.kind == CallKind::InProtocol
                && self
                    .index
                    .non_reentrant_global_membership
                    .contains(&call.span.into())
            {
                // Operand effects were indexed/evaluated normally. The separate
                // author contract certifies only this exact host protocol's
                // lack of callback reentry; purity and throw checks stay intact.
                return;
            }
            if call.kind == CallKind::InProtocol
                && self.ordinary_prototype_chain(&operands[0])
                && operands[0]
                    .iter()
                    .all(|value| matches!(value, AbstractValue::Origin(ValueOrigin::Object(_))))
            {
                return;
            }
            if call.kind == CallKind::InstanceOfProtocol
                && self.default_class_has_instance(&operands[0])
            {
                // OrdinaryHasInstance consults the LHS prototype chain, never
                // the constructor's deferred method bodies.
                if self.ordinary_prototype_chain(&operands[1]) {
                    return;
                }
                operands.remove(0);
            }
            let values: Values = operands.into_iter().flatten().collect();
            self.expose_class_capabilities(&values);
            self.taint_objects(&values);
            self.promote_raw_callbacks(&values, outcome);
            self.invoke_possible_callbacks(&[values], false, outcome);
            self.note_unknown_call(call);
            outcome.unsupported(
                match call.kind {
                    CallKind::InProtocol => ExecutionOperation::MembershipProtocol,
                    CallKind::InstanceOfProtocol => ExecutionOperation::InstanceOfProtocol,
                    _ => ExecutionOperation::DestructuringProtocol,
                },
                call.owner,
                call.span,
            );
            outcome.unknown_call = true;
            return;
        }
        if call.kind == CallKind::LocalMemberRead {
            if !self.private_constructor_result_owners.is_empty()
                && self.closed_enum_member_read(&call.callee)
            {
                return;
            }
            let ValueExpr::Member(receiver, property, _) = &call.callee else {
                unreachable!()
            };
            let values = self.evaluate(receiver, env, captures);
            if values.contains(&AbstractValue::ForeignDeferredRecord) {
                self.promote_foreign_record(outcome);
                self.note_unknown_call(call);
                self.invoke_possible_callbacks(&[values], false, outcome);
                return;
            }
            let unsupported_local = values.iter().any(|value| match value {
                AbstractValue::Origin(ValueOrigin::Object(object)) => {
                    self.tainted_objects.contains(object)
                        || ((!self.index.plain_data_objects.contains(object)
                            && !self.ordinary_spread_copies.contains(object)
                            && !self.dense_arrays.contains_key(object))
                            || !self.prefix_object_properties.get(object).is_some_and(
                                |properties| {
                                    property.as_ref().is_some_and(|property| {
                                        properties.contains_key(&Some(property.clone()))
                                            || self.absent_plain_data_property(*object, property)
                                    })
                                },
                            ))
                }
                AbstractValue::Instance(key, _) => {
                    self.tainted_classes.contains(key)
                        || !property.as_ref().is_some_and(|property| {
                            self.index.class_values.get(key).is_some_and(|class| {
                                class.ordinary_instance_methods.contains(property)
                                    || class
                                        .instance_fields
                                        .get(property)
                                        .is_some_and(|value| !matches!(value, ValueExpr::Unknown))
                            }) || self.index.local_constructors.get(key).is_some_and(
                                |constructor| constructor.own_fields.contains(property),
                            )
                        })
                }
                _ => false,
            });
            if unsupported_local {
                self.taint_objects(&values);
                let selected = self.evaluate(&call.callee, env, captures);
                self.promote_raw_callbacks(&selected, outcome);
                if self.array_iteration_depth > 0 {
                    self.array_iteration_unsupported = true;
                }
                outcome.unsupported(ExecutionOperation::MemberRead, call.owner, call.span);
            }
            return;
        }
        if call.kind == CallKind::ObjectSpread {
            let values = self.evaluate(&call.args[0], env, captures);
            if self.ordinary_data_properties(&values).is_some() {
                return;
            }
            if self.array_iteration_depth > 0 {
                self.array_iteration_unsupported = true;
            }
            outcome.unsupported(ExecutionOperation::ObjectSpread, call.owner, call.span);
            outcome.unknown_call = true;
            self.note_unknown_call(call);
            self.promote_raw_callbacks(&values, outcome);
            self.invoke_possible_callbacks(&[values], false, outcome);
            return;
        }
        if call.kind == CallKind::ArraySpread {
            let values = self.evaluate(&call.args[0], env, captures);
            if self.dense_source(&values).is_some() {
                return;
            }
            outcome.unsupported(ExecutionOperation::ArraySpread, call.owner, call.span);
            outcome.unknown_call = true;
            self.note_unknown_call(call);
            // Consuming a custom iterator can execute deferred generator bodies.
            self.promote_raw_callbacks(&values, outcome);
            self.invoke_possible_callbacks(&[values], false, outcome);
            return;
        }
        if call.kind == CallKind::ImplicitCoercion {
            // The binding read and its TDZ/import ordering facts remain in the
            // statement. This proof removes only a possible protocol callback.
            if matches!(call.args.first(), Some(ValueExpr::Binding(id))
                if self.index.imported_bindings.contains(id)
                    && self.imported_primitives.contains(id.0.as_ref()))
            {
                return;
            }
            let values = call
                .args
                .first()
                .map(|argument| self.evaluate(argument, env, captures))
                .unwrap_or_else(unknown);
            if values.iter().all(|value| {
                matches!(
                    value,
                    AbstractValue::NonCallable
                        | AbstractValue::Undefined
                        | AbstractValue::Unavailable
                )
            }) {
                return;
            }
            let mut possible_values = values;
            possible_values.remove(&AbstractValue::Unavailable);
            if possible_values.is_empty() {
                return;
            }
            // ToPrimitive may call an own [Symbol.toPrimitive], valueOf, or
            // toString method. Treat that protocol step like an opaque
            // synchronous call and expose callbacks carried by the operand;
            // this covers both the exotic and ordinary conversion paths
            // without assuming which method succeeds first.
            outcome.unsupported(ExecutionOperation::ImplicitCoercion, call.owner, call.span);
            outcome.unknown_call = true;
            self.note_unknown_call(call);
            // A conversion property may be an accessor that returns a
            // conversion function. Promote the operand's retained callback
            // closure as well as invoking its directly visible callbacks so
            // effects in such returned functions remain visible.
            self.promote_deferred_callbacks(&possible_values, outcome);
            self.invoke_possible_callbacks(&[possible_values], false, outcome);
            return;
        }
        // Only a fresh primitive literal or a checked const alias to one is
        // safe to omit from callback effects. A pure object/function element
        // can be retained by the Set and invoked by a later opaque consumer.
        if call.kind == CallKind::New
            && call.caller.is_none()
            && self.set_iterable_proof.contains_constructor(call.span)
            && self.index.set_iterable_intrinsics_are_unmodified()
        {
            return;
        }
        // Empty intrinsic collections return before consulting an adder or
        // iterator. This proves only their construction has no callbacks;
        // later operations and any constructor arguments remain conservative.
        if call.kind == CallKind::New
            && call.args.is_empty()
            && matches!(&call.callee, ValueExpr::Binding(id)
                if self.index.empty_collection_constructor_is_unmodified(id, self.flow, self.shadowed))
        {
            return;
        }
        let targets = self.evaluate(&call.callee, env, captures);
        if targets.len() == 1 && targets.contains(&AbstractValue::Unavailable) {
            return;
        }
        let mut args: Vec<Values> = call
            .args
            .iter()
            .map(|arg| self.evaluate(arg, env, captures))
            .collect();
        if args
            .iter()
            .any(|values| values.len() == 1 && values.contains(&AbstractValue::Unavailable))
        {
            return;
        }
        for values in &mut args {
            values.remove(&AbstractValue::Unavailable);
        }
        if call.kind == CallKind::Call
            && !call.has_spread
            && self.source_record_factory(&call.callee, &call.args)
        {
            return;
        }
        if !call.has_spread
            && self
                .resolve_native_call(call.kind, &call.callee, &call.args, &args)
                .is_some()
        {
            return;
        }
        if self.host_call_does_not_reenter(call) {
            return;
        }
        if call.kind == CallKind::Call
            && !call.has_spread
            && let Some(callbacks) = self.no_reentry_callbacks(call.span.into(), &call.args, &args)
        {
            // Argument evaluation has completed. The provider cannot reenter
            // during this call, but may retain callbacks anywhere, including a
            // private cross-call registry. Every subsequent opaque operation
            // must therefore see them. Do not give the result a known shape.
            let before = self.global_escaped_callbacks.len();
            self.global_escaped_callbacks.extend(callbacks);
            if self.global_escaped_callbacks.len() != before {
                self.prefix_revision = self.prefix_revision.wrapping_add(1);
            }
            return;
        }
        if call.kind == CallKind::Call
            && !call.has_spread
            && self
                .retained_factory_callbacks(call.span.into(), &call.args, &args)
                .is_some()
        {
            return;
        }
        if call.kind == CallKind::New
            && !call.has_spread
            && self
                .retained_constructor_callbacks(call.span.into(), &call.args, &args)
                .is_some()
        {
            return;
        }
        // The author assertion covers this direct imported call with checked
        // primitive arguments. Its result remains Unknown; only opaque
        // callback promotion is suppressed.
        if call.kind == CallKind::Call
            && call.no_reentry_candidate
            && let ValueExpr::Binding(binding) = &call.callee
            && self.index.no_reentry_exports.contains(binding)
        {
            return;
        }
        // Distinct author assertions cover primitive and fresh-record inputs.
        // Both use the same member identity and lookup guards. The provider
        // may have private effects; this only suppresses callback reentry.
        if call.kind == CallKind::Call
            && let ValueExpr::Member(receiver, Some(property), true) = &call.callee
            && let ValueExpr::Binding(namespace) = receiver.as_ref()
            && ((call.no_reentry_candidate
                && self
                    .index
                    .no_reentry_members
                    .get(namespace)
                    .is_some_and(|members| members.contains(property)))
                || (call.no_reentry_record_candidate
                    && self
                        .index
                        .no_reentry_record_members
                        .get(namespace)
                        .is_some_and(|members| members.contains(property))))
        {
            return;
        }
        let from_entries_source_callbacks = if call.kind == CallKind::Call {
            if self
                .proven_from_entries_callbacks(call.span.into(), env, captures)
                .is_some()
            {
                return;
            }
            self.callbacks_in_from_entries_source(call.span.into(), env, captures)
        } else {
            None
        };
        if call.kind == CallKind::New
            && self.is_proven_map_entry_array(&call.callee, &call.args, env, captures, call.owner)
        {
            return;
        }
        if call.kind == CallKind::Call
            && self.execute_array_iteration(call.span.into(), env, captures, outcome)
        {
            return;
        }
        // Known local methods still receive an implicit `this` capability.
        // Its effects are not resolved by invoke, so it can mutate any retained
        // array reachable from the receiver before a later intrinsic iteration.
        if call.kind == CallKind::Call
            && let ValueExpr::Member(receiver, _, _) = &call.callee
        {
            let values = self.evaluate(receiver, env, captures);
            let contains_array = self.contains_dense_array(&values);
            self.taint_objects(&values);
            if contains_array {
                self.promote_raw_callbacks(&targets, outcome);
            }
        }
        let mut opaque_args = args.clone();
        if let Some(proof) = self.index.array_iterations.get(&call.span.into()) {
            let source = self.evaluate(&proof.source.clone(), env, captures);
            self.promote_raw_callbacks(&source, outcome);
            opaque_args.push(source);
        }

        if let Some(callbacks) = from_entries_source_callbacks {
            opaque_args.push(callbacks);
        }
        if targets.is_empty()
            || targets.iter().any(|target| {
                matches!(
                    target,
                    AbstractValue::Unknown
                        | AbstractValue::DeferredCallbacks(_)
                        | AbstractValue::ClassMembers(_)
                        | AbstractValue::Instance(_, _)
                )
            })
        {
            // Opaque calls can mutate objects passed as arguments or used as
            // method receivers. Preserve that possibility for later narrow
            // construction proofs (including changes to an entry's own data
            // properties or an array's iterator/species surface).
            let mut possible_objects: Values = args.iter().flatten().cloned().collect();
            if let ValueExpr::Member(receiver, _, _) = &call.callee {
                possible_objects.extend(self.evaluate(receiver, env, captures));
            }
            self.expose_class_capabilities(&possible_objects);
            self.taint_objects(&possible_objects);
        }
        let retained_callback = matches!(args.as_slice(), [values] if !values.is_empty()
            && values.iter().all(|value| matches!(value, AbstractValue::DeferredCallbacks(_))));
        if targets.is_empty() {
            outcome.lose_precision(
                ExecutionPrecisionLossReason::UnknownCallTarget,
                call.owner,
                call.span,
            );
            outcome.unknown_call = true;
            self.note_unknown_call(call);
            self.invoke_possible_callbacks(&opaque_args, call.no_sync_arguments, outcome);
        }
        let mut invoked_unknown_callbacks = targets.is_empty();
        for target in targets {
            match target {
                AbstractValue::CallbackFactory
                    if call.kind == CallKind::Call
                        && !call.has_spread
                        && (retained_callback
                            || matches!(
                                call.args.as_slice(),
                                [ValueExpr::Origin(ValueOrigin::Function(_))]
                            )) =>
                {
                    if let ValueExpr::Binding(imported_local) = &call.callee {
                        self.record_callback_factory_use(
                            imported_local,
                            CallbackFactoryUseKind::CallTiming,
                            call.owner,
                            call.span,
                        );
                    }
                }
                AbstractValue::InlineCallbackFactory
                    if call.kind == CallKind::Call
                        && !call.has_spread
                        && matches!(
                            call.args.as_slice(),
                            [ValueExpr::Origin(ValueOrigin::Function(_))]
                        ) => {}
                AbstractValue::ForeignDeferredRecord => {
                    self.promote_foreign_record(outcome);
                    self.note_unknown_call(call);
                    self.invoke_possible_callbacks(&opaque_args, false, outcome);
                }
                AbstractValue::DeferredCallbacks(captured) => {
                    outcome.lose_precision(
                        ExecutionPrecisionLossReason::UnknownCallTarget,
                        call.owner,
                        call.span,
                    );
                    outcome.unknown_call = true;
                    self.note_unknown_call(call);
                    let mut possible_args = opaque_args.clone();
                    possible_args.push((*captured.values).clone());
                    self.promote_deferred_callbacks(&captured.values, outcome);
                    self.invoke_possible_callbacks(&possible_args, false, outcome);
                }
                AbstractValue::ClassMembers(key) => {
                    outcome.lose_precision(
                        ExecutionPrecisionLossReason::UnknownCallTarget,
                        call.owner,
                        call.span,
                    );
                    outcome.unknown_call = true;
                    self.note_unknown_call(call);
                    self.promote_class_members(key, outcome);
                    self.invoke_possible_callbacks(&opaque_args, false, outcome);
                }
                AbstractValue::Instance(key, captured) => {
                    outcome.lose_precision(
                        ExecutionPrecisionLossReason::UnknownCallTarget,
                        call.owner,
                        call.span,
                    );
                    outcome.unknown_call = true;
                    self.note_unknown_call(call);
                    self.promote_class_members(key, outcome);
                    let mut possible_args = opaque_args.clone();
                    possible_args.push((*captured.values).clone());
                    self.invoke_possible_callbacks(&possible_args, false, outcome);
                }
                AbstractValue::Unavailable
                | AbstractValue::Undefined
                | AbstractValue::NonCallable => {}
                AbstractValue::ObjectAssign
                    if call.kind == CallKind::Call
                        && call.plain_object_sources
                        && matches!(call.args.first(), Some(ValueExpr::Binding(id)) if call.caller
                        .and_then(|caller| self.index.callables.get(&caller).map(|body| (caller, body)))
                        .is_some_and(|(caller, body)| self.stable_fresh_function_target(caller, body, id))) =>
                    {}
                AbstractValue::ObjectAssign if call.kind == CallKind::Call => {
                    // Once the target's descriptor/prototype integrity is
                    // unproven, copying a property may synchronously call a
                    // setter. Include nested lazy bodies of owners that ran
                    // before this call, as well as the owners of the source
                    // and target values. The first-order fallback excludes
                    // those bodies because merely creating a closure does
                    // not execute it.
                    self.note_unknown_call(call);
                    let possible_objects = args.iter().flatten().cloned().collect();
                    self.taint_objects(&possible_objects);
                    let mut possible_accessors: BTreeSet<_> = self
                        .reachability
                        .owners
                        .iter()
                        .copied()
                        .filter(|candidate| self.may_be_available_accessor_owner(*candidate))
                        .collect();
                    possible_accessors.extend(outcome.executed_owners.iter().copied());
                    if self.reachability.incomplete {
                        possible_accessors.extend(self.all_owners());
                    }
                    let effect_owner = self.current_top_owner.unwrap_or(call.owner);
                    for accessor_owner in possible_accessors {
                        self.promote_lazy_owner(accessor_owner, effect_owner, outcome);
                    }
                    outcome.unsupported(ExecutionOperation::ObjectAssign, call.owner, call.span);
                    outcome.unknown_call = true;
                }
                AbstractValue::Origin(ValueOrigin::Class(key)) if call.kind == CallKind::New => {
                    if let Some((nested, _)) = (!call.has_spread)
                        .then(|| self.construct_local_class(key, &args))
                        .flatten()
                    {
                        self.apply_rebind_effects(&nested.effects.rebinds);
                        outcome.effects.reads.extend(nested.effects.reads);
                        outcome.effects.rebinds.extend(nested.effects.rebinds);
                        outcome
                            .effects
                            .uninitialized_reads
                            .extend(nested.effects.uninitialized_reads);
                        outcome.executed_owners.extend(nested.executed_owners);
                        outcome.unknown_call |= nested.unknown_call;
                        outcome
                            .inherit_completeness(nested.complete, nested.effects.precision_losses);
                    } else {
                        outcome.unsupported(
                            ExecutionOperation::Construction,
                            call.owner,
                            call.span,
                        );
                        outcome.unknown_call = true;
                        self.note_unknown_call(call);
                        self.escaped_class_capabilities.insert(key);
                        // Unsupported constructors still execute their
                        // instance fields and body at `new`, even without a
                        // callback argument to seed the opaque fallback.
                        self.promote_class_members(key, outcome);
                        if !invoked_unknown_callbacks {
                            self.invoke_possible_callbacks(&args, false, outcome);
                            invoked_unknown_callbacks = true;
                        }
                    }
                }
                AbstractValue::Origin(ValueOrigin::Function(key))
                    if call.kind == CallKind::Call =>
                {
                    let nested = self.invoke(key, args.clone(), None, false);
                    self.apply_rebind_effects(&nested.effects.rebinds);
                    outcome.effects.reads.extend(nested.effects.reads);
                    outcome.effects.rebinds.extend(nested.effects.rebinds);
                    outcome
                        .effects
                        .uninitialized_reads
                        .extend(nested.effects.uninitialized_reads);
                    outcome.executed_owners.extend(nested.executed_owners);
                    outcome.unknown_call |= nested.unknown_call;
                    outcome.inherit_completeness(nested.complete, nested.effects.precision_losses);
                }
                AbstractValue::Closure(key, captured) if call.kind == CallKind::Call => {
                    let nested = self.invoke(key, args.clone(), Some(captured), false);
                    self.apply_rebind_effects(&nested.effects.rebinds);
                    outcome.effects.reads.extend(nested.effects.reads);
                    outcome.effects.rebinds.extend(nested.effects.rebinds);
                    outcome
                        .effects
                        .uninitialized_reads
                        .extend(nested.effects.uninitialized_reads);
                    outcome.executed_owners.extend(nested.executed_owners);
                    outcome.unknown_call |= nested.unknown_call;
                    outcome.inherit_completeness(nested.complete, nested.effects.precision_losses);
                }
                _ => {
                    outcome.lose_precision(
                        ExecutionPrecisionLossReason::UnknownCallTarget,
                        call.owner,
                        call.span,
                    );
                    outcome.unknown_call = true;
                    self.note_unknown_call(call);
                    if !invoked_unknown_callbacks {
                        self.invoke_possible_callbacks(
                            &opaque_args,
                            call.no_sync_arguments,
                            outcome,
                        );
                        invoked_unknown_callbacks = true;
                    }
                }
            }
        }
    }
}

fn callback_source_units_for_signature(
    callback_argument_exprs: &BTreeMap<usize, ValueExpr>,
    signature: &spec::RetainedCallbackSignature,
    flow: &ChunkValueFlow,
) -> Option<BTreeSet<SourceUnitOrdinal>> {
    let mut source_units = BTreeSet::new();
    for position in &signature.callback_arguments {
        let expression = callback_argument_exprs.get(position)?;
        let values: BTreeSet<_> = match expression {
            ValueExpr::Binding(binding) => flow.values_of_binding(binding).into_iter().collect(),
            ValueExpr::Origin(origin) => BTreeSet::from([*origin]),
            // Calls, object members and aggregate expressions can return or
            // expose callbacks through paths this local witness does not link.
            _ => return None,
        };
        if values.len() != 1 {
            return None;
        }
        for value in values {
            if !matches!(value, ValueOrigin::Function(_)) {
                return None;
            }
            source_units.insert(*flow.callable_source_unit.get(&value)?);
        }
    }
    Some(source_units)
}

/// Prove the narrow return-only wrapper shape using the same execution index
/// as call promotion. Whole-chunk flow must contain exactly that function;
/// aliases with unknown/reassigned targets and dynamic scopes remain opaque.
pub(crate) struct SynchronousCallProofs<'a> {
    pub(crate) set_iterable: &'a SetIterableProof,
    pub(crate) graph: &'a ChunkCodeGraph,
    pub(crate) index: PreparedSynchronousCallIndex,
}

pub(crate) struct PreparedSynchronousCallIndex {
    index: ExecutionIndex,
}

impl PreparedSynchronousCallIndex {
    pub(crate) fn set_iterable_intrinsics_are_unmodified(&self) -> bool {
        self.index.set_iterable_intrinsics_are_unmodified()
    }
}

pub(crate) fn prepare_synchronous_call_index(
    body: &[TopLevelItemView<'_>],
    flow: &ChunkValueFlow,
    hints: &AnalysisInputs,
    plain_array_bindings: &BTreeSet<String>,
    local_class_construction: &LocalClassConstructionProof,
) -> PreparedSynchronousCallIndex {
    PreparedSynchronousCallIndex {
        index: ExecutionIndex::collect(
            body,
            hints,
            Some(flow),
            plain_array_bindings,
            local_class_construction,
        ),
    }
}

/// Finalize enum own-key candidates with the already collected binding-write
/// facts. The recognized initializer may contain its own `X || (X = {})`
pub(crate) fn annotate_synchronous_calls(
    body: &[TopLevelItemView<'_>],
    flow: &ChunkValueFlow,
    proofs: SynchronousCallProofs<'_>,
    shadowed: &BTreeSet<&'static str>,
    hints: &AnalysisInputs,
    export_reentry_policy: ExportReentryPolicy,
    facts: &mut [StatementFacts],
) {
    let index = proofs.index.index;
    for candidate in index.retained_callback_calls.values() {
        if candidate.caller.is_some() {
            continue;
        }
        let Some((namespace, member)) = &candidate.call.namespace_member else {
            continue;
        };
        if index
            .audited_factory_members
            .get(namespace)
            .is_some_and(|members| members.contains(member))
        {
            continue;
        }
        let witness = index
            .rejected_namespace_member_writes
            .get(&(namespace.clone(), member.clone()))
            .cloned();
        let dynamic_write_witness = index
            .rejected_namespace_dynamic_property_writes
            .get(&(namespace.clone(), member.clone()))
            .cloned();
        let call_argument_witness = index
            .rejected_namespace_call_arguments
            .get(namespace)
            .cloned();
        for signature in candidate.call.signatures.iter().filter(|signature| {
            // `retained_callback_invocation` already applies these guards to
            // the parsed call. Keep them explicit here so a configured but
            // inapplicable overload can never be reported as the match.
            signature.argument_count == candidate.call.argument_count
                && signature.callback_arguments.iter().all(|position| {
                    *position < candidate.call.argument_count
                        && candidate
                            .call
                            .callback_argument_exprs
                            .contains_key(position)
                })
        }) {
            let callback_source_units = callback_source_units_for_signature(
                &candidate.call.callback_argument_exprs,
                signature,
                flow,
            );
            facts[candidate.owner.0]
                .callback_factory_barriers
                .push(CallbackFactoryBarrierFact {
                    namespace_binding: namespace.0.to_string(),
                    member: member.clone(),
                    argument_count: signature.argument_count,
                    callback_arguments: signature.callback_arguments.clone(),
                    callback_source_units,
                    rejection: match &witness {
                        Some(witness) => CallbackFactoryBarrierRejection::NamespaceMemberWrite {
                            witness: witness.clone(),
                        },
                        None => match &dynamic_write_witness {
                            Some(witness) => {
                                CallbackFactoryBarrierRejection::NamespaceDynamicPropertyWrite {
                                    witness: witness.clone(),
                                }
                            }
                            None => match &call_argument_witness {
                                Some(witness) => {
                                    CallbackFactoryBarrierRejection::NamespaceCallArgument {
                                        witness: witness.clone(),
                                    }
                                }
                                None => CallbackFactoryBarrierRejection::UnsupportedGuard,
                            },
                        },
                    },
                });
        }
    }
    // Every top-level call is executed by `top_calls`, regardless of whether
    // its result is retained. Keep an initializer's value only if a later
    // call may read it, directly or through another initializer/function.
    // This avoids re-evaluating thousands of unused factory results.
    let mut needed: BTreeSet<Id> = index
        .callables
        .values()
        .flat_map(|callable| callable.reads.iter().cloned())
        .collect();
    for calls in &index.top_calls {
        for call in calls {
            call.callee.bindings(&mut needed);
            for arg in &call.args {
                arg.bindings(&mut needed);
            }
        }
    }
    // Property-write analysis also needs the receiver's prefix identity, even
    // when only another alias is passed to a later call. In particular, a
    // callable's custom fields/prototype are outside the object-record model.
    for write in index.top_callback_result_writes.iter().flatten() {
        write.receiver.bindings(&mut needed);
        write.value.bindings(&mut needed);
    }
    // Retain prefix values for direct heritage reachability and captured bases.
    for key in index.class_bindings.keys() {
        if let Some(super_class) = index
            .class_values
            .get(key)
            .and_then(|class| class.super_class.as_ref())
        {
            super_class.bindings(&mut needed);
        }
    }
    needed.extend(index.exported_bindings.iter().cloned());
    for exported in &index.top_exported_values {
        for expression in exported {
            expression.bindings(&mut needed);
        }
    }
    let mut dependencies: BTreeMap<Id, BTreeSet<Id>> = BTreeMap::new();
    for assignments in &index.top_assignments {
        for (id, expression) in assignments {
            expression.bindings(dependencies.entry(id.clone()).or_default());
        }
    }
    let mut member_write_receivers = BTreeMap::<ValueOrigin, BTreeSet<Id>>::new();
    for writes in &index.top_member_value_writes {
        for (receiver, _, expression) in writes {
            if index.global_object_names.contains(receiver.0.as_ref()) {
                // Global-object writes are potential callback escape roots at
                // every later opaque call, even when the receiver alias is
                // not mentioned by a statically visible callee.
                expression.bindings(&mut needed);
            } else {
                expression.bindings(dependencies.entry(receiver.clone()).or_default());
                // A later call may read an object through a different local
                // alias than the one used for this property write. Retain
                // that receiver's prefix value whenever its known object
                // identity also reaches a needed binding.
                for origin in flow.values_of_binding(receiver) {
                    if matches!(origin, ValueOrigin::Object(_)) {
                        member_write_receivers
                            .entry(origin)
                            .or_default()
                            .insert(receiver.clone());
                    }
                }
            }
        }
    }
    let mut pending: Vec<Id> = needed.iter().cloned().collect();
    let mut visited_objects = BTreeSet::new();
    while let Some(id) = pending.pop() {
        if let Some(inputs) = dependencies.get(&id) {
            for input in inputs {
                if needed.insert(input.clone()) {
                    pending.push(input.clone());
                }
            }
        }
        for origin in flow.values_of_binding(&id) {
            if !matches!(origin, ValueOrigin::Object(_)) || !visited_objects.insert(origin) {
                continue;
            }
            if let Some(receivers) = member_write_receivers.get(&origin) {
                for receiver in receivers {
                    if needed.insert(receiver.clone()) {
                        pending.push(receiver.clone());
                    }
                }
            }
        }
    }
    let source_unit_read_closure = SourceUnitReadClosure::new(facts);
    let prefix_reads: Vec<_> = facts
        .iter()
        .map(|fact| {
            index.owner_callable_prefix_reads[fact.ordinal.0]
                .as_ref()
                .filter(|reads| {
                    !index.dynamic_scope_owners.contains(&fact.ordinal)
                        && !index.unindexed_accessor_owners.contains(&fact.ordinal)
                        && fact.reads.first_order_lazy.is_subset(reads)
                })
        })
        .collect();
    let synchronous_owner_adjacency =
        source_unit_read_closure.synchronous_adjacency(facts, &prefix_reads);
    let mut exported_owners = BTreeSet::new();
    if export_reentry_policy == ExportReentryPolicy::Conservative {
        exported_owners.extend(index.exported_declaration_owners.iter().copied());
        exported_owners
            .extend(source_unit_read_closure.source_units_for_bindings(&index.exported_bindings));
    }
    let initial_export_roots = exported_owners
        .intersection(&index.hoisted_function_owners)
        .copied()
        .collect();
    let top_bindings: BTreeSet<_> = facts
        .iter()
        .flat_map(|fact| fact.declared.iter().cloned())
        .collect();
    let var_bindings: BTreeSet<_> = facts
        .iter()
        .flat_map(|fact| fact.var_declared.iter().cloned())
        .collect();
    let changed_member_roots: BTreeSet<_> = if index.top_metadata_decorations.iter().any(|call| {
        call.as_ref()
            .is_some_and(|call| !call.member_decorators.is_empty())
    }) {
        facts
            .iter()
            .flat_map(|fact| {
                fact.member_write_roots
                    .iter()
                    .chain(&fact.rebinds.eager)
                    .chain(&fact.rebinds.lazy)
                    .cloned()
            })
            .collect()
    } else {
        BTreeSet::new()
    };
    let mut initial_values = Context::new();
    let mut initialized_bindings = BTreeSet::new();
    for (binding, key) in &index.hoisted_bindings {
        initial_values.insert(
            binding.clone(),
            BTreeSet::from([AbstractValue::Origin(ValueOrigin::Function(*key))]),
        );
        initialized_bindings.insert(binding.clone());
    }
    let enum_data_members = super::stable_ts_enum_named_data_members(proofs.graph, facts);
    let mut executor = Executor {
        index: &index,
        graph: proofs.graph,
        facts,
        flow,
        set_iterable_proof: proofs.set_iterable,
        shadowed,
        imported_primitives: &hints.imported_primitives,
        enum_data_members,
        top_bindings,
        binding_owner: facts
            .iter()
            .flat_map(|fact| fact.declared.iter().cloned().map(|id| (id, fact.ordinal)))
            .collect(),
        source_unit_read_closure,
        synchronous_owner_adjacency,
        synchronous_owner_reads: prefix_reads,
        class_member_effects: BTreeMap::new(),
        class_superclasses: BTreeMap::new(),
        active_class_member_promotions: BTreeSet::new(),
        exported_owners,
        prior_reachability_roots: Some(initial_export_roots),
        current_top_owner: None,
        call_site_owners: index
            .top_calls
            .iter()
            .flatten()
            .chain(
                index
                    .callables
                    .values()
                    .flat_map(|callable| callable.calls.iter()),
            )
            .map(|call| (CallSite::from(call.span), (call.owner, call.span)))
            .collect(),
        proof_uses: Vec::new(),
        private_constructor_result_owners: BTreeSet::new(),
        prior_effect_owners: BTreeSet::new(),
        accepted_deferred_global_writes: BTreeSet::new(),
        frozen_namespace_calls: BTreeSet::new(),
        reachability: ReachabilityState::default(),
        mutable_captures: index
            .callables
            .values()
            .flat_map(|callable| callable.rebinds.iter().cloned())
            .collect(),
        prefix_values: initial_values,
        initialized_bindings,
        var_bindings,
        changed_member_roots,
        global_escaped_callbacks: Values::new(),
        prefix_object_properties: BTreeMap::new(),
        dense_arrays: BTreeMap::new(),
        ordinary_spread_copies: BTreeSet::new(),
        array_iteration_depth: 0,
        array_iteration_protected_objects: BTreeSet::new(),
        array_iteration_unsupported: false,
        array_intrinsic_mutated: false,
        tainted_objects: BTreeSet::new(),
        tainted_functions: BTreeSet::new(),
        tainted_classes: BTreeSet::new(),
        escaped_class_capabilities: BTreeSet::new(),
        exported_runtime_values: index
            .exported_hoisted_functions
            .iter()
            .copied()
            .map(|key| AbstractValue::Origin(ValueOrigin::Function(key)))
            .collect(),
        export_reentry_enabled: export_reentry_policy == ExportReentryPolicy::Conservative,
        fresh_mutation_callables: fresh_mutation_callables(
            body,
            facts,
            !index.dynamic_scope_owners.is_empty(),
        ),
        prefix_revision: 0,
        invocation_depth: 0,
        invocation_count: 0,
        callback_fallback: CallbackFallbackSummary::default(),
        fallback_outcome: Outcome::default(),
        fallback_callback_sizes: None,
        fallback_local_owners: BTreeSet::new(),
        returned_callback_owners: BTreeSet::new(),
        capture_contexts: BTreeMap::new(),
        callback_sets: BTreeMap::new(),
        active: BTreeSet::new(),
        cache: BTreeMap::new(),
    };
    let mut top_env = Context::new();
    let mut top_captures = Context::new();
    let mut effects = Vec::with_capacity(facts.len());
    for (fact, (calls, assignments)) in facts
        .iter()
        .zip(index.top_calls.iter().zip(&index.top_assignments))
    {
        executor.current_top_owner = Some(fact.ordinal);
        executor.proof_uses.clear();
        // Cache keys include this owner; previous statement snapshots cannot
        // be reused and should not consume this statement's bounded budget.
        executor.cache.clear();
        executor.invocation_count = 0;
        executor.fallback_outcome = Outcome::default();
        executor.fallback_callback_sizes = None;
        executor.fallback_local_owners.clear();
        executor.returned_callback_owners.clear();
        executor.private_constructor_result_owners = executor
            .closed_local_constructor_factory_result(fact)
            .unwrap_or_default();
        // A recognized metadata statement is a whole call expression. Its
        // eligibility proof is stable across this statement's preparatory
        // lanes, so use the same proof for reachability and call execution.
        let metadata_application = index.top_metadata_decorations[fact.ordinal.0].as_ref();
        let prevalidated_metadata = metadata_application
            .filter(|_| {
                !index.opaque_top.contains(&fact.ordinal)
                    && index.top_assignments[fact.ordinal.0].is_empty()
                    && index.top_member_value_writes[fact.ordinal.0].is_empty()
                    && index.top_callback_result_writes[fact.ordinal.0].is_empty()
            })
            .and_then(|application| {
                executor.validated_metadata_decoration(application, &top_env, &top_captures)
            });
        let certified_deferred_write =
            executor.accepts_deferred_global_write(fact.ordinal, &top_env);
        if certified_deferred_write {
            executor
                .accepted_deferred_global_writes
                .insert(fact.ordinal);
        }
        let frozen_namespace = executor.private_frozen_namespace(fact);
        if let Some(proof) = &frozen_namespace {
            executor.frozen_namespace_calls.insert(proof.freeze_site);
            executor.frozen_namespace_calls.insert(proof.define_site);
        }
        let closed_result = executor.closed_callback_factory_result(fact);
        let fact_roots = executor.fact_roots(
            fact,
            prevalidated_metadata.map(|validated| validated.target_owner),
            &executor.private_constructor_result_owners,
            frozen_namespace.as_ref(),
            closed_result.is_some(),
        );
        if let Some((factory, span)) = closed_result
            && !fact_roots.contains(&fact.ordinal)
            && !executor.exported_owners.contains(&fact.ordinal)
            && fact.purity.is_pure()
            && !fact.declares_direct_function
            && frozen_namespace.is_none()
            && executor.private_constructor_result_owners.is_empty()
        {
            executor.record_callback_factory_use(
                &factory,
                CallbackFactoryUseKind::ClosedResultRoot,
                fact.ordinal,
                span,
            );
        }
        if executor.reachability.unknown_call_seen {
            executor.extend_reachable_owners(fact_roots.iter().copied());
        } else if let Some(roots) = &mut executor.prior_reachability_roots {
            // Include the active call site before any nested unknown call can
            // consume the prefix roots.
            roots.extend(fact_roots.iter().copied());
        }
        if index.opaque_top.contains(&fact.ordinal) {
            executor.note_unknown_execution(fact.ordinal);
        }
        // A class object keeps the superclass value from its definition.
        // Snapshot a simple lexical heritage read from this source prefix so
        // a later assignment to the binding cannot replace the base whose
        // constructor the emitted class actually inherited.
        for (key, class_binding) in &index.class_bindings {
            let owns_needed_class = index.class_owners.get(key) == Some(&fact.ordinal)
                && needed.contains(class_binding);
            if !owns_needed_class {
                continue;
            }
            let Some(class) = index.class_values.get(key) else {
                continue;
            };
            let Some(super_class) = &class.super_class else {
                continue;
            };
            let values = match super_class {
                ValueExpr::Binding(_) => executor.evaluate(super_class, &top_env, &top_captures),
                _ => unknown(),
            };
            executor.class_superclasses.insert(*key, values);
        }
        let mut outcome = Outcome {
            complete: true,
            ..Outcome::default()
        };
        // A source owner is indivisible, but can assign an existing binding
        // before one of its calls, e.g. `(reader = () => later, host())`.
        // Join these possible values before the flattened calls. Keep the
        // previous values and do not mark initialization definite until the
        // owner finishes; assignments on skipped/later paths are only may
        // values. A lexical TDZ still blocks publication through that binding.
        for (id, expression) in assignments {
            if !needed.contains(id)
                || !fact.rebinds.eager.contains(id)
                || !(executor.var_bindings.contains(id)
                    || executor.initialized_bindings.contains(id))
            {
                continue;
            }
            let values = executor.evaluate(expression, &top_env, &top_captures);
            top_env
                .entry(id.clone())
                .or_default()
                .extend(values.iter().cloned());
            let possible = executor.prefix_values.entry(id.clone()).or_default();
            let before = possible.len();
            possible.extend(values);
            if possible.len() != before {
                executor.prefix_revision = executor.prefix_revision.wrapping_add(1);
            }
        }
        // Like callable writes above, top-level property writes are kept in a
        // separate visitor lane. Applying all of this statement's writes
        // before its flattened call list may expose a later write early, which
        // is conservative and avoids missing a callback assigned before an
        // opaque call in the same statement.
        let member_writes = &index.top_member_value_writes[fact.ordinal.0];
        executor.apply_member_value_writes(
            member_writes,
            &top_env,
            &top_captures,
            &BTreeSet::new(),
            certified_deferred_write,
        );
        if !certified_deferred_write {
            executor.promote_callback_result_writes(
                &index.top_callback_result_writes[fact.ordinal.0],
                &top_env,
                &top_captures,
                fact.ordinal,
                &mut outcome,
                &BTreeSet::new(),
            );
        }
        let validated_metadata = prevalidated_metadata.or_else(|| {
            metadata_application.and_then(|application| {
                executor.validated_metadata_decoration(application, &top_env, &top_captures)
            })
        });
        if let (Some(application), Some(validated)) = (metadata_application, validated_metadata) {
            executor.summarize_metadata_decoration(
                application,
                validated,
                fact.ordinal,
                &mut outcome,
            );
        } else {
            for call in calls {
                executor.execute_call(call, &top_env, &top_captures, &mut outcome);
            }
        }
        // The fresh-instance proof applies only to this whole top-level
        // assignment. Nested callable writes owned by the same statement
        // still take the normal setter fallback when invoked later.
        if !certified_deferred_write && !index.fresh_instance_field_writes.contains(&fact.ordinal) {
            executor.promote_property_writes(
                &index.top_property_writes[fact.ordinal.0],
                fact.ordinal,
                &mut outcome,
            );
        }
        let fallback = std::mem::take(&mut executor.fallback_outcome);
        if fallback.unknown_call {
            outcome.effects.reads.extend(fallback.effects.reads);
            outcome.effects.rebinds.extend(fallback.effects.rebinds);
            outcome
                .effects
                .uninitialized_reads
                .extend(fallback.effects.uninitialized_reads);
            outcome.executed_owners.extend(fallback.executed_owners);
            outcome.unknown_call = true;
            outcome.inherit_completeness(false, fallback.effects.precision_losses);
        }
        if index.opaque_top.contains(&fact.ordinal) {
            outcome.unsupported(ExecutionOperation::OpaqueExecution, fact.ordinal, DUMMY_SP);
        }
        outcome.effects.complete = outcome.complete;
        // A failed runtime path forfeits the prevalidated private-result proof.
        // Keep the actual execution/effect outcome in either case.
        if !executor.private_constructor_result_owners.is_empty()
            && (!outcome.complete || outcome.unknown_call)
        {
            let owners = std::mem::take(&mut executor.private_constructor_result_owners);
            executor.extend_reachable_owners(owners.into_iter().chain([fact.ordinal]));
            // The source proof normally makes this unreachable. If an actual
            // executor path fails, restore the current statement's omitted
            // callback effects as well as all future reachability roots.
            for owner in executor.reachability.owners.clone() {
                executor.promote_lazy_owner(owner, fact.ordinal, &mut outcome);
            }
            executor.apply_rebind_effects(&outcome.effects.rebinds);
        }
        let mut prior_effect_owners = outcome.executed_owners.clone();
        prior_effect_owners
            .retain(|owner| !executor.private_constructor_result_owners.contains(owner));
        if !fact.purity.is_pure() && executor.private_constructor_result_owners.is_empty() {
            prior_effect_owners.insert(fact.ordinal);
        }
        executor.prior_effect_owners.extend(prior_effect_owners);
        if !executor.reachability.unknown_call_seen
            && let Some(roots) = &mut executor.prior_reachability_roots
        {
            roots.extend(
                outcome
                    .executed_owners
                    .iter()
                    .copied()
                    .filter(|owner| !executor.private_constructor_result_owners.contains(owner)),
            );
        }
        for (id, expression) in assignments {
            if !needed.contains(id) {
                continue;
            }
            let values = executor.evaluate(expression, &top_env, &top_captures);
            if !executor.top_bindings.contains(id) {
                top_captures
                    .entry(id.clone())
                    .or_default()
                    .extend(values.clone());
            }
            top_env
                .entry(id.clone())
                .or_default()
                .extend(values.clone());
            if executor.top_bindings.contains(id) {
                executor
                    .prefix_values
                    .entry(id.clone())
                    .or_default()
                    .extend(values);
                executor.prefix_revision = executor.prefix_revision.wrapping_add(1);
            }
        }
        executor.initialized_bindings.extend(
            index.top_definite_initializers[fact.ordinal.0]
                .iter()
                .cloned(),
        );
        for expression in &index.top_exported_values[fact.ordinal.0] {
            let values = executor.evaluate(expression, &top_env, &top_captures);
            executor.exported_runtime_values.extend(values);
            executor.prefix_revision = executor.prefix_revision.wrapping_add(1);
        }
        outcome.effects.callback_factory_uses = std::mem::take(&mut executor.proof_uses);
        effects.push(outcome.effects);
    }
    for (fact, effect) in facts.iter_mut().zip(effects) {
        fact.synchronous_calls = Some(effect);
    }
}
