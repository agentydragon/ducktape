//! Escape of untracked values, iterator protocol, and callable invocation.

use super::*;

impl<'source, 'rule> Executor<'source, 'rule> {
    /// A class value created now: its members read the enclosing scope's
    /// bindings as unknown later, so publish what those hold here. Its
    /// superclass can also be called after this environment has gone away.
    pub(crate) fn escape_class_captures(&mut self, class: u32, env: &Context, captures: &Context) {
        let mut values: Values = self
            .inputs
            .class_captured_reads
            .get(&class)
            .into_iter()
            .flatten()
            .filter(|binding| !self.inputs.top_bindings.contains(*binding))
            .flat_map(|binding| env.get(binding).or_else(|| captures.get(binding)))
            .flatten()
            .cloned()
            .collect();

        let facts = self.inputs.index.class_values.get(&class);
        match facts.and_then(|facts| facts.super_class.as_ref()) {
            Some(ValueExpr::Binding(binding))
                if facts.is_some_and(|facts| facts.lexical_heritage.is_some()) =>
            {
                if !self.inputs.top_bindings.contains(binding) {
                    values.extend(
                        env.get(binding)
                            .or_else(|| captures.get(binding))
                            .into_iter()
                            .flatten()
                            .cloned(),
                    );
                }
            }
            None => {}
            Some(heritage) => {
                // The eventual base retains none of this environment. Publish
                // its inputs without evaluating the expression again: a
                // heritage factory may have effects already accounted for by
                // the flattened call pass.
                let mut inputs = facts
                    .map(|facts| {
                        facts
                            .discarded_heritage_call
                            .as_ref()
                            .filter(|call| {
                                call.argument_reads.iter().all(|binding| {
                                    self.inputs.flow.is_declared(binding)
                                        && (self.inputs.top_bindings.contains(binding)
                                            || env.contains_key(binding)
                                            || captures.contains_key(binding))
                                }) && self
                                    .inputs
                                    .index
                                    .inert_heritage_callee(&call.callee, self.inputs.flow)
                                    && self
                                        .inputs
                                        .index
                                        .stable_heritage_function(&call.base, self.inputs.flow)
                                        .is_some()
                            })
                            .map_or_else(
                                || facts.super_class_inputs.clone(),
                                |call| call.remaining_inputs.clone(),
                            )
                    })
                    .unwrap_or_default();
                heritage.bindings(&mut inputs);
                let mut incomplete = heritage.bindings_may_be_incomplete()
                    || facts.is_some_and(|facts| facts.super_class_inputs_incomplete);
                for binding in inputs {
                    let possible = if self.inputs.top_bindings.contains(&binding) {
                        self.state.world.module.prefix_values.get(&binding)
                    } else {
                        env.get(&binding).or_else(|| captures.get(&binding))
                    };
                    if let Some(possible) = possible {
                        values.extend(possible.iter().cloned());
                    } else {
                        incomplete = true;
                    }
                }
                if incomplete {
                    // An untracked local factory or lowered-away input can
                    // return a base reaching any of these capabilities.
                    values.extend(
                        env.iter()
                            .chain(captures)
                            .filter(|(binding, _)| !self.inputs.top_bindings.contains(*binding))
                            .flat_map(|(_, possible)| possible.iter().cloned()),
                    );
                }
                if incomplete && let Some(owner) = self.inputs.index.class_owners.get(&class) {
                    let reads = &self.inputs.facts[owner.0].reads;
                    values.extend(
                        reads
                            .eager
                            .iter()
                            .chain(&reads.lazy)
                            .filter_map(|binding| {
                                self.state.world.module.prefix_values.get(binding)
                            })
                            .flatten()
                            .cloned(),
                    );
                }
            }
        }
        if !values.is_empty() {
            self.escape_untracked_values(&values);
        }
    }

    /// Values flowing where a later read sees only unknown. Unknown reaches
    /// only published callbacks, so a value may leave a tracked set only once
    /// what it can reach is published.
    pub(crate) fn escape_untracked_values(&mut self, values: &Values) {
        self.publish_values(
            values,
            ClassMembersPublicationWriter::UntrackedLocalStore,
            None,
        );
    }

    /// A parameter's incoming value, or a captured value, enters the
    /// environment without a store; publish it where the binding is
    /// untracked, as a store would. A pattern parameter's bindings have no
    /// environment entry: an untracked one reads unknown, and the whole
    /// argument is published.
    #[inline(never)]
    pub(super) fn escape_untracked_environment(
        &mut self,
        callable: &Callable,
        reads_arguments: bool,
        args: &[Values],
        env: &mut Context,
    ) {
        for (id, values) in env.iter() {
            if self.inputs.untracked_locals.contains(id) {
                self.escape_untracked_values(values);
            }
        }
        // `arguments` reads every incoming value as unknown, also from an
        // arrow nested in the callable, whose `arguments` is this one's.
        if reads_arguments {
            for values in args {
                self.escape_untracked_values(values);
            }
        }
        for (index, parameter) in callable.params.iter().enumerate() {
            if parameter.binding.is_some() {
                continue;
            }
            let mut values = args
                .get(index)
                .cloned()
                .unwrap_or_else(|| Values::from([AbstractValue::UNDEFINED]));
            if let Some(default) = &parameter.default
                && may_be_undefined(&values)
            {
                values.remove(&AbstractValue::UNDEFINED);
                values.extend(self.evaluate(default, env, env));
            }
            // Bind each pattern binding to its projection of the incoming
            // value. A part the pattern or the projection loses to unknown
            // is published with the whole value.
            env.insert(pattern_source(), values.clone());
            let mut loses_values = parameter.pattern_loses_values;
            for (binding, projection) in &parameter.pattern_assignments {
                let projected = self.evaluate(projection, env, env);
                loses_values |= projected.contains(&AbstractValue::Value(Value::Top));
                env.entry(binding.clone()).or_default().extend(projected);
            }
            env.remove(&pattern_source());
            if loses_values {
                self.escape_untracked_values(&values);
            }
            let untracked: Vec<&Id> = parameter
                .bindings
                .iter()
                .filter(|binding| self.inputs.untracked_locals.contains(*binding))
                .collect();
            if !untracked.is_empty() {
                self.escape_untracked_values(&values);
                for binding in untracked {
                    env.entry(binding.clone()).or_default().extend(unknown());
                }
            }
        }
    }

    /// Binding an array-destructuring parameter runs the iterator protocol
    /// on its incoming value, the default included.
    #[inline(never)]
    pub(super) fn iterate_parameters(
        &mut self,
        callable: &Callable,
        args: &[Values],
        env: &Context,
        outcome: &mut Outcome,
    ) {
        for (index, parameter) in callable.params.iter().enumerate() {
            if !parameter.pattern_iterates {
                continue;
            }
            let mut values = args
                .get(index)
                .cloned()
                .unwrap_or_else(|| Values::from([AbstractValue::UNDEFINED]));
            if let Some(default) = &parameter.default
                && may_be_undefined(&values)
            {
                values.remove(&AbstractValue::UNDEFINED);
                values.extend(self.evaluate(default, env, env));
            }
            self.run_iterator_protocol(&values, callable.owner, outcome);
            if parameter.pattern_iterates_part {
                self.run_iterator_protocol(&unknown(), callable.owner, outcome);
            }
            for default in &parameter.iterated_defaults {
                let values = self.evaluate(default, env, env);
                self.run_iterator_protocol(&values, callable.owner, outcome);
            }
        }
    }

    /// The iterator protocol a top-level owner runs on its
    /// `top_iterated_sources`.
    pub(crate) fn iterate_top_sources(
        &mut self,
        owner: SourceUnitOrdinal,
        env: &Context,
        captures: &Context,
        outcome: &mut Outcome,
    ) {
        let Some(sources) = self.inputs.index.top_iterated_sources.get(&owner) else {
            return;
        };
        for source in sources {
            let values = self.evaluate(source, env, captures);
            self.run_iterator_protocol(&values, owner, outcome);
        }
    }

    /// Calling generator `key` creates an opaque generator object. Its body
    /// runs when some later code resumes the object (`next()`, `for`-`of`,
    /// spread, destructuring): the body is a published callback, so each
    /// resumption, an unknown call, promotes the body's effects to the
    /// resuming owner. What the body can reach (the arguments and captures,
    /// which the frame's environment holds) is published with it.
    #[inline(never)]
    pub(super) fn create_generator(
        &mut self,
        key: u32,
        args: &[Values],
        env: &Context,
        outcome: &mut Outcome,
    ) {
        let reached: Values = args.iter().chain(env.values()).flatten().cloned().collect();
        self.publish_values(
            &reached,
            ClassMembersPublicationWriter::GeneratorCreation,
            None,
        );
        let body = self.deferred_callbacks(Values::from([AbstractValue::Value(Value::Alloc(
            Kind::Function,
            key,
        ))]));
        self.insert_escaped_callback_values(
            &Values::from([body]),
            ClassMembersPublicationWriter::GeneratorCreation,
            None,
            None,
        );
        outcome.returns.insert(AbstractValue::Value(Value::Top));
    }

    /// The iterator protocol a callable's body runs on its
    /// `iterated_sources`.
    #[inline(never)]
    pub(super) fn iterate_callable_sources(
        &mut self,
        callable: &Callable,
        env: &Context,
        outcome: &mut Outcome,
    ) {
        for iterated in &callable.iterated_sources {
            let values = self.evaluate(iterated, env, env);
            self.run_iterator_protocol(&values, callable.owner, outcome);
        }
    }

    /// Iterating `values` calls their `[Symbol.iterator]` and the iterator's
    /// `next`. Over anything but a proven native iterable those are unknown
    /// callees: the iterated value is published and the operation may run
    /// any published callback (a generator body, a hand-written `next`),
    /// charged to the operation's owner. A primitive's method comes from its
    /// prototype, where only a published function can have been installed:
    /// the operation may run the published callbacks, and nothing else.
    #[inline(never)]
    pub(super) fn run_iterator_protocol(
        &mut self,
        values: &Values,
        owner: SourceUnitOrdinal,
        outcome: &mut Outcome,
    ) {
        match self.iteration_proof(values) {
            IterationProof::Native => {}
            IterationProof::PrototypeLookup => {
                self.invoke_possible_callbacks_at(&[], false, None, outcome);
            }
            IterationProof::Unknown => {
                outcome.lose_call_target(&self.inputs.metrics, owner, DUMMY_SP);
                outcome.unknown_call = true;
                self.invoke_possible_callbacks_at(
                    std::slice::from_ref(values),
                    false,
                    None,
                    outcome,
                );
            }
        }
    }

    /// Which user code iterating `values` can run. `Native`: none, for
    /// `null`/`undefined` (which throw), a throwing read, or an array
    /// allocation no opaque code reached under intact Array intrinsics.
    /// `PrototypeLookup`: what another primitive's prototype holds.
    pub(super) fn iteration_proof(&mut self, values: &Values) -> IterationProof {
        if values.is_empty() {
            return IterationProof::Unknown;
        }
        let mut arrays = false;
        let mut primitives = false;
        for value in values {
            match value {
                AbstractValue::Value(Value::Prim(kinds))
                    if kinds.is_only(PrimKinds::UNDEFINED.union(PrimKinds::NULL)) => {}
                AbstractValue::Value(Value::Prim(_)) => primitives = true,
                AbstractValue::Value(Value::Unavailable) => {}
                AbstractValue::Value(Value::Alloc(kind, object))
                    if (*kind == Kind::Array
                        || (*kind == Kind::Object
                            && self.inputs.index.array_elements.contains_key(object)))
                        && !self.state.world.surface.tainted_objects.contains(object) =>
                {
                    arrays = true;
                }
                _ => return IterationProof::Unknown,
            }
        }
        if arrays && !self.array_slots_intact(ARRAY_PROTOTYPE_SLOTS) {
            return IterationProof::Unknown;
        }
        if primitives {
            IterationProof::PrototypeLookup
        } else {
            IterationProof::Native
        }
    }

    /// A top-level owner's values whose parts reach a binding only as
    /// unknown, published like a callable's `published_sources`.
    pub(crate) fn publish_top_sources(
        &mut self,
        owner: SourceUnitOrdinal,
        env: &Context,
        captures: &Context,
    ) {
        let Some(sources) = self.inputs.index.top_published_sources.get(&owner) else {
            return;
        };
        for source in sources {
            let values = self.evaluate(source, env, captures);
            self.escape_untracked_values(&values);
        }
    }

    /// Values whose parts reach a binding only as unknown (an iterated
    /// value's elements, a destructured value's parts, a thrown or yielded
    /// value); publish what they can reach.
    #[inline(never)]
    pub(super) fn publish_sources(&mut self, callable: &Callable, env: &Context) {
        for iterated in &callable.published_sources {
            let values = self.evaluate(iterated, env, env);
            self.escape_untracked_values(&values);
        }
    }

    pub(crate) fn invoke(
        &mut self,
        key: u32,
        args: Vec<Values>,
        captured: Option<CapturedContext>,
        fresh_checked_constructor: bool,
    ) -> Outcome {
        let argument_selections = vec![None; args.len()];
        self.invoke_selected(
            key,
            args,
            argument_selections,
            captured,
            fresh_checked_constructor,
        )
    }

    pub(super) fn invoke_selected(
        &mut self,
        key: u32,
        args: Vec<Values>,
        argument_selections: Vec<Option<CallableSelection>>,
        captured: Option<CapturedContext>,
        fresh_checked_constructor: bool,
    ) -> Outcome {
        // The invocation's outcome (and its memo) records provenance relative
        // to the invocation; the caller's merge applies its own assumption.
        let assumption = self.state.assumption.take();
        let result = self
            .request_transfer(
                RuntimeOperation::Invoke {
                    callable: key,
                    arguments: args,
                    argument_selections,
                    captured,
                    fresh_checked_constructor,
                },
                assumption.clone(),
            )
            .0;
        self.state.assumption = assumption;
        let RuntimeValue::Outcome(outcome) = result else {
            unreachable!("invocation transfers produce outcomes");
        };
        *outcome
    }

    pub(crate) fn invoke_transfer(
        &mut self,
        key: u32,
        args: Vec<Values>,
        argument_selections: Vec<Option<CallableSelection>>,
        captured: Option<CapturedContext>,
        fresh_checked_constructor: bool,
    ) -> Outcome {
        let outer_cells = std::mem::replace(
            &mut self.state.world_mut().active_cells,
            captured
                .as_ref()
                .map(|context| context.values.cells.clone())
                .unwrap_or_default(),
        );
        let mut outcome = self.invoke_with_cells(
            key,
            args,
            argument_selections,
            captured,
            fresh_checked_constructor,
        );
        self.record_callable_heap_contents(key, &mut outcome);
        if self.callable_has_no_normal_return(key)
            && outcome
                .returns
                .iter()
                .any(|value| *value != AbstractValue::Value(Value::Unavailable))
        {
            // An opaque nested operation can carry values for effect reach.
            // They are not successful returns of a source throw-only body.
            // Preserve their publication while retaining the abrupt result.
            let reached = std::mem::take(&mut outcome.returns);
            self.publish_values(
                &reached,
                ClassMembersPublicationWriter::ThrowValuePublication,
                None,
            );
            outcome
                .returns
                .insert(AbstractValue::Value(Value::Unavailable));
        }
        if !outcome.complete || outcome.unknown_call {
            let cells: Vec<_> = self.state.world.active_cells.values().copied().collect();
            self.havoc_cells(cells);
        }
        self.state.world_mut().active_cells = outer_cells;
        outcome
    }

    pub(crate) fn callable_has_no_normal_return(&self, key: u32) -> bool {
        self.inputs
            .index
            .callables
            .get(&key)
            .is_some_and(|callable| {
                !callable.generator
                    && !self.inputs.index.async_callables.contains(&key)
                    && !callable.may_fall_through
                    && callable.body_facts.value_flow().returns().is_empty()
            })
    }

    pub(super) fn invoke_with_cells(
        &mut self,
        key: u32,
        args: Vec<Values>,
        argument_selections: Vec<Option<CallableSelection>>,
        captured: Option<CapturedContext>,
        fresh_checked_constructor: bool,
    ) -> Outcome {
        let Some((callable_owner, uses_arguments)) =
            self.inputs.index.callables.get(&key).map(|callable| {
                (
                    callable.owner,
                    callable.reads.iter().any(|id| id.0.as_ref() == "arguments"),
                )
            })
        else {
            return Outcome::default();
        };
        let unsupported_inputs = uses_arguments
            || self.inputs.index.callables[&key]
                .params
                .iter()
                .any(|parameter| parameter.binding.is_none());
        // Only ordinary calls with no live captures can acquire parameter
        // receipts. Other invocation kinds retain their compatibility path.
        let callable = &self.inputs.index.callables[&key];
        let covered_prologue = !uses_arguments
            && !callable.implicit_capture
            && !callable.generator
            && !callable.opaque
            && !self.inputs.index.async_callables.contains(&key)
            && !fresh_checked_constructor
            && captured
                .as_ref()
                .is_none_or(|context| context.values.is_empty());
        if unsupported_inputs {
            self.taint_unsupported_inputs(&args);
        }
        let captured_context =
            self.materialize_captures(captured.as_ref().map(|context| context.values.as_ref()));
        self.record_executed_owner(callable_owner);
        self.state.statement_invoked_callables.insert(key);
        // Memoized outcomes still represent an invocation at this point.
        self.activate_native_effects(callable_owner, Some(key));
        let signature = InvocationSignature {
            callable: key,
            arguments: args.clone(),
            argument_selections: argument_selections.clone(),
            captured_context: captured.clone(),
            fresh_checked_constructor,
            reachability_revision: self.state.world.revision.reachability,
            prefix_revision: self.state.world.revision.prefix,
            top_owner: self.state.current_top_owner,
        };
        if let Some(outcome) = self.memoized_invocation(key, &signature) {
            return outcome;
        }
        let active_invocation = ActiveInvocation {
            callable: key,
            top_owner: self.state.current_top_owner,
        };
        if let Some(reason) = self.invocation_bound_reason(&active_invocation) {
            let reach = self.invocation_reach(key, callable_owner, &args, &captured_context);
            return self.bounded_invocation_fallback(BoundedInvocationFallbackRequest {
                owner: callable_owner,
                arguments: args,
                captured: captured_context,
                reason,
                reach,
                span: DUMMY_SP,
                diagnostics: BoundedInvocationFallbackDiagnostics::default(),
            });
        }
        self.state.active.insert(active_invocation);
        self.state.invocation_depth += 1;
        self.state.invocation_count += 1;
        let Some(callable) = self.inputs.index.callables.get(&key) else {
            self.state.active.remove(&active_invocation);
            self.state.invocation_depth -= 1;
            return Outcome::default();
        };
        let mut env = captured_context.clone();
        // A callable reading `arguments` (itself or through a nested arrow)
        // runs on the source-summary lane, which publishes its arguments.
        let reads_arguments = self.inputs.reads_arguments.contains(&key);
        let ordered_prologue = covered_prologue
            && !unsupported_inputs
            && !reads_arguments
            && self.ordered_prologue_admitted(callable, &env);
        self.bind_parameters(callable, &args, ordered_prologue, &mut env);
        let unsupported_before = self.state.array_iteration_unsupported;
        if self.state.array_iteration_depth > 0 {
            self.check_iteration_body_inputs(key, callable, &env);
        }
        if let Some(body) = self.ordered_lane_body(
            key,
            callable,
            &env,
            unsupported_inputs || reads_arguments,
            ordered_prologue,
        ) {
            let mut outcome = self.execute_local_body(
                body,
                env,
                ordered_prologue.then(|| {
                    args.iter()
                        .cloned()
                        .zip(argument_selections.iter().cloned())
                        .map(|(values, selection)| SelectedValue { values, selection })
                        .collect()
                }),
                callable.owner,
                LocalActivation::Callable(key),
            );
            if !covered_prologue || (!callable.params.is_empty() && !ordered_prologue) {
                outcome.effect_coverage = EffectCoverageFragment::default();
            }
            outcome.body_invoked = true;
            self.end_invocation(&active_invocation, signature, &outcome);
            return outcome;
        }
        self.prepare_summary_environment(callable, reads_arguments, &args, &mut env);
        let mut outcome = Outcome {
            complete: true,
            ..Outcome::default()
        };
        outcome.executed_owners.insert(callable.owner);
        self.iterate_parameters(callable, &args, &env, &mut outcome);
        if callable.generator {
            self.create_summary_generator(key, callable, &args, &env, &mut outcome);
            self.end_invocation(&active_invocation, signature, &outcome);
            return outcome;
        }
        self.record_summary_body_bindings(callable, &mut outcome);
        let (fresh_data_writes, mut certified_data_receivers) = self.apply_summary_body_writes(
            key,
            callable,
            &env,
            fresh_checked_constructor,
            &mut outcome,
        );
        self.execute_summary_body_calls(callable, &mut env, &mut outcome);
        // The lane is flow-insensitive: iterate after the body's writes and
        // calls, so the published pool holds what they install.
        self.iterate_callable_sources(callable, &env, &mut outcome);
        self.forfeit_unsettled_data_receivers(&mut certified_data_receivers);
        self.collect_summary_body_returns(callable, &env, &mut outcome);
        self.promote_summary_body_member_writes(
            key,
            callable,
            fresh_checked_constructor
                || fresh_data_writes.is_some()
                || !certified_data_receivers.is_empty(),
            &mut outcome,
        );
        if unsupported_inputs
            || (self.state.array_iteration_depth > 0
                && self.state.array_iteration_unsupported
                && !unsupported_before)
        {
            self.promote_unsupported_summary_body(callable, unsupported_inputs, &mut outcome);
        }
        self.end_invocation(&active_invocation, signature, &outcome);
        outcome
    }

    /// Leaves the active invocation and memoizes its outcome.
    fn end_invocation(
        &mut self,
        active_invocation: &ActiveInvocation,
        signature: InvocationSignature,
        outcome: &Outcome,
    ) {
        self.state.active.remove(active_invocation);
        self.state.invocation_depth -= 1;
        self.remember_invocation(signature, outcome);
    }

    /// Implicit arguments can alias any actual input even without a
    /// corresponding formal parameter. Invalidate input capabilities before
    /// consulting cached outcomes, including calls outside array iteration.
    fn taint_unsupported_inputs(&mut self, args: &[Values]) {
        for values in args {
            self.taint_objects(values);
        }
        if self.state.array_iteration_depth > 0 {
            self.state.array_iteration_unsupported = true;
        }
    }

    /// Replays a memoized outcome for this exact signature outside array
    /// iteration, unless the callable's native effects are tainted.
    fn memoized_invocation(
        &mut self,
        key: u32,
        signature: &InvocationSignature,
    ) -> Option<Outcome> {
        if self.state.array_iteration_depth == 0
            && !self.state.world.native.tainted_functions.contains(&key)
            && let Some(point) = self.state.invocation_memos.get(signature)
        {
            let outcome = self.queries.invocation_memo(point);
            self.evaluation_effects
                .extend(&outcome.effects.evaluation_effects);
            return Some(outcome);
        }
        None
    }

    /// Generator parameter initializers run at call time, although the body
    /// waits for the first iteration. Their effects are not separated in the
    /// index, so retain the conservative path.
    fn create_summary_generator(
        &mut self,
        key: u32,
        callable: &Callable,
        args: &[Values],
        env: &Context,
        outcome: &mut Outcome,
    ) {
        outcome.unsupported(
            &self.inputs.metrics,
            ExecutionOperation::GeneratorParameters,
            callable.owner,
            DUMMY_SP,
        );
        self.create_generator(key, args, env, outcome);
    }

    /// The precision loss bounding this invocation: recursion, depth or
    /// total expansion. `None` admits it.
    fn invocation_bound_reason(
        &self,
        active_invocation: &ActiveInvocation,
    ) -> Option<ExecutionPrecisionLossReason> {
        if self.state.active.contains(active_invocation)
            || self.state.invocation_depth >= MAX_SYNCHRONOUS_INVOCATION_DEPTH
            || self.state.invocation_count >= MAX_SYNCHRONOUS_INVOCATIONS
        {
            let reason = if self.state.active.contains(active_invocation) {
                ExecutionPrecisionLossReason::RecursiveInvocation
            } else if self.state.invocation_depth >= MAX_SYNCHRONOUS_INVOCATION_DEPTH {
                ExecutionPrecisionLossReason::InvocationDepthLimit {
                    limit: MAX_SYNCHRONOUS_INVOCATION_DEPTH,
                }
            } else {
                ExecutionPrecisionLossReason::InvocationExpansionLimit {
                    limit: MAX_SYNCHRONOUS_INVOCATIONS,
                }
            };
            return Some(reason);
        }
        None
    }

    /// Whether the shared kernel can run the parameter prologue of the
    /// callable's ordered plan in `env`.
    fn ordered_prologue_admitted(&self, callable: &Callable, env: &Context) -> bool {
        self.state.array_iteration_depth == 0
            && self.inputs.index.dynamic_scope_owners.is_empty()
            && callable.member_value_writes.is_empty()
            && callable.body_facts.ordered_plan().is_some_and(|body| {
                !body.requires_keyed_collection_new()
                    && body
                        .captured_writes()
                        .iter()
                        .all(|binding| self.state.world.active_cells.contains_key(binding))
                    && body.free_reads().iter().all(|binding| {
                        env.contains_key(binding)
                            || self.inputs.top_bindings.contains(binding)
                            || self.inputs.imported_primitive_read(binding).is_some()
                            || self.inputs.index.foreign_call_imports.contains_key(binding)
                            || self.substitutes_factory_calls(&callable.calls, binding)
                            || (!self.inputs.flow.is_declared(binding)
                                && !self.inputs.shadowed.contains(binding.0.as_ref())
                                && NativeIntrinsic::for_global_binding(binding.0.as_ref())
                                    .is_some())
                    })
            })
    }

    /// Binds actual arguments, with evaluated defaults, to the formal
    /// parameters unless the ordered prologue binds them.
    fn bind_parameters(
        &mut self,
        callable: &Callable,
        args: &[Values],
        ordered_prologue: bool,
        env: &mut Context,
    ) {
        for (index, param) in callable
            .params
            .iter()
            .enumerate()
            .filter(|_| !ordered_prologue)
        {
            let mut values = args
                .get(index)
                .cloned()
                .unwrap_or_else(|| Values::from([AbstractValue::UNDEFINED]));
            if let Some(default) = &param.default
                && may_be_undefined(&values)
            {
                values.remove(&AbstractValue::UNDEFINED);
                values.extend(self.evaluate(default, env, env));
            }
            if let Some(binding) = &param.binding {
                env.insert(binding.clone(), values);
            }
        }
    }

    /// Inside an array iteration, marks the iteration unsupported when the
    /// body reads `arguments` or a member the prefix has not settled.
    fn check_iteration_body_inputs(&mut self, key: u32, callable: &Callable, env: &Context) {
        if callable.reads.iter().any(|id| id.0.as_ref() == "arguments") {
            self.state.array_iteration_unsupported = true;
        }
        if let Some(reads) = self.inputs.index.callable_member_reads.get(&key) {
            for (receiver, property) in reads.clone() {
                let values = self.evaluate(&receiver, env, env);
                if property.is_none()
                    || values.is_empty()
                    || values.iter().any(|value| match value {
                        AbstractValue::Value(Value::Prim(_)) => false,
                        AbstractValue::Value(Value::Alloc(Kind::Object, object)) => {
                            (!self.inputs.index.plain_data_objects.contains(object)
                                && !self
                                    .state
                                    .world
                                    .surface
                                    .ordinary_spread_copies
                                    .contains(object)
                                && !self.state.world.arrays.dense.contains_key(object))
                                || !self.state.world.object_properties_are_settled(*object)
                                || !self
                                    .state
                                    .world
                                    .surface
                                    .prefix_object_properties
                                    .get(object)
                                    .is_some_and(|properties| properties.contains_key(&property))
                        }
                        AbstractValue::Instance(key, _, InstanceOrigin::Constructed) => {
                            self.class_may_run_on_borrowed_receiver(*key)
                                || !self.known_local_instance_member(*key, property.as_deref())
                        }
                        _ => true,
                    })
                {
                    self.state.array_iteration_unsupported = true;
                }
            }
        }
    }

    /// The ordered plan the shared kernel executes for this invocation, or
    /// `None` for the source-summary lane.
    fn ordered_lane_body<'callable>(
        &mut self,
        key: u32,
        callable: &'callable Callable,
        env: &Context,
        summary_inputs: bool,
        ordered_prologue: bool,
    ) -> Option<&'callable LocalBody> {
        // CLEANUP(added 2026-11-09): the source-summary lane after this block
        // is the flat lane for bodies that have an ordered plan. Each gate
        // below routes such a body there until the kernel has the rule named;
        // remove the gate with that rule, and the lane with the last gate.
        // - `requires_keyed_collection_new`: an application keyed-collection
        //   lane.
        // - captured writes to bindings without an active cell: a kernel
        //   store to a module binding that records the rebind
        //   (`Unproved::CaptureStore` summarizes the remainder today).
        // - free reads outside module, summarized-import, imported-primitive
        //   and intact-native bindings: global-object reads, and reads of a
        //   callback-factory import the ordered lane does not substitute,
        //   with their global-cell provenance.
        // - `established_writes`: member-write proofs (fresh local records,
        //   borrowed parameters, array iteration) in the kernel's own-data
        //   store.
        if !summary_inputs
            && self.inputs.index.dynamic_scope_owners.is_empty()
            && let Some(body) = callable.body_facts.ordered_plan()
            // A default-bearing plan must execute its prologue in the shared
            // kernel. Pre-evaluating its value would omit the default's calls.
            && (ordered_prologue || body.parameters.iter().all(|parameter| parameter.default.is_none()))
            && !body.requires_keyed_collection_new()
            && body
                .captured_writes()
                .iter()
                .all(|binding| self.state.world.active_cells.contains_key(binding))
            && body.free_reads().iter().all(|binding| {
                env.contains_key(binding)
                    || self.inputs.top_bindings.contains(binding)
                    || self.inputs.imported_primitive_read(binding).is_some()
                    || self.inputs.index.foreign_call_imports.contains_key(binding)
                    || self.substitutes_factory_calls(&callable.calls, binding)
                    || (!self.inputs.flow.is_declared(binding)
                        && !self.inputs.shadowed.contains(binding.0.as_ref())
                        && NativeIntrinsic::for_global_binding(binding.0.as_ref()).is_some_and(
                            |intrinsic| {
                                self.native_guard_intact(
                                    NativeIntegrityGuard::NativeCallIdentity,
                                    &[IntrinsicSlot::Global(intrinsic)],
                                )
                            },
                        ))
            })
        {
            // Keep existing record/input and array-iteration write proofs on
            // their established path until they consume ordered operations.
            // An unrebound parameter remains a borrowed receiver. Reads use
            // the existing application proofs when the fresh-record check fails.
            let established_writes =
                !callable.member_value_writes.is_empty()
                    && (self.state.array_iteration_depth > 0
                        || self.fresh_local_record_receiver(key, callable).is_some()
                        || callable.member_value_writes.iter().any(|write| {
                            callable.params.iter().any(|parameter| {
                                parameter.binding.as_ref() == Some(&write.receiver)
                            }) && !callable
                                .local_assignments
                                .iter()
                                .any(|(binding, _)| binding == &write.receiver)
                        }));
            if !established_writes {
                return Some(body);
            }
        }
        None
    }

    /// Source-summary lanes pre-evaluate assignments and flatten control
    /// flow. They cannot preserve a live mutable capture's ordered value
    /// proof.
    fn prepare_summary_environment(
        &mut self,
        callable: &Callable,
        reads_arguments: bool,
        args: &[Values],
        env: &mut Context,
    ) {
        let cells: Vec<_> = self.state.world.active_cells.values().copied().collect();
        self.havoc_cells(cells);
        for (binding, cell) in &self.state.world.active_cells {
            env.insert(
                binding.clone(),
                self.state.world.cells[cell.0].values.clone(),
            );
        }
        seed_undefined_before_assignment(
            env,
            &callable.undefined_before_assignment,
            &callable.local_assignments,
        );
        self.widen_untracked_locals(callable, env);
        self.escape_untracked_environment(callable, reads_arguments, args, env);
        for (id, value) in &callable.local_assignments {
            let values = self.evaluate(value, env, env);
            // A chunk binding read later in this prefix must see the assigned
            // callable, not only the opaque value `apply_rebind_effects` adds:
            // calling that opaque value cannot reach an unescaped closure.
            // Like a top-level owner, publish possible values before the body's
            // calls; a lexical binding still in TDZ throws instead of storing.
            if self.inputs.top_bindings.contains(id)
                && (self.inputs.var_bindings.contains(id)
                    || self.state.world.module.initialized_bindings.contains(id))
            {
                let possible = self
                    .state
                    .world_mut()
                    .module
                    .prefix_values
                    .entry_or_default(id);
                let before = possible.len();
                possible.extend(values.iter().cloned());
                if possible.len() != before {
                    self.state.world_mut().revision.bump_prefix();
                }
            }
            if self.inputs.untracked_locals.contains(id) {
                self.escape_untracked_values(&values);
            }
            env.entry(id.clone()).or_default().extend(values);
        }
        self.publish_sources(callable, env);
    }

    /// Records the summary body's invocation, opacity and top-level binding
    /// reads and rebinds.
    fn record_summary_body_bindings(&self, callable: &Callable, outcome: &mut Outcome) {
        outcome.body_invoked = true;
        if callable.opaque {
            outcome.unsupported(
                &self.inputs.metrics,
                ExecutionOperation::OpaqueExecution,
                callable.owner,
                DUMMY_SP,
            );
        }
        for id in &callable.reads {
            if self.inputs.top_bindings.contains(id) {
                outcome.effects.reads.insert((id.clone(), callable.owner));
                if !self.state.world.module.initialized_bindings.contains(id) {
                    outcome.effects.uninitialized_reads.insert(id.clone());
                }
            }
        }
        for id in &callable.rebinds {
            if self.inputs.top_bindings.contains(id) {
                outcome.effects.rebinds.insert((id.clone(), callable.owner));
            }
        }
    }

    /// Applies the summary body's member writes. Returns its fresh data
    /// write objects and the receivers certified to hold only data.
    fn apply_summary_body_writes(
        &mut self,
        key: u32,
        callable: &Callable,
        env: &Context,
        fresh_checked_constructor: bool,
        outcome: &mut Outcome,
    ) -> (Option<BTreeSet<u32>>, BTreeSet<u32>) {
        // The visitor stores value-bearing member writes separately from call
        // nodes. Make same-owner callback publication visible before nested
        // opaque calls; if source order put a write afterward, this only adds
        // a conservative callback possibility.
        let fresh_data_writes = self.fresh_data_writes(callable, env);
        if let Some(objects) = &fresh_data_writes {
            self.state
                .world_mut()
                .publication
                .parameter_initialized_objects
                .extend(objects.iter().copied());
        }
        let mut certified_data_receivers = self.fresh_primitive_write_receivers(callable, env);
        let fresh_record_receiver = self.fresh_local_record_receiver(key, callable);
        if let Some(object) = fresh_record_receiver {
            certified_data_receivers.insert(object);
        }
        self.apply_member_value_writes(
            &callable.member_value_writes,
            env,
            env,
            &certified_data_receivers,
            false,
        );
        self.publish_destructuring_target_receivers(
            &callable.destructuring_member_receivers,
            env,
            env,
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
                env,
                env,
                callable.owner,
                outcome,
                &certified_data_receivers,
            );
        }
        (fresh_data_writes, certified_data_receivers)
    }

    /// Executes the summary body's calls, forfeiting exact local aliases
    /// after any skipped operation.
    fn execute_summary_body_calls(
        &mut self,
        callable: &Callable,
        env: &mut Context,
        outcome: &mut Outcome,
    ) {
        for call in &callable.calls {
            if !self.execute_call(call, env, env, outcome) {
                // Local initializers are pre-evaluated for capability reach.
                // A skipped operation may precede their real evaluation and
                // change even a primitive member value into an object.
                // Preserve retained capabilities, but forfeit exact aliases.
                let mut widened = false;
                for (binding, _) in &callable.local_assignments {
                    widened |= env
                        .entry(binding.clone())
                        .or_default()
                        .insert(AbstractValue::Value(Value::Top));
                    if self.inputs.top_bindings.contains(binding)
                        && let Some(values) =
                            self.state.world_mut().module.prefix_values.get_mut(binding)
                        && values.insert(AbstractValue::Value(Value::Top))
                    {
                        widened = true;
                    }
                }
                if widened {
                    self.state.world_mut().revision.bump_prefix();
                }
            }
        }
    }

    /// Taints and forgets the certified data receivers once an iteration,
    /// array intrinsic or property settlement no longer supports them.
    fn forfeit_unsettled_data_receivers(&mut self, certified_data_receivers: &mut BTreeSet<u32>) {
        if !certified_data_receivers.is_empty()
            && (self.state.array_iteration_unsupported
                || !self.array_intrinsics_intact()
                || certified_data_receivers
                    .iter()
                    .any(|object| !self.state.world.object_properties_are_settled(*object)))
        {
            self.state
                .world_mut()
                .surface
                .tainted_objects
                .extend(certified_data_receivers.iter().copied());
            certified_data_receivers.clear();
        }
    }

    /// Evaluates the summary body's return expressions into the outcome.
    fn collect_summary_body_returns(
        &mut self,
        callable: &Callable,
        env: &Context,
        outcome: &mut Outcome,
    ) {
        for expr in callable.body_facts.value_flow().returns() {
            let returned = self.evaluate(expr, env, env);
            for value in &returned {
                if let AbstractValue::OwnDescriptorMap(site) = value {
                    self.invalidate_own_descriptor_map(*site);
                }
            }
            outcome.returns.extend(returned);
        }
        if callable.may_fall_through {
            outcome.returns.insert(AbstractValue::UNDEFINED);
        }
    }

    /// Promotes the summary body's member writes unless a constructor or data
    /// write proof (`proven_writes`) already covers them.
    fn promote_summary_body_member_writes(
        &mut self,
        key: u32,
        callable: &Callable,
        proven_writes: bool,
        outcome: &mut Outcome,
    ) {
        let mut writes: Vec<_> = callable
            .member_writes
            .iter()
            .filter(|(receiver, _)| !self.stable_fresh_function_target(key, callable, receiver))
            .map(|(receiver, property)| (Some(receiver.clone()), Some(property.clone())))
            .collect();
        if callable.dynamic_property_effect {
            writes.push((None, None));
        }
        if !proven_writes {
            self.promote_property_writes(&writes, callable.owner, outcome);
        }
    }

    /// What this execution reached stays observed; the rest of the body is
    /// assumed.
    fn promote_unsupported_summary_body(
        &mut self,
        callable: &Callable,
        unsupported_inputs: bool,
        outcome: &mut Outcome,
    ) {
        let inputs = self.inputs;
        let fact = &inputs.facts[callable.owner.0];
        let operation = if unsupported_inputs {
            ExecutionOperation::CallableInputs
        } else {
            ExecutionOperation::ArrayIteration
        };
        let effect_owner = self.state.current_top_owner.unwrap_or(callable.owner);
        self.assuming(
            AssumedEffectReason::WholeBodyPromotion { operation },
            |this| {
                this.promote_lazy_effects(
                    &fact.reads.lazy,
                    &fact.rebinds.lazy,
                    effect_owner,
                    outcome,
                )
            },
        );
        outcome.unsupported(&self.inputs.metrics, operation, callable.owner, DUMMY_SP);
    }
}
