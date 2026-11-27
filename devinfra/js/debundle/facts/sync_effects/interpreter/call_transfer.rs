//! Call execution and the transfer of evaluated calls.

use super::*;

impl<'source, 'rule> Executor<'source, 'rule> {
    pub(crate) fn promote_importer_cell_reads(&mut self, call: &Call, outcome: &mut Outcome) {
        if call.kind != CallKind::Call {
            return;
        }
        let ValueExpr::Binding(callee) = &call.callee else {
            return;
        };
        let Some(reads) = self.inputs.index.importer_cell_reads.get(callee) else {
            return;
        };
        // A syntactic descendant may be conditional or deferred. This is a
        // may-read constraint, never observed execution or an effect receipt.
        self.assuming(AssumedEffectReason::OpaqueCallbackInvocation, |this| {
            let reads = this.top_restricted(reads);
            this.promote_top_effects(
                &reads,
                &[],
                this.state.current_top_owner.unwrap_or(call.owner),
                outcome,
            );
        });
    }

    pub(crate) fn execute_call(
        &mut self,
        call: &Call,
        env: &Context,
        captures: &Context,
        outcome: &mut Outcome,
    ) -> bool {
        // Completeness is cumulative in the returned outcome, but only this
        // transfer's skipped effects can invalidate its own inputs here.
        let complete_before = outcome.complete;
        outcome.complete = true;
        let result = self.request(RuntimeOperation::Call {
            call: call.clone(),
            environment: env.clone(),
            captures: captures.clone(),
            outcome: Box::new(std::mem::take(outcome)),
            operands: None,
        });
        let RuntimeValue::Call {
            outcome: result, ..
        } = result
        else {
            unreachable!("call transfers produce outcomes");
        };
        *outcome = *result;
        if !outcome.complete {
            self.widen_native_effects();
            // A truncated transfer can mutate its inputs before the next
            // flattened operation in this same top-level statement or body.
            let mut reached = self.evaluate(&call.callee, env, captures);
            for argument in &call.args {
                reached.extend(self.evaluate(argument, env, captures));
            }
            reached.extend(
                outcome
                    .effects
                    .reads
                    .iter()
                    .filter_map(|(binding, _)| self.state.world.module.prefix_values.get(binding))
                    .flatten()
                    .cloned(),
            );
            self.unsettle(&reached);
        }
        let transfer_complete = outcome.complete;
        outcome.complete &= complete_before;
        transfer_complete
    }

    pub(crate) fn method_receiver_is_unused(&self, targets: &Values) -> bool {
        self.inputs.index.dynamic_scope_owners.is_empty()
            && !targets.is_empty()
            && targets.iter().all(|target| {
                let key = match target {
                    AbstractValue::Value(Value::Alloc(Kind::Function, key))
                    | AbstractValue::Closure(HeapAddress { callable: key, .. }, _) => key,
                    _ => return false,
                };
                self.inputs.index.callables.get(key).is_some_and(|body| {
                    !body.opaque
                        && !body.generator
                        && body.calls.is_empty()
                        && !body.reads.iter().any(|id| id.0.as_ref() == "arguments")
                        && self
                            .inputs
                            .index
                            .callables
                            .values()
                            .filter(|nested| nested.owner == body.owner)
                            .all(|nested| !nested.implicit_capture)
                })
            })
    }

    /// Whether a method call to `targets` can read its receiver through
    /// `this` (including from an arrow inside the method), which the summary
    /// does not bind.
    pub(super) fn method_may_read_receiver(&self, targets: &Values) -> bool {
        let index = self.inputs.index;
        !index.dynamic_scope_owners.is_empty()
            || targets.iter().any(|target| match target {
                AbstractValue::Value(Value::Alloc(Kind::Function, key))
                | AbstractValue::Closure(HeapAddress { callable: key, .. }, _) => {
                    index.reads_this(*key)
                }
                _ => false,
            })
    }

    /// Whether an object literal, or a prototype its `__proto__` names, has an
    /// accessor that reads `this`. A prototype that is not a known literal
    /// may have one.
    pub(super) fn literal_chain_may_read_this(&self, object: u32) -> bool {
        let index = self.inputs.index;
        let mut pending = vec![object];
        let mut visited = BTreeSet::new();
        while let Some(object) = pending.pop() {
            if !visited.insert(object) {
                continue;
            }
            if index.this_reading_accessor_objects.contains(&object) {
                return true;
            }
            let Some(prototype) = index.literal_prototypes.get(&object) else {
                continue;
            };
            let values = match prototype {
                ValueExpr::ObjectLiteral(literal, _) => vec![Value::Alloc(Kind::Object, *literal)],
                ValueExpr::Binding(binding) if self.inputs.flow.is_declared(binding) => self
                    .inputs
                    .flow
                    .values_of_binding(binding)
                    .into_iter()
                    .collect(),
                _ => return true,
            };
            for value in values {
                match value {
                    Value::Alloc(Kind::Object, prototype) => pending.push(prototype),
                    Value::Prim(_) => {}
                    _ => return true,
                }
            }
        }
        false
    }

    /// A destructuring assignment to a member target is a [[Set]]: it may run
    /// a setter reading `this`.
    pub(crate) fn publish_destructuring_target_receivers(
        &mut self,
        receivers: &[ValueExpr],
        env: &Context,
        captures: &Context,
    ) {
        for receiver in receivers {
            let values = self.evaluate(receiver, env, captures);
            self.publish_accessor_receivers(&values, DUMMY_SP);
        }
    }

    /// A member read or write on `receivers` may run an accessor that reads
    /// `this`, which the summary does not bind: publish those receivers.
    /// Kept out of line for the call transfer's frame size.
    #[inline(never)]
    pub(crate) fn publish_accessor_receivers(&mut self, receivers: &Values, span: Span) {
        let index = self.inputs.index;
        let reached: Values = receivers
            .iter()
            .filter(|receiver| {
                let object = match receiver {
                    AbstractValue::Value(Value::Alloc(Kind::Object, object)) => object,
                    AbstractValue::Value(Value::Alloc(Kind::Class, class))
                    | AbstractValue::ClassMembers(class)
                    | AbstractValue::Instance(class, _, _) => {
                        return self
                            .class_chain(*class)
                            .iter()
                            .any(|class| index.this_reading_accessor_classes.contains(class));
                    }
                    _ => return false,
                };
                self.literal_chain_may_read_this(*object)
                    || self
                        .state
                        .world
                        .surface
                        .applied_accessors
                        .get(object)
                        .is_some_and(|accessors| {
                            accessors.values().any(|accessor| {
                                accessor.getter.iter().chain(accessor.setter.iter()).any(
                                    |function| match function {
                                        AbstractValue::Value(Value::Alloc(Kind::Function, key))
                                        | AbstractValue::Closure(
                                            HeapAddress { callable: key, .. },
                                            _,
                                        ) => index.reads_this(*key),
                                        AbstractValue::Value(
                                            Value::Prim(_) | Value::Unavailable,
                                        ) => false,
                                        _ => true,
                                    },
                                )
                            })
                        })
            })
            .cloned()
            .collect();
        if !reached.is_empty() {
            self.publish_values(
                &reached,
                ClassMembersPublicationWriter::AccessorReceiver,
                Some(span),
            );
        }
    }

    /// The `Call`/`New` transfer over already evaluated targets and arguments.
    /// `supplied` carries the kernel's operand selections; `None` is the
    /// self-evaluating mode, which requests no value.
    pub(crate) fn transfer_evaluated_call(
        &mut self,
        call: &Call,
        env: &Context,
        captures: &Context,
        outcome: &mut Outcome,
        operands: EvaluatedCallOperands,
        supplied: Option<SuppliedCallSelections>,
    ) -> CallTransfer {
        let needs_value = supplied.is_some();
        let SuppliedCallSelections {
            selected_pure_initializer_call,
            selected_const_factory_result,
            selected_member_dependencies,
            selected_value,
            argument_selections,
        } = supplied.unwrap_or_else(|| SuppliedCallSelections {
            selected_pure_initializer_call: false,
            selected_const_factory_result: false,
            selected_member_dependencies: None,
            selected_value: None,
            argument_selections: vec![None; call.args.len()],
        });
        let EvaluatedCallOperands {
            targets,
            mut args,
            prepared_receiver,
        } = operands;
        let operands_may_throw = targets.contains(&AbstractValue::Value(Value::Unavailable))
            || args
                .iter()
                .any(|values| values.contains(&AbstractValue::Value(Value::Unavailable)));
        if args.iter().any(|values| {
            values.len() == 1 && values.contains(&AbstractValue::Value(Value::Unavailable))
        }) {
            return CallTransfer::requested(needs_value, || {
                Values::from([AbstractValue::Value(Value::Unavailable)])
            });
        }
        for values in &mut args {
            values.remove(&AbstractValue::Value(Value::Unavailable));
        }
        // The provider body runs no other code and writes only its own
        // allocations. The call stays impure for purity and S-chain order.
        if !call.has_spread
            && let Some(summary) = self.foreign_call_summary(call.kind, &call.callee)
        {
            self.retain_foreign_arguments(&summary, &args);
            self.record_foreign_call_use(&summary);
            return CallTransfer::requested(needs_value, || {
                let mut values = self.foreign_call_result(&summary, &args);
                if operands_may_throw {
                    values.insert(AbstractValue::Value(Value::Unavailable));
                }
                values
            });
        }
        if call.kind == CallKind::Call
            && !call.has_spread
            && self.source_record_factory(&call.callee, &call.args)
        {
            self.record_returned_record_use(
                &call.callee,
                analysis_vocabulary::report_schema::ReturnedRecordUseKind::CallTiming,
                call.owner,
                call.span,
            );
            return CallTransfer::requested(needs_value, || {
                Values::from([AbstractValue::ForeignDeferredRecord])
            });
        }
        if call.kind == CallKind::Call
            && !call.has_spread
            && let Some(source_object) =
                self.source_namespace_wrapper(&call.callee, &args, call.span.into())
        {
            return CallTransfer::requested(needs_value, || {
                let mut retained = Values::from([AbstractValue::Value(Value::Alloc(
                    Kind::Object,
                    source_object,
                ))]);
                retained.extend(self.callback_values(&retained));
                Values::from([
                    self.deferred_callbacks(retained),
                    AbstractValue::Value(Value::Top),
                    AbstractValue::Value(Value::Unavailable),
                ])
            });
        }
        if call.kind == CallKind::Call
            && !call.has_spread
            && let Some(proof) =
                self.descriptor_copy_projection(&call.callee, &args, call.span.into())
        {
            return CallTransfer::requested(needs_value, || {
                self.install_descriptor_copy_result(proof, &args, call.span.into())
            });
        }
        if !call.has_spread
            && let Some(resolution) = self.resolve_native_call(
                call.kind,
                &call.callee,
                CallSite::from(call.span),
                &call.args,
                &args,
            )
        {
            // This branch is the consumed runtime transfer: operands have
            // completed, and the admitted row's checked plan is committed to
            // this prefix whether or not the value is used.
            if needs_value {
                // The kernel's per-call outcome: the admitted row is the one
                // operation it performed, with no application callback,
                // accessor, host operation or exterior write.
                let mut coverage =
                    EffectCoverageFragment::native_operation(&resolution.dependencies);
                if let (Some(caller), Some(intrinsic)) = (
                    self.local_body_identity_for_call(call),
                    resolution.row.family(),
                ) {
                    coverage.selected_local_call(LocalCallReceipt {
                        caller,
                        call_span: call.span,
                        target: LocalBodyIdentity::Native {
                            intrinsic,
                            property: resolution.row.op.callee().1.map(str::to_owned),
                        },
                        dependencies: Box::new(resolution.dependencies.clone()),
                    });
                }
                outcome.effect_coverage = coverage;
            }
            for cell in &resolution.projected_reads {
                outcome
                    .effects
                    .record_heap_read(cell.clone(), self.state.assumption.is_none());
            }
            if let Some(receipt) = &resolution.iterable {
                outcome.effects.record_heap_contents(
                    &receipt.external_heap_contents(),
                    self.state.assumption.is_none(),
                );
                outcome.effect_coverage.compose(&receipt.coverage);
            }
            let values = self.execute_native_resolution(
                resolution,
                &args,
                operands_may_throw,
                call.span.into(),
            );
            return CallTransfer::requested(needs_value, || values);
        }
        if self.host_call_does_not_reenter(call) {
            return CallTransfer::EffectsOnly;
        }
        if call.kind == CallKind::Call
            && !call.has_spread
            && let Some(callbacks) = self.no_reentry_callbacks(call.span.into(), &call.args, &args)
        {
            // Argument evaluation has completed. The provider cannot reenter
            // during this call, but may retain callbacks anywhere, including a
            // private cross-call registry. Every subsequent opaque operation
            // must therefore see them. Do not give the result a known shape.
            self.insert_escaped_callback_values(
                &callbacks,
                ClassMembersPublicationWriter::NoReentryCallRetention,
                Some(call.span),
                None,
            );
            return CallTransfer::EffectsOnly;
        }
        if call.kind == CallKind::Call
            && !call.has_spread
            && let Some(callbacks) =
                self.retained_factory_callbacks(call.span.into(), &call.args, &args)
        {
            return CallTransfer::requested(needs_value, || {
                Values::from([self.audited_retained_result(RetainedResultKind::Factory, callbacks)])
            });
        }
        if call.kind == CallKind::New
            && !call.has_spread
            && self
                .retained_constructor_callbacks(call.span.into(), &call.args, &args)
                .is_some()
        {
            return CallTransfer::EffectsOnly;
        }
        // An exact author-pinned root call returns normally with an opaque
        // callable. Its separately asserted no-reentry and purity premises
        // cover timing and completion; argument evaluation has already run.
        // Keep later invocation, member reads and escape conservative.
        if selected_pure_initializer_call
            && selected_const_factory_result
            && needs_value
            && call.kind == CallKind::Call
            && !call.has_spread
            && !operands_may_throw
            && prepared_receiver.is_none()
            && let ValueExpr::Binding(callee) = &call.callee
            && self.inputs.index.no_reentry_exports.contains(callee)
            && matches!(call.args.as_slice(), [ValueExpr::Binding(binding)]
                if self.inputs.index.dynamic_scope_owners.is_empty()
                    && self.closed_primitive_binding(binding))
            && matches!(args.as_slice(), [values]
                if !values.is_empty() && values.iter().all(AbstractValue::is_primitive))
        {
            return CallTransfer::Values(Values::from([self.deferred_callbacks(args[0].clone())]));
        }
        // The author assertion covers this direct imported call with checked
        // primitive arguments. Its result remains Unknown; only opaque
        // callback promotion is suppressed.
        if call.kind == CallKind::Call
            && call.no_reentry_candidate
            && let ValueExpr::Binding(binding) = &call.callee
            && self.inputs.index.no_reentry_exports.contains(binding)
        {
            return CallTransfer::EffectsOnly;
        }
        // Distinct author assertions cover primitive and fresh-record inputs.
        // Both use the same member identity and lookup guards. The provider
        // may have private effects; this only suppresses callback reentry.
        if call.kind == CallKind::Call
            && let ValueExpr::Member(receiver, Some(property), MemberKey::Static, _) = &call.callee
            && let ValueExpr::Binding(namespace) = receiver.as_ref()
            && ((call.no_reentry_candidate
                && self
                    .inputs
                    .index
                    .no_reentry_members
                    .get(namespace)
                    .is_some_and(|members| members.contains(property)))
                || (call.no_reentry_record_candidate
                    && self
                        .inputs
                        .index
                        .no_reentry_record_members
                        .get(namespace)
                        .is_some_and(|members| members.contains(property))))
        {
            return CallTransfer::EffectsOnly;
        }
        if call.kind == CallKind::Call
            && !call.has_spread
            && self.execute_dense_array_push(
                &call.callee,
                &args,
                env,
                captures,
                prepared_receiver.as_ref(),
            )
        {
            return CallTransfer::requested(needs_value, || {
                // Contents and multiplicity stay unknown after a push. Its
                // completing result is numeric; a length overflow may throw.
                Values::from([
                    AbstractValue::Value(Value::Prim(PrimKinds::NUMBER)),
                    AbstractValue::Value(Value::Unavailable),
                ])
            });
        }
        // Proofs that reevaluate source expressions have not migrated to
        // evaluated operands. A missing result transfer truncates the ordered
        // caller; it must never replay those expressions to recover a value.
        let prepared_receiver_unused = prepared_receiver
            .as_ref()
            .map(|_| self.method_receiver_is_unused(&targets));
        // The callback-factory evidence supplies a fresh ordinary carrier
        // retaining this callback. The exact root's separate purity premise
        // excludes provider throws. Keep the carrier opaque and use the
        // already evaluated callee and argument without replaying either.
        if selected_pure_initializer_call
            && needs_value
            && call.kind == CallKind::Call
            && !call.has_spread
            && !operands_may_throw
            && prepared_receiver.is_none()
            && matches!(&call.callee, ValueExpr::Binding(_))
            && let [ValueExpr::Alloc(Kind::Function, source)] = call.args.as_slice()
            && targets.len() == 1
            && targets.iter().all(|target| {
                matches!(
                    target,
                    AbstractValue::CallbackFactory | AbstractValue::InlineCallbackFactory
                )
            })
            && matches!(args.as_slice(), [values] if values.len() == 1
            && values.iter().all(|value| matches!(
                value,
                    AbstractValue::Value(Value::Alloc(Kind::Function, actual))
                        | AbstractValue::Closure(HeapAddress { callable: actual, .. }, _)
                        if actual == source
            )))
        {
            let records_evidence = targets.contains(&AbstractValue::CallbackFactory);
            return CallTransfer::Values(self.retained_factory_carrier(
                call,
                records_evidence,
                &args[0],
            ));
        }
        if needs_value {
            // Publication can change properties and captured state, but cannot
            // replace an ordinary source function's [[Call]]. Invocation reads
            // live capture cells and retains their existing havoc/heap guards.
            let local_target = self.local_call_target(&targets).is_some();
            let abrupt = !targets.is_empty()
                && targets.iter().all(|target| {
                    matches!(
                        target,
                        AbstractValue::Value(Value::Unavailable)
                            | AbstractValue::Value(Value::Prim(_))
                    )
                });
            let admission_refusal = if !local_target && !abrupt {
                let refusal = if targets.len() > 1 {
                    OrderedCallAdmissionRefusal::MultipleTargets
                } else {
                    match targets.iter().next() {
                        None
                        | Some(AbstractValue::Value(Value::Top))
                        | Some(AbstractValue::SymbolicInput(_)) => {
                            OrderedCallAdmissionRefusal::UnknownTarget
                        }
                        Some(_) => OrderedCallAdmissionRefusal::NonLocalTarget,
                    }
                };
                Some(refusal)
            } else if !abrupt && prepared_receiver_unused == Some(false) {
                Some(OrderedCallAdmissionRefusal::UnsupportedReceiver)
            } else {
                None
            };
            if let Some(refusal) = admission_refusal {
                return CallTransfer::UnsupportedOperands(refusal);
            }
        }
        let from_entries_source_callbacks = if call.kind == CallKind::Call && !needs_value {
            if self
                .proven_from_entries_callbacks(call.span.into(), env, captures)
                .is_some()
            {
                return CallTransfer::EffectsOnly;
            }
            self.callbacks_in_from_entries_source(call.span.into(), env, captures)
        } else {
            None
        };
        if call.kind == CallKind::New
            && self.is_proven_map_entry_array(&call.callee, &call.args, env, captures, call.owner)
        {
            // The Map retains its entries, and the interpreter does not track
            // what it holds: whoever reaches the Map reaches them.
            let entries: Values = args.iter().flatten().cloned().collect();
            self.taint_objects(&entries);
            return CallTransfer::EffectsOnly;
        }
        if call.kind == CallKind::Call
            && !needs_value
            && self.execute_array_iteration(call.span.into(), env, captures, outcome)
        {
            return CallTransfer::EffectsOnly;
        }
        // Known local methods still receive an implicit `this` capability.
        // Its effects are not resolved by invoke, so it can mutate any retained
        // array reachable from the receiver before a later intrinsic iteration.
        if call.kind == CallKind::Call
            && let Some(receiver) = method_callee_receiver(&call.callee)
        {
            let unused_receiver = prepared_receiver_unused
                .unwrap_or_else(|| self.method_receiver_is_unused(&targets));
            if !unused_receiver {
                let values = prepared_receiver
                    .clone()
                    .unwrap_or_else(|| self.evaluate(receiver, env, captures));
                let contains_array = self.contains_dense_array(&values);
                if self.method_may_read_receiver(&targets) {
                    // The summary reads `this` as unknown, which stands only
                    // for published values.
                    self.publish_values(
                        &values,
                        ClassMembersPublicationWriter::MethodReceiver,
                        Some(call.span),
                    );
                } else {
                    self.taint_objects(&values);
                }
                if contains_array {
                    // A dense receiver preserves possible callback reach, but
                    // does not prove that the method invoked those callbacks.
                    self.promote_escaped_callbacks(&targets, outcome);
                }
            }
        }
        let mut opaque_args = args.clone();
        if let Some(receiver) = method_callee_receiver(&call.callee) {
            let receiver_values = prepared_receiver
                .clone()
                .unwrap_or_else(|| self.evaluate(receiver, env, captures));
            // The receiver is an input capability too: an opaque method can
            // invoke its stored callbacks through `this`, including callbacks
            // nested in collections or retained wrapper/instance values.
            // Keep the original receiver taint above; callback reachability
            // does not prove that the foreign call preserves its contents.
            opaque_args.push(receiver_values);
        }
        if !needs_value
            && let Some(proof) = self.inputs.index.array_iterations.get(&call.span.into())
        {
            let source = self.evaluate(&proof.source.clone(), env, captures);
            self.promote_escaped_callbacks(&source, outcome);
            opaque_args.push(source);
        }

        if let Some(callbacks) = from_entries_source_callbacks {
            opaque_args.push(callbacks);
        }
        if targets.is_empty()
            || targets.iter().any(|target| {
                matches!(
                    target,
                    AbstractValue::Value(Value::Top)
                        | AbstractValue::DeferredCallbacks(_)
                        | AbstractValue::AuditedRetainedResult(_, _)
                        | AbstractValue::ClassMembers(_)
                        | AbstractValue::Instance(_, _, _)
                )
            })
        {
            // Opaque calls can mutate objects passed as arguments or used as
            // method receivers. Preserve that possibility for later narrow
            // construction proofs (including changes to an entry's own data
            // properties or an array's iterator/species surface).
            let mut possible_objects: Values = args.iter().flatten().cloned().collect();
            if let Some(receiver) = method_callee_receiver(&call.callee) {
                possible_objects.extend(
                    prepared_receiver
                        .clone()
                        .unwrap_or_else(|| self.evaluate(receiver, env, captures)),
                );
            }
            self.expose_class_capabilities(&possible_objects);
            self.taint_objects(&possible_objects);
        }
        let retained_callback = matches!(args.as_slice(), [values] if !values.is_empty()
            && values.iter().all(|value| matches!(value, AbstractValue::AuditedRetainedResult(RetainedResultKind::Factory, captured) if self.audited_result_is_clean(captured))));
        if targets.is_empty() {
            outcome.lose_call_target(&self.inputs.metrics, call.owner, call.span);
            outcome.unknown_call = true;
            self.note_unknown_call(call);
            self.invoke_possible_callbacks_at(
                &opaque_args,
                call.no_sync_arguments,
                Some(call.span),
                outcome,
            );
        }
        let mut invoked_unknown_callbacks = targets.is_empty();
        let mut result_values = Values::new();
        if needs_value
            && (operands_may_throw
                || targets
                    .iter()
                    .any(|target| matches!(target, AbstractValue::Value(Value::Prim(_)))))
        {
            result_values.insert(AbstractValue::Value(Value::Unavailable));
        }
        // The receipt belongs to this exact selected invocation. A member
        // target is admitted only when LocalBody already consumed its
        // initialized-module own-data selection before evaluating arguments;
        // An alias instead needs the actual value-selection evidence carried
        // by its ordered producer. Native/imported targets and raw known
        // return values do not manufacture a selection receipt.
        let direct_binding = match &call.callee {
            ValueExpr::Binding(binding)
                if self
                    .inputs
                    .index
                    .direct_top_level_functions
                    .contains(binding)
                    && prepared_receiver.is_none()
                    && self.inputs.index.hoisted_bindings.contains_key(binding) =>
            {
                Some(binding)
            }
            _ => None,
        };
        let member_selection = matches!(
            (&call.callee, prepared_receiver.as_ref(), selected_member_dependencies.as_ref()),
            (
                ValueExpr::Member(receiver, Some(_), MemberKey::Static, _),
                Some(_),
                Some(_)
            ) if matches!(receiver.as_ref(), ValueExpr::Binding(_))
        );
        let coverage_target = if needs_value
            && call.kind == CallKind::Call
            && !call.has_spread
            && !operands_may_throw
            && (direct_binding.is_some() || member_selection || selected_value.is_some())
            && let Some((key, callable)) = self.local_call_target(&targets)
            && let Some(caller) = self.local_body_identity_for_call(call)
            && direct_binding
                .is_none_or(|binding| self.inputs.index.hoisted_bindings.get(binding) == Some(&key))
        {
            let mut prerequisites = self.boxed_settled_prefix_dependencies();
            if let Some(selection) = &selected_member_dependencies {
                prerequisites.extend(selection);
            }
            if let Some(selection) = &selected_value {
                prerequisites.extend(&selection.dependencies);
            }
            Some((
                key,
                caller,
                LocalBodyIdentity::Callable {
                    key,
                    owner: callable.owner,
                },
                prerequisites,
            ))
        } else {
            None
        };
        let single_target = targets.len() == 1;
        let nested_record_factory_premise = if single_target && !operands_may_throw {
            self.checked_nested_record_factory_call(call, &args, env, captures)
        } else {
            None
        };
        let mut result_selection = None;
        // `new` of a local plain function runs its body with an untracked
        // `this`: what it stores through `this` or into other bindings is
        // part of the call's effects. The instance stays unknown (the
        // unknown-callee arm below).
        if call.kind == CallKind::New {
            self.construct_plain_functions(&targets, &args, outcome);
        }
        for target in targets {
            match target {
                AbstractValue::CallbackFactory
                    if call.kind == CallKind::Call
                        && !call.has_spread
                        && (retained_callback
                            || matches!(
                                call.args.as_slice(),
                                [ValueExpr::Alloc(Kind::Function, _)]
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
                            [ValueExpr::Alloc(Kind::Function, _)]
                        ) => {}
                AbstractValue::ForeignDeferredRecord => {
                    self.promote_foreign_record(outcome);
                    self.note_unknown_call(call);
                    self.invoke_possible_callbacks_at(
                        &opaque_args,
                        false,
                        Some(call.span),
                        outcome,
                    );
                }
                AbstractValue::DeferredCallbacks(captured)
                | AbstractValue::AuditedRetainedResult(_, captured) => {
                    outcome.lose_call_target(&self.inputs.metrics, call.owner, call.span);
                    outcome.unknown_call = true;
                    self.note_unknown_call(call);
                    let mut possible_args = opaque_args.clone();
                    possible_args.push((*captured.values).clone());
                    self.promote_deferred_callbacks(&captured.values, outcome);
                    self.invoke_possible_callbacks_at(
                        &possible_args,
                        false,
                        Some(call.span),
                        outcome,
                    );
                }
                AbstractValue::ClassMembers(key) => {
                    outcome.lose_call_target(&self.inputs.metrics, call.owner, call.span);
                    outcome.unknown_call = true;
                    self.note_unknown_call(call);
                    self.assuming(AssumedEffectReason::EscapedCallbackClosure, |this| {
                        this.promote_class_members(key, outcome)
                    });
                    self.invoke_possible_callbacks_at(
                        &opaque_args,
                        false,
                        Some(call.span),
                        outcome,
                    );
                }
                AbstractValue::Instance(key, captured, _) => {
                    outcome.lose_call_target(&self.inputs.metrics, call.owner, call.span);
                    outcome.unknown_call = true;
                    self.note_unknown_call(call);
                    self.assuming(AssumedEffectReason::EscapedCallbackClosure, |this| {
                        this.promote_class_members(key, outcome)
                    });
                    let mut possible_args = opaque_args.clone();
                    possible_args.push((*captured.values).clone());
                    self.invoke_possible_callbacks_at(
                        &possible_args,
                        false,
                        Some(call.span),
                        outcome,
                    );
                }
                AbstractValue::Value(Value::Unavailable) | AbstractValue::Value(Value::Prim(_)) => {
                }
                AbstractValue::ObjectAssign
                    if call.kind == CallKind::Call
                        && call.plain_object_sources
                        && matches!(call.args.first(), Some(ValueExpr::Binding(id)) if call.caller
                        .and_then(|caller| self.inputs.index.callables.get(&caller).map(|body| (caller, body)))
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
                    let possible_objects: Values = args.iter().flatten().cloned().collect();
                    // The target may be reachable by anyone (an intrinsic
                    // prototype): what is copied onto it escapes.
                    self.publish_values(
                        &possible_objects,
                        ClassMembersPublicationWriter::ObjectAssignCopy,
                        Some(call.span),
                    );
                    let mut possible_accessors: BTreeSet<_> = self
                        .state
                        .world
                        .reachability
                        .owners
                        .iter()
                        .copied()
                        .filter(|candidate| self.may_be_available_accessor_owner(*candidate))
                        .collect();
                    possible_accessors.extend(outcome.executed_owners.iter().copied());
                    if self.state.world.reachability.incomplete {
                        possible_accessors.extend(self.all_owners());
                    }
                    let effect_owner = self.state.current_top_owner.unwrap_or(call.owner);
                    self.assuming(AssumedEffectReason::PossibleSetter, |this| {
                        for accessor_owner in possible_accessors {
                            this.promote_lazy_owner(accessor_owner, effect_owner, outcome);
                        }
                    });
                    outcome.unsupported(
                        &self.inputs.metrics,
                        ExecutionOperation::ObjectAssign,
                        call.owner,
                        call.span,
                    );
                    outcome.unknown_call = true;
                    self.widen_native_effects();
                }
                AbstractValue::Value(Value::Alloc(Kind::Class, key))
                    if call.kind == CallKind::New =>
                {
                    let constructed = if call.has_spread {
                        Err(ConstructionRejection::SpreadArguments)
                    } else {
                        self.construct_local_class(key, &args)
                    };
                    match constructed {
                        Ok((nested, _)) => {
                            self.absorb_nested_call(outcome, nested);
                        }
                        Err(rejection) => {
                            self.reject_local_construction(key, &args, call, rejection, outcome);
                            if !invoked_unknown_callbacks {
                                self.invoke_possible_callbacks_at(
                                    &args,
                                    false,
                                    Some(call.span),
                                    outcome,
                                );
                                invoked_unknown_callbacks = true;
                            }
                        }
                    }
                }
                AbstractValue::Value(Value::Alloc(Kind::Function, key))
                    if call.kind == CallKind::Call =>
                {
                    let premises_before = self
                        .observations
                        .record_factory_prototype_premises()
                        .clone();
                    let previous_premise = std::mem::replace(
                        &mut self.state.nested_record_factory_premise,
                        nested_record_factory_premise
                            .as_ref()
                            .filter(|(factory, _, _, _)| *factory == key)
                            .map(|(_, class, object, _)| (*class, *object)),
                    );
                    self.execute_local_call_target(
                        LocalCallTarget {
                            key,
                            captured: None,
                            args: &args,
                            argument_selections: &argument_selections,
                            coverage_target: coverage_target.as_ref(),
                            single_target,
                            needs_value,
                            call_span: call.span,
                        },
                        &mut result_values,
                        &mut result_selection,
                        outcome,
                    );
                    self.state.nested_record_factory_premise = previous_premise;
                    let scoped_factory = nested_record_factory_premise
                        .as_ref()
                        .filter(|(factory_key, _, _, _)| *factory_key == key);
                    if let Some((_, _, _, properties)) = scoped_factory.filter(|_| outcome.complete)
                        && !properties.is_empty()
                    {
                        let ValueExpr::Binding(factory) = &call.callee else {
                            unreachable!()
                        };
                        outcome.effects.local_record_factory_uses.push(
                            analysis_vocabulary::report_schema::LocalRecordFactoryUse {
                                factory_binding: factory.0.to_string(),
                                prototype_properties: properties.clone(),
                                lexical_statement_ordinal: self
                                    .state
                                    .current_top_owner
                                    .unwrap_or(call.owner),
                                source_location: None,
                                span: call.span,
                            },
                        );
                    }
                    if scoped_factory.is_some() {
                        *self.observations.record_factory_prototype_premises_mut() =
                            premises_before;
                    }
                }
                AbstractValue::Closure(HeapAddress { callable: key, .. }, captured)
                    if call.kind == CallKind::Call =>
                {
                    self.execute_local_call_target(
                        LocalCallTarget {
                            key,
                            captured: Some(captured),
                            args: &args,
                            argument_selections: &argument_selections,
                            coverage_target: coverage_target.as_ref(),
                            single_target,
                            needs_value,
                            call_span: call.span,
                        },
                        &mut result_values,
                        &mut result_selection,
                        outcome,
                    );
                }
                _ => {
                    outcome.lose_call_target(&self.inputs.metrics, call.owner, call.span);
                    outcome.unknown_call = true;
                    self.note_unknown_call(call);
                    if !invoked_unknown_callbacks {
                        self.invoke_possible_callbacks_at(
                            &opaque_args,
                            call.no_sync_arguments,
                            Some(call.span),
                            outcome,
                        );
                        invoked_unknown_callbacks = true;
                    }
                }
            }
        }
        outcome.return_selection =
            result_selection.filter(|selection| selection.matches(&result_values));
        CallTransfer::requested(needs_value, || result_values)
    }
}
