//! The application adapter: the five kernel context traits over the
//! application `Executor`, for ordered root initializers and callable bodies.

use super::super::alternatives::execution_state::ExecutionPath;
use super::super::array_transfer::{self, ArrayPushSelection};
use super::super::effect_coverage::{EffectCoverageFragment, LocalBodyIdentity, LocalCallReceipt};
use super::super::execution_index::{
    Call, CallKind, ExecutionIndex, MemberValueWrite, native_own_data_read_slots,
};
use super::super::interpreter::{Executor, Outcome, unknown};
use super::super::kernel::cells::missing_activation_binding;
use super::super::kernel::closure::{CapturePolicy, ClosureAdmission};
use super::super::kernel::context::{
    BindingContext, CallContext, CellPolicy, ControlContext, Disposition, ForkPolicy, HeapContext,
    NativeContext, Unproved,
};
// Module-qualified: the builtin table's `Invocation` also has a
// tagged-template form the kernel never requests.
use super::super::kernel::frame::{
    self as kernel_frame, CallableSelection, EvaluatedCallee, LocalCallRequest, LocalFrame,
    LocalValueResult, RemainderDisposition,
};
use super::super::local_operations::{LocalBody, LocalExpression, LocalOperation};
use super::super::own_data_transfer::OwnDataContext;
use super::super::runtime_heap::{NativeIterableReceipt, NativeIterableUse};
use super::super::runtime_inference::{CallOperands, CallTransfer, RuntimeOperation, RuntimeValue};
use super::super::value_model::{
    AbstractValue, CallSite, CapturedContext, LexicalCaptures, MemberKey, OwnPropertyShape,
    ValueExpr, Values,
};
use super::super::world::World;
use crate::analysis_inputs::{NativeIntrinsic, ProofDependencies};
use crate::execution_evidence::{
    AssumedEffectReason, ExecutionOperation, ExecutionPrecisionLossReason,
};
use crate::facts::OwnerInitializationCoverageRefusalReason;
use crate::facts::abstract_domain::builtins;
use crate::facts::abstract_domain::value::{Kind, Value};
use crate::facts::evaluation_effects::{EvaluationEffects, GlobalAccessKind};
use crate::reports::schema::NativeIntegrityGuard;
use source_identity::SourceUnitOrdinal;
use std::collections::BTreeSet;
use std::sync::Arc;
use swc_common::{DUMMY_SP, Span};
use swc_ecma_ast::*;

#[derive(Clone, Copy)]
pub(in crate::facts::sync_effects) enum LocalExecutionScope {
    Callable(u32),
    RootInitializer,
    /// The lowered base-constructor body of this class, run by the kernel's
    /// construct rule. It has no call operation of its own.
    Constructor(u32),
}

impl LocalExecutionScope {
    pub(in crate::facts::sync_effects) fn caller(self) -> Option<u32> {
        match self {
            Self::Callable(key) => Some(key),
            Self::RootInitializer | Self::Constructor(_) => None,
        }
    }
}

pub(in crate::facts::sync_effects) struct LocalExecutionContext<'a, 'source, 'rule> {
    pub(in crate::facts::sync_effects) executor: &'a mut Executor<'source, 'rule>,
    pub(in crate::facts::sync_effects) outcome: &'a mut Outcome,
    pub(in crate::facts::sync_effects) owner: SourceUnitOrdinal,
    pub(in crate::facts::sync_effects) scope: LocalExecutionScope,
    pub(in crate::facts::sync_effects) body: &'a LocalBody,
}

impl OwnDataContext for LocalExecutionContext<'_, '_, '_> {
    fn ordinary_record_shape(&self, allocation: u32) -> Option<&OwnPropertyShape> {
        self.executor.ordinary_record_shape(allocation)
    }

    fn object_properties_are_settled(&self, allocation: u32) -> bool {
        self.executor.object_properties_are_settled(allocation)
    }

    fn property_values(&self, allocation: u32, property: &str) -> Option<&Values> {
        self.executor.property_values(allocation, property)
    }

    fn property_is_accessor(&self, allocation: u32, property: &str) -> bool {
        self.executor.property_is_accessor(allocation, property)
    }

    fn property_is_writable(&self, allocation: u32, property: &str) -> bool {
        self.executor.property_is_writable(allocation, property)
    }
}

impl<'source> LocalExecutionContext<'_, 'source, '_> {
    fn calls(&self) -> &'source [Call] {
        match self.scope {
            LocalExecutionScope::Callable(key) => &self.executor.inputs.index.callables[&key].calls,
            LocalExecutionScope::RootInitializer => {
                &self.executor.inputs.index.top_calls[self.owner.0]
            }
            LocalExecutionScope::Constructor(_) => &[],
        }
    }

    fn binding_values<'a>(&'a self, binding: &Id, frame: &'a LocalFrame) -> Option<&'a Values> {
        if self.executor.inputs.top_bindings.contains(binding) {
            self.executor.state.world.module.prefix_values.get(binding)
        } else if let Some(cell) = self.executor.state.world.active_cells.get(binding) {
            Some(&self.executor.state.world.cells[cell.0].values)
        } else {
            frame.environment.get(binding)
        }
    }

    fn retain_global_fallback_accesses(
        &mut self,
        frame: &LocalFrame,
        reason: &ExecutionPrecisionLossReason,
        truncation: Span,
    ) {
        let mut accesses = Vec::new();
        for call in self.calls() {
            if !matches!(
                call.kind,
                CallKind::LocalMemberRead | CallKind::AccessorCalleeLookup
            ) || call.owner != self.owner
                || call.caller != self.scope.caller()
                || call.span.hi < truncation.lo
            {
                continue;
            }
            let ValueExpr::Member(receiver, Some(property), MemberKey::Static, _) = &call.callee
            else {
                continue;
            };
            let ValueExpr::Binding(binding) = receiver.as_ref() else {
                continue;
            };
            if self
                .binding_values(binding, frame)
                .is_some_and(|values| values.contains(&AbstractValue::GlobalObject))
            {
                accesses.push((
                    call.owner,
                    call.span,
                    GlobalAccessKind::Read,
                    property.clone(),
                ));
            }
        }

        let writes: &[MemberValueWrite] = match self.scope {
            LocalExecutionScope::Callable(key) => {
                &self.executor.inputs.index.callables[&key].member_value_writes
            }
            LocalExecutionScope::RootInitializer => {
                &self.executor.inputs.index.top_member_value_writes[self.owner.0]
            }
            LocalExecutionScope::Constructor(_) => &[],
        };
        for write in writes {
            let source_span = write.site.source_span();
            let Some(property) = &write.property else {
                continue;
            };
            if write.owner != self.owner || source_span.hi < truncation.lo {
                continue;
            }
            if self
                .binding_values(&write.receiver, frame)
                .is_some_and(|values| values.contains(&AbstractValue::GlobalObject))
            {
                accesses.push((
                    write.owner,
                    source_span,
                    GlobalAccessKind::Write,
                    property.clone(),
                ));
            }
        }

        if accesses.is_empty() {
            return;
        }
        let mut fallback_accesses = EvaluationEffects::default();
        for (owner, span, kind, property) in accesses {
            fallback_accesses.record(owner, span, kind, &property);
        }
        let dependencies = self.executor.boxed_settled_prefix_dependencies();
        fallback_accesses.depend_on(&dependencies);
        fallback_accesses.assume(Some(&AssumedEffectReason::FallbackSummary {
            loss: reason.clone(),
        }));
        self.executor.evaluation_effects.extend(&fallback_accesses);
    }

    fn refuse_activation_value(&mut self, frame: &LocalFrame) {
        let span = self
            .scope
            .caller()
            .and_then(|key| self.executor.inputs.index.callable_spans.get(&key))
            .copied()
            .unwrap_or(DUMMY_SP);
        self.fallback(
            frame,
            Values::new(),
            ExecutionPrecisionLossReason::UnsupportedOperation {
                operation: ExecutionOperation::OpaqueExecution,
                ordered_field_read_refusal: None,
                ordered_call_admission_refusal: None,
            },
            span,
        );
    }

    pub(in crate::facts::sync_effects) fn fallback(
        &mut self,
        frame: &LocalFrame,
        reached: Values,
        reason: ExecutionPrecisionLossReason,
        span: Span,
    ) {
        self.retain_global_fallback_accesses(frame, &reason, span);
        self.executor.local_operation_fallback(
            frame,
            reached,
            self.owner,
            reason,
            span,
            self.outcome,
        );
        // The opaque operation can complete and reach later calls. Retain
        // their unknown direct-callee alternatives at their lexical sites,
        // rather than reporting every loss only at the truncation point.
        // This inspects indexed operands; it executes no later operation.
        for call in self.calls() {
            if call.kind != CallKind::Call || call.span.lo < span.hi {
                continue;
            }
            let ValueExpr::Binding(binding) = &call.callee else {
                continue;
            };
            let values = self.binding_values(binding, frame);
            if values.is_some_and(|values| {
                values.iter().any(|value| {
                    matches!(
                        value,
                        AbstractValue::Value(Value::Top) | AbstractValue::SymbolicInput(_)
                    )
                })
            }) {
                self.outcome.lose_call_target(call.owner, call.span);
            }
        }
    }
}

impl LocalExecutionContext<'_, '_, '_> {
    /// The foreign call summary whose substitution is also the call's effect
    /// receipt. A body that reads a private provider cell gets none: the
    /// summary proves nothing about that cell's writers.
    fn substituted_summary(
        &self,
        kind: CallKind,
        callee: &ValueExpr,
    ) -> Option<crate::analysis_inputs::ForeignCallSummary> {
        self.executor
            .foreign_call_summary(kind, callee)
            .filter(|summary| summary.provider_cell_reads.is_empty())
    }

    /// As `substituted_summary`, for the call itself: a retaining factory's
    /// summary is the receipt only for the call shape its transfer
    /// substitutes.
    fn substituted_call_summary(
        &self,
        call: &super::super::execution_index::Call,
    ) -> Option<crate::analysis_inputs::ForeignCallSummary> {
        if call.has_spread {
            return None;
        }
        self.substituted_summary(call.kind, &call.callee)
            .or_else(|| {
                self.executor
                    .retaining_factory_summary(call)
                    .filter(|summary| summary.provider_cell_reads.is_empty())
                    .cloned()
            })
    }

    /// The kernel evaluated `new`'s callee and arguments in order. A checked
    /// local class runs its lowered body through the kernel's construct rule;
    /// a refused construction keeps the flat lane's typed loss; any other
    /// target summarizes the remainder.
    fn construct(
        &mut self,
        frame: &mut LocalFrame,
        request: &LocalCallRequest,
    ) -> Result<LocalValueResult, RemainderDisposition> {
        let span = request.span;
        let targets = request.callee.targets();
        let mut reached = targets.clone();
        reached.extend(
            request
                .arguments
                .iter()
                .flat_map(|argument| &argument.values)
                .cloned(),
        );
        frame
            .borrowed_records
            .extend(self.executor.reachable_objects(&reached));
        let call = self
            .calls()
            .iter()
            .find(|call| call.kind == CallKind::New && call.span == span && !call.has_spread);
        let class = match targets.iter().collect::<Vec<_>>().as_slice() {
            [AbstractValue::Value(Value::Alloc(Kind::Class, key))] => Some(*key),
            _ => None,
        };
        let (Some(call), Some(class)) = (call, class) else {
            self.fallback(
                frame,
                reached,
                ExecutionPrecisionLossReason::UnsupportedOperation {
                    operation: ExecutionOperation::OpaqueExecution,
                    ordered_field_read_refusal: None,
                    ordered_call_admission_refusal: None,
                },
                span,
            );
            return Err(RemainderDisposition::AlreadySummarized);
        };
        let caller = self.executor.local_body_identity_for_call(call);
        let constructed = self.executor.construct_local_class_ordered(
            class,
            request.arguments.clone(),
            call,
            self.outcome,
        );
        frame.inputs.extend(constructed.values.iter().cloned());
        frame
            .borrowed_records
            .extend(self.executor.reachable_objects(&constructed.values));
        let effects = match (caller, constructed.body) {
            (Some(caller), Some((target, mut effects))) => {
                let dependencies = self.executor.boxed_settled_prefix_dependencies();
                effects.depend_on(&dependencies);
                effects.selected_local_call(LocalCallReceipt {
                    caller,
                    call_span: span,
                    target,
                    dependencies,
                });
                effects
            }
            _ => EffectCoverageFragment::default(),
        };
        Ok(LocalValueResult {
            allocation: super::super::allocation_transfer::AllocationValue::opaque(
                constructed.values,
            ),
            effects,
            selection: None,
        })
    }
}

impl BindingContext for LocalExecutionContext<'_, '_, '_> {
    fn cell_policy(&self) -> CellPolicy<'_> {
        CellPolicy {
            captured: &self.executor.inputs.captured_local_bindings,
        }
    }
    fn capture_policy(&self) -> CapturePolicy<'_> {
        self.executor.inputs.capture_policy()
    }
    fn cell_effect(&self) -> EffectCoverageFragment {
        // This activation-local binding has no live cell to expose a heap,
        // global, or host effect. Its value's evaluation is covered
        // separately by the ordered operation that produced it.
        EffectCoverageFragment::no_effects()
    }
    fn module_prefix_receiver(&self, binding: &Id, _receivers: &Values) -> bool {
        self.executor.initialized_local_binding(binding)
    }
    fn read_binding(
        &mut self,
        binding: &Id,
        frame: &mut LocalFrame,
    ) -> Result<LocalValueResult, RemainderDisposition> {
        if missing_activation_binding(self.body, binding, frame) {
            self.refuse_activation_value(frame);
            return Err(RemainderDisposition::AlreadySummarized);
        }
        if self.executor.inputs.top_bindings.contains(binding) {
            self.executor
                .promote_top_effects(vec![binding], Vec::new(), self.owner, self.outcome);
        }
        let values = self.executor.evaluate_binding(binding, &frame.environment);
        if self.executor.inputs.top_bindings.contains(binding)
            || (self.executor.state.world.active_cells.contains_key(binding)
                && !self.body.bindings.contains_key(binding))
        {
            frame
                .borrowed_records
                .extend(self.executor.reachable_objects(&values));
            // A later local assignment can discard the last alias of this
            // external value. A truncated remainder can still reach it via
            // an already captured reference, so preserve its capabilities.
            frame.inputs.extend(values.iter().cloned());
        }
        // Declarative module GetBindingValue has no property/heap/host
        // operation. This proves its effects independently of the value kind.
        // Captures, globals, and possible abrupt reads have no receipt. An
        // imported binding needs its own source-derived normal-read proof.
        let normal =
            !values.is_empty() && !values.contains(&AbstractValue::Value(Value::Unavailable));
        let imported_primitive_read = normal
            && self
                .executor
                .inputs
                .imported_primitive_read(binding)
                .is_some_and(|read| {
                    values == Values::from([AbstractValue::Value(Value::Prim(read.kinds))])
                });
        let effects = if normal
            && (self.executor.initialized_local_binding(binding) || imported_primitive_read)
        {
            EffectCoverageFragment::initialized_module_read()
        } else if normal
            && frame.initialized_bindings.contains(binding)
            && !self.executor.state.world.active_cells.contains_key(binding)
        {
            EffectCoverageFragment::no_effects()
        } else {
            EffectCoverageFragment::default()
        };
        let selection = if self.executor.initialized_local_binding(binding) {
            self.executor
                .state
                .world
                .module
                .prefix_callable_selections
                .get(binding)
                .filter(|selection| selection.matches(&values))
                .cloned()
                .or_else(|| {
                    let key = self.executor.inputs.index.hoisted_bindings.get(binding)?;
                    (self
                        .executor
                        .inputs
                        .index
                        .direct_top_level_functions
                        .contains(binding)
                        && self
                            .executor
                            .local_call_target(&values)
                            .is_some_and(|(target, _)| target == *key))
                    .then(|| {
                        CallableSelection::observed(
                            &values,
                            self.executor.boxed_settled_prefix_dependencies(),
                        )
                    })
                    .flatten()
                })
        } else {
            frame
                .binding_selections
                .get(binding)
                .filter(|selection| selection.matches(&values))
                .cloned()
        };
        Ok(LocalValueResult {
            allocation: super::super::allocation_transfer::AllocationValue::opaque(values),
            effects,
            selection,
        })
    }
}

impl CallContext for LocalExecutionContext<'_, '_, '_> {
    fn read_call_callee(
        &mut self,
        binding: &Id,
        call_span: Span,
        frame: &mut LocalFrame,
    ) -> Result<LocalValueResult, RemainderDisposition> {
        let mut result = self.read_binding(binding, frame)?;
        let selected = matches!(self.scope, LocalExecutionScope::RootInitializer)
            && self
                .outcome
                .root_binding_effect_identity
                .as_ref()
                .is_some_and(|identity| {
                    identity.owner == self.owner
                        && self
                            .executor
                            .inputs
                            .initializer_call_effect_input(
                                self.owner,
                                &identity.binding,
                                binding,
                                call_span,
                            )
                            .is_some()
                });
        // A published foreign call summary has proved the provider binding
        // available to this importer, so reading it as a callee is a normal
        // declarative read under the summary's premises.
        let summary = self
            .calls()
            .iter()
            .find(|call| call.kind == CallKind::Call && call.span == call_span)
            .and_then(|call| self.substituted_call_summary(call));
        if let Some(summary) = summary
            && !result
                .allocation
                .values
                .contains(&AbstractValue::Value(Value::Unavailable))
        {
            result.effects = EffectCoverageFragment::initialized_module_read();
            result.effects.depend_on(&summary.dependencies);
        }
        if selected
            && !result.allocation.values.is_empty()
            && !result
                .allocation
                .values
                .contains(&AbstractValue::Value(Value::Unavailable))
        {
            result.effects = EffectCoverageFragment::initialized_module_read();
        }
        Ok(result)
    }
    fn call(
        &mut self,
        frame: &mut LocalFrame,
        request: &LocalCallRequest,
    ) -> Result<LocalValueResult, RemainderDisposition> {
        if request.invocation == kernel_frame::Invocation::Construct {
            return self.construct(frame, request);
        }
        let LocalCallRequest {
            callee,
            arguments,
            span,
            invocation: _,
        } = request;
        let span = *span;
        if let EvaluatedCallee::Member {
            array_push: Some(selection),
            receiver,
            ..
        } = callee
        {
            let args: Vec<_> = arguments
                .iter()
                .map(|argument| argument.values.clone())
                .collect();
            let call = self
                .calls()
                .iter()
                .find(|call| call.kind == CallKind::Call && call.span == span)
                .expect("a source-covered call has its canonical call record");
            let Some(caller) = self.executor.local_body_identity_for_call(call) else {
                return Err(RemainderDisposition::NeedsSummary);
            };
            if let Some(mut result) = array_transfer::push(self.executor, selection, &args) {
                for cell in result.reads {
                    self.outcome
                        .effects
                        .record_heap_read(cell, self.executor.state.assumption.is_none());
                }
                for cell in result.writes {
                    self.outcome
                        .effects
                        .record_heap_write(cell, self.executor.state.assumption.is_none());
                }
                result.effects.selected_local_call(LocalCallReceipt {
                    caller,
                    call_span: span,
                    target: LocalBodyIdentity::Native {
                        intrinsic: NativeIntrinsic::Array,
                        property: Some("prototype.push".into()),
                    },
                    dependencies: Box::new(result.dependencies),
                });
                return Ok(LocalValueResult {
                    allocation: super::super::allocation_transfer::AllocationValue::opaque(
                        result.values,
                    ),
                    effects: result.effects,
                    selection: None,
                });
            }
            let mut reached = receiver.clone();
            reached.extend(args.into_iter().flatten());
            self.fallback(
                frame,
                reached,
                ExecutionPrecisionLossReason::UnsupportedOperation {
                    operation: ExecutionOperation::OpaqueExecution,
                    ordered_field_read_refusal: None,
                    ordered_call_admission_refusal: None,
                },
                span,
            );
            return Err(RemainderDisposition::AlreadySummarized);
        }
        let targets = callee.targets();
        let mut reached = targets.clone();
        if let Some(receiver) = callee.receiver() {
            reached.extend(receiver.iter().cloned());
        }
        reached.extend(
            arguments
                .iter()
                .flat_map(|argument| &argument.values)
                .cloned(),
        );
        frame
            .borrowed_records
            .extend(self.executor.reachable_objects(&reached));
        // Reuse the canonical call's source policy and proof sites, while
        // transporting the operands selected by the ordered kernel.
        let call = self
            .calls()
            .iter()
            .find(|call| call.kind == CallKind::Call && call.span == span)
            .expect("a source-covered call has its canonical call record")
            .clone();
        let audited_input =
            self.outcome
                .root_binding_effect_identity
                .as_ref()
                .filter(|identity| {
                    matches!(self.scope, LocalExecutionScope::RootInitializer)
                        && identity.owner == self.owner
                })
                .and_then(|identity| match &call.callee {
                    ValueExpr::Binding(callee) => self
                        .executor
                        .inputs
                        .initializer_call_effect_input(self.owner, &identity.binding, callee, span),
                    _ => None,
                })
                .map(|input| input.reference.clone());
        let pure_initializer = audited_input.is_some()
            && self
                .outcome
                .root_binding_effect_identity
                .as_ref()
                .is_some_and(|identity| {
                    self.executor
                        .inputs
                        .declared_pure_initializers
                        .contains(identity.binding.0.as_ref())
                });
        let substituted_summary = self.substituted_call_summary(&call);
        let result = self.executor.request(RuntimeOperation::Call {
            call,
            environment: frame.environment.clone(),
            captures: frame.environment.clone(),
            outcome: Box::new(Outcome {
                complete: true,
                ..Outcome::default()
            }),
            operands: Some(Box::new(CallOperands {
                callee: callee.clone(),
                arguments: arguments
                    .iter()
                    .map(|argument| argument.values.clone())
                    .collect(),
                argument_selections: arguments
                    .iter()
                    .map(|argument| argument.selection.clone())
                    .collect(),
                selected_pure_initializer_call: pure_initializer,
            })),
        });
        let RuntimeValue::Call {
            mut outcome,
            transfer,
        } = result
        else {
            unreachable!("call transfers return effects and their result together");
        };
        let complete = outcome.complete && !outcome.unknown_call;
        let no_child_losses = outcome.effects.precision_losses.is_empty()
            && outcome.effects.uninitialized_reads.is_empty();
        let missing_result = complete && matches!(transfer, CallTransfer::EffectsOnly);
        let admission_refusal = match &transfer {
            CallTransfer::UnsupportedOperands(refusal) => Some(*refusal),
            _ => None,
        };
        if (!complete || admission_refusal.is_some())
            && targets.iter().any(|value| {
                matches!(
                    value,
                    AbstractValue::Value(Value::Top) | AbstractValue::SymbolicInput(_)
                )
            })
        {
            self.outcome.lose_call_target(self.owner, span);
        }
        self.outcome.absorb_call_effects(
            &mut outcome.effects,
            self.executor.state.assumption.as_ref(),
        );
        self.outcome.executed_owners.extend(outcome.executed_owners);
        self.outcome.unknown_call |= outcome.unknown_call;
        self.outcome
            .inherit_completeness(outcome.complete, outcome.effects.precision_losses);
        if let CallTransfer::Values(values) = transfer {
            frame.inputs.extend(values.iter().cloned());
            frame
                .borrowed_records
                .extend(self.executor.reachable_objects(&values));
            if complete {
                // A complete call is not an allocation-only certificate.
                let normal_value = !values.is_empty()
                    && !values.contains(&AbstractValue::Value(Value::Unavailable))
                    && !values.contains(&AbstractValue::Value(Value::Top));
                let effects = if no_child_losses
                    && normal_value
                    && pure_initializer
                    && let Some(reference) = audited_input
                {
                    let mut effects = outcome.effect_coverage.audited_initializer_call_domains(
                        &ProofDependencies {
                            initializer_call_effect_inputs: BTreeSet::from([reference]),
                            ..ProofDependencies::default()
                        },
                    );
                    effects.normal_completion_from_pure_initializer();
                    effects
                } else if let Some(summary) = &substituted_summary {
                    // The call transfer replaced the provider body by this
                    // summary and ran nothing.
                    let mut effects =
                        EffectCoverageFragment::provider_summary(&summary.dependencies);
                    if !normal_value {
                        // An unavailable operand throws before the body runs.
                        effects.uncertain_completion();
                    }
                    effects
                } else {
                    outcome.effect_coverage
                };
                return Ok(LocalValueResult {
                    allocation: super::super::allocation_transfer::AllocationValue::opaque(values),
                    effects,
                    selection: outcome.return_selection,
                });
            }
            let terminal_position = match self.scope {
                LocalExecutionScope::Callable(_) => matches!(
                    self.body.operations.last(),
                    Some(LocalOperation::Return(Some(LocalExpression::Call {
                        span: terminal_span,
                        ..
                    }))) if *terminal_span == span
                ),
                // This canonical root has one direct zero-argument call and
                // no suffix. Other root expressions retain remainder fallback.
                LocalExecutionScope::RootInitializer => {
                    self.body.single_identifier_call_initializer() == Some(span)
                }
                // A constructor body is admitted without calls.
                LocalExecutionScope::Constructor(_) => false,
            };
            let terminal_local_target = (terminal_position
                && matches!(callee, EvaluatedCallee::Direct(_)))
            .then(|| self.executor.local_call_target(targets))
            .flatten()
            .filter(|(key, target)| {
                !target.opaque
                    && !target.generator
                    && !self.executor.inputs.index.async_callables.contains(key)
            });
            if let Some((key, _)) = terminal_local_target {
                // The admitted source call already ran and supplied its
                // conservative effects. This exact return has no caller
                // remainder to summarize: publishing the entire caller frame
                // would additionally expose its merely selected callee.
                // Stop the kernel, retaining uncertainty and every child loss;
                // neither a partial value nor a terminal position is coverage.
                self.outcome.complete = false;
                self.outcome.effect_coverage = EffectCoverageFragment::default();
                let mut possible = values;
                if !self.executor.callable_has_no_normal_return(key) {
                    possible.insert(AbstractValue::Value(Value::Top));
                }
                frame.complete_unknown_return(possible);
                return Err(RemainderDisposition::AlreadySummarized);
            }
            reached.extend(values);
        }
        // Preserve the partial call's effects and returned capabilities, then
        // summarize the remainder without repeating any operand or invocation.
        let fallback_reason = if missing_result {
            ExecutionPrecisionLossReason::MissingCallResult
        } else {
            ExecutionPrecisionLossReason::UnsupportedOperation {
                operation: ExecutionOperation::OpaqueExecution,
                ordered_field_read_refusal: None,
                ordered_call_admission_refusal: if complete { admission_refusal } else { None },
            }
        };
        self.fallback(frame, reached, fallback_reason, span);
        Err(RemainderDisposition::AlreadySummarized)
    }
}

impl HeapContext for LocalExecutionContext<'_, '_, '_> {
    fn index(&self) -> &ExecutionIndex {
        self.executor.inputs.index
    }
    fn world(&self) -> &World {
        &self.executor.state.world
    }
    fn world_mut(&mut self) -> &mut World {
        self.executor.state.world_mut()
    }
    fn current_prerequisites(&self) -> Box<ProofDependencies> {
        self.executor.boxed_settled_prefix_dependencies()
    }
    fn admit_closure(
        &mut self,
        frame: &LocalFrame,
        creation: super::super::local_operations::DeferredCallableCreation,
        captures: &LexicalCaptures,
    ) -> Result<ClosureAdmission, RemainderDisposition> {
        let callable = creation.callable();
        let Some(body) = self.executor.inputs.index.callables.get(&callable) else {
            return Err(RemainderDisposition::NeedsSummary);
        };
        let reads = self
            .executor
            .inputs
            .index
            .prototype_callable_reads
            .get(&callable)
            .unwrap_or(&body.reads);
        if reads
            .iter()
            .any(|binding| missing_activation_binding(self.body, binding, frame))
        {
            self.refuse_activation_value(frame);
            return Err(RemainderDisposition::AlreadySummarized);
        }
        // This exact root creates one fresh ordinary function as the provider
        // call's argument. Its body does not run at creation, even when the
        // deferred body contains syntax the callable interpreter marks opaque.
        // Keep the capture-cell restriction and preserve the closure value so
        // an eager invocation or later escape still follows its usual path.
        let selected_function_argument = body.opaque
            && matches!(
                creation,
                super::super::local_operations::DeferredCallableCreation::Function(_)
            )
            && matches!(self.scope, LocalExecutionScope::RootInitializer)
            && self
                .outcome
                .root_binding_effect_identity
                .as_ref()
                .is_some_and(|identity| {
                    identity.owner == self.owner
                        && identity.initializer_span == self.body.initializer_span()
                        && self
                            .body
                            .single_identifier_call_with_function_argument_initializer()
                            .is_some_and(|(callee, call_span)| {
                                self.executor
                                    .inputs
                                    .initializer_call_effect_input(
                                        self.owner,
                                        &identity.binding,
                                        callee,
                                        call_span,
                                    )
                                    .is_some()
                            })
                });
        let effects = if captures.cells.is_empty()
            && (!body.opaque || selected_function_argument)
            && !body.generator
            && !self
                .executor
                .inputs
                .index
                .async_callables
                .contains(&callable)
        {
            // Creating this checked closure allocates a callable without
            // evaluating its body or touching exterior state. The prefix
            // prerequisites still select its exact captured environment.
            let mut effects = EffectCoverageFragment::no_effects();
            effects.depend_on(&self.executor.settled_prefix_dependencies());
            effects
        } else {
            if !captures.cells.is_empty() {
                self.executor.observations.refuse_owner_coverage(
                    OwnerInitializationCoverageRefusalReason::CallbackCapturesLiveCell,
                );
            }
            EffectCoverageFragment::default()
        };
        Ok(ClosureAdmission {
            effects,
            judgment: super::super::allocation_transfer::AllocationJudgment::Opaque,
        })
    }
    fn intern_captures(&mut self, captures: LexicalCaptures) -> CapturedContext {
        self.executor.intern_captures(captures)
    }
    /// The checked constructor proved each of its own field writes absent from
    /// the class and built-in prototypes, so [[Set]] defines a data property.
    fn constructor_assignment_absence(
        &mut self,
        class: u32,
        property: &str,
    ) -> Option<Box<ProofDependencies>> {
        let LocalExecutionScope::Constructor(scope) = self.scope else {
            return None;
        };
        (scope == class
            && self
                .executor
                .inputs
                .index
                .local_constructors
                .get(&class)
                .is_some_and(|constructor| constructor.own_fields.contains(property))
            && self
                .executor
                .constructor_field_prototype_absent(class, property))
        .then(|| self.executor.boxed_settled_prefix_dependencies())
    }
}

impl NativeContext for LocalExecutionContext<'_, '_, '_> {
    fn native_iterable(
        &mut self,
        operation: NativeIterableUse,
        operand: &Values,
    ) -> Option<NativeIterableReceipt> {
        self.executor.native_iterable(operation, operand)
    }
    fn array_method_callee(
        &mut self,
        receivers: &Values,
        property: &str,
    ) -> Option<(ArrayPushSelection, EffectCoverageFragment)> {
        let (selection, effects) = array_transfer::select_push(self.executor, receivers, property)?;
        for cell in [
            crate::facts::EffectCell::ObjectShape(selection.object),
            crate::facts::EffectCell::OwnProperty {
                origin: selection.object,
                property: "push".into(),
            },
        ] {
            self.outcome
                .effects
                .record_heap_read(cell, self.executor.state.assumption.is_none());
        }
        Some((selection, effects))
    }
    fn native_callee(
        &mut self,
        receiver: &Id,
        property: &str,
        call_span: Span,
    ) -> Option<LocalValueResult> {
        // Only the exact canonical root call has an ordered execution witness.
        if !matches!(self.scope, LocalExecutionScope::RootInitializer) {
            return None;
        }
        let callee = ValueExpr::Member(
            Arc::new(ValueExpr::Binding(receiver.clone())),
            Some(property.to_owned()),
            MemberKey::Static,
            call_span.into(),
        );
        let (_, source) = self.executor.exact_native_call_row(
            CallKind::Call,
            &callee,
            CallSite::from(call_span),
        )?;
        // Reading the intact intrinsic slot is the whole callee evaluation;
        // operand admission and the row's mutations stay with the call.
        let mut effects = EffectCoverageFragment::no_effects();
        effects.depend_on(source.dependencies());
        Some(LocalValueResult {
            allocation: super::super::allocation_transfer::AllocationValue::opaque(unknown()),
            effects,
            selection: None,
        })
    }
    fn native_field_read(
        &mut self,
        frame: &mut LocalFrame,
        receiver: &Id,
        property: &str,
        span: Span,
    ) -> Option<LocalValueResult> {
        // Only the exact canonical root read has an ordered execution witness.
        if !matches!(self.scope, LocalExecutionScope::RootInitializer) {
            return None;
        }
        let callee = ValueExpr::Member(
            Arc::new(ValueExpr::Binding(receiver.clone())),
            Some(property.to_owned()),
            MemberKey::Static,
            span.into(),
        );
        native_own_data_read_slots(&callee, |id| {
            self.executor.inputs.flow.is_declared(id)
                || self.executor.inputs.shadowed.contains(id.0.as_ref())
        })?;
        let data = builtins::data_property(receiver.0.as_ref(), property)?;
        self.calls().iter().find(|call| {
            call.kind == CallKind::LocalMemberRead
                && call.span == span
                && matches!(&call.callee,
                    ValueExpr::Member(source_receiver, Some(source_property), MemberKey::Static, _)
                        if source_property == property
                            && matches!(source_receiver.as_ref(), ValueExpr::Binding(source) if source == receiver))
        })?;
        let premise = self
            .executor
            .inputs
            .index
            .native_own_data_read_premise(span)?;
        let dependencies = premise.dependencies.clone();
        self.executor.record_inference_use(
            NativeIntegrityGuard::NativeCallIdentity,
            premise.inference_use,
            &dependencies,
        );
        if !premise.census {
            let evidence = self
                .executor
                .inputs
                .index
                .native_operation_input_evidence
                .get(&CallSite::from(span))?
                .clone();
            self.executor
                .observations
                .record_native_operation_input_use(
                    span,
                    crate::reports::schema::NativeOperationInputUse {
                        evidence,
                        source_location: None,
                        span,
                    },
                );
        }
        let values = match data.value {
            // A well-known symbol or numeric constant: the slot's value is
            // its kind; the premise proves the slot was never written.
            builtins::PropertyValue::Primitive(kinds) => {
                Values::from([AbstractValue::Value(Value::Prim(kinds))])
            }
            // The source operands are declarative. This preserves the
            // existing value projection without replaying receiver code or
            // authorizing a later invocation through the returned reference.
            builtins::PropertyValue::Function => {
                self.executor
                    .evaluate(&callee, &frame.environment, &frame.environment)
            }
        };
        let mut effects = EffectCoverageFragment::no_effects();
        effects.depend_on(&dependencies);
        Some(LocalValueResult {
            allocation: super::super::allocation_transfer::AllocationValue::opaque(values),
            effects,
            selection: None,
        })
    }
}

impl ControlContext for LocalExecutionContext<'_, '_, '_> {
    type Branch = ExecutionPath;
    fn fork_policy(&self) -> ForkPolicy {
        ForkPolicy::Explore
    }
    fn branch(&self) -> ExecutionPath {
        self.executor
            .state
            .path(&self.executor.allocation_judgment, self.outcome)
    }
    fn enter(&mut self, path: ExecutionPath) {
        let (allocation_judgment, outcome) = self.executor.state.enter(path);
        self.executor.allocation_judgment = allocation_judgment;
        *self.outcome = outcome;
    }
    fn unproved(&mut self, frame: &mut LocalFrame, what: Unproved<'_>) -> Disposition {
        match what {
            Unproved::FieldRead {
                receiver,
                receivers,
                property,
                span,
                refusal,
            } => {
                if matches!(self.scope, LocalExecutionScope::RootInitializer)
                    && let Some(read) = self.executor.present_own_data_read(receivers, property)
                {
                    self.outcome
                        .effects
                        .record_heap_read(read.cell, self.executor.state.assumption.is_none());
                    return Disposition::Resolved(
                        super::super::allocation_transfer::AllocationValue::opaque(read.values),
                    );
                }
                let callee = ValueExpr::Member(
                    Arc::new(ValueExpr::Binding(receiver.clone())),
                    Some(property.to_owned()),
                    MemberKey::Static,
                    span.into(),
                );
                let lookup = self
                    .calls()
                    .iter()
                    .find(|call| call.kind == CallKind::AccessorCalleeLookup && call.span == span);
                let call = match lookup {
                    Some(lookup)
                        if self.executor.callee_lookup_is_certified(lookup)
                            || (property == "push"
                                && self
                                    .executor
                                    .dense_array_push_receiver(receivers)
                                    .is_some()) =>
                    {
                        lookup.clone()
                    }
                    _ => Call::member_read(self.owner, self.scope.caller(), callee, span),
                };
                // Retain established native, array and borrowed-record read proofs.
                // The receiver is only a declarative cell, so the value projection
                // cannot replay receiver code. Its judgment stays opaque; execution
                // completeness is not a source or provider coverage certificate.
                if self.executor.execute_call(
                    &call,
                    &frame.environment,
                    &frame.environment,
                    self.outcome,
                ) && !self.outcome.unknown_call
                {
                    let values = self.executor.evaluate(
                        &call.callee,
                        &frame.environment,
                        &frame.environment,
                    );
                    frame.inputs.extend(values.iter().cloned());
                    frame
                        .borrowed_records
                        .extend(self.executor.reachable_objects(&values));
                    return Disposition::Resolved(
                        super::super::allocation_transfer::AllocationValue::opaque(values),
                    );
                }
                self.fallback(
                    frame,
                    receivers.clone(),
                    ExecutionPrecisionLossReason::UnsupportedOperation {
                        operation: ExecutionOperation::MemberRead,
                        ordered_field_read_refusal: Some(refusal),
                        ordered_call_admission_refusal: None,
                    },
                    span,
                );
                Disposition::Summarized
            }
            Unproved::FieldWrite {
                receivers,
                values,
                span,
            } => {
                let mut reached = receivers.clone();
                reached.extend(values.iter().cloned());
                self.fallback(
                    frame,
                    reached,
                    ExecutionPrecisionLossReason::UnsupportedOperation {
                        operation: ExecutionOperation::PropertyWrite,
                        ordered_field_read_refusal: None,
                        ordered_call_admission_refusal: None,
                    },
                    span,
                );
                Disposition::Summarized
            }
            Unproved::CaptureStore { values, span } => {
                self.fallback(
                    frame,
                    values.clone(),
                    ExecutionPrecisionLossReason::UnsupportedOperation {
                        operation: ExecutionOperation::OpaqueExecution,
                        ordered_field_read_refusal: None,
                        ordered_call_admission_refusal: None,
                    },
                    span,
                );
                Disposition::Summarized
            }
            Unproved::Operator { operands, span } => {
                self.fallback(
                    frame,
                    operands.clone(),
                    ExecutionPrecisionLossReason::UnsupportedOperation {
                        operation: ExecutionOperation::ImplicitCoercion,
                        ordered_field_read_refusal: None,
                        ordered_call_admission_refusal: None,
                    },
                    span,
                );
                Disposition::Summarized
            }
            Unproved::IterateSpread { operand, span } => {
                // Consuming an unproved iterator can execute deferred bodies;
                // the operand and everything it reaches are summarized.
                self.fallback(
                    frame,
                    operand.clone(),
                    ExecutionPrecisionLossReason::UnsupportedOperation {
                        operation: ExecutionOperation::ArraySpread,
                        ordered_field_read_refusal: None,
                        ordered_call_admission_refusal: None,
                    },
                    span,
                );
                Disposition::Summarized
            }
        }
    }
}
