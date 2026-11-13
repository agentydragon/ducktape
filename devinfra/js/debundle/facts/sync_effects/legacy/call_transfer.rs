//! The self-evaluating call transfer: a flattened `Call` record whose callee
//! and arguments the transfer evaluates itself, and every non-`Call` kind
//! (accessor lookups, protocols, spreads, coercions, opaque execution).
//!
// CLEANUP(added 2026-11-09): remove with `legacy/` in L4e, once no
//   `RuntimeOperation::Call` is requested with `operands: None`. The last
//   callers are `Executor::execute_call` from the flattened lanes:
//   `roots::body`'s flattened owner calls, `Executor::invoke_with_cells` and
//   the application adapter's `ControlContext::unproved` remainder.

use super::super::execution_index::{Call, CallKind, object_own_data_read_property};
use super::super::interpreter::{EvaluatedCallOperands, Executor, Outcome, unknown};
use super::super::runtime_heap::NativeIterableUse;
use super::super::runtime_inference::CallTransfer;
use super::super::value_model::{AbstractValue, CallSite, Context, ValueExpr, Values};
use crate::execution_evidence::ExecutionOperation;
use crate::facts::abstract_domain::value::{Kind, Value};
use crate::reports::schema::NativeIntegrityGuard;

impl Executor<'_, '_> {
    pub(in crate::facts::sync_effects) fn execute_call_transfer(
        &mut self,
        call: &Call,
        env: &Context,
        captures: &Context,
        outcome: &mut Outcome,
    ) -> CallTransfer {
        if call.kind == CallKind::OpaqueExecution {
            let fact = &self.inputs.facts[call.owner.0];
            let mut reached: Values = env
                .values()
                .chain(captures.values())
                .flatten()
                .chain(
                    fact.reads
                        .eager
                        .iter()
                        .chain(&fact.reads.lazy)
                        .filter_map(|binding| self.state.world.module.prefix_values.get(binding))
                        .flatten(),
                )
                .cloned()
                .collect();
            for argument in &call.args {
                reached.extend(self.evaluate(argument, env, captures));
            }
            self.note_unknown_call(call);
            self.unsettle(&reached);
            self.promote_escaped_callbacks(&reached, outcome);
            self.invoke_possible_callbacks_at(&[reached], false, Some(call.span), outcome);
            outcome.unsupported(ExecutionOperation::OpaqueExecution, call.owner, call.span);
            outcome.unknown_call = true;
            return CallTransfer::EffectsOnly;
        }
        if call.kind == CallKind::Call && call.has_spread {
            // The canonical argument values are iterable capabilities, not
            // the actual positional arguments. No exact local invocation can
            // use them as though spreading had completed without effects.
            let mut reached = self.evaluate(&call.callee, env, captures);
            for argument in &call.args {
                reached.extend(self.evaluate(argument, env, captures));
            }
            self.note_unknown_call(call);
            self.unsettle(&reached);
            self.promote_escaped_callbacks(&reached, outcome);
            self.invoke_possible_callbacks_at(&[reached], false, Some(call.span), outcome);
            outcome.unsupported(ExecutionOperation::OpaqueExecution, call.owner, call.span);
            outcome.unknown_call = true;
            return CallTransfer::EffectsOnly;
        }
        if call.kind == CallKind::AccessorCalleeLookup {
            let ValueExpr::Member(receiver, property, _, _) = &call.callee else {
                unreachable!("accessor callee lookup always records a member");
            };
            let values = self.evaluate(receiver, env, captures);
            if values.contains(&AbstractValue::Value(Value::Top))
                && (self.callee_lookup_is_certified(call)
                    || self
                        .inputs
                        .index
                        .no_reentry_accessor_calls
                        .get(&CallSite::from(call.span))
                        .copied()
                        .is_some_and(|site| {
                            self.no_reentry_member_lookup_admitted(site, env, captures)
                        }))
            {
                return CallTransfer::EffectsOnly;
            }
            if values.iter().any(|value| {
                matches!(value, AbstractValue::Value(Value::Top))
                    || matches!(value, AbstractValue::Value(Value::Alloc(Kind::RegExp, _)))
                    || matches!(value,
                AbstractValue::Value(Value::Alloc(Kind::Object, object))
                    if !self.state.world.surface.object_accessors_are_settled(*object))
            }) {
                self.promote_possible_getter(call, &values, outcome);
                return CallTransfer::EffectsOnly;
            }
            for value in &values {
                if let AbstractValue::OwnDescriptorMap(site) = value {
                    self.invalidate_own_descriptor_map(*site);
                }
            }
            let accessor_getters = self.applied_getters(&values, property.as_deref());
            if accessor_getters.is_empty() {
                return CallTransfer::EffectsOnly;
            }
            let no_reentry_accessor_call = self
                .inputs
                .index
                .no_reentry_accessor_calls
                .get(&CallSite::from(call.span))
                .copied();
            let no_reentry_admitted = no_reentry_accessor_call
                .is_some_and(|site| self.no_reentry_member_lookup_admitted(site, env, captures));
            if !no_reentry_admitted {
                self.promote_raw_callbacks(&accessor_getters, outcome);
            }
            self.taint_objects(&values);
            if self.state.array_iteration_depth > 0 {
                self.state.array_iteration_unsupported = true;
            }
            outcome.unsupported(ExecutionOperation::MemberRead, call.owner, call.span);
            return CallTransfer::EffectsOnly;
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
                    .inputs
                    .index
                    .non_reentrant_global_membership
                    .contains(&call.span.into())
            {
                // Operand effects were indexed/evaluated normally. The separate
                // author contract certifies only this exact host protocol's
                // lack of callback reentry; purity and throw checks stay intact.
                return CallTransfer::EffectsOnly;
            }
            if call.kind == CallKind::InProtocol
                && self.ordinary_prototype_chain(&operands[0])
                && operands[0].iter().all(|value| {
                    matches!(value, AbstractValue::Value(Value::Alloc(Kind::Object, _)))
                })
            {
                return CallTransfer::EffectsOnly;
            }
            if call.kind == CallKind::InstanceOfProtocol
                && self.default_class_has_instance(&operands[0])
            {
                // OrdinaryHasInstance consults the LHS prototype chain, never
                // the constructor's deferred method bodies.
                if self.ordinary_prototype_chain(&operands[1]) {
                    return CallTransfer::EffectsOnly;
                }
                operands.remove(0);
            }
            let values: Values = operands.into_iter().flatten().collect();
            self.expose_class_capabilities(&values);
            self.taint_objects(&values);
            self.promote_escaped_callbacks(&values, outcome);
            self.invoke_possible_callbacks_at(&[values], false, Some(call.span), outcome);
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
            return CallTransfer::EffectsOnly;
        }
        if call.kind == CallKind::LocalMemberRead {
            if let Some(premise) = self
                .inputs
                .index
                .primitive_class_key_read_premise(call.span)
            {
                self.record_proof_dependencies(
                    NativeIntegrityGuard::NativeCallIdentity,
                    &premise.dependencies,
                );
                self.observations
                    .record_inference_use(premise.inference_use);
                // Data and primitive do not imply a constant property value.
                // Keep its external heap observation on the ordinary timeline.
                outcome.effects.heap_contents.opaque = true;
                outcome.effects.heap_contents.observes_external_heap = true;
                return CallTransfer::EffectsOnly;
            }
            if self.native_member_lookup_is_data(&call.callee, Some(call.span.lo.0)) {
                return CallTransfer::EffectsOnly;
            }
            if self.closed_enum_member_read(&call.callee) {
                return CallTransfer::EffectsOnly;
            }
            if let Some(read) = self.root_own_data_read(&call.callee) {
                outcome
                    .effects
                    .record_heap_read(read.cell, self.state.assumption.is_none());
                return CallTransfer::EffectsOnly;
            }
            if object_own_data_read_property(&call.callee, |id| self.inputs.flow.is_declared(id))
                .is_some()
                && self
                    .inputs
                    .index
                    .native_own_data_read_premise(call.span)
                    .is_none()
            {
                let selected = self.evaluate(&call.callee, env, captures);
                self.promote_escaped_callbacks(&selected, outcome);
                self.invoke_possible_callbacks_at(&[selected], false, Some(call.span), outcome);
                self.note_unknown_call(call);
                outcome.unsupported(ExecutionOperation::MemberRead, call.owner, call.span);
                outcome.unknown_call = true;
                return CallTransfer::EffectsOnly;
            }
            let ValueExpr::Member(receiver, property, _, _) = &call.callee else {
                unreachable!()
            };
            let values = self.evaluate(receiver, env, captures);
            let private_own_field =
                self.private_own_field_read(receiver, property.as_deref(), call, &values);
            if !private_own_field {
                self.publish_accessor_receivers(&values, call.span);
            }
            if values.iter().any(|value| {
                matches!(value, AbstractValue::Value(Value::Top))
                    || matches!(value,
                AbstractValue::Value(Value::Alloc(Kind::Object, object))
                    if !self.state.world.surface.object_accessors_are_settled(*object))
            }) {
                self.promote_possible_getter(call, &values, outcome);
                return CallTransfer::EffectsOnly;
            }
            if values.contains(&AbstractValue::ForeignDeferredRecord) {
                self.promote_foreign_record(outcome);
                self.note_unknown_call(call);
                self.invoke_possible_callbacks_at(&[values], false, Some(call.span), outcome);
                return CallTransfer::EffectsOnly;
            }
            for value in &values {
                if let AbstractValue::OwnDescriptorMap(site) = value {
                    self.invalidate_own_descriptor_map(*site);
                }
            }
            let accessor_getters = self.applied_getters(&values, property.as_deref());
            if !accessor_getters.is_empty() {
                self.promote_raw_callbacks(&accessor_getters, outcome);
            }
            let unsupported_local = values.iter().any(|value| match value {
                AbstractValue::Value(Value::Alloc(kind, object))
                    if kind.supports_ordinary_own_data_read_proof() =>
                {
                    if !self.state.world.object_properties_are_settled(*object)
                        || (!self.inputs.index.plain_data_objects.contains(object)
                            && !self
                                .state
                                .world
                                .surface
                                .ordinary_spread_copies
                                .contains(object)
                            && !self
                                .state
                                .world
                                .surface
                                .descriptor_copy_results
                                .contains(object)
                            && !self.state.world.arrays.dense.contains_key(object))
                    {
                        true
                    } else {
                        let own_key_present = property.as_ref().and_then(|property| {
                            self.state
                                .world
                                .surface
                                .prefix_object_properties
                                .get(object)
                                .map(|properties| properties.contains_key(&Some(property.clone())))
                        });
                        match (property.as_ref(), own_key_present) {
                            (Some(_), Some(true)) => false,
                            (Some(property), Some(false)) => {
                                !self.absent_plain_data_property(*object, property)
                            }
                            _ => true,
                        }
                    }
                }
                // Non-ordinary allocation kinds retain identity or containment
                // only. Their own and inherited property semantics still
                // need an explicit native/descriptor proof (for example,
                // RegExp.prototype.source is an accessor).
                AbstractValue::Value(Value::Alloc(kind, _))
                    if !kind.supports_ordinary_own_data_read_proof() =>
                {
                    true
                }
                AbstractValue::Instance(key, _, _) => {
                    !self.known_local_instance_member(*key, property.as_deref())
                }
                AbstractValue::DeferredCallbacks(_)
                | AbstractValue::AuditedRetainedResult(_, _) => true,
                _ => false,
            }) || !accessor_getters.is_empty();
            if unsupported_local {
                if values.iter().any(|value| {
                    matches!(value,
                    AbstractValue::Value(Value::Alloc(Kind::Object, object))
                    if self.state.world.surface.descriptor_copy_results.contains(object))
                }) {
                    self.promote_escaped_callbacks(&values, outcome);
                }
                // This receiver is still settled: preserve the known member's
                // capabilities for effects before taint discards its value.
                // Unsettled receivers took the possible-getter path above.
                let selected = self.selected_member_capabilities(&values, property.as_deref());
                self.taint_objects(&values);
                self.promote_escaped_callbacks(&selected, outcome);
                if self.state.array_iteration_depth > 0 {
                    self.state.array_iteration_unsupported = true;
                }
                outcome.unsupported(ExecutionOperation::MemberRead, call.owner, call.span);
            } else if private_own_field
                && accessor_getters.is_empty()
                && self
                    .inputs
                    .index
                    .unconditionally_evaluated_member_read(call)
            {
                outcome
                    .effects
                    .proven_private_own_field_reads
                    .push((call.owner, call.span));
            }
            return CallTransfer::EffectsOnly;
        }
        if call.kind == CallKind::ObjectSpread {
            let values = self.evaluate(&call.args[0], env, captures);
            if self.ordinary_data_properties(&values).is_some() {
                return CallTransfer::EffectsOnly;
            }
            if self.state.array_iteration_depth > 0 {
                self.state.array_iteration_unsupported = true;
            }
            outcome.unsupported(ExecutionOperation::ObjectSpread, call.owner, call.span);
            outcome.unknown_call = true;
            self.note_unknown_call(call);
            self.promote_escaped_callbacks(&values, outcome);
            self.invoke_possible_callbacks_at(&[values], false, Some(call.span), outcome);
            return CallTransfer::EffectsOnly;
        }
        if call.kind == CallKind::ArraySpread {
            let values = self.evaluate(&call.args[0], env, captures);
            if let Some(receipt) =
                self.native_iterable(NativeIterableUse::ArraySpread(call.span), &values)
            {
                outcome.effects.record_heap_contents(
                    &receipt.external_heap_contents(),
                    self.state.assumption.is_none(),
                );
                outcome.effect_coverage.compose(&receipt.coverage);
                return CallTransfer::EffectsOnly;
            }
            outcome.unsupported(ExecutionOperation::ArraySpread, call.owner, call.span);
            outcome.unknown_call = true;
            self.note_unknown_call(call);
            // Consuming a custom iterator can execute deferred generator bodies.
            self.promote_escaped_callbacks(&values, outcome);
            self.invoke_possible_callbacks_at(&[values], false, Some(call.span), outcome);
            return CallTransfer::EffectsOnly;
        }
        if call.kind == CallKind::ImplicitCoercion {
            let values = call
                .args
                .first()
                .map(|argument| self.evaluate(argument, env, captures))
                .unwrap_or_else(unknown);
            if values.iter().all(|value| {
                matches!(
                    value,
                    AbstractValue::Value(Value::Prim(_)) | AbstractValue::Value(Value::Unavailable)
                )
            }) {
                return CallTransfer::EffectsOnly;
            }
            let mut possible_values = values;
            possible_values.remove(&AbstractValue::Value(Value::Unavailable));
            if possible_values.is_empty() {
                return CallTransfer::EffectsOnly;
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
            self.invoke_possible_callbacks_at(&[possible_values], false, Some(call.span), outcome);
            return CallTransfer::EffectsOnly;
        }
        let targets = self.evaluate(&call.callee, env, captures);
        if targets.len() == 1 && targets.contains(&AbstractValue::Value(Value::Unavailable)) {
            return CallTransfer::EffectsOnly;
        }
        let args = call
            .args
            .iter()
            .map(|arg| self.evaluate(arg, env, captures))
            .collect();
        self.transfer_evaluated_call(
            call,
            env,
            captures,
            outcome,
            EvaluatedCallOperands {
                targets,
                args,
                prepared_receiver: None,
            },
            None,
        )
    }
}
