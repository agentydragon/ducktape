//! Dense-array transfer: settled slots, `push`, native array iteration, and the
//! `Object.fromEntries` callbacks of proven map-entry arrays.

use super::*;

impl<'source, 'rule> Executor<'source, 'rule> {
    pub(super) fn array_intrinsics_intact(&mut self) -> bool {
        self.array_slots_intact(ARRAY_PROTOTYPE_SLOTS)
    }

    pub(super) fn array_slots_intact(&mut self, slots: &[IntrinsicSlot]) -> bool {
        let array = ("Array".into(), SyntaxContext::empty());
        !self.state.world.arrays.intrinsic_mutated
            && !self.inputs.flow.is_declared(&array)
            && !self.inputs.shadowed.contains("Array")
            && self.inputs.index.dynamic_scope_owners.is_empty()
            && self.native_guard_intact(NativeIntegrityGuard::ArrayIntrinsics, slots)
    }

    pub(super) fn settled_dense_array_slot_count(&self, object: u32) -> Option<usize> {
        if !self.state.world.object_properties_are_settled(object) {
            return None;
        }
        self.state
            .world
            .arrays
            .dense
            .get(&object)
            .map(|array| array.slots.len())
    }

    pub(super) fn settled_dense_array_slots(&self, object: u32) -> Option<Vec<Values>> {
        self.state
            .world
            .object_properties_are_settled(object)
            .then(|| {
                self.state
                    .world
                    .arrays
                    .dense
                    .get(&object)
                    .map(|array| array.slots.clone())
            })
            .flatten()
    }

    /// Transfer the ordinary dense-array iterator over the evaluated operand's
    /// prefix heap. The operation adapter supplies its already-defined native
    /// protocol premise (Set's typed certificate or a spread's exact Array
    /// slots) at the same point the previous dense-source check consulted it.
    pub(super) fn dense_array_iteration_transfer(
        &mut self,
        values: &Values,
        protocol_intact: impl FnOnce(&mut Self) -> bool,
    ) -> DenseArrayIterationTransfer {
        let [AbstractValue::Value(Value::Alloc(Kind::Object, object))] =
            values.iter().collect::<Vec<_>>().as_slice()
        else {
            return DenseArrayIterationTransfer::Refused;
        };
        // Exact singleton matching rejects an Unavailable throw alternative
        // before consulting protocol slots, as the previous dense-source
        // guard did.
        if !protocol_intact(self) {
            return DenseArrayIterationTransfer::Refused;
        }
        match self.settled_dense_array_slots(*object) {
            Some(slots) => DenseArrayIterationTransfer::Yielded {
                source: *object,
                slots,
            },
            None => DenseArrayIterationTransfer::Refused,
        }
    }

    pub(crate) fn dense_array_push_receiver(&mut self, receiver: &Values) -> Option<u32> {
        let [AbstractValue::Value(Value::Alloc(Kind::Object, object))] =
            receiver.iter().collect::<Vec<_>>().as_slice()
        else {
            return None;
        };
        (self.state.world.arrays.dense.contains_key(object)
            && self
                .state
                .world
                .surface
                .object_accessors_are_settled(*object)
            && self.array_slots_intact(ARRAY_PROTOTYPE_SLOTS))
        .then_some(*object)
    }

    pub(super) fn execute_dense_array_push(
        &mut self,
        callee: &ValueExpr,
        args: &[Values],
        env: &Context,
        captures: &Context,
        prepared_receiver: Option<&Values>,
    ) -> bool {
        let ValueExpr::Member(receiver, Some(method), MemberKey::Static, _) = callee else {
            return false;
        };
        if method != "push"
            || !matches!(receiver.as_ref(), ValueExpr::Binding(_))
            || args
                .iter()
                .any(|values| values.is_empty() || values.iter().any(|value| !value.is_primitive()))
        {
            return false;
        }
        let receiver = prepared_receiver
            .cloned()
            .unwrap_or_else(|| self.evaluate(receiver, env, captures));
        let Some(object) = self.dense_array_push_receiver(&receiver) else {
            return false;
        };
        // An intact ordinary array has its own writable length, and the
        // guarded prototypes cannot intercept the appended primitive slots.
        // Preserve its contained callbacks, but invalidate the precise array
        // contents after this real mutation. No descriptor replacement or
        // getter installation occurred. Refuse exact array snapshots without
        // claiming a fresh value or modeling how often this site executed.
        if self
            .state
            .world_mut()
            .arrays
            .unknown_contents
            .insert(object)
        {
            self.state.world_mut().revision.bump_prefix();
        }
        self.invalidate_writable_object(object);
        true
    }

    pub(super) fn retain_dense_array(&mut self, object: u32, slots: Vec<Values>) {
        self.literal_heap().dense_array(object, slots);
    }

    pub(crate) fn literal_heap(&mut self) -> super::super::allocation_transfer::LiteralHeap<'_> {
        self.state.world_mut().literal_heap()
    }

    // CLEANUP(added 2026-11-10): delete with the legacy gate (L4e). The
    //   kernel row `LocalExecutionContext::iterate_array`
    //   (`native/array_iteration.rs`) replaces it for kernel-run bodies; this
    //   path stays while the legacy gate reads its `Outcome` for flattened
    //   owners, the flat `invoke_with_cells` lane and refused remainders.
    pub(crate) fn array_iteration_plan(
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
        let proof = self.inputs.index.array_iterations.get(&span)?.clone();
        proof.result?;
        let values = self.evaluate(&proof.source, env, captures);
        let DenseArrayIterationTransfer::Yielded { source, slots } = self
            .dense_array_iteration_transfer(&values, |executor| {
                executor.array_slots_intact(proof.row.requires)
            })
        else {
            return None;
        };
        let dense = DenseArray { slots };
        if dense.slots.len() > MAX_SYNCHRONOUS_INVOCATIONS {
            return None;
        }
        for values in &dense.slots {
            if values.is_empty()
                || values.iter().any(|value| match value {
                    AbstractValue::Value(Value::Alloc(Kind::Object, object)) => {
                        (!self.inputs.index.plain_data_objects.contains(object)
                            && !self
                                .state
                                .world
                                .surface
                                .ordinary_spread_copies
                                .contains(object))
                            || !self.state.world.object_properties_are_settled(*object)
                    }
                    AbstractValue::Value(Value::Alloc(Kind::Function, key))
                    | AbstractValue::Closure(HeapAddress { callable: key, .. }, _) => {
                        self.state.world.native.tainted_functions.contains(key)
                    }
                    AbstractValue::Instance(key, _, InstanceOrigin::Constructed) => {
                        self.class_may_run_on_borrowed_receiver(*key)
                    }
                    value if value.is_primitive() => false,
                    _ => true,
                })
            {
                return None;
            }
        }
        let callback = self.evaluate(&proof.callback, env, captures);
        let (key, captured) = match callback.iter().collect::<Vec<_>>().as_slice() {
            [AbstractValue::Value(Value::Alloc(Kind::Function, key))] => (*key, None),
            [AbstractValue::Closure(HeapAddress { callable: key, .. }, captured)] => {
                (*key, Some(captured.clone()))
            }
            _ => return None,
        };
        let callable = self.inputs.index.callables.get(&key)?;
        if callable.generator
            || self.inputs.index.async_callables.contains(&key)
            || callable.params.iter().any(|param| param.binding.is_none())
        {
            return None;
        }
        Some((proof, source, dense, key, captured))
    }

    /// The value an admitted iteration call evaluates to. A fresh array
    /// result is its allocation, whose slots the transfer retains; the
    /// accumulator is what the transfer last stored.
    pub(crate) fn array_iteration_result(
        &mut self,
        proof: &ArrayIteration,
        dense: &DenseArray,
    ) -> Values {
        let result = proof.result.unwrap();
        match proof.row.result {
            IterationResult::Undefined => Values::from([AbstractValue::UNDEFINED]),
            IterationResult::Mapped | IterationResult::Filtered | IterationResult::FlatMapped => {
                Values::from([AbstractValue::Value(Value::Alloc(Kind::Object, result))])
            }
            IterationResult::Boolean => {
                Values::from([AbstractValue::Value(Value::Prim(PrimKinds::BOOLEAN))])
            }
            IterationResult::Index => {
                Values::from([AbstractValue::Value(Value::Prim(PrimKinds::NUMBER))])
            }
            IterationResult::Element => {
                let mut values: Values = dense.slots.iter().flatten().cloned().collect();
                values.insert(AbstractValue::UNDEFINED);
                values
            }
            // Not yet transferred: what the accumulator can hold is unknown,
            // but every callback it can reach stays retained.
            IterationResult::Accumulated => {
                match self.state.world.arrays.accumulated_values.get(&result) {
                    Some(values) => values.clone(),
                    None => {
                        let mut reachable: Values = dense.slots.iter().flatten().cloned().collect();
                        reachable.insert(AbstractValue::Value(Value::Alloc(Kind::Object, result)));
                        let callbacks = self.callback_values(&reachable);
                        Values::from([
                            self.deferred_callbacks(callbacks),
                            AbstractValue::Value(Value::Top),
                        ])
                    }
                }
            }
        }
    }

    pub(super) fn merge_array_effects(&mut self, outcome: &mut Outcome, mut nested: Outcome) {
        self.apply_rebind_effects(&nested.effects.rebinds);
        outcome.absorb_call_effects(&mut nested.effects, self.state.assumption.as_ref());
        outcome.executed_owners.extend(nested.executed_owners);
        outcome.unknown_call |= nested.unknown_call;
        outcome.inherit_completeness(nested.complete, nested.effects.precision_losses);
    }

    pub(super) fn execute_array_iteration(
        &mut self,
        span: CallSite,
        env: &Context,
        captures: &Context,
        outcome: &mut Outcome,
    ) -> bool {
        let result = self.request(RuntimeOperation::Model(
            super::super::runtime_models::ModelOperation::ExecuteArrayIteration {
                span,
                environment: env.clone(),
                captures: captures.clone(),
                outcome: Box::new(std::mem::take(outcome)),
            },
        ));
        let RuntimeValue::Model(super::super::runtime_models::ModelResult::ArrayIteration {
            handled,
            outcome: model_outcome,
        }) = result
        else {
            unreachable!("array iteration models produce iteration results");
        };
        *outcome = *model_outcome;
        handled
    }

    /// Whether a callback result can be stored in a fresh result array, or
    /// spread into one, without running user code when the result is later
    /// read, iterated or spread.
    pub(crate) fn retainable_iteration_value(&self, value: &AbstractValue) -> bool {
        match value {
            AbstractValue::Value(Value::Prim(_))
            | AbstractValue::Value(Value::Alloc(Kind::Function, _))
            | AbstractValue::Closure(_, _) => true,
            AbstractValue::Value(Value::Alloc(Kind::Object, object)) => {
                self.state.world.object_properties_are_settled(*object)
                    && (self.inputs.index.plain_data_objects.contains(object)
                        || self
                            .state
                            .world
                            .surface
                            .ordinary_spread_copies
                            .contains(object)
                        || self.state.world.arrays.dense.contains_key(object))
            }
            _ => false,
        }
    }

    /// The slots a `flatMap` callback result contributes: one untainted
    /// dense array spreads its slots; any other retainable value is one
    /// slot. `None` when an array may sit beside other values, so slot
    /// positions are unknown.
    pub(super) fn flattened_iteration_slots(&self, returned: &Values) -> Option<Vec<Values>> {
        let arrays: Vec<_> = returned
            .iter()
            .filter_map(|value| match value {
                AbstractValue::Value(Value::Alloc(Kind::Object, object)) => {
                    self.state.world.arrays.dense.get(object)
                }
                _ => None,
            })
            .collect();
        match arrays.as_slice() {
            [] => Some(vec![returned.clone()]),
            [array] if returned.len() == 1 => Some(array.slots.clone()),
            _ => None,
        }
    }

    // CLEANUP(added 2026-11-10): delete with the legacy gate (L4e). The
    //   kernel row `LocalExecutionContext::iterate_array`
    //   (`native/array_iteration.rs`) replaces it for kernel-run bodies; this
    //   path stays while the legacy gate reads its `Outcome` for flattened
    //   owners, the flat `invoke_with_cells` lane and refused remainders.
    pub(crate) fn execute_array_iteration_transfer(
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
        let row = proof.row;
        let result = proof.result.unwrap();
        let source_values =
            Values::from([AbstractValue::Value(Value::Alloc(Kind::Object, source))]);
        let original_callbacks = self.callback_values(&source_values);
        let protected_objects = self.reachable_objects(&source_values);
        self.state
            .array_iteration_protected_objects
            .extend(protected_objects);
        let mut accumulator = proof
            .initial
            .as_ref()
            .map(|initial| self.evaluate(initial, env, captures));
        let mut slots = Some(Vec::new());
        for values in &dense.slots {
            if row.result == IterationResult::Accumulated && accumulator.is_none() {
                accumulator = Some(values.clone());
                continue;
            }
            let mut arguments = vec![
                values.clone(),
                Values::from([AbstractValue::PRIMITIVE]),
                source_values.clone(),
            ];
            if let Some(accumulator) = &accumulator {
                arguments.insert(0, accumulator.clone());
            }
            let previous_unsupported = self.state.array_iteration_unsupported;
            self.state.array_iteration_unsupported = false;
            self.state.array_iteration_depth += 1;
            let nested = self.invoke(key, arguments, captured.clone(), false);
            self.state.array_iteration_depth -= 1;
            let unsupported = self.state.array_iteration_unsupported;
            self.state.array_iteration_unsupported |= previous_unsupported;
            let returned = nested.returns.clone();
            let callback_executed_completely = nested.body_invoked && nested.complete;
            self.merge_array_effects(outcome, nested);
            let current_source = self.evaluate(&proof.source, env, captures);
            let current_source = self.dense_array_iteration_transfer(&current_source, |executor| {
                executor.array_slots_intact(row.requires)
            });
            let valid = !unsupported && matches!(current_source,
                DenseArrayIterationTransfer::Yielded { source: current_object, slots }
                    if current_object == source && slots == dense.slots)
                && dense.slots.iter().flatten().all(|value| !matches!(value, AbstractValue::Value(Value::Alloc(Kind::Object, object)) if !self.state.world.object_properties_are_settled(*object)))
                && dense.slots.iter().flatten().all(|value| !matches!(value, AbstractValue::Value(Value::Alloc(Kind::Function, key)) | AbstractValue::Closure(HeapAddress { callable: key, .. }, _) if self.state.world.native.tainted_functions.contains(key)))
                && dense.slots.iter().flatten().all(|value| !matches!(value, AbstractValue::Instance(key, _, InstanceOrigin::Constructed) if self.class_may_run_on_borrowed_receiver(*key)));
            let valid = valid
                && (row.result != IterationResult::Mapped
                    || returned.iter().all(|value| {
                        self.retainable_iteration_value(value)
                            || matches!(value, AbstractValue::Instance(_, _, _))
                    }))
                && (row.result != IterationResult::Mapped
                    || returned
                        .iter()
                        .all(|value| self.retainable_iteration_value(value))
                    || callback_executed_completely)
                && (row.result != IterationResult::FlatMapped
                    || returned
                        .iter()
                        .all(|value| self.retainable_iteration_value(value)));
            if !valid {
                let callback_owner = self.inputs.index.callables[&key].owner;
                let effect_owner = self.state.current_top_owner.unwrap_or(callback_owner);
                // Effects the per-slot invocations reached stay observed.
                self.assuming(
                    AssumedEffectReason::WholeBodyPromotion {
                        operation: ExecutionOperation::ArrayIteration,
                    },
                    |this| this.promote_lazy_owner(callback_owner, effect_owner, outcome),
                );
                self.state
                    .world_mut()
                    .surface
                    .tainted_objects
                    .insert(result);
                self.state
                    .world_mut()
                    .surface
                    .prefix_object_properties
                    .entry(result)
                    .or_default()
                    .entry(None)
                    .or_default()
                    .extend(original_callbacks.iter().cloned());
                if row.result == IterationResult::Accumulated {
                    let mut retained = original_callbacks.clone();
                    retained.extend(self.callback_values(&returned));
                    retained.extend(accumulator.iter().flatten().cloned());
                    let fallback = Values::from([
                        self.deferred_callbacks(retained),
                        AbstractValue::Value(Value::Top),
                    ]);
                    self.join_accumulated_value(result, fallback);
                }
                self.invoke_possible_callbacks_at(
                    &[original_callbacks],
                    false,
                    Some(span.source_span()),
                    outcome,
                );
                return false;
            }
            match row.result {
                IterationResult::Mapped => {
                    let contents = self
                        .state
                        .world_mut()
                        .surface
                        .prefix_object_properties
                        .entry(result)
                        .or_default()
                        .entry(None)
                        .or_default();
                    let before = contents.len();
                    contents.extend(returned.iter().cloned());
                    if contents.len() != before {
                        self.state.world_mut().revision.bump_prefix();
                    }
                    if returned
                        .iter()
                        .all(|value| self.retainable_iteration_value(value))
                    {
                        if let Some(slots) = &mut slots {
                            slots.push(returned);
                        }
                    } else {
                        slots = None;
                        // A projected instance capability can retain the
                        // receiver's callbacks. Keep those source roots
                        // reachable from the result's unknown contents.
                        let contents = self
                            .state
                            .world_mut()
                            .surface
                            .prefix_object_properties
                            .entry(result)
                            .or_default()
                            .entry(None)
                            .or_default();
                        let before = contents.len();
                        contents.extend(original_callbacks.iter().cloned());
                        if contents.len() != before {
                            self.state.world_mut().revision.bump_prefix();
                        }
                        // The same allocation site may have a prior exact result.
                        // Revoke it before the next callback can read that site.
                        self.literal_heap().invalidate_dense_array(result);
                        let primitive_removed = self
                            .state
                            .world_mut()
                            .arrays
                            .primitive
                            .remove(&result)
                            .is_some();
                        let newly_tainted = self
                            .state
                            .world_mut()
                            .surface
                            .tainted_objects
                            .insert(result);
                        if primitive_removed || newly_tainted {
                            self.state.world_mut().revision.bump_prefix();
                        }
                    }
                }
                IterationResult::Filtered => {
                    if let Some(slots) = &mut slots {
                        slots.push(values.clone());
                    }
                }
                IterationResult::FlatMapped => {
                    match (slots.take(), self.flattened_iteration_slots(&returned)) {
                        (Some(mut retained), Some(flattened)) => {
                            retained.extend(flattened);
                            slots = Some(retained);
                        }
                        // Positions are unknown: keep the values as contents.
                        (previous, _) => {
                            let mut contents: Values =
                                previous.into_iter().flatten().flatten().collect();
                            for value in &returned {
                                match value {
                                    AbstractValue::Value(Value::Alloc(Kind::Object, object))
                                        if self.state.world.arrays.dense.contains_key(object) =>
                                    {
                                        contents.extend(
                                            self.state.world.arrays.dense[object]
                                                .slots
                                                .iter()
                                                .flatten()
                                                .cloned(),
                                        );
                                    }
                                    value => {
                                        contents.insert(value.clone());
                                    }
                                }
                            }
                            self.state
                                .world_mut()
                                .surface
                                .prefix_object_properties
                                .entry(result)
                                .or_default()
                                .entry(None)
                                .or_default()
                                .extend(contents);
                        }
                    }
                }
                IterationResult::Accumulated => accumulator = Some(returned),
                IterationResult::Undefined
                | IterationResult::Boolean
                | IterationResult::Element
                | IterationResult::Index => {}
            }
        }
        match row.result {
            IterationResult::Mapped | IterationResult::Filtered | IterationResult::FlatMapped => {
                match slots {
                    Some(slots) => self.retain_dense_array(result, slots),
                    None => {
                        self.literal_heap().invalidate_dense_array(result);
                        let primitive_removed = self
                            .state
                            .world_mut()
                            .arrays
                            .primitive
                            .remove(&result)
                            .is_some();
                        let newly_tainted = self
                            .state
                            .world_mut()
                            .surface
                            .tainted_objects
                            .insert(result);
                        if primitive_removed || newly_tainted {
                            self.state.world_mut().revision.bump_prefix();
                        }
                    }
                }
            }
            IterationResult::Accumulated => {
                // An empty receiver without an initial value throws.
                let values = accumulator
                    .unwrap_or_else(|| Values::from([AbstractValue::Value(Value::Unavailable)]));
                self.join_accumulated_value(result, values);
            }
            IterationResult::Undefined
            | IterationResult::Boolean
            | IterationResult::Element
            | IterationResult::Index => {}
        }
        true
    }

    /// Repeated evaluations of one `reduce` call join their accumulators.
    pub(super) fn join_accumulated_value(&mut self, result: u32, values: Values) {
        let current = self
            .state
            .world_mut()
            .arrays
            .accumulated_values
            .entry(result)
            .or_default();
        let before = current.len();
        current.extend(values);
        if current.len() != before {
            self.state.world_mut().revision.bump_prefix();
        }
    }

    pub(super) fn is_proven_map_entry_array(
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
            && !self.inputs.flow.is_declared(id)
            && !self.inputs.shadowed.contains("Map")
            && !env.contains_key(id)
            && !captures.contains_key(id)
            && !self.inputs.index.reassigned_map
            && self.inputs.index.dynamic_scope_owners.is_empty()
            && !self.inputs.index.dynamic_scope_owners.contains(&owner)
            && matches!(args, [ValueExpr::ObjectLiteral(span, _)]
                if self.inputs.index.map_entry_arrays.contains(span))
    }

    pub(crate) fn proven_from_entries_callbacks(
        &mut self,
        call_site: CallSite,
        env: &Context,
        captures: &Context,
    ) -> Option<Values> {
        let proof = self
            .inputs
            .index
            .from_entries_map_calls
            .get(&call_site)
            .or_else(|| self.inputs.index.mapped_array_map_calls.get(&call_site))?
            .clone();
        let array_intrinsic = ("Array".into(), SyntaxContext::empty());
        if !self.inputs.index.dynamic_scope_owners.is_empty()
            || self.inputs.flow.is_declared(&proof.object_intrinsic)
            || self.inputs.shadowed.contains("Object")
            || self.inputs.flow.is_declared(&array_intrinsic)
            || self.inputs.shadowed.contains("Array")
            || !self.census_slots_intact(
                NativeIntegrityGuard::MappedFromEntries,
                &[
                    IntrinsicSlot::Global(NativeIntrinsic::Object),
                    IntrinsicSlot::Static(NativeIntrinsic::Object, "fromEntries"),
                    IntrinsicSlot::Prototype(NativeIntrinsic::Array),
                    IntrinsicSlot::ArrayIteratorPrototype,
                ],
            )
        {
            return None;
        }
        let ValueExpr::Binding(source_id) = &proof.source else {
            return None;
        };
        // This path deliberately accepts only a chunk-top plain-array binding.
        // Local parameters, aliases from opaque calls, and synthetic map results
        // stay on the ordinary unknown-call path.
        if !self.inputs.top_bindings.contains(source_id)
            || !self
                .inputs
                .index
                .plain_array_bindings
                .contains(source_id.0.as_ref())
        {
            return None;
        }
        let source_values = self.evaluate(&proof.source, env, captures);
        let [AbstractValue::Value(Value::Alloc(Kind::Object, array))] =
            source_values.iter().collect::<Vec<_>>().as_slice()
        else {
            return None;
        };
        if !self.state.world.object_properties_are_settled(*array) {
            return None;
        }
        let count = *self.inputs.index.dense_array_literals.get(array)?;
        let mut expected_entries = Values::new();
        for index in 0..count {
            let slot = self
                .inputs
                .flow
                .values_of_property(*array, &index.to_string())?;
            if slot.iter().next().is_none() || slot.iter().any(|origin| origin == Value::Top) {
                return None;
            }
            for origin in slot {
                let Value::Alloc(Kind::Object, entry) = origin else {
                    return None;
                };
                if !self.inputs.index.plain_data_objects.contains(&entry)
                    || !self
                        .inputs
                        .index
                        .primitive_data_properties
                        .contains(&(entry, proof.key.clone()))
                    || !self.state.world.object_properties_are_settled(entry)
                {
                    return None;
                }
                let key_values = self
                    .state
                    .world
                    .surface
                    .prefix_object_properties
                    .get(&entry)
                    .and_then(|properties| properties.get(&Some(proof.key.clone())))?;
                if key_values.is_empty() || key_values.iter().any(|value| !value.is_primitive()) {
                    return None;
                }
                expected_entries.insert(AbstractValue::Value(Value::Alloc(Kind::Object, entry)));
            }
        }
        let actual_entries = self
            .state
            .world
            .surface
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

    pub(super) fn callbacks_in_from_entries_source(
        &mut self,
        call_site: CallSite,
        env: &Context,
        captures: &Context,
    ) -> Option<Values> {
        let proof = self
            .inputs
            .index
            .from_entries_map_calls
            .get(&call_site)
            .or_else(|| self.inputs.index.mapped_array_map_calls.get(&call_site))?
            .clone();
        let source_values = self.evaluate(&proof.source, env, captures);
        Some(self.callback_values(&source_values))
    }
}
