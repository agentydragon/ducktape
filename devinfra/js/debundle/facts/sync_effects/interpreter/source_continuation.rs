//! Runtime ownership of shared-kernel continuations. Every bind installs the
//! callee leaf's world before it resumes the caller's unevaluated suffix.

#[cfg(test)]
#[path = "source_continuation_tests.rs"]
mod tests;

use super::*;
use local_transfer::{LocalCallRequest, LocalContinuation, LocalSuspension};

#[cfg(test)]
pub(super) fn diagnostic(message: String) {
    tests::observe_diagnostic(message);
}

struct OrderedCallBinding<'a> {
    transfer: Option<CallTransfer>,
    receipt: Option<LocalCallReceipt>,
    request: &'a LocalCallRequest,
    prefix: Outcome,
    audited_initializer: Option<crate::analysis_inputs::InitializerCallEffectInputRef>,
    body: &'a LocalBody,
    owner: SourceUnitOrdinal,
    scope: LocalExecutionScope,
    constructor: Option<ConstructorExecution>,
}

struct SourceLocalCallTarget {
    key: u32,
    captured: Option<CapturedContext>,
    receiver: Option<Values>,
    receipt: Option<LocalCallReceipt>,
}

#[cfg(test)]
pub(super) fn observe_array_callback(
    executor: &Executor<'_, '_>,
    index: usize,
    input_branch: AlternativeBranchId,
    callback_branch: AlternativeBranchId,
    outcome: &Outcome,
) {
    tests::observe_array_callback(executor, index, input_branch, callback_branch, outcome);
}

#[cfg(test)]
pub(super) fn observe_complete(
    executor: &Executor<'_, '_>,
    scope: LocalExecutionScope,
    outcome: &Outcome,
) {
    tests::observe_complete(executor, scope, outcome);
}

#[cfg(test)]
pub(super) fn observe_class_member_promotion(
    executor: &Executor<'_, '_>,
    class: ClassDefinitionId,
) {
    tests::observe_class_member_promotion(executor, class);
}

#[cfg(test)]
pub(super) fn observe_call_receipt(site: &SourceOperationSite, selected: bool) {
    tests::observe_call_receipt(site, selected);
}

impl Executor<'_, '_> {
    pub(super) fn resume_source_continuation(
        &mut self,
        body: &LocalBody,
        mut continuation: LocalContinuation,
        mut outcome: Outcome,
        owner: SourceUnitOrdinal,
        scope: LocalExecutionScope,
        constructor: Option<ConstructorExecution>,
    ) -> RuntimeTransferFamily {
        loop {
            let suspension = continuation.poll(
                &mut LocalExecutionContext {
                    executor: self,
                    outcome: &mut outcome,
                    owner,
                    scope,
                    body,
                    retained_query: None,
                },
                body,
            );
            match suspension {
                LocalSuspension::Complete(attempt) => {
                    #[cfg(test)]
                    tests::observe_activation(scope, &continuation.frame.activation);
                    if let Some(constructor) = constructor.clone() {
                        return self.finish_constructor_continuation(
                            constructor,
                            body,
                            continuation,
                            outcome,
                            attempt,
                        );
                    }
                    let outcome = self.finish_local_body(
                        body,
                        continuation.frame,
                        outcome,
                        attempt,
                        super::LocalBodyCompletionContext {
                            owner,
                            scope,
                            retaining: false,
                        },
                    );
                    #[cfg(test)]
                    tests::observe_complete(self, scope, &outcome);
                    return RuntimeTransferFamily::capture(self, outcome);
                }
                LocalSuspension::Fork(branches) => {
                    #[cfg(test)]
                    tests::observe_fork(scope, branches.len());
                    let family = RuntimeTransferFamily::fork(self, outcome);
                    let mut branches = branches.into_iter();
                    return family.bind(self, |executor, prefix| {
                        let continuation =
                            branches.next().expect("one continuation per branch world");
                        executor.resume_source_continuation(
                            body,
                            continuation,
                            prefix,
                            owner,
                            scope,
                            constructor.clone(),
                        )
                    });
                }
                LocalSuspension::Call(call) => {
                    #[cfg(test)]
                    let mut call = call;
                    #[cfg(test)]
                    let forced_unselected = tests::drop_selection_for(self, &mut call);
                    #[cfg(test)]
                    tests::observe_call(
                        self,
                        &call,
                        forced_unselected,
                        continuation.pending_argument_context(),
                    );
                    // The checkpoint owns callee, receiver, evaluated operands
                    // and lexical continuations throughout the child transfer.
                    // No collector may run until all owner transfers unwind.
                    continuation
                        .frame
                        .inputs
                        .extend(continuation.retained_values());
                    let Some(canonical_call) = self.canonical_source_call(&call, scope, owner)
                    else {
                        let mut context = LocalExecutionContext {
                            executor: self,
                            outcome: &mut outcome,
                            owner,
                            scope,
                            body,
                            retained_query: None,
                        };
                        context.fallback(
                            &continuation.frame,
                            call_reached_values(&call),
                            ExecutionPrecisionLossReason::UnsupportedOperation {
                                operation: ExecutionOperation::OpaqueExecution,
                                ordered_field_read_refusal: None,
                                ordered_call_admission_refusal: None,
                                object_spread_refusal: None,
                            },
                            call.span,
                        );
                        continuation.resume_call(&mut context, None);
                        continue;
                    };
                    if !self.source_call_request_matches(&call, &canonical_call) {
                        let mut context = LocalExecutionContext {
                            executor: self,
                            outcome: &mut outcome,
                            owner,
                            scope,
                            body,
                            retained_query: None,
                        };
                        context.fallback(
                            &continuation.frame,
                            call_reached_values(&call),
                            ExecutionPrecisionLossReason::UnsupportedOperation {
                                operation: ExecutionOperation::OpaqueExecution,
                                ordered_field_read_refusal: None,
                                ordered_call_admission_refusal: None,
                                object_spread_refusal: None,
                            },
                            call.span,
                        );
                        continuation.resume_call(&mut context, None);
                        continue;
                    }
                    if let Some((class, receipt)) =
                        self.source_local_constructor_target(&call, &canonical_call)
                    {
                        let inputs = call_reached_values(&call);
                        continuation.frame.inputs.extend(inputs.iter().cloned());
                        continuation
                            .frame
                            .borrowed_records
                            .extend(self.reachable_objects(&inputs));
                        let selected_arguments = call
                            .arguments
                            .iter()
                            .map(|argument| SelectedValue {
                                values: argument.values.clone(),
                                selection: argument.selection.clone(),
                                postcondition: None,
                            })
                            .collect::<Vec<_>>();
                        if let Ok(family) =
                            self.construct_local_class_family(class, selected_arguments)
                        {
                            return self.bind_ordered_call_family(
                                family.with_continuation(continuation),
                                OrderedCallBinding {
                                    transfer: None,
                                    receipt,
                                    request: &call,
                                    prefix: outcome,
                                    audited_initializer: None,
                                    body,
                                    owner,
                                    scope,
                                    constructor: constructor.clone(),
                                },
                            );
                        }
                        // The checked source constructor declined before any
                        // construction effects. The established call route may
                        // now handle an unsupported class shape exactly once.
                    }
                    let local_target = self.source_local_call_target(&call, &canonical_call);
                    if let Some(SourceLocalCallTarget {
                        key,
                        captured,
                        receiver,
                        receipt,
                    }) = local_target
                    {
                        let inputs = call_reached_values(&call);
                        continuation.frame.inputs.extend(inputs.iter().cloned());
                        continuation
                            .frame
                            .borrowed_records
                            .extend(self.reachable_objects(&inputs));
                        let family = self.invoke_selected(super::InvocationInput {
                            key,
                            args: call
                                .arguments
                                .iter()
                                .map(|argument| argument.values.clone())
                                .collect(),
                            argument_selections: call
                                .arguments
                                .iter()
                                .map(|argument| argument.selection.clone())
                                .collect(),
                            captured,
                            fresh_checked_constructor: false,
                            receiver,
                            continuation: Some(Box::new(continuation)),
                        });
                        return self.bind_ordered_call_family(
                            family,
                            OrderedCallBinding {
                                transfer: None,
                                receipt,
                                request: &call,
                                prefix: outcome,
                                audited_initializer: None,
                                body,
                                owner,
                                scope,
                                constructor: constructor.clone(),
                            },
                        );
                    }
                    if call.spread_expanded
                        && !matches!(&call.callee, EvaluatedCallee::FunctionCallMethod { .. })
                    {
                        // The generic Call adapter does not own the
                        // canonical spread-site correspondence. A direct
                        // spread is executable only through the source-local
                        // path above; otherwise consume the evaluated prefix
                        // once instead of replaying source arguments.
                        let mut context = LocalExecutionContext {
                            executor: self,
                            outcome: &mut outcome,
                            owner,
                            scope,
                            body,
                            retained_query: None,
                        };
                        context.fallback(
                            &continuation.frame,
                            call_reached_values(&call),
                            ExecutionPrecisionLossReason::UnsupportedOperation {
                                operation: ExecutionOperation::OpaqueExecution,
                                ordered_field_read_refusal: None,
                                ordered_call_admission_refusal: None,
                                object_spread_refusal: None,
                            },
                            call.span,
                        );
                        continuation.resume_call(&mut context, None);
                        continue;
                    }
                    let audited_initializer =
                        self.audited_initializer_reference(&canonical_call, &outcome, owner, scope);
                    let pure_initializer = audited_initializer.is_some()
                        && outcome.root_effect_identity.as_ref().is_some_and(|identity| {
                            matches!(
                                &identity.source,
                                RootEffectSource::BindingInitializer { binding, .. }
                                    if self.inputs.declared_pure_initializers.contains(binding.0.as_ref())
                            )
                        });
                    let result = self.request(RuntimeOperation::Call {
                        call: canonical_call,
                        environment: continuation.frame.environment.clone(),
                        captures: continuation.frame.environment.clone(),
                        outcome: Box::new(Outcome {
                            complete: true,
                            ..Outcome::default()
                        }),
                        operands: Some(Box::new(CallOperands {
                            callee: call.callee.clone(),
                            arguments: call.arguments.clone(),
                            selected_pure_initializer_call: pure_initializer,
                        })),
                        continuation: Some(Box::new(continuation)),
                    });
                    let RuntimeValue::Call { family, transfer } = result else {
                        unreachable!("call runtime operations return their family")
                    };
                    let mut family = *family;
                    family.reanchor(self);
                    return self.bind_ordered_call_family(
                        family,
                        OrderedCallBinding {
                            transfer: Some(transfer),
                            receipt: None,
                            request: &call,
                            prefix: outcome,
                            audited_initializer,
                            body,
                            owner,
                            scope,
                            constructor,
                        },
                    );
                }
            }
        }
    }

    fn bind_ordered_call_family(
        &mut self,
        family: RuntimeTransferFamily,
        binding: OrderedCallBinding<'_>,
    ) -> RuntimeTransferFamily {
        let OrderedCallBinding {
            transfer,
            receipt,
            request,
            prefix,
            audited_initializer,
            body,
            owner,
            scope,
            constructor,
        } = binding;
        let terminal_discarded_call =
            matches!(transfer.as_ref(), Some(CallTransfer::SummarizedOpaque(_)))
                && request.invocation == ForeignInvocation::Call
                && matches!(scope, LocalExecutionScope::Root)
                && body.terminal_call_root() == Some(request.span)
                && prefix
                    .root_effect_identity
                    .as_ref()
                    .is_some_and(|identity| {
                        matches!(
                            identity.source,
                            RootEffectSource::ExpressionStatement { .. }
                        )
                    });
        let pure_initializer = audited_initializer.is_some()
            && prefix
                .root_effect_identity
                .as_ref()
                .is_some_and(|identity| {
                    matches!(
                        &identity.source,
                        RootEffectSource::BindingInitializer { binding, .. }
                            if self.inputs.declared_pure_initializers.contains(binding.0.as_ref())
                    )
                });

        family.bind_continuations(self, move |executor, mut child, mut caller| {
            let mut outcome = prefix.clone();
            if let Some(values) = child.normal_values() {
                caller.frame.inputs.extend(values.iter().cloned());
                caller
                    .frame
                    .borrowed_records
                    .extend(executor.reachable_objects(values));
            }
            caller
                .frame
                .reached_values
                .extend(child.reached.iter().cloned());

            #[cfg(test)]
            tests::observe_call_receipt(&request.source_site, receipt.is_some());
            if let Some(receipt) = &receipt {
                child.ledger.effects.depend_on(&receipt.dependencies);
                child.ledger.effects.selected_local_call(receipt.clone());
            }
            let current_body = match scope {
                LocalExecutionScope::Callable(key) => Some(LocalBodyIdentity::Callable {
                    key,
                    owner: executor.inputs.index.callables[&key].owner,
                }),
                LocalExecutionScope::Construction(class) => {
                    Some(LocalBodyIdentity::Construction { class, owner })
                }
                LocalExecutionScope::Root => outcome
                    .root_effect_identity
                    .as_ref()
                    .map(|identity| identity.body_identity()),
            };
            let exact_child_throw_site = matches!(
                &child.ledger.completion,
                Completion::Abrupt(thrown)
                    if executor.throw_origin_is_current(&thrown.origin)
            );
            if child.complete
                && request.source_site.step == OperationStep::CallInvocation
                && request.source_site.body.source_owner() == Some(request.source_site.owner)
                && current_body.as_ref() == Some(&request.source_site.body)
                && executor
                    .inputs
                    .index
                    .source_operation_sites
                    .contains(&request.source_site)
                && exact_child_throw_site
                && child.normal_values().is_none()
                && child.ledger.known_abrupt_dependencies().is_some()
                && let Completion::Abrupt(thrown) = &mut child.ledger.completion
            {
                thrown.frontier = request.source_site.clone();
            }

            let complete = child.complete && !child.unknown_call;
            let retained = !child.unknown_call
                && child.effects.uninitialized_reads.is_empty()
                && child
                    .ledger
                    .effects
                    .covers_retained_loss_domains(&child.effects.precision_losses);
            let no_child_losses = child.effects.precision_losses.is_empty()
                && child.effects.uninitialized_reads.is_empty();
            outcome.absorb_call_effects(
                &mut child.effects,
                executor.state.assumption.as_ref(),
                executor.state.heap_access_origin,
            );
            outcome.executed_owners.append(&mut child.executed_owners);
            outcome.unknown_call |= child.unknown_call;
            outcome.inherit_completeness(
                child.complete,
                std::mem::take(&mut child.effects.precision_losses),
            );

            let exact_no_normal = child.ledger.completion.has_no_normal_path();
            let transfer_has_result = matches!(
                transfer.as_ref(),
                None | Some(CallTransfer::ResultAvailable)
            );
            let values = child.normal_values().cloned().unwrap_or_default();
            let terminal_position = match scope {
                LocalExecutionScope::Callable(_) => matches!(
                    body.operations.last(),
                    Some(operation)
                        if matches!(
                            operation.without_source(),
                            LocalOperation::Return(Some(value))
                                if matches!(value.without_source(), LocalExpression::Call(call) if call.span == request.span)
                        )
                ),
                LocalExecutionScope::Root => body.terminal_call_root() == Some(request.span),
                LocalExecutionScope::Construction(_) => false,
            };
            let retained_result = retained
                && (terminal_position
                    || super::retained_effects::primitive_return_envelope(&values));
            let accepted_result = if transfer.is_none() {
                true
            } else if exact_no_normal {
                transfer_has_result || complete
            } else {
                transfer_has_result
                    && !values.is_empty()
                    && (complete || retained_result)
            };

            #[cfg(test)]
            {
                let disposition = match transfer.as_ref() {
                    None => "local-family".to_owned(),
                    Some(CallTransfer::EffectsOnly) => "effects-only".to_owned(),
                    Some(CallTransfer::ResultAvailable) => "result-available".to_owned(),
                    Some(CallTransfer::UnsupportedOperands(refusal)) => {
                        format!("unsupported-operands({refusal:?})")
                    }
                    Some(CallTransfer::SummarizedOpaque(refusal)) => {
                        format!("summarized-opaque({refusal:?})")
                    }
                };
                let target = executor
                    .local_call_target(request.callee.targets())
                    .map(|(key, _)| key);
                let pending_argument = caller.pending_argument_context();
                diagnostic(format!(
                    "call-resume span={:?} target={target:?} disposition={disposition} exact_normal={} exact_abrupt={} normal_values={} pending_argument={pending_argument:?} accepted={accepted_result} branch={:?}",
                    request.span,
                    child.ledger.is_exact_normal(),
                    child.ledger.is_exact_abrupt(),
                    child
                        .normal_values()
                        .map_or(0, |values| values.iter().count()),
                    executor.queries.branch_lineage(),
                ));
            }

            if pure_initializer
                && complete
                && no_child_losses
                && matches!(&child.ledger.completion, Completion::Normal(_))
                && !values.is_empty()
                && !values.contains(&AbstractValue::Value(Value::Unavailable))
                && !values.contains(&AbstractValue::Value(Value::Top))
            {
                let reference = audited_initializer
                    .as_ref()
                    .expect("the pure initializer has an audited input");
                child.ledger.effects = child.ledger.effects.audited_initializer_call_domains(
                    &ProofDependencies {
                        initializer_call_effect_inputs: BTreeSet::from([reference.clone()]),
                        ..ProofDependencies::default()
                    },
                );
                if let Some(selected) = child.normal_projection().cloned() {
                    child.ledger.completion = Completion::Normal(selected);
                }
            }

            {
                let mut context = LocalExecutionContext {
                    executor,
                    outcome: &mut outcome,
                    owner,
                    scope,
                    body,
                    retained_query: None,
                };
                if accepted_result {
                    caller.resume_call(
                        &mut context,
                        Some(LocalValueResult {
                            ledger: child.ledger,
                        }),
                    );
                } else if terminal_discarded_call
                    && context.executor.state.world.get().active_cells.is_empty()
                {
                    let mut reached = call_reached_values(request);
                    reached.extend(values);
                    context.outcome.lose_precision(
                        ExecutionPrecisionLossReason::UnsupportedOperation {
                            operation: ExecutionOperation::OpaqueExecution,
                            ordered_field_read_refusal: None,
                            ordered_call_admission_refusal: match transfer.as_ref() {
                                Some(
                                    CallTransfer::UnsupportedOperands(refusal)
                                    | CallTransfer::SummarizedOpaque(refusal),
                                ) => Some(*refusal),
                                _ => None,
                            },
                            object_spread_refusal: None,
                        },
                        HavocReach::Global {
                            cause: GlobalReachCause::UnmodelledOperation,
                        },
                        owner,
                        request.span,
                    );
                    context.outcome.ledger.effects = EffectCoverageFragment::default();
                    caller.frame.reached_values.extend(reached);
                    caller.frame.remainder_summarized.set(true);
                    caller.resume_call(&mut context, None);
                } else {
                    let mut reached = call_reached_values(request);
                    reached.extend(values);
                    reached.extend(child.reached);
                    let reason = if matches!(transfer.as_ref(), Some(CallTransfer::EffectsOnly)) {
                        ExecutionPrecisionLossReason::MissingCallResult
                    } else {
                        ExecutionPrecisionLossReason::UnsupportedOperation {
                            operation: ExecutionOperation::OpaqueExecution,
                            ordered_field_read_refusal: None,
                            ordered_call_admission_refusal: match transfer.as_ref() {
                                Some(
                                    CallTransfer::UnsupportedOperands(refusal)
                                    | CallTransfer::SummarizedOpaque(refusal),
                                ) => Some(*refusal),
                                _ => None,
                            },
                            object_spread_refusal: None,
                        }
                    };
                    context.fallback(&caller.frame, reached, reason, request.span);
                    caller.resume_call(&mut context, None);
                }
            }
            executor.resume_source_continuation(
                body,
                caller,
                outcome,
                owner,
                scope,
                constructor.clone(),
            )
        })
    }

    fn audited_initializer_reference(
        &self,
        call: &Call,
        outcome: &Outcome,
        owner: SourceUnitOrdinal,
        scope: LocalExecutionScope,
    ) -> Option<crate::analysis_inputs::InitializerCallEffectInputRef> {
        if call.kind != CallKind::Call {
            return None;
        }
        let identity = outcome.root_effect_identity.as_ref().filter(|identity| {
            matches!(scope, LocalExecutionScope::Root)
                && identity.owner == owner
                && matches!(identity.source, RootEffectSource::BindingInitializer { .. })
        })?;
        let RootEffectSource::BindingInitializer { binding, .. } = &identity.source else {
            return None;
        };
        let ValueExpr::Binding(callee) = &call.callee else {
            return None;
        };
        self.inputs
            .initializer_call_effect_input(owner, binding, callee, call.span)
            .map(|input| input.reference.clone())
    }

    fn canonical_source_call(
        &self,
        request: &LocalCallRequest,
        scope: LocalExecutionScope,
        owner: SourceUnitOrdinal,
    ) -> Option<Call> {
        let kind = match request.invocation {
            ForeignInvocation::Call => CallKind::Call,
            ForeignInvocation::Construct => CallKind::New,
        };
        let calls = match scope {
            LocalExecutionScope::Root => self.inputs.index.top_calls[owner.0].clone(),
            LocalExecutionScope::Callable(key) => self.inputs.index.callables[&key].calls.clone(),
            LocalExecutionScope::Construction(class) => {
                self.inputs
                    .index
                    .class_construction_call_inventory(class)?
                    .0
            }
        };
        calls
            .into_iter()
            .find(|call| call.kind == kind && call.has_source_site(&request.source_site))
    }

    fn source_local_call_target(
        &self,
        call: &LocalCallRequest,
        canonical: &Call,
    ) -> Option<SourceLocalCallTarget> {
        if call.invocation != ForeignInvocation::Call
            || !self.source_call_request_matches(call, canonical)
        {
            return None;
        }
        let (targets, receiver, selection_dependencies) = match &call.callee {
            EvaluatedCallee::Direct(selected) => (
                &selected.values,
                None,
                selected
                    .selection
                    .as_ref()
                    .filter(|selection| selection.matches(&selected.values))
                    .map(|selection| selection.dependencies.as_ref().clone()),
            ),
            EvaluatedCallee::Member {
                receiver,
                targets,
                selection_dependencies,
                array_method: None,
                ..
            } => (
                targets,
                Some(receiver.clone()),
                selection_dependencies.as_deref().cloned(),
            ),
            EvaluatedCallee::FunctionCallMethod {
                target,
                receiver: Some(receiver),
                dependencies,
                ..
            } => {
                let selection = target
                    .selection
                    .as_ref()
                    .filter(|selection| selection.matches(&target.values))?;
                let mut dependencies = dependencies.as_ref().clone();
                dependencies.extend(selection.dependencies.as_ref());
                (&target.values, Some(receiver.clone()), Some(dependencies))
            }
            _ => return None,
        };
        let (key, callable) = self.local_call_target(targets)?;
        if callable.generator || self.inputs.index.async_callables.contains(&key) {
            return None;
        }
        let captured = match targets.first()? {
            AbstractValue::Closure(_, captured) => Some(captured.clone()),
            AbstractValue::Value(Value::Alloc(Kind::Function, _)) => None,
            _ => return None,
        };
        let receipt = selection_dependencies.map(|selection_dependencies| {
            let mut dependencies = self.boxed_settled_prefix_dependencies();
            dependencies.extend(&selection_dependencies);
            LocalCallReceipt {
                caller: call.source_site.body.clone(),
                site: call.source_site.clone(),
                target: LocalBodyIdentity::Callable {
                    key,
                    owner: callable.owner,
                },
                dependencies,
            }
        });
        Some(SourceLocalCallTarget {
            key,
            captured,
            receiver,
            receipt,
        })
    }

    fn source_local_constructor_target(
        &self,
        call: &LocalCallRequest,
        canonical: &Call,
    ) -> Option<(u32, Option<LocalCallReceipt>)> {
        if canonical.kind != CallKind::New
            || canonical.has_spread
            || call.invocation != ForeignInvocation::Construct
            || call.source_site.step != OperationStep::CallInvocation
            || !self
                .inputs
                .index
                .source_operation_sites
                .contains(&call.source_site)
        {
            return None;
        }
        let (targets, selection_dependencies) = match &call.callee {
            EvaluatedCallee::Direct(selected) => (
                &selected.values,
                selected
                    .selection
                    .as_ref()
                    .filter(|selection| selection.matches(&selected.values))
                    .map(|selection| selection.dependencies.as_ref()),
            ),
            EvaluatedCallee::Member {
                targets,
                selection_dependencies: Some(dependencies),
                array_method: None,
                ..
            } if matches!(&canonical.callee, ValueExpr::Member(..)) => {
                (targets, Some(dependencies.as_ref()))
            }
            _ => return None,
        };
        let class = self.local_class_constructor_target(targets)?;
        if !self.inputs.index.local_constructors.contains_key(&class)
            || !self.inputs.index.class_provider_bodies.contains_key(&class)
        {
            return None;
        }
        let receipt = selection_dependencies.map(|selection_dependencies| {
            let mut dependencies = self.boxed_settled_prefix_dependencies();
            dependencies.extend(selection_dependencies);
            LocalCallReceipt {
                caller: call.source_site.body.clone(),
                site: call.source_site.clone(),
                target: LocalBodyIdentity::Construction {
                    class,
                    owner: self.inputs.index.class_owners[&class],
                },
                dependencies,
            }
        });
        Some((class, receipt))
    }

    fn local_class_constructor_target(&self, targets: &Values) -> Option<u32> {
        if targets.len() != 1 {
            return None;
        }
        let AbstractValue::Value(Value::Alloc(Kind::Class, address)) = targets.first()? else {
            return None;
        };
        let class = self.local_class_object_origin(*address)?;
        (self.inputs.index.local_constructors.contains_key(&class)
            && self.inputs.index.class_provider_bodies.contains_key(&class))
        .then_some(class)
    }

    fn source_call_request_matches(&self, call: &LocalCallRequest, canonical: &Call) -> bool {
        let Some(source_call_site) = canonical.source_site_for_body(&call.source_site.body) else {
            return false;
        };
        let expected_kind = match call.invocation {
            ForeignInvocation::Call => CallKind::Call,
            ForeignInvocation::Construct => CallKind::New,
        };
        if canonical.kind != expected_kind
            || !canonical.has_source_site(&call.source_site)
            || call.source_site.step != OperationStep::CallInvocation
            || source_call_site != &call.source_site
            || !self
                .inputs
                .index
                .source_operation_sites
                .contains(&call.source_site)
            || !source_call_spreads_match(
                canonical,
                call,
                &self.inputs.index.source_operation_sites,
            )
        {
            return false;
        }
        let canonical_method = match &canonical.callee {
            ValueExpr::Member(_, Some(property), MemberKey::Static, _) if property == "call" => {
                Some(FunctionCallMethod::Call)
            }
            ValueExpr::Member(_, Some(property), MemberKey::Static, _) if property == "apply" => {
                Some(FunctionCallMethod::Apply)
            }
            _ => None,
        };
        // FunctionCallMethod is recorded when GetValue selected the intact
        // intrinsic before arguments. This check only matches that existing
        // receipt to canonical syntax; it never re-reads the method.
        match (&call.callee, canonical_method) {
            (
                EvaluatedCallee::FunctionCallMethod {
                    method,
                    target,
                    receiver: Some(_),
                    ..
                },
                Some(canonical_method),
            ) => {
                *method == canonical_method
                    && target
                        .selection
                        .as_ref()
                        .is_some_and(|selection| selection.matches(&target.values))
            }
            (EvaluatedCallee::FunctionCallMethod { .. }, _) | (_, Some(_)) => false,
            (_, None) => true,
        }
    }
}

fn source_call_spreads_match(
    canonical: &Call,
    request: &LocalCallRequest,
    source_sites: &BTreeSet<SourceOperationSite>,
) -> bool {
    if canonical.has_spread != !canonical.spread_arguments.is_empty()
        || request.spread_expanded != !request.spread_sites.is_empty()
        || canonical.spread_arguments.len() != request.spread_sites.len()
        || canonical
            .spread_arguments
            .keys()
            .ne(request.spread_sites.keys())
    {
        return false;
    }
    let Some(invocation_site) = canonical.source_site_for_body(&request.source_site.body) else {
        return false;
    };
    canonical.spread_arguments.iter().all(|(index, span)| {
        request.spread_sites.get(index).is_some_and(|site| {
            site.owner == invocation_site.owner
                && site.body == invocation_site.body
                && site.ordinal == invocation_site.ordinal
                && site.span == *span
                && site.step == OperationStep::IterateSpread
                && request.source_site.owner == site.owner
                && source_sites.contains(site)
        })
    })
}

fn call_reached_values(call: &LocalCallRequest) -> Values {
    let mut values = call.retained_operands.clone();
    values.extend(call.callee.targets().iter().cloned());
    values.extend(call.callee.receiver().into_iter().flatten().cloned());
    values.extend(
        call.arguments
            .iter()
            .flat_map(SelectedValue::retained_values),
    );
    values
}
