//! The self-evaluating evaluator: a `ValueExpr` evaluated against an
//! environment, outside any ordered body. The kernel transfers the same
//! expressions through `kernel::continuation::value_of`.

// CLEANUP(added 2026-11-09): remove with `legacy/` in L4e, once no
//   `RuntimeOperation::Evaluate` or `EvaluateInitializer` is requested. The
//   last callers are `Executor::evaluate` (the flattened lanes, the remainder
//   fallbacks and the preparatory proofs of `roots::operands`) and
//   `Executor::evaluate_initializer_transfer`.

use super::super::allocation_transfer::AllocationContext;
use super::super::execution_index::CallKind;
use super::super::interpreter::{Executor, from_flow, unknown};
use super::super::runtime_heap::{NativeIterableReceipt, NativeIterableUse};
use super::super::value_model::{
    AbstractValue, Context, HeapAddress, InstanceOrigin, RetainedResultKind, ValueExpr, Values,
};
use crate::facts::abstract_domain::builtins::IntrinsicOp;
use crate::facts::abstract_domain::value::{Kind, Value};
use crate::facts::evaluation_effects::GlobalAccessKind;
use crate::reports::schema::{CallbackFactoryUseKind, NativeIntegrityGuard};
use std::collections::BTreeMap;
use swc_common::Span;
use swc_ecma_ast::*;

pub(in crate::facts::sync_effects) struct ApplicationAllocationContext<'a, 'source, 'rule> {
    executor: &'a mut Executor<'source, 'rule>,
    environment: &'a Context,
    captures: &'a Context,
}

impl AllocationContext for ApplicationAllocationContext<'_, '_, '_> {
    fn array_created(&mut self, allocation: u32, tainted: bool) {
        if tainted {
            self.executor
                .state
                .world_mut()
                .surface
                .tainted_objects
                .insert(allocation);
        }
    }
    fn array_spread(&mut self, span: Span, operand: &Values) -> Option<NativeIterableReceipt> {
        // The flattened call pass records this protocol's effects at its
        // `CallKind::ArraySpread` record; here only the yields are consumed.
        self.executor
            .native_iterable(NativeIterableUse::ArraySpread(span), operand)
    }
    fn source_covered(&self) -> bool {
        self.executor.source_covered
    }

    fn binding(
        &mut self,
        binding: &Id,
    ) -> Option<super::super::allocation_transfer::AllocationValue> {
        let values = self.executor.evaluate_binding(binding, self.environment);
        let judgment =
            if self.executor.source_covered && self.executor.initialized_local_binding(binding) {
                super::super::allocation_transfer::AllocationJudgment::CoveredAllocationOnly(
                    Box::default(),
                )
            } else {
                super::super::allocation_transfer::AllocationJudgment::Opaque
            };
        Some(super::super::allocation_transfer::AllocationValue { values, judgment })
    }

    fn child(
        &mut self,
        expression: &ValueExpr,
    ) -> Option<super::super::allocation_transfer::AllocationValue> {
        Some(
            self.executor
                .evaluate_allocation_child(expression, self.environment, self.captures),
        )
    }

    fn literal_property_kind(
        &self,
        allocation: u32,
        position: usize,
    ) -> super::super::value_model::LiteralPropertyKind {
        self.executor
            .inputs
            .index
            .literal_property_kind(allocation, position)
    }
    fn literal_layout(&self, allocation: u32) -> super::super::allocation_transfer::LiteralLayout {
        self.executor.inputs.index.allocation_layout(allocation)
    }

    fn heap(&mut self) -> super::super::allocation_transfer::LiteralHeap<'_> {
        self.executor.literal_heap()
    }
}

impl Executor<'_, '_> {
    pub(in crate::facts::sync_effects) fn evaluate_transfer(
        &mut self,
        expr: &ValueExpr,
        env: &Context,
        captures: &Context,
    ) -> Values {
        if let Some(value) = super::super::allocation_transfer::transfer(
            &mut ApplicationAllocationContext {
                executor: self,
                environment: env,
                captures,
            },
            expr,
        ) {
            self.allocation_judgment.compose(&value.judgment);
            return value.values;
        }
        self.mark_allocation_opaque();
        match expr {
            ValueExpr::Binding(_) => unreachable!("the shared transfer resolves bindings"),
            ValueExpr::Alloc(Kind::Function, key) => Values::from([AbstractValue::Closure(
                HeapAddress {
                    scope: self.state.world.scope,
                    callable: *key,
                },
                self.closure_captures(*key, captures),
            )]),
            ValueExpr::Alloc(kind, site) => {
                if *kind == Kind::Class {
                    self.escape_class_captures(*site, env, captures);
                }
                Values::from([AbstractValue::Value(Value::Alloc(*kind, *site))])
            }
            ValueExpr::ObjectLiteral(key, properties) => {
                if self.inputs.index.exact_object_spread_copies.contains(key)
                    && properties.iter().all(|(name, _)| name.is_none())
                {
                    let mut copied = BTreeMap::new();
                    let mut supported = true;
                    for (_, source) in properties.iter() {
                        if properties.len() > 1
                            && !matches!(source, ValueExpr::Binding(binding)
                                if self.inputs.index.plain_string_record_bindings.contains(binding)
                                    && self.inputs.single_writer_bindings.contains(binding)
                                    && self.state.world.module.initialized_bindings.contains(binding)
                                    && !self.inputs.index.reassigned_bindings.contains(binding)
                                    && self.inputs.index.dynamic_scope_owners.is_empty())
                        {
                            supported = false;
                            break;
                        }
                        let source = self.evaluate(source, env, captures);
                        let Some(source_properties) = self.ordinary_data_properties(&source) else {
                            supported = false;
                            break;
                        };
                        if properties.len() > 1
                            && source_properties.values().any(|values| {
                                values.is_empty() || !values.iter().all(AbstractValue::is_primitive)
                            })
                        {
                            supported = false;
                            break;
                        }
                        copied.extend(source_properties);
                    }
                    if supported {
                        let properties = copied;
                        let previously_evaluated = self
                            .state
                            .world
                            .surface
                            .ordinary_spread_copies
                            .contains(key);
                        let current = self
                            .state
                            .world_mut()
                            .surface
                            .prefix_object_properties
                            .entry(*key)
                            .or_default();
                        let mut changed = false;
                        if previously_evaluated {
                            for (property, values) in current.iter_mut() {
                                if !properties.contains_key(property) {
                                    changed |= values.insert(AbstractValue::UNDEFINED);
                                }
                            }
                        }
                        for (property, values) in properties {
                            let new_property = !current.contains_key(&property);
                            let slot = current.entry(property).or_default();
                            let before = slot.len();
                            if previously_evaluated && new_property {
                                slot.insert(AbstractValue::UNDEFINED);
                            }
                            slot.extend(values);
                            changed |= slot.len() != before;
                        }
                        if changed {
                            self.state.world_mut().revision.bump_prefix();
                        }
                        self.state
                            .world_mut()
                            .surface
                            .ordinary_spread_copies
                            .insert(*key);
                        return Values::from([AbstractValue::Value(Value::Alloc(
                            Kind::Object,
                            *key,
                        ))]);
                    }
                    self.state.world_mut().surface.tainted_objects.insert(*key);
                    if self.state.array_iteration_depth > 0 {
                        self.state.array_iteration_unsupported = true;
                    }
                }
                let definitions = properties
                    .iter()
                    .enumerate()
                    .map(|(position, (name, value))| {
                        super::super::value_model::PropertyDefinition::new(
                            self.inputs.index.literal_property_kind(*key, position),
                            name.clone(),
                            value,
                        )
                    })
                    .collect::<Vec<_>>();
                super::super::allocation_transfer::record(
                    &mut ApplicationAllocationContext {
                        executor: self,
                        environment: env,
                        captures,
                    },
                    *key,
                    definitions,
                    super::super::allocation_transfer::AllocationJudgment::Opaque,
                    |context, expression| context.child(expression),
                )
                .expect("application child transfers always produce values")
                .values
            }

            ValueExpr::Undefined | ValueExpr::NonCallable(_) => {
                unreachable!("the shared transfer evaluates primitive values")
            }
            // CLEANUP(added 2026-11-09): a read the kernel's own-data transfer
            // resolves never reaches this arm. Its remaining callers are the
            // unproved-read fallback in `ControlContext::unproved` (gate: the
            // Wave 3 descriptor and native read slices give those reads a
            // kernel path) and this evaluator's own flattened lanes (gate:
            // L4e deletes `legacy/`). Delete the arm with the last one.
            ValueExpr::Member(object, property, is_static_identifier, site) => {
                if let Some(premise) = self
                    .inputs
                    .index
                    .primitive_class_key_read_premise(site.source_span())
                {
                    self.record_proof_dependencies(
                        NativeIntegrityGuard::NativeCallIdentity,
                        &premise.dependencies,
                    );
                    self.observations
                        .record_inference_use(premise.inference_use);
                    return Values::from([AbstractValue::PRIMITIVE]);
                }
                if let Some(read) = self.root_own_data_read(expr) {
                    return read.values;
                }
                if let Some(values) = property
                    .as_deref()
                    .and_then(|property| self.closed_enum_member_parts(object, property))
                {
                    return values;
                }
                if let ValueExpr::Binding(id) = object.as_ref()
                    && is_static_identifier.is_static()
                    && property.as_ref().is_some_and(|property| {
                        self.inputs
                            .index
                            .callback_factory_members
                            .get(id)
                            .is_some_and(|members| members.contains(property))
                    })
                {
                    return Values::from([AbstractValue::CallbackFactory]);
                }
                if matches!(object.as_ref(), ValueExpr::Binding(id) if id.0.as_ref() == "Object" && !self.inputs.flow.is_declared(id))
                    && property.as_deref() == Some("assign")
                    && !self.inputs.shadowed.contains("Object")
                {
                    return Values::from([AbstractValue::ObjectAssign]);
                }
                let objects = self.evaluate(object, env, captures);
                let mut values = Values::new();
                for object in objects {
                    match object {
                        AbstractValue::OwnDescriptorMap(site) => {
                            self.invalidate_own_descriptor_map(site);
                            let callbacks = self.descriptor_map_callbacks(site);
                            values.insert(self.deferred_callbacks(callbacks));
                            values.insert(AbstractValue::Value(Value::Top));
                        }
                        AbstractValue::ForeignDeferredRecord => {
                            values.insert(AbstractValue::ForeignDeferredRecord);
                        }
                        AbstractValue::ArrayIntrinsic => {
                            values.insert(AbstractValue::ArrayIntrinsic);
                        }
                        AbstractValue::GlobalObject => {
                            if let (Some(property), Some(owner)) =
                                (property, self.inputs.index.member_owners.get(site))
                            {
                                self.evaluation_effects.record(
                                    *owner,
                                    site.source_span(),
                                    GlobalAccessKind::Read,
                                    property,
                                );
                            }
                            // A global slot can hold an escaped callback, but
                            // this lookup does not establish that identity.
                            // Keep it opaque so call dispatch promotes the
                            // escaped pool under its possible-call assumption.
                            values.insert(AbstractValue::Value(Value::Top));
                        }
                        AbstractValue::Value(Value::Unavailable) => {
                            values.insert(AbstractValue::Value(Value::Unavailable));
                        }
                        AbstractValue::DeferredCallbacks(captured)
                        | AbstractValue::AuditedRetainedResult(_, captured) => {
                            values.insert(AbstractValue::DeferredCallbacks(captured));
                        }
                        AbstractValue::Value(Value::Alloc(Kind::Class, key))
                        | AbstractValue::ClassMembers(key) => {
                            values.insert(AbstractValue::ClassMembers(key));
                        }
                        AbstractValue::Instance(key, captured, _) => {
                            if matches!(property.as_deref(), Some("constructor" | "__proto__")) {
                                self.state.world_mut().classes.tainted.insert(key);
                            }
                            values.insert(AbstractValue::Instance(
                                key,
                                captured,
                                InstanceOrigin::Projected,
                            ));
                        }
                        AbstractValue::Value(Value::Alloc(Kind::Object, object_id)) => {
                            // Unknown alone reaches no unescaped callable: an
                            // unsettled object or a non-data lookup still holds
                            // the values stored to it, beside what unknown code
                            // or an accessor adds.
                            if !self.state.world.object_properties_are_settled(object_id)
                                || !self.payload_lookup_is_data(object_id, property.as_deref())
                            {
                                if let Some(properties) = self
                                    .state
                                    .world
                                    .surface
                                    .prefix_object_properties
                                    .get(&object_id)
                                {
                                    match property {
                                        Some(property) => {
                                            for key in [Some(property.clone()), None] {
                                                values.extend(
                                                    properties
                                                        .get(&key)
                                                        .into_iter()
                                                        .flatten()
                                                        .cloned(),
                                                );
                                            }
                                        }
                                        None => values.extend(
                                            properties
                                                .values()
                                                .flat_map(|found| found.iter().cloned()),
                                        ),
                                    }
                                }
                                values.insert(AbstractValue::Value(Value::Top));
                                continue;
                            }
                            if self.state.world.arrays.dense.contains_key(&object_id)
                                && (property.is_none()
                                    || matches!(
                                        property.as_deref(),
                                        Some("constructor" | "__proto__")
                                    ))
                            {
                                values.insert(AbstractValue::ArrayIntrinsic);
                            }

                            if let Some(properties) = self
                                .state
                                .world
                                .surface
                                .prefix_object_properties
                                .get(&object_id)
                            {
                                if let Some(property) = property {
                                    let found = properties.get(&Some(property.clone())).cloned();
                                    let dynamic = properties.get(&None).cloned();
                                    match found {
                                        Some(found) => values.extend(found.iter().cloned()),
                                        None if self
                                            .absent_plain_data_property(object_id, property) =>
                                        {
                                            values.insert(AbstractValue::UNDEFINED);
                                        }
                                        None => {
                                            values.insert(AbstractValue::Value(Value::Top));
                                        }
                                    }
                                    if let Some(dynamic) = dynamic {
                                        values.extend(dynamic.iter().cloned());
                                    }
                                } else {
                                    values.extend(
                                        properties.values().flat_map(|found| found.iter().cloned()),
                                    );
                                }
                            } else if let Some(property) = property {
                                if let Some(found) =
                                    self.inputs.flow.values_of_property(object_id, property)
                                {
                                    values.extend(from_flow(found.clone()));
                                } else {
                                    values.insert(AbstractValue::Value(Value::Top));
                                }
                            } else {
                                values.insert(AbstractValue::Value(Value::Top));
                            }
                        }
                        _ => {
                            values.insert(AbstractValue::Value(Value::Top));
                        }
                    }
                }
                values
            }
            ValueExpr::Call(callee, args, span) => {
                if let Some((proof, _, dense, _, _)) =
                    self.array_iteration_plan(*span, env, captures)
                {
                    return self.array_iteration_result(&proof, &dense);
                }
                let targets = self.evaluate(callee, env, captures);
                if targets.len() == 1 && targets.contains(&AbstractValue::Value(Value::Unavailable))
                {
                    return Values::from([AbstractValue::Value(Value::Unavailable)]);
                }
                let inline_callback =
                    matches!(args.as_ref(), [ValueExpr::Alloc(Kind::Function, _)]);
                let expressions = args;
                let mut args: Vec<Values> = args
                    .iter()
                    .map(|arg| self.evaluate(arg, env, captures))
                    .collect();
                let args_may_throw = args
                    .iter()
                    .any(|values| values.contains(&AbstractValue::Value(Value::Unavailable)));
                if args.iter().any(|values| {
                    values.len() == 1 && values.contains(&AbstractValue::Value(Value::Unavailable))
                }) {
                    return Values::from([AbstractValue::Value(Value::Unavailable)]);
                }
                for values in &mut args {
                    values.remove(&AbstractValue::Value(Value::Unavailable));
                }
                if let Some(summary) = self.foreign_call_summary(CallKind::Call, callee) {
                    let mut result = self.foreign_call_result(&summary, &args);
                    if args_may_throw {
                        result.insert(AbstractValue::Value(Value::Unavailable));
                    }
                    return result;
                }
                if self.source_record_factory(callee, expressions) {
                    if let Some(&(owner, call_span)) = self.inputs.call_site_owners.get(span) {
                        self.record_returned_record_use(
                            callee,
                            crate::reports::schema::ReturnedRecordUseKind::ReturnCapability,
                            owner,
                            call_span,
                        );
                    }
                    return Values::from([AbstractValue::ForeignDeferredRecord]);
                }
                if let Some(source_object) = self.source_namespace_wrapper(callee, &args, *span) {
                    // A cached hit may return the same facade as an earlier
                    // call. Keep result identity opaque and retain the source
                    // and every deferred callback for later property reads or
                    // publication, without running a getter at this call.
                    let mut retained = Values::from([AbstractValue::Value(Value::Alloc(
                        Kind::Object,
                        source_object,
                    ))]);
                    retained.extend(self.callback_values(&retained));
                    // The provider's cache may still be in its temporal dead
                    // zone during an ESM cycle. No success-only return fact
                    // may erase that possible throw.
                    return Values::from([
                        self.deferred_callbacks(retained),
                        AbstractValue::Value(Value::Top),
                        AbstractValue::Value(Value::Unavailable),
                    ]);
                }
                if let Some(proof) = self.descriptor_copy_projection(callee, &args, *span) {
                    return self.install_descriptor_copy_result(proof, &args, *span);
                }
                if let Some(resolution) =
                    self.resolve_native_call(CallKind::Call, callee, *span, expressions, &args)
                {
                    return self.execute_native_resolution(
                        resolution,
                        &args,
                        args_may_throw,
                        *span,
                    );
                }
                if args.len() == 2
                    && self.ordinary_object_prototype_intact()
                    && self
                        .exact_native_call_row(CallKind::Call, callee, *span)
                        .is_some_and(|(row, _)| row.op == IntrinsicOp::ObjectDefineProperties)
                {
                    // This is only a successful-result identity fact. The
                    // call-timing lane still needs a live descriptor map and
                    // an empty target, and falls back opaquely otherwise.
                    let mut result = args[0].clone();
                    result.insert(AbstractValue::Value(Value::Unavailable));
                    return result;
                }
                if let Some(callbacks) = self.retained_factory_callbacks(*span, expressions, &args)
                {
                    let result =
                        self.audited_retained_result(RetainedResultKind::Factory, callbacks);
                    return if args_may_throw {
                        Values::from([result, AbstractValue::Value(Value::Unavailable)])
                    } else {
                        Values::from([result])
                    };
                }
                if let Some(callbacks) = self.proven_from_entries_callbacks(*span, env, captures) {
                    return Values::from([self.deferred_callbacks(callbacks)]);
                }
                let deferred_callback = matches!(args.as_slice(), [values] if !values.is_empty()
                    && values.iter().all(|value| matches!(value, AbstractValue::AuditedRetainedResult(RetainedResultKind::Factory, captured) if self.audited_result_is_clean(captured))));
                let mut returns = Values::new();
                let no_targets = targets.is_empty();
                let may_throw = args_may_throw
                    || no_targets
                    || targets.iter().any(|target| {
                        matches!(
                            target,
                            AbstractValue::Value(Value::Unavailable)
                                | AbstractValue::Value(Value::Prim(_))
                        )
                    });
                for target in targets {
                    match target {
                        // CLEANUP(added 2026-11-10): the ordered lane
                        // substitutes a source-inferred factory that has a
                        // derived summary (`retaining_factory_result` in
                        // `roots/call_operands.rs`). This arm stays for the
                        // roots the legacy gate still evaluates here: an
                        // author-asserted or unsummarized factory, a
                        // retained-result argument, and owners outside the
                        // ordered root family. Remove with `legacy/` in L4e.
                        AbstractValue::CallbackFactory if inline_callback || deferred_callback => {
                            if let ValueExpr::Binding(imported_local) = callee.as_ref()
                                && let Some(&(owner, call_span)) =
                                    self.inputs.call_site_owners.get(span)
                            {
                                self.record_callback_factory_use(
                                    imported_local,
                                    CallbackFactoryUseKind::ReturnShape,
                                    owner,
                                    call_span,
                                );
                            }
                            let callbacks = self.callback_values(&args[0]);
                            returns.insert(
                                self.audited_retained_result(
                                    RetainedResultKind::Factory,
                                    callbacks,
                                ),
                            );
                        }
                        AbstractValue::InlineCallbackFactory if inline_callback => {
                            let callbacks = self.callback_values(&args[0]);
                            returns.insert(
                                self.audited_retained_result(
                                    RetainedResultKind::Factory,
                                    callbacks,
                                ),
                            );
                        }
                        AbstractValue::DeferredCallbacks(captured)
                        | AbstractValue::AuditedRetainedResult(_, captured) => {
                            returns.insert(AbstractValue::DeferredCallbacks(captured));
                            returns.insert(AbstractValue::Value(Value::Top));
                        }
                        AbstractValue::Value(Value::Unavailable)
                        | AbstractValue::Value(Value::Prim(_)) => {}
                        AbstractValue::Value(Value::Alloc(Kind::Function, key)) => {
                            returns.extend(self.invoke(key, args.clone(), None, false).returns);
                        }
                        AbstractValue::Closure(HeapAddress { callable: key, .. }, captured) => {
                            returns.extend(
                                self.invoke(key, args.clone(), Some(captured), false)
                                    .returns,
                            );
                        }
                        AbstractValue::ObjectAssign => {
                            returns.extend(args.first().cloned().unwrap_or_else(unknown));
                        }
                        AbstractValue::Value(Value::Top) => {
                            returns.insert(AbstractValue::Value(Value::Top));
                        }
                        _ => {
                            returns.insert(AbstractValue::Value(Value::Top));
                        }
                    }
                }
                if returns.is_empty() && may_throw {
                    return Values::from([AbstractValue::Value(Value::Unavailable)]);
                }
                if may_throw {
                    returns.insert(AbstractValue::Value(Value::Unavailable));
                }
                returns
            }
            ValueExpr::New(callee, args, span) => {
                self.evaluate_new(callee, args, *span, env, captures).values
            }
            ValueExpr::Primitive(operands) => {
                let mut can_complete = true;
                let mut may_throw = false;
                for operand in operands.iter() {
                    let mut values = self.evaluate(operand, env, captures);
                    if values.remove(&AbstractValue::Value(Value::Unavailable)) {
                        may_throw = true;
                    }
                    if values.is_empty() {
                        can_complete = false;
                        break;
                    }
                }
                let mut results = Values::new();
                if can_complete {
                    results.insert(AbstractValue::PRIMITIVE);
                }
                if may_throw || !can_complete {
                    results.insert(AbstractValue::Value(Value::Unavailable));
                }
                results
            }
            ValueExpr::StrictEquality(..)
            | ValueExpr::Void(_)
            | ValueExpr::LogicalNot(_)
            | ValueExpr::Sequence(_) => {
                unreachable!("the shared transfer handles noncoercing primitive operations")
            }
            ValueExpr::Conditional { test, alternatives } => {
                // Preserve general branches' conservative reach, including the
                // test that the old result-only Alternatives lowering erased.
                let mut tested = self.evaluate(test, env, captures);
                let may_throw = tested.remove(&AbstractValue::Value(Value::Unavailable));
                if tested.is_empty() {
                    return Values::from([AbstractValue::Value(Value::Unavailable)]);
                }
                let mut results: Values = alternatives
                    .iter()
                    .flat_map(|alternative| self.evaluate(alternative, env, captures))
                    .collect();
                if may_throw {
                    results.insert(AbstractValue::Value(Value::Unavailable));
                }
                results
            }
            ValueExpr::Alternatives(options) => options
                .iter()
                .flat_map(|option| self.evaluate(option, env, captures))
                .collect(),
            ValueExpr::ArrayIteratorPrototype(expression)
            | ValueExpr::SetIteratorPrototype(expression) => {
                self.evaluate(expression, env, captures)
            }
            ValueExpr::Unknown => unknown(),
        }
    }
}
