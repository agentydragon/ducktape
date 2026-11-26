//! Expression evaluation: bindings, `new`, local bodies and descriptor-copy results.

use super::*;

impl<'source, 'rule> Executor<'source, 'rule> {
    /// A single-execution imported call may carry a fresh ordinary descriptor
    /// graph. The author certifies the provider's native slots and return-only
    /// retention; the source index proves the exact input and key collisions.
    pub(crate) fn descriptor_copy_projection(
        &mut self,
        callee: &ValueExpr,
        args: &[Values],
        site: CallSite,
    ) -> Option<OwnPropertyProof> {
        let ValueExpr::Binding(binding) = callee else {
            return None;
        };
        let certificate = self.inputs.index.descriptor_copy_calls.get(&site)?;
        if &certificate.provider_binding != binding {
            return None;
        }
        let [source] = args else { return None };
        let [AbstractValue::Value(Value::Alloc(Kind::Object, actual))] =
            source.iter().collect::<Vec<_>>().as_slice()
        else {
            return None;
        };
        if actual != &certificate.source_object {
            return None;
        }
        let proof = self.own_property_projection(
            source,
            OwnPropertyRequest::AuditedDescriptorCopy {
                semantic_call: SemanticKey::DescriptorCopyCall(site.source_span().lo.0),
            },
        )?;
        self.queries.assume(&proof.dependencies);
        self.observations
            .record_inference_use(proof.inference_use());
        Some(proof)
    }

    pub(crate) fn install_descriptor_copy_result(
        &mut self,
        proof: OwnPropertyProof,
        args: &[Values],
        site: CallSite,
    ) -> Values {
        let result = site.allocation_id();
        let OwnPropertyProjection::Descriptors(projection) = proof.projection else {
            return Values::from([AbstractValue::Value(Value::Unavailable)]);
        };
        // The audited certificate admits this definition onto the factory's
        // fresh result; the commit is the same one every native row uses.
        self.descriptor_heap().capture(site, projection);
        let application = self
            .state
            .world
            .descriptors
            .get(&site)
            .and_then(|map| DescriptorApplication::from_map(result, map));
        let commit = match application {
            Some(application) => NativeOperationCommit::DefineProperties(application),
            None => NativeOperationCommit::Integrity {
                targets: BTreeSet::from([result]),
            },
        };
        native_operation::commit(&mut self.descriptor_heap(), commit);
        let Some(certificate) = self.inputs.index.descriptor_copy_calls.get(&site) else {
            unreachable!()
        };
        let keys = certificate.signature.assigned_data_keys.clone();
        let mut captured = args[0].clone();
        captured.extend(self.callback_values(&args[0]));
        let method = Values::from([self.deferred_callbacks(captured)]);
        let properties = self
            .state
            .world_mut()
            .surface
            .prefix_object_properties
            .entry(result)
            .or_default();
        for key in keys {
            properties.insert(Some(key.clone()), method.clone());
        }
        if let Some(accessors) = self
            .state
            .world_mut()
            .surface
            .applied_accessors
            .get_mut(&result)
        {
            for key in &certificate.signature.assigned_data_keys {
                accessors.remove(key);
            }
        }
        self.state
            .world_mut()
            .surface
            .descriptor_copy_results
            .insert(result);
        self.state.world_mut().revision.bump_prefix();
        // Provider code and native operations may still throw after argument
        // evaluation. Keep normal-return identity and abrupt completion apart.
        Values::from([
            AbstractValue::Value(Value::Alloc(Kind::Object, result)),
            AbstractValue::Value(Value::Unavailable),
        ])
    }

    /// Instantiates a provider's checked wrapper timing proof for this exact
    /// caller-owned ordinary object. The raw source getter remains deferred in
    /// the returned capability; only the wrapper's eager phase is certified.
    pub(crate) fn source_namespace_wrapper(
        &mut self,
        callee: &ValueExpr,
        args: &[Values],
        site: CallSite,
    ) -> Option<u32> {
        let ValueExpr::Binding(binding) = callee else {
            return None;
        };
        let summary = self.inputs.index.namespace_wrapper_summaries.get(binding)?;
        let [source, flag] = args else {
            return None;
        };
        if !self
            .inputs
            .index
            .literal_true_second_arguments
            .contains(&site)
            || flag.is_empty()
            || !self.ordinary_object_prototype_intact()
        {
            return None;
        }
        let input = self.ordinary_source_object(source)?;
        let OwnPropertyProjection::Ordinary { source_object } = &input.projection else {
            return None;
        };
        self.observations
            .record_inference_use(input.inference_use());
        self.record_inference_use(
            NativeIntegrityGuard::NativeCallIdentity,
            summary.inference.clone(),
            &summary.dependencies,
        );
        Some(*source_object)
    }

    pub(crate) fn promote_foreign_record(&mut self, outcome: &mut Outcome) {
        // An opaque foreign capability can read or rebind importer cells,
        // through a direct ESM capture or a retained callback. Precision is
        // gained at construction only; later use preserves both obligations.
        self.assuming(AssumedEffectReason::EscapedCallbackClosure, |this| {
            // Foreign code cannot assign an ESM import binding directly. A
            // retained importer callback can perform any source rebind; eval
            // or with defeats that finite source census.
            let rebinds: BTreeSet<_> = if this.state.world.reachability.incomplete {
                this.inputs.binding_owner.keys().cloned().collect()
            } else {
                this.inputs
                    .facts
                    .iter()
                    .flat_map(|fact| fact.rebinds.eager.iter().chain(&fact.rebinds.lazy))
                    .filter(|binding| this.inputs.top_bindings.contains(*binding))
                    .cloned()
                    .collect()
            };
            let reads: Vec<&Id> = this.inputs.binding_owner.keys().collect();
            let rebinds: Vec<&Id> = rebinds.iter().collect();
            this.promote_top_effects(
                &reads,
                &rebinds,
                this.state
                    .current_top_owner
                    .expect("foreign record use has a top owner"),
                outcome,
            )
        });
        self.apply_rebind_effects(&outcome.effects.rebinds);
        let reached: Values = self
            .state
            .world
            .module
            .prefix_values
            .values()
            .flatten()
            .chain(self.state.world.publication.exported_runtime_values.iter())
            .chain(self.state.world.publication.global_escaped_callbacks.iter())
            .cloned()
            .collect();
        self.unsettle(&reached);
        outcome.unknown_call = true;
        self.widen_native_effects();
        outcome.lose_precision(
            &self.inputs.metrics,
            ExecutionPrecisionLossReason::UnknownCallTarget,
            HavocReach::Global {
                cause: GlobalReachCause::ForeignRecord,
            },
            self.state
                .current_top_owner
                .expect("foreign record use has a top owner"),
            DUMMY_SP,
        );
    }

    pub(crate) fn evaluate(
        &mut self,
        expr: &ValueExpr,
        env: &Context,
        captures: &Context,
    ) -> Values {
        self.evaluate_allocation_child(expr, env, captures).values
    }

    pub(crate) fn evaluate_allocation_child(
        &mut self,
        expr: &ValueExpr,
        env: &Context,
        captures: &Context,
    ) -> world::allocation_transfer::AllocationValue {
        let (result, judgment) =
            self.request_with_allocation_judgment(RuntimeOperation::Evaluate {
                expression: expr.clone(),
                environment: env.clone(),
                captures: captures.clone(),
                source_covered: self.source_covered,
            });
        let RuntimeValue::Values(values) = result else {
            unreachable!("expression transfers produce values");
        };
        world::allocation_transfer::AllocationValue { values, judgment }
    }

    /// Availability of a module-local declarative cell at this exact prefix.
    /// Imported cells need a separate provider initialization proof; a global
    /// name may resolve through an accessor on the host's global object.
    pub(crate) fn initialized_local_binding(&self, id: &Id) -> bool {
        self.inputs.top_bindings.contains(id)
            && !self.inputs.index.imported_bindings.contains(id)
            && self.state.world.module.initialized_bindings.contains(id)
    }

    /// The flattened call pass and later initializer projection see the same
    /// prefix only when this owner has no intervening operation. Compound
    /// initializers still need ordered result transport through the runtime.
    pub(crate) fn root_own_data_read(&self, expression: &ValueExpr) -> Option<OwnDataRead> {
        let ValueExpr::Member(receiver, Some(property), key, site) = expression else {
            return None;
        };
        if !key.supports_own_data_read(property) {
            return None;
        }
        let ValueExpr::Binding(receiver) = receiver.as_ref() else {
            return None;
        };
        let owner = *self.inputs.index.member_owners.get(site)?;
        let fact = &self.inputs.facts[owner.0];
        if self.state.current_top_owner != Some(owner)
            || !self.initialized_local_binding(receiver)
            || !self.inputs.index.dynamic_scope_owners.is_empty()
            || !matches!(fact.kind, StatementKind::VarDecl)
            || fact.declared.len() != 1
            || !fact.rebinds.eager.is_empty()
            || !fact.global_writes.is_empty()
            || self.inputs.index.opaque_top.contains(&owner)
            || !self.inputs.index.top_property_writes[owner.0].is_empty()
            || !self.inputs.index.top_member_value_writes[owner.0].is_empty()
            || !self.inputs.index.top_callback_result_writes[owner.0].is_empty()
            || !self.inputs.index.top_exported_values[owner.0].is_empty()
        {
            return None;
        }
        let [
            (
                binding,
                ValueExpr::Member(_, Some(initializer_property), initializer_key, initializer_site),
            ),
        ] = self.inputs.index.top_assignments[owner.0].as_slice()
        else {
            return None;
        };
        let [call] = self.inputs.index.top_calls[owner.0].as_slice() else {
            return None;
        };
        if !initializer_key.supports_own_data_read(initializer_property)
            || initializer_property != property
            || *initializer_site != *site
            || !fact.declared.contains(binding)
            || !self.inputs.index.top_definite_initializers[owner.0].contains(binding)
            || call.kind != CallKind::LocalMemberRead
            || call.caller.is_some()
            || CallSite::from(call.span) != *site
        {
            return None;
        }
        let receivers = self.state.world.module.prefix_values.get(receiver)?;
        self.present_own_data_read(receivers, property)
    }

    pub(crate) fn present_own_data_read(
        &self,
        receivers: &Values,
        property: &str,
    ) -> Option<OwnDataRead> {
        let slot = own_data_transfer::resolve_present_read(
            self,
            receivers,
            property,
            OwnDataOwnership::CurrentPrefix,
        )
        .ok()?;
        if !slot.is_array_specific()
            && !self
                .inputs
                .index
                .plain_data_objects
                .contains(&slot.object())
            || self
                .state
                .world
                .surface
                .applied_accessors
                .get(&slot.object())
                .is_some_and(|accessors| accessors.contains_key(property))
        {
            return None;
        }
        Some(slot.read(self))
    }

    pub(crate) fn local_call_target(&self, targets: &Values) -> Option<(u32, &Callable)> {
        if targets.len() != 1 {
            return None;
        }
        let key = match targets.first()? {
            AbstractValue::Value(Value::Alloc(Kind::Function, key))
            | AbstractValue::Closure(HeapAddress { callable: key, .. }, _) => key,
            _ => return None,
        };
        self.inputs
            .index
            .callables
            .get(key)
            .map(|callable| (*key, callable))
    }

    pub(crate) fn local_body_identity_for_call(&self, call: &Call) -> Option<LocalBodyIdentity> {
        match call.caller {
            Some(key) => {
                let callable = self.inputs.index.callables.get(&key)?;
                Some(LocalBodyIdentity::Callable {
                    key,
                    owner: callable.owner,
                })
            }
            None => {
                let body = self.inputs.index.ordered_initializers.get(&call.owner)?;
                Some(LocalBodyIdentity::RootInitializer {
                    owner: call.owner,
                    initializer_span: body.root_span()?,
                })
            }
        }
    }

    /// The exact root `new C(...)` of a stable local class whose checked
    /// constructor body the kernel's construct rule can run: no other root
    /// operation and no call in the class body.
    pub(crate) fn ordered_root_construction(
        &mut self,
        body: &LocalBody,
        calls: &[Arc<Call>],
    ) -> bool {
        let Some((callee, span)) = body.single_construction_initializer() else {
            return false;
        };
        let [call] = calls else {
            return false;
        };
        if call.kind != CallKind::New
            || call.span != span
            || call.has_spread
            || !matches!(&call.callee, ValueExpr::Binding(binding) if binding == callee)
        {
            return false;
        }
        let Some(key) = self.stable_local_class_key(callee) else {
            return false;
        };
        let inputs = self.inputs;
        !inputs
            .index
            .retained_constructor_calls
            .contains_key(&CallSite::from(call.span))
            && self
                .eligible_local_constructor(key, call.args.len())
                .is_ok_and(|constructor| {
                    kernel_constructor_body(inputs.index, key, constructor).is_some()
                })
    }

    /// Every call of the root is an exact intact intrinsic, so the ordered
    /// lane runs no application code. Identity is decided here; the ordered
    /// call transfer admits or refuses the operands.
    pub(crate) fn ordered_root_calls_are_native(&self, calls: &[Arc<Call>]) -> bool {
        let mut calls = calls
            .iter()
            .filter(|call| call.kind == CallKind::Call)
            .peekable();
        calls.peek().is_some() && calls.all(|call| self.exact_native_root_call(call))
    }

    pub(super) fn exact_native_root_call(&self, call: &Call) -> bool {
        call.kind == CallKind::Call
            && !call.has_spread
            && matches!(
                call.callee,
                ValueExpr::Member(_, Some(_), MemberKey::Static, _)
            )
            && matches!(
                self.native_call_identity(CallKind::Call, &call.callee, CallSite::from(call.span)),
                Some(NativeCallIdentity::Proved(..))
            )
    }

    pub(crate) fn evaluate_binding(&mut self, id: &Id, env: &Context) -> Values {
        if self.inputs.index.callback_factory_imports.contains(id) {
            return Values::from([AbstractValue::CallbackFactory]);
        }
        if self
            .inputs
            .index
            .inline_callback_factory_imports
            .contains(id)
        {
            return Values::from([AbstractValue::InlineCallbackFactory]);
        }
        if self.inputs.index.imported_bindings.contains(id)
            && let Some(value) = self.inputs.imported_values.get(id.0.as_ref())
        {
            // Preserve the source-proved exported value through the
            // ordinary value transfers, including local copies. Reads
            // and import ordering remain separate statement facts.
            return Values::from([AbstractValue::Value(*value)]);
        }
        if self.inputs.top_bindings.contains(id) {
            if let Some(prefix_values) = self.state.world.module.prefix_values.get(id) {
                let mut values = prefix_values.clone();
                if !self.state.world.module.initialized_bindings.contains(id) {
                    values.insert(if self.inputs.var_bindings.contains(id) {
                        AbstractValue::UNDEFINED
                    } else {
                        AbstractValue::Value(Value::Unavailable)
                    });
                }
                values
            } else if self.inputs.var_bindings.contains(id) {
                Values::from([AbstractValue::UNDEFINED])
            } else {
                // Whole-chunk flow includes future initializers. A
                // missing prefix value for a local binding is TDZ.
                Values::from([AbstractValue::Value(Value::Unavailable)])
            }
        } else if let Some(cell) = self.state.world.active_cells.get(id) {
            self.state.world.cells[cell.0].values.clone()
        } else if let Some(values) = env.get(id) {
            values.clone()
        } else if self
            .inputs
            .index
            .global_object_names
            .contains(id.0.as_ref())
            && !self.inputs.flow.is_declared(id)
            && !self.inputs.shadowed.contains(id.0.as_ref())
        {
            Values::from([AbstractValue::GlobalObject])
        } else {
            let values = from_flow(self.inputs.flow.values_of_binding(id));
            if values.is_empty() { unknown() } else { values }
        }
    }

    pub(crate) fn evaluate_new(
        &mut self,
        callee: &ValueExpr,
        args: &[ValueExpr],
        span: CallSite,
        env: &Context,
        captures: &Context,
    ) -> NewEvaluation {
        let mut operation_coverage = None;
        let values = (|| {
            let targets = self.evaluate(callee, env, captures);
            let evaluated_args: Vec<_> = args
                .iter()
                .map(|arg| self.evaluate(arg, env, captures))
                .collect();
            let mut successful_args = evaluated_args.clone();
            let mut args_may_throw = targets.contains(&AbstractValue::Value(Value::Unavailable));
            for values in &mut successful_args {
                args_may_throw |= values.remove(&AbstractValue::Value(Value::Unavailable));
            }
            if let Some(summary) = self.foreign_call_summary(CallKind::New, callee) {
                if (targets.len() == 1
                    && targets.contains(&AbstractValue::Value(Value::Unavailable)))
                    || evaluated_args.iter().any(|values| {
                        values.len() == 1
                            && values.contains(&AbstractValue::Value(Value::Unavailable))
                    })
                {
                    return Values::from([AbstractValue::Value(Value::Unavailable)]);
                }
                let mut result = self.foreign_call_result(&summary, &successful_args);
                if args_may_throw {
                    result.insert(AbstractValue::Value(Value::Unavailable));
                }
                return result;
            }
            if let Some(returns) =
                self.resolve_native_call(CallKind::New, callee, span, args, &successful_args)
            {
                self.record_native_initializer_transfer(
                    callee,
                    args,
                    &successful_args,
                    args_may_throw,
                    span,
                    &returns,
                );
                if !args_may_throw {
                    operation_coverage = returns.operation_coverage.clone();
                }
                return self.execute_native_resolution(
                    returns,
                    &successful_args,
                    args_may_throw,
                    span,
                );
            }
            // The iterable assertion does not bypass native-row checks:
            // if this exact request and indexed SetIterableProof do not
            // admit the Set New row, retain the conservative value.
            if self
                .inputs
                .set_iterable_proof
                .author_input_for_constructor(span.source_span())
                .is_some()
            {
                return Values::from([
                    AbstractValue::Value(Value::Alloc(Kind::Set, span.allocation_id())),
                    AbstractValue::Value(Value::Unavailable),
                ]);
            }
            if self
                .inputs
                .index
                .retained_constructor_calls
                .contains_key(&span)
            {
                if targets.len() == 1 && targets.contains(&AbstractValue::Value(Value::Unavailable))
                {
                    return Values::from([AbstractValue::Value(Value::Unavailable)]);
                }
                if evaluated_args.iter().any(|values| {
                    values.len() == 1 && values.contains(&AbstractValue::Value(Value::Unavailable))
                }) {
                    return Values::from([AbstractValue::Value(Value::Unavailable)]);
                }
                let mut successful_args = evaluated_args.clone();
                let mut may_throw = false;
                for values in &mut successful_args {
                    may_throw |= values.remove(&AbstractValue::Value(Value::Unavailable));
                }
                if let Some(callbacks) =
                    self.retained_constructor_callbacks(span, args, &successful_args)
                {
                    let result =
                        self.audited_retained_result(RetainedResultKind::Constructor, callbacks);
                    return if may_throw {
                        Values::from([result, AbstractValue::Value(Value::Unavailable)])
                    } else {
                        Values::from([result])
                    };
                }
            }
            let mut results = Values::new();
            if self.state.current_top_owner.is_some_and(|owner| {
                self.is_proven_map_entry_array(callee, args, env, captures, owner)
            }) {
                let mut can_complete = true;
                let mut may_throw = false;
                let mut successful_args = Values::new();
                for values in &evaluated_args {
                    if values.contains(&AbstractValue::Value(Value::Unavailable)) {
                        may_throw = true;
                        can_complete &= values.len() > 1;
                    }
                    successful_args.extend(
                        values
                            .iter()
                            .filter(|value| **value != AbstractValue::Value(Value::Unavailable))
                            .cloned(),
                    );
                }
                if can_complete {
                    let callbacks = self.callback_values(&successful_args);
                    results.insert(self.deferred_callbacks(callbacks));
                }
                if may_throw || !can_complete {
                    results.insert(AbstractValue::Value(Value::Unavailable));
                }
                return results;
            }
            for target in targets {
                match target {
                    AbstractValue::Value(Value::Alloc(Kind::Class, key)) => {
                        if let Ok((_, instance)) = self.construct_local_class(key, &evaluated_args)
                        {
                            results.insert(instance);
                        } else {
                            // Re-evaluation may lose the private instance proof after
                            // constructor execution taints one of its inputs. Keep the
                            // result opaque, but do not drop callbacks reachable from
                            // the arguments: the unknown instance may retain them.
                            let refused = self.refused_construction_values(&successful_args);
                            results.extend(refused);
                        }
                    }
                    AbstractValue::Value(Value::Unavailable) => {
                        results.insert(AbstractValue::Value(Value::Unavailable));
                    }
                    // `new` of a plain function yields the instance, unknown
                    // here, or an object the body returns. Unknown alone
                    // reaches no unescaped callable: keep what the body can
                    // return and what the arguments can reach.
                    AbstractValue::Value(Value::Alloc(Kind::Function, function))
                    | AbstractValue::Closure(
                        HeapAddress {
                            callable: function, ..
                        },
                        _,
                    ) if self.inputs.index.callables.contains_key(&function) => {
                        let captured = match &target {
                            AbstractValue::Closure(_, captured) => Some(captured.clone()),
                            _ => None,
                        };
                        let returned = self
                            .invoke(function, successful_args.clone(), captured, false)
                            .returns;
                        results.extend(returned.into_iter().filter(|value| !value.is_primitive()));
                        results.extend(self.refused_construction_values(&successful_args));
                    }
                    _ => {
                        results.insert(AbstractValue::Value(Value::Top));
                    }
                }
            }
            if results.is_empty() {
                unknown()
            } else {
                results
            }
        })();
        NewEvaluation {
            values,
            operation_coverage,
        }
    }

    /// Evaluate one source-checked root. A native operation returns its own
    /// effect receipt; the owner later seals that receipt against source identity.
    pub(crate) fn evaluate_initializer_transfer(
        &mut self,
        source: &OriginalInitializer,
        expression: &ValueExpr,
        env: &Context,
        captures: &Context,
    ) -> (
        Values,
        world::allocation_transfer::AllocationJudgment,
        Option<NativeConstructorTransfer>,
        Option<EffectCoverageFragment>,
    ) {
        let expression = if let Some(covered) = source.composed_expression() {
            self.cover_source_expression();
            covered
        } else {
            expression
        };
        self.native_initializer_candidate = source.native_constructor_candidate(expression);
        self.native_initializer_transfer = None;
        let (values, operation_coverage) = match expression {
            ValueExpr::New(callee, arguments, site)
                if source.operation_source(expression).is_some() =>
            {
                self.mark_allocation_opaque();
                let result = self.evaluate_new(callee, arguments, *site, env, captures);
                (result.values, result.operation_coverage)
            }
            _ => (self.evaluate_transfer(expression, env, captures), None),
        };
        let transfer = self.native_initializer_transfer.take();
        self.native_initializer_candidate = None;
        (
            values,
            self.allocation_judgment.clone(),
            transfer,
            operation_coverage,
        )
    }

    /// Stop the frame without replaying its earlier operations. An unproved
    /// read, store or call may publish capabilities the remainder can reach,
    /// including objects whose local binding has since been replaced.
    pub(crate) fn local_operation_fallback(
        &mut self,
        frame: &LocalFrame,
        mut reached: Values,
        owner: SourceUnitOrdinal,
        reason: ExecutionPrecisionLossReason,
        span: Span,
        outcome: &mut Outcome,
    ) {
        reached.extend(frame.inputs.iter().cloned());
        reached.extend(frame.environment.values().flatten().cloned());
        reached.extend(
            self.state
                .world
                .active_cells
                .values()
                .flat_map(|cell| self.state.world.cells[cell.0].values.iter().cloned()),
        );
        reached.extend(
            frame
                .fresh_records
                .iter()
                .map(|object| AbstractValue::Value(Value::Alloc(Kind::Object, *object))),
        );
        let cells: Vec<_> = self.state.world.active_cells.values().copied().collect();
        self.havoc_cells(cells);
        self.publish_values(
            &reached,
            ClassMembersPublicationWriter::LocalOperationFallback,
            Some(span),
        );
        let mut fallback = self.bounded_invocation_fallback(BoundedInvocationFallbackRequest {
            owner,
            arguments: vec![reached],
            captured: frame.environment.clone(),
            reason,
            reach: HavocReach::Global {
                cause: GlobalReachCause::UnmodelledOperation,
            },
            span,
            diagnostics: BoundedInvocationFallbackDiagnostics::default(),
        });
        outcome.absorb_call_effects(&mut fallback.effects, self.state.assumption.as_ref());
        outcome.executed_owners.extend(fallback.executed_owners);
        outcome.unknown_call |= fallback.unknown_call;
        outcome.inherit_completeness(fallback.complete, fallback.effects.precision_losses);
        outcome.returns.extend(fallback.returns);
        outcome
            .returns
            .insert(AbstractValue::Value(Value::Unavailable));
    }

    pub(crate) fn execute_local_body(
        &mut self,
        body: &LocalBody,
        environment: Context,
        parameters: Option<Vec<SelectedValue>>,
        owner: SourceUnitOrdinal,
        activation: LocalActivation,
    ) -> Outcome {
        let scope = activation.scope();
        let mut frame = LocalFrame::new(environment);
        if let Some(parameters) = &parameters {
            frame.inputs.extend(
                parameters
                    .iter()
                    .flat_map(|parameter| parameter.values.iter())
                    .cloned(),
            );
        }
        frame.parameter_inputs = parameters;
        let root_binding_effect_identity = match activation {
            LocalActivation::RootInitializer(identity) => Some(identity),
            LocalActivation::Constructor { class, instance } => {
                construct::bind_receiver(&mut frame, class, instance);
                None
            }
            LocalActivation::Callable(_) | LocalActivation::RootStatement => None,
        };
        frame.borrowed_records = self.reachable_objects(&frame.inputs);
        let mut outcome = Outcome {
            complete: true,
            root_binding_effect_identity,
            ..Outcome::default()
        };
        if let Some(identity) = &outcome.root_binding_effect_identity {
            outcome.effects.binding_effect_coverage =
                BindingEffectCoverage::OrderedRootInitializer {
                    identity: identity.clone(),
                    receipt: None,
                };
        }
        outcome.executed_owners.insert(owner);
        // The source-covered application consumer supplies access effects and
        // conservative fallback. The shared kernel controls operation order.
        let family = driver::drive(
            &mut LocalExecutionContext {
                executor: self,
                outcome: &mut outcome,
                owner,
                scope,
                body,
            },
            body,
            LocalContinuation::new(frame),
        );
        match family {
            Family::One(leaf) => {
                self.adopt_local_leaf(body, *leaf, &mut outcome, owner, scope);
            }
            Family::Forked(leaves) => {
                // Each leaf is adopted in the path state it completed in;
                // the body's caller resumes from their join.
                let mut frames = Vec::with_capacity(leaves.len());
                let mut paths = Vec::with_capacity(leaves.len());
                for (leaf, path) in leaves {
                    let mut context = LocalExecutionContext {
                        executor: self,
                        outcome: &mut outcome,
                        owner,
                        scope,
                        body,
                    };
                    context.enter(path);
                    let frame = self.adopt_local_leaf(body, leaf, &mut outcome, owner, scope);
                    paths.push(self.state.path(&self.allocation_judgment, &outcome));
                    frames.push(frame);
                }
                let JoinedPath { path, refused } = ExecutionPath::join(paths);
                let mut context = LocalExecutionContext {
                    executor: self,
                    outcome: &mut outcome,
                    owner,
                    scope,
                    body,
                };
                context.enter(path);
                if refused.is_some() {
                    // The leaves left worlds no single caller can resume.
                    // Everything any leaf could reach is summarized once
                    // against the pointwise join.
                    let span = self.local_body_span(body, scope);
                    LocalExecutionContext {
                        executor: self,
                        outcome: &mut outcome,
                        owner,
                        scope,
                        body,
                    }
                    .fallback(
                        &join_frames(frames),
                        Values::new(),
                        ExecutionPrecisionLossReason::UnsupportedOperation {
                            operation: ExecutionOperation::OpaqueExecution,
                            ordered_field_read_refusal: None,
                            ordered_call_admission_refusal: None,
                        },
                        span,
                    );
                    outcome.effect_coverage = EffectCoverageFragment::default();
                    outcome.return_selection = None;
                }
            }
        }
        outcome
    }

    pub(super) fn local_body_span(&self, body: &LocalBody, scope: LocalExecutionScope) -> Span {
        match scope {
            LocalExecutionScope::Callable(key) => self
                .inputs
                .index
                .callable_spans
                .get(&key)
                .copied()
                .unwrap_or(DUMMY_SP),
            LocalExecutionScope::Root => body.root_span().unwrap_or(DUMMY_SP),
            LocalExecutionScope::Constructor(class) => self
                .inputs
                .index
                .class_spans
                .get(&class)
                .copied()
                .unwrap_or(DUMMY_SP),
        }
    }

    /// Record one completed path of a body in the live path state: the cells
    /// it touched, the values it threw, the summary of a refused remainder
    /// and its results. Returns the leaf's frame.
    pub(super) fn adopt_local_leaf(
        &mut self,
        body: &LocalBody,
        leaf: Leaf,
        outcome: &mut Outcome,
        owner: SourceUnitOrdinal,
        scope: LocalExecutionScope,
    ) -> LocalFrame {
        let Leaf { frame, attempt } = leaf;
        // The kernel returns the cells it touched and the values it threw;
        // this scope records them as the owner's effects and publications.
        let non_assumed = self.state.assumption.is_none();
        for cell in frame.ledger.heap_accesses.reads.iter().cloned() {
            outcome.effects.record_heap_read(cell, non_assumed);
        }
        for cell in frame.ledger.heap_accesses.writes.iter().cloned() {
            outcome.effects.record_heap_write(cell, non_assumed);
        }
        for thrown in frame.ledger.completion.exact_abrupt() {
            self.publish_values(
                &thrown.values,
                ClassMembersPublicationWriter::ThrowValuePublication,
                None,
            );
        }
        match attempt {
            LocalExecutionAttempt::Completed(result) => {
                outcome.effect_coverage = result.ledger.effects;
            }
            LocalExecutionAttempt::Refused(RemainderDisposition::AlreadySummarized) => {}
            LocalExecutionAttempt::Refused(RemainderDisposition::NeedsSummary) => {
                // The kernel stopped at an operation it cannot represent and no
                // adapter path summarized the remainder on the way out.
                let span = self.local_body_span(body, scope);
                LocalExecutionContext {
                    executor: self,
                    outcome,
                    owner,
                    scope,
                    body,
                }
                .fallback(
                    &frame,
                    Values::new(),
                    ExecutionPrecisionLossReason::UnsupportedOperation {
                        operation: ExecutionOperation::OpaqueExecution,
                        ordered_field_read_refusal: None,
                        ordered_call_admission_refusal: None,
                    },
                    span,
                );
            }
        }
        let returns = frame.returns();
        // A callable's caller cannot track a returned map; a root
        // initializer's completion value is the declared binding's, whose
        // map identity the prefix keeps.
        if let LocalExecutionScope::Callable(_) = scope {
            for value in &returns {
                if let AbstractValue::OwnDescriptorMap(site) = value {
                    self.invalidate_own_descriptor_map(*site);
                }
            }
        }
        outcome.returns.extend(returns);
        outcome.return_selection = frame
            .return_selection()
            .filter(|selection| selection.matches(&outcome.returns));
        frame
    }

    /// A source-summary lane flattens control flow, and a value may leave
    /// a set the lane tracks only once it is published: reading unknown
    /// then reaches it through the escaped-callback pool. A local another
    /// callable writes, one an assignment reads before a later assignment,
    /// or a loop-head binding reads as unknown in addition to the lane's own
    /// assignments; stores to `untracked_locals` and iterated values publish.
    /// Only widens an entry the environment or an assignment gives. Kept out
    /// of line for `invoke`'s frame size.
    #[inline(never)]
    pub(super) fn widen_untracked_locals(&self, callable: &Callable, env: &mut Context) {
        for (id, _) in &callable.local_assignments {
            if self.inputs.stale_locals.contains(id) || callable.head_bindings.contains(id) {
                env.entry(id.clone()).or_default().extend(unknown());
            }
        }
        let stale: Vec<Id> = env
            .keys()
            .filter(|id| {
                self.inputs.stale_locals.contains(*id) || callable.head_bindings.contains(*id)
            })
            .cloned()
            .collect();
        for id in stale {
            env.entry(id).or_default().extend(unknown());
        }
    }
}
