//! The application adapter: the five kernel context traits over the
//! application `Executor`, for ordered root initializers and callable bodies.

use super::super::alternatives::execution_state::ExecutionPath;
use super::super::array_transfer::{self, ArrayMethodSelection};
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
use crate::facts::abstract_domain::builtins;
use crate::facts::abstract_domain::value::{Kind, Value};
use crate::facts::evaluation_effects::{EvaluationEffects, GlobalAccessKind};
use crate::program_execution::RuntimeAllocationId;
use crate::reports::schema::NativeIntegrityGuard;
use source_identity::SourceUnitOrdinal;
use std::collections::BTreeSet;
use std::sync::Arc;
use swc_common::{DUMMY_SP, Span};
use swc_ecma_ast::*;

#[derive(Clone, Copy)]
pub(in crate::facts::sync_effects) enum LocalExecutionScope {
    Callable(RuntimeAllocationId),
    /// A root owner's variable initializer or expression statement.
    Root,
    /// The lowered base-constructor body of this class, run by the kernel's
    /// construct rule. It has no call operation of its own.
    Constructor(RuntimeAllocationId),
}

impl LocalExecutionScope {
    pub(in crate::facts::sync_effects) fn caller(self) -> Option<RuntimeAllocationId> {
        match self {
            Self::Callable(key) => Some(key),
            Self::Root | Self::Constructor(_) => None,
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
    fn ordinary_record_shape(&self, allocation: RuntimeAllocationId) -> Option<&OwnPropertyShape> {
        self.executor.ordinary_record_shape(allocation)
    }

    fn object_properties_are_settled(&self, allocation: RuntimeAllocationId) -> bool {
        self.executor.object_properties_are_settled(allocation)
    }

    fn property_values(&self, allocation: RuntimeAllocationId, property: &str) -> Option<&Values> {
        self.executor.property_values(allocation, property)
    }

    fn property_is_accessor(&self, allocation: RuntimeAllocationId, property: &str) -> bool {
        self.executor.property_is_accessor(allocation, property)
    }

    fn property_is_writable(&self, allocation: RuntimeAllocationId, property: &str) -> bool {
        self.executor.property_is_writable(allocation, property)
    }
}

impl<'source> LocalExecutionContext<'_, 'source, '_> {
    pub(in crate::facts::sync_effects) fn calls(&self) -> &'source [Call] {
        match self.scope {
            LocalExecutionScope::Callable(key) => {
                &self
                    .executor
                    .source_index()
                    .callables
                    .get(&key)
                    .expect("kernel callable activation checks its source index")
                    .calls
            }
            LocalExecutionScope::Root => &self.executor.source_index().top_calls[self.owner.0],
            LocalExecutionScope::Constructor(_) => &[],
        }
    }

    fn binding_values<'a>(&'a self, binding: &Id, frame: &'a LocalFrame) -> Option<&'a Values> {
        if self.executor.source_inputs().top_bindings.contains(binding) {
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
                || call.caller
                    != self
                        .scope
                        .caller()
                        .and_then(|key| self.executor.source_index().local_origin(key))
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
                &self
                    .executor
                    .source_index()
                    .callables
                    .get(&key)
                    .expect("kernel callable activation checks its source index")
                    .member_value_writes
            }
            LocalExecutionScope::Root => {
                &self.executor.source_index().top_member_value_writes[self.owner.0]
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
            .and_then(|key| self.executor.source_index().callable_spans.get(&key))
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

    /// The summarized remainder of the truncated body may read, store and
    /// pass anything it mentions before it completes: the current values of
    /// the bindings it references, and the functions it creates (a
    /// descriptor's `value`, a stored callback), none of which has a value
    /// yet. Kept out of line for the kernel's recursion depth.
    #[inline(never)]
    fn extend_with_remainder(&self, frame: &LocalFrame, span: Span, reached: &mut Values) {
        let index = self.executor.source_index();
        // A constructor body lies inside its statement.
        let body = self
            .scope
            .caller()
            .and_then(|key| index.callable_spans.get(&key).copied())
            .unwrap_or_else(|| index.statement_span(self.owner));
        reached.extend(
            index
                .callable_spans
                .iter()
                .filter(|(key, callable)| {
                    callable.lo >= span.hi
                        && callable.hi <= body.hi
                        && index
                            .callables
                            .get(key)
                            .is_some_and(|callable| callable.owner == self.owner)
                })
                .map(|(key, _)| {
                    AbstractValue::Value(Value::Alloc(
                        Kind::Function,
                        self.executor.source_index().allocation(*key),
                    ))
                }),
        );
        for binding in index.references_within(span.hi.0, body.hi.0) {
            if let Some(values) = self.binding_values(binding, frame) {
                reached.extend(values.iter().cloned());
            }
        }
    }

    pub(in crate::facts::sync_effects) fn fallback(
        &mut self,
        frame: &LocalFrame,
        reached: Values,
        reason: ExecutionPrecisionLossReason,
        span: Span,
    ) {
        self.retain_global_fallback_accesses(frame, &reason, span);
        let mut reached = reached;
        self.extend_with_remainder(frame, span, &mut reached);
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
                self.outcome.lose_call_target(
                    self.executor.source_inputs().module_id,
                    call.owner,
                    call.span,
                );
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
            .or_else(|| self.executor.retaining_factory_summary(call).cloned())
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
            captured: &self.executor.source_inputs().captured_local_bindings,
        }
    }
    fn capture_policy(&self) -> CapturePolicy<'_> {
        self.executor.source_inputs().capture_policy()
    }
    fn cell_effect(&self) -> EffectCoverageFragment {
        // Creating/initializing an activation-local binding, or storing to
        // an uncaptured one, invokes no user code and touches no exterior
        // cell. Evaluation of the stored value is covered separately.
        EffectCoverageFragment::no_effects()
    }
    /// Only an admitted root statement stores to a module binding: a root
    /// initializer has its own binding identity, and a callable's store
    /// would need the closure's view of the binding.
    fn store_module_binding(
        &mut self,
        binding: &Id,
        values: &Values,
    ) -> Option<EffectCoverageFragment> {
        if !matches!(self.scope, LocalExecutionScope::Root)
            || self.outcome.root_binding_effect_identity.is_some()
            || !self.executor.source_inputs().top_bindings.contains(binding)
            || self
                .executor
                .module_binding_store_refusal(binding)
                .is_some()
        {
            return None;
        }
        // The flattened lane's join, which the owner's assignment phase
        // repeats: the statement may be one of several writers.
        if self
            .executor
            .source_inputs()
            .needed_bindings
            .contains(binding)
        {
            let world = self.executor.state.world_mut();
            let possible = world.module.prefix_values.entry_or_default(binding);
            let before = possible.len();
            possible.extend(values.iter().cloned());
            if possible.len() != before {
                world.revision.bump_prefix();
            }
        }
        Some(EffectCoverageFragment::module_binding_store())
    }
    fn module_namespace_member(
        &mut self,
        receivers: &Values,
        property: &str,
    ) -> Option<LocalValueResult> {
        let mut receivers = receivers.iter();
        let AbstractValue::ModuleNamespace(scope) = receivers.next()? else {
            return None;
        };
        if receivers.next().is_some() {
            return None;
        }
        let super::super::interpreter::program_activation::ProgramMemberRead {
            values,
            initialized,
            target,
        } = self.executor.namespace_member(*scope, property)?;
        if let Some(target) = &target {
            let projection = self.executor.source_index().source_scope();
            self.executor.record_program_cell_read(
                target,
                self.executor
                    .state
                    .runtime_effect_owner(projection, self.owner),
                !initialized,
                self.outcome,
            );
        }
        let selection =
            target.and_then(|target| self.executor.program_cell_selection(&target, &values));
        Some(LocalValueResult {
            allocation: super::super::allocation_transfer::AllocationValue::opaque(values),
            effects: if initialized {
                EffectCoverageFragment::initialized_module_read()
            } else {
                EffectCoverageFragment::default()
            },
            selection,
        })
    }
    fn prefix_tracked_receiver(&self, binding: &Id, receivers: &Values) -> bool {
        self.executor.initialized_local_binding(binding)
            || self.executor.initialized_import_binding(binding)
            || (!receivers.is_empty()
                && receivers.iter().all(|value| {
                    matches!(value, AbstractValue::Value(Value::Alloc(Kind::Object, record))
                        if self.executor.state.iteration_element_records.contains(record))
                }))
    }
    fn read_binding(
        &mut self,
        binding: &Id,
        frame: &mut LocalFrame,
    ) -> Result<LocalValueResult, RemainderDisposition> {
        if matches!(self.scope, LocalExecutionScope::Callable(_))
            && binding.0.as_ref() == "arguments"
            && !self.body.bindings.contains_key(binding)
        {
            // The implicit object can expose every actual input, including
            // inputs with no formal parameter. Its first read truncates this
            // activation; earlier operations keep their own provenance.
            self.fallback(
                frame,
                Values::new(),
                ExecutionPrecisionLossReason::UnsupportedOperation {
                    operation: ExecutionOperation::CallableInputs,
                    ordered_field_read_refusal: None,
                    ordered_call_admission_refusal: None,
                },
                DUMMY_SP,
            );
            return Err(RemainderDisposition::AlreadySummarized);
        }
        if missing_activation_binding(self.body, binding, frame) {
            self.refuse_activation_value(frame);
            return Err(RemainderDisposition::AlreadySummarized);
        }
        if self.executor.source_inputs().top_bindings.contains(binding)
            || self
                .executor
                .source_index()
                .imported_bindings
                .contains(binding)
        {
            self.executor
                .promote_top_effects(&[binding], &[], self.owner, self.outcome);
        }
        let values = self.executor.evaluate_binding(binding, &frame.environment);
        if self.executor.source_inputs().top_bindings.contains(binding)
            || self
                .executor
                .source_index()
                .imported_bindings
                .contains(binding)
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
                .source_inputs()
                .imported_primitive_read(binding)
                .is_some_and(|read| {
                    values == Values::from([AbstractValue::Value(Value::Prim(read.kinds))])
                });
        let effects = if normal
            && (self.executor.initialized_local_binding(binding)
                || self.executor.initialized_import_binding(binding)
                || imported_primitive_read)
        {
            EffectCoverageFragment::initialized_module_read()
        } else if normal
            && frame.initialized_bindings.contains(binding)
            && self.body.bindings.contains_key(binding)
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
                    let key = self.executor.source_index().hoisted_bindings.get(binding)?;
                    (self
                        .executor
                        .source_index()
                        .direct_top_level_functions
                        .contains(binding)
                        && self
                            .executor
                            .local_call_target(&values)
                            .is_some_and(|(target, _)| {
                                self.executor.source_index().local_origin(target) == Some(*key)
                            }))
                    .then(|| {
                        CallableSelection::observed(
                            &values,
                            self.executor.boxed_settled_prefix_dependencies(),
                        )
                    })
                    .flatten()
                })
        } else if self.executor.initialized_import_binding(binding) {
            self.executor.imported_callable_selection(binding, &values)
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
        let selected = matches!(self.scope, LocalExecutionScope::Root)
            && self
                .outcome
                .root_binding_effect_identity
                .as_ref()
                .is_some_and(|identity| {
                    identity.owner == self.owner
                        && self
                            .executor
                            .source_inputs()
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
            array_method: Some(selection),
            receiver,
            ..
        } = callee
        {
            let selection = match selection.as_ref() {
                ArrayMethodSelection::Push(selection) => selection,
                ArrayMethodSelection::Iteration(selection) => {
                    return self.iterate_array(frame, selection, arguments, span);
                }
            };
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
                    self.outcome.record_runtime_heap_read(
                        self.executor.inputs.root.index.source_scope(),
                        cell,
                        self.executor.state.assumption.is_none(),
                    );
                }
                for cell in result.writes {
                    self.outcome.record_runtime_heap_write(
                        self.executor.inputs.root.index.source_scope(),
                        cell,
                        self.executor.state.assumption.is_none(),
                    );
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
        let audited_input = self
            .outcome
            .root_binding_effect_identity
            .as_ref()
            .filter(|identity| {
                matches!(self.scope, LocalExecutionScope::Root) && identity.owner == self.owner
            })
            .and_then(|identity| match &call.callee {
                ValueExpr::Binding(callee) => self
                    .executor
                    .source_inputs()
                    .initializer_call_effect_input(self.owner, &identity.binding, callee, span),
                _ => None,
            });
        let selected_const_factory_result =
            audited_input.is_some_and(|input| input.returns_opaque_function_from_const_primitive);
        let audited_input = audited_input.map(|input| input.reference.clone());
        let pure_initializer = audited_input.is_some()
            && self
                .outcome
                .root_binding_effect_identity
                .as_ref()
                .is_some_and(|identity| {
                    self.executor
                        .source_inputs()
                        .declared_pure_initializers
                        .contains(identity.binding.0.as_ref())
                });
        let substituted_summary = self.substituted_call_summary(&call);
        let caller = self.executor.local_body_identity_for_call(&call);
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
                selected_const_factory_result,
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
            && !outcome.runtime_binding_effects.has_uninitialized_reads();
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
            self.outcome.lose_call_target(
                self.executor.source_inputs().module_id,
                self.owner,
                span,
            );
        }
        self.outcome
            .absorb_call_outcome(&mut outcome, self.executor.state.assumption.as_ref());
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
                    if let Some(caller) = caller {
                        effects.selected_local_call(LocalCallReceipt {
                            caller,
                            call_span: span,
                            target: LocalBodyIdentity::Imported {
                                defining_module: summary.defining_module.clone(),
                                export: summary.export.clone(),
                            },
                            dependencies: Box::new(summary.dependencies.clone()),
                        });
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
                LocalExecutionScope::Root => {
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
                    && self
                        .executor
                        .allocation_index(*key)
                        .is_some_and(|index| !index.async_callables.contains(key))
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
        self.executor.source_index()
    }
    fn instantiate_allocation(
        &mut self,
        local: u32,
    ) -> crate::program_execution::RuntimeAllocationId {
        self.executor.instantiate_allocation(local)
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
        _captures: &LexicalCaptures,
    ) -> Result<ClosureAdmission, RemainderDisposition> {
        let callable = creation.callable();
        let Some(body) = self.executor.source_index().callables.get(&callable) else {
            return Err(RemainderDisposition::NeedsSummary);
        };
        let reads = self
            .executor
            .source_index()
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
        // The kernel checked this exact function/arrow/method creation. Its
        // body and parameter defaults do not execute, including async and
        // generator bodies and syntax invocation cannot model. Captures keep
        // live cell identities; creation performs no GetBindingValue on them.
        // Later initialization, reads, writes and invocation retain their own
        // guards and effects rather than inheriting this creation receipt.
        let mut effects = EffectCoverageFragment::no_effects();
        effects.depend_on(&self.executor.settled_prefix_dependencies());
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
        class: RuntimeAllocationId,
        property: &str,
    ) -> Option<Box<ProofDependencies>> {
        let LocalExecutionScope::Constructor(scope) = self.scope else {
            return None;
        };
        (scope == class
            && self
                .executor
                .source_index()
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
    ) -> Option<(ArrayMethodSelection, EffectCoverageFragment)> {
        let (selection, effects) =
            array_transfer::select_method(self.executor, receivers, property)?;
        for cell in [
            crate::facts::EffectCell::ObjectShape(selection.object()),
            crate::facts::EffectCell::OwnProperty {
                origin: selection.object(),
                property: selection.property().into(),
            },
        ] {
            self.outcome.record_runtime_heap_read(
                self.executor.inputs.root.index.source_scope(),
                cell,
                self.executor.state.assumption.is_none(),
            );
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
        if !matches!(self.scope, LocalExecutionScope::Root) {
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
        if !matches!(self.scope, LocalExecutionScope::Root) {
            return None;
        }
        let callee = ValueExpr::Member(
            Arc::new(ValueExpr::Binding(receiver.clone())),
            Some(property.to_owned()),
            MemberKey::Static,
            span.into(),
        );
        native_own_data_read_slots(&callee, |id| {
            self.executor.source_inputs().flow.is_declared(id)
                || self
                    .executor
                    .source_inputs()
                    .shadowed
                    .contains(id.0.as_ref())
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
            .source_index()
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
                .source_index()
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
                if matches!(self.scope, LocalExecutionScope::Root)
                    && let Some(read) = self.executor.present_own_data_read(receivers, property)
                {
                    self.outcome.record_runtime_heap_read(
                        self.executor.inputs.root.index.source_scope(),
                        read.cell,
                        self.executor.state.assumption.is_none(),
                    );
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
                    _ => Call::member_read(
                        self.owner,
                        self.scope
                            .caller()
                            .and_then(|key| self.executor.source_index().local_origin(key)),
                        callee,
                        span,
                    ),
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
            Unproved::CaptureStore {
                binding,
                values,
                span,
            } => {
                if self.executor.initialized_local_binding(binding)
                    && !self
                        .executor
                        .source_index()
                        .top_const_bindings
                        .contains(binding)
                {
                    // SetMutableBinding was reached with a value and an
                    // initialized mutable module cell. We cannot continue its
                    // value transfer, but its lexical write already happened;
                    // the remainder's havoc cannot turn it into a hypothetical
                    // callback effect or confer an operation receipt.
                    let source = self.executor.source_index().source_scope();
                    self.outcome.record_runtime_binding_rebind(
                        source,
                        source.binding(binding.clone()),
                        self.executor.state.runtime_effect_owner(source, self.owner),
                        self.executor.state.assumption.as_ref(),
                    );
                }
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
