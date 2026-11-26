//! Proof-dependency, native-integrity and prototype-absence bookkeeping, plus metadata decoration summaries.

use super::*;

impl<'source, 'rule> Executor<'source, 'rule> {
    pub(crate) fn mark_allocation_opaque(&mut self) {
        self.allocation_judgment = world::allocation_transfer::AllocationJudgment::Opaque;
    }

    /// Called only for an expression whose canonical source coverage was
    /// retained by its owner, or a child of that exact expression.
    pub(crate) fn cover_source_expression(&mut self) {
        self.source_covered = true;
        self.allocation_judgment =
            world::allocation_transfer::AllocationJudgment::CoveredAllocationOnly(Box::default());
    }

    /// Conditions of the settled prefix, including value-producing child
    /// transfers, plus queries used by the current target selection. This is
    /// a conservative prerequisite set, never operation coverage evidence.
    pub(crate) fn settled_prefix_dependencies(&self) -> ProofDependencies {
        let mut dependencies = self.state.world.prerequisites.clone();
        dependencies.extend(&self.queries.dependencies());
        dependencies
    }

    pub(crate) fn boxed_settled_prefix_dependencies(&self) -> Box<ProofDependencies> {
        let mut dependencies = Box::<ProofDependencies>::default();
        dependencies.extend(&self.state.world.prerequisites);
        self.queries.extend_dependencies(&mut dependencies);
        dependencies
    }

    /// Fold the queries' prerequisites into the world's snapshot before the
    /// state crosses a transfer boundary. Writes only on change, so an
    /// unchanged shared world is not copied.
    pub(super) fn settle_query_prerequisites(&mut self) {
        let mut prerequisites = self.state.world.prerequisites.clone();
        prerequisites.extend(&self.queries.dependencies());
        if prerequisites != self.state.world.prerequisites {
            self.state.world_mut().prerequisites = prerequisites;
        }
    }

    pub(crate) fn request(&mut self, operation: RuntimeOperation) -> RuntimeValue {
        self.request_with_allocation_judgment(operation).0
    }

    pub(super) fn request_with_allocation_judgment(
        &mut self,
        operation: RuntimeOperation,
    ) -> (RuntimeValue, world::allocation_transfer::AllocationJudgment) {
        self.request_transfer(operation, self.state.assumption.clone())
    }

    pub(super) fn request_transfer(
        &mut self,
        operation: RuntimeOperation,
        assumption: Option<AssumedEffectReason>,
    ) -> (RuntimeValue, world::allocation_transfer::AllocationJudgment) {
        self.settle_query_prerequisites();
        let state = self
            .state
            .0
            .take()
            .expect("the requesting transfer owns its state");
        let observations = std::mem::take(&mut self.observations);
        let mut result = self.queries.request(state, observations, operation);
        self.state.0 = Some(result.state);
        self.observations = result.observations;
        self.allocation_judgment
            .compose(&result.allocation_judgment);
        // Transport effects before exposing any payload, including Values.
        result.evaluation_effects.assume(assumption.as_ref());
        self.evaluation_effects.extend(&result.evaluation_effects);
        (result.value, result.allocation_judgment)
    }

    pub(super) fn request_callback_operation(
        &mut self,
        operation: CallbackOperation,
    ) -> CallbackResult {
        let result = self.request(RuntimeOperation::Callbacks(operation));
        let RuntimeValue::Callbacks(result) = result else {
            unreachable!("callback operations produce callback results");
        };
        result
    }

    /// Runs `run` as the conservative path `reason`: binding effects it
    /// contributes are assumed. An enclosing path keeps its reason.
    pub(super) fn assuming<T>(
        &mut self,
        reason: AssumedEffectReason,
        run: impl FnOnce(&mut Self) -> T,
    ) -> T {
        let outermost = self.state.assumption.is_none();
        if outermost {
            self.state.assumption = Some(reason);
        }
        let result = run(self);
        if outermost {
            self.state.assumption = None;
        }
        result
    }

    pub(super) fn remember_invocation(
        &mut self,
        signature: InvocationSignature,
        outcome: &Outcome,
    ) {
        if self
            .state
            .world
            .native
            .tainted_functions
            .contains(&signature.callable)
        {
            return;
        }
        // This executor owns only this invocation's child ledger. A memo
        // must never capture the caller's accumulated effects.
        let mut memo = outcome.clone();
        self.record_callable_heap_contents(signature.callable, &mut memo);
        memo.effects
            .evaluation_effects
            .extend(&self.evaluation_effects);
        memo.effects
            .evaluation_effects
            .depend_on(&self.state.world.prerequisites);
        memo.effects
            .evaluation_effects
            .depend_on(&self.queries.dependencies());
        let point = self.queries.publish_invocation_memo(&memo);
        if let Some(previous) = self.state.invocation_memos.insert(signature, point.clone())
            && previous != point
        {
            self.queries.retire_invocation_memo(previous);
        }
    }

    /// Static first-order body cells belong to this invocation, including
    /// when it is memoized. The caller later decides whether its path was
    /// assumed; a statement-wide callable set loses that distinction.
    pub(super) fn record_callable_heap_contents(&self, key: u32, outcome: &mut Outcome) {
        if let Some(cells) = self.inputs.callable_heap_contents.get(&key) {
            outcome
                .effects
                .record_heap_contents(cells, outcome.body_invoked);
        }
    }

    /// The abstract callback-factory value also represents namespace members
    /// and aliases. Only a checked direct imported binding identifies the
    /// summary without changing value or cache semantics.
    pub(crate) fn record_callback_factory_use(
        &mut self,
        imported_local: &Id,
        kind: CallbackFactoryUseKind,
        lexical_statement_ordinal: SourceUnitOrdinal,
        span: Span,
    ) {
        let Some(summary) = self
            .inputs
            .index
            .callback_factory_summaries
            .get(imported_local)
        else {
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
        self.record_callback_factory_use_fact(proof_use);
    }

    pub(crate) fn record_callback_factory_use_fact(&mut self, proof_use: CallbackFactoryUseFact) {
        if !self.state.proof_uses.contains(&proof_use) {
            self.state.proof_uses.push(proof_use);
        }
    }

    pub(crate) fn closed_enum_member_read(&mut self, value: &ValueExpr) -> bool {
        let ValueExpr::Member(receiver, Some(property), _, _) = value else {
            return false;
        };
        self.closed_enum_member_parts(receiver, property).is_some()
    }

    /// The primitive values of `receiver.property` when `receiver` is a
    /// settled `PlainData` binding holding the key as own data. The read
    /// keeps its ordinary eager dependency, but cannot retrieve a getter or a
    /// callable value.
    pub(crate) fn closed_enum_member_parts(
        &mut self,
        receiver: &ValueExpr,
        property: &str,
    ) -> Option<Values> {
        let ValueExpr::Binding(binding) = receiver else {
            return None;
        };
        let values = self
            .inputs
            .plain_data_member(&self.state, binding, property)?;
        if let Some(uses) = self
            .inputs
            .graph
            .plain_data_inference_uses(binding.0.as_ref())
        {
            self.observations
                .record_inference_uses(uses.iter().cloned());
        }
        Some(values)
    }

    pub(crate) fn record_returned_record_use(
        &mut self,
        callee: &ValueExpr,
        kind: analysis_vocabulary::report_schema::ReturnedRecordUseKind,
        lexical_statement_ordinal: SourceUnitOrdinal,
        span: Span,
    ) {
        let ValueExpr::Binding(imported_local) = callee else {
            return;
        };
        let Some(evidence) = self
            .inputs
            .index
            .returned_record_summaries
            .get(imported_local)
        else {
            return;
        };
        self.observations
            .record_inference_use(evidence.inference.clone());
        let proof_use = ReturnedRecordUseFact {
            imported_local: imported_local.0.to_string(),
            evidence: evidence.clone(),
            kind,
            lexical_statement_ordinal,
            source_location: None,
            span,
        };
        if !self.state.returned_record_proof_uses.contains(&proof_use) {
            self.state.returned_record_proof_uses.push(proof_use);
        }
    }

    /// Every slot census-intact. A failure records the mutated families.
    pub(super) fn native_guard_intact(
        &mut self,
        guard: NativeIntegrityGuard,
        slots: &[IntrinsicSlot],
    ) -> bool {
        self.observe_native_prefix(guard, None);
        let integrity = &self.inputs.index.integrity;
        if let Some(premise) =
            native_premise(integrity, slots, None, InferenceRule::NativeIntegrity)
        {
            self.record_inference_use(guard, premise.inference_use, &premise.dependencies);
            return true;
        }
        self.record_failed_native_guard(guard, slots);
        false
    }

    /// Every slot census-intact, for a proof that records no inference use.
    /// Success records the census's proof dependencies (the program premises
    /// that kept the slots intact); a failure records the mutated families
    /// like [`Self::native_guard_intact`].
    pub(super) fn census_slots_intact(
        &mut self,
        guard: NativeIntegrityGuard,
        slots: &[IntrinsicSlot],
    ) -> bool {
        self.observe_native_prefix(guard, None);
        match self.inputs.index.integrity.require(slots, None) {
            Integrity::Intact(dependencies) => {
                self.record_proof_dependencies(guard, dependencies);
                true
            }
            _ => {
                self.record_failed_native_guard(guard, slots);
                false
            }
        }
    }

    pub(super) fn record_failed_native_guard(
        &mut self,
        guard: NativeIntegrityGuard,
        slots: &[IntrinsicSlot],
    ) {
        self.observations
            .record_failed_native_guard(NativeIntegrityCheck {
                guard,
                slots: slots.iter().map(ToString::to_string).collect(),
                affected_roots: self.inputs.index.integrity.mutated_roots(slots),
            });
    }

    pub(crate) fn constructor_field_prototype_absent(
        &mut self,
        class: u32,
        property: &str,
    ) -> bool {
        if Self::standard_object_prototype_property(property) {
            return false;
        }
        let premise = (class, property.to_owned());
        if self.state.checking_record_factory_contract {
            self.observations
                .record_factory_prototype_premises_mut()
                .constructor_fields
                .insert(premise);
            return true;
        }
        if self
            .state
            .nested_record_factory_premise
            .is_some_and(|(checked, _)| checked == class)
        {
            self.observations
                .record_factory_prototype_premises_mut()
                .constructor_fields
                .insert(premise);
            return true;
        }
        self.observations
            .record_factory_prototype_premises()
            .constructor_fields
            .contains(&premise)
            || self.object_prototype_property_absent(property)
    }

    pub(super) fn input_field_prototype_absent(&mut self, object: u32, property: &str) -> bool {
        if Self::standard_object_prototype_property(property) {
            return false;
        }
        let premise = (object, property.to_owned());
        if self.state.checking_record_factory_contract {
            self.observations
                .record_factory_prototype_premises_mut()
                .missing_input_fields
                .insert(premise);
            return true;
        }
        if self
            .state
            .nested_record_factory_premise
            .is_some_and(|(_, checked)| checked == object)
        {
            self.observations
                .record_factory_prototype_premises_mut()
                .missing_input_fields
                .insert(premise);
            return true;
        }
        self.observations
            .record_factory_prototype_premises()
            .missing_input_fields
            .contains(&premise)
            || self.object_prototype_property_absent(property)
    }

    /// Absence is stronger than getter-freedom: an inherited data value may
    /// contain callbacks missing from an argument-only instance graph.
    pub(super) fn object_prototype_property_absent(&mut self, property: &str) -> bool {
        self.observe_native_prefix(
            NativeIntegrityGuard::OrdinaryObjectPrototype,
            Some(property),
        );
        if Self::standard_object_prototype_property(property) {
            return false;
        }
        let integrity = &self.inputs.index.integrity;
        let intact = self
            .observations
            .prototype_property_check(property, || {
                let (intact, blocking_mutation_count) =
                    integrity.object_prototype_property(property);
                NativePrototypePropertyCheck {
                    property: property.to_owned(),
                    intact,
                    blocking_mutation_count,
                }
            })
            .intact;
        if intact {
            // Finite mutation keys may depend on Symbol/Object/Array identity,
            // which can itself consume a captured-value census refinement.
            self.record_native_integrity_use(NativeIntegrityGuard::OrdinaryObjectPrototype);
        }
        intact
    }

    pub(super) fn record_native_integrity_use(&mut self, guard: NativeIntegrityGuard) {
        self.record_proof_dependencies(guard, &self.inputs.index.native_integrity_dependencies());
    }

    pub(crate) fn record_proof_dependencies(
        &mut self,
        guard: NativeIntegrityGuard,
        dependencies: &analysis_vocabulary::inputs::ProofDependencies,
    ) {
        self.consume_proof_dependencies(dependencies);
        if !dependencies.value_inputs.is_empty()
            || !dependencies.no_accessor_install_callables.is_empty()
            || !dependencies.program_premises.is_empty()
        {
            self.observations.record_native_integrity_use(
                analysis_vocabulary::report_schema::NativeIntegrityUse {
                    guard,
                    value_input_refs: dependencies.value_inputs.clone(),
                    no_accessor_install_refs: dependencies.no_accessor_install_callables.clone(),
                    program_premises: dependencies.program_premises.clone(),
                },
            );
        }
    }

    pub(super) fn consume_proof_dependencies(
        &mut self,
        dependencies: &analysis_vocabulary::inputs::ProofDependencies,
    ) {
        self.queries.assume(dependencies);
        self.observations.consume_dependencies(dependencies);
    }

    pub(crate) fn record_inference_use(
        &mut self,
        guard: NativeIntegrityGuard,
        proof: analysis_vocabulary::provenance::InferenceUse,
        dependencies: &ProofDependencies,
    ) {
        self.record_proof_dependencies(guard, dependencies);
        for reference in &dependencies.native_operation_inputs {
            for input in self
                .inputs
                .index
                .resolved_native_operation_inputs
                .iter()
                .filter(|input| &input.evidence.reference == reference)
            {
                let span = input.call_span;
                self.observations.record_native_operation_input_use(
                    span,
                    analysis_vocabulary::report_schema::NativeOperationInputUse {
                        evidence: input.evidence.clone(),
                        source_location: input.evidence.initializer_location.clone(),
                        span,
                    },
                );
            }
        }
        self.observations.record_inference_use(proof);
    }

    /// Descriptor application targets need the ordinary prototype premise;
    /// private constructors keep their separate property-specific checks.
    pub(crate) fn ordinary_object_prototype_intact(&mut self) -> bool {
        self.inputs.index.dynamic_scope_owners.is_empty()
            && self.native_guard_intact(
                NativeIntegrityGuard::OrdinaryObjectPrototype,
                &[IntrinsicSlot::Prototype(NativeIntrinsic::Object)],
            )
    }

    pub(super) fn own_property_projection(
        &self,
        values: &Values,
        request: OwnPropertyRequest,
    ) -> Option<OwnPropertyProof> {
        let Some(AbstractValue::Value(Value::Alloc(Kind::Object, object))) = values.first() else {
            return None;
        };
        if values.len() != 1 {
            return None;
        }
        let properties = self
            .state
            .world
            .surface
            .prefix_object_properties
            .get(object)?;
        self.queries.project_own_property(OwnPropertyQuery {
            source_object: *object,
            source_values: values.clone(),
            prefix_properties: properties.clone(),
            tainted: !self.state.world.object_properties_are_settled(*object),
            request,
        })
    }

    pub(super) fn ordinary_source_object(&self, values: &Values) -> Option<OwnPropertyProof> {
        self.own_property_projection(values, OwnPropertyRequest::Ordinary)
    }

    pub(crate) fn invalidate_own_descriptor_map(&mut self, site: CallSite) {
        self.descriptor_heap().invalidate(site);
    }

    pub(crate) fn descriptor_map_callbacks(&self, site: CallSite) -> Values {
        self.state
            .world
            .descriptors
            .get(&site)
            .map_or_else(Values::new, |map| map.retained_values.clone())
    }

    pub(crate) fn applied_getters(&self, receivers: &Values, property: Option<&str>) -> Values {
        let mut getters = Values::new();
        for value in receivers {
            let AbstractValue::Value(Value::Alloc(Kind::Object, object)) = value else {
                continue;
            };
            let Some(accessors) = self.state.world.surface.applied_accessors.get(object) else {
                continue;
            };
            if let Some(property) = property {
                if let Some(accessor) = accessors.get(property) {
                    getters.extend(accessor.getter.iter().cloned());
                }
            } else {
                getters.extend(
                    accessors
                        .values()
                        .flat_map(|accessor| accessor.getter.iter().cloned()),
                );
            }
        }
        getters
    }

    /// The fresh allocation's value: its contents are the row's writes to
    /// it; an allocation with no tracked contents is unknown.
    pub(super) fn fresh_allocation_values(
        &mut self,
        row: &'static Row,
        args: &[Values],
        site: CallSite,
    ) -> Values {
        if args.is_empty()
            && world::allocation_transfer::keyed_collection_new(
                row,
                site,
                ProofDependencies::default(),
            )
            .is_some()
        {
            // The operation kernel proves the row's fresh empty-construction
            // semantics. This interpreter has no keyed-collection heap lane,
            // so keep its established conservative value projection.
            return unknown();
        }
        let mut values = Values::new();
        for mutation in row.mutates {
            match *mutation {
                // The checked plan supplies these fresh allocations; a row
                // reaching here without one has no tracked contents.
                Mutation::FreshOwnDescriptorsOf(_) | Mutation::FreshOwnKeysOf(_) => {
                    return unknown();
                }
                Mutation::FreshRetains(ArgSel::Arg(index)) => {
                    let Some(argument) = args.get(index) else {
                        return unknown();
                    };
                    // Data descriptors retain object identities, whose fields can
                    // acquire callbacks later. Also snapshot current callbacks so
                    // replacing a source property cannot erase copied closures.
                    let mut retained = argument.clone();
                    retained.extend(self.callback_values(argument));
                    // This retained graph establishes no recursive ordinary-data
                    // shape or stronger retained-factory input contract.
                    values.extend([
                        self.deferred_callbacks(retained),
                        AbstractValue::Value(Value::Top),
                    ]);
                }
                Mutation::Integrity(_)
                | Mutation::DefineProperties { .. }
                | Mutation::DefineProperty { .. } => {}
                Mutation::FreshRetains(ArgSel::Rest(_)) => return unknown(),
            }
        }
        if values.is_empty() {
            return unknown();
        }
        values
    }

    pub(super) fn standard_object_prototype_property(property: &str) -> bool {
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
    pub(crate) fn absent_plain_data_property(&mut self, object: u32, property: &str) -> bool {
        if (self
            .state
            .world
            .classes
            .private_constructor_result_owners
            .is_empty()
            && !self
                .state
                .world
                .classes
                .checked_constructor_inputs
                .contains(&object))
            || Self::standard_object_prototype_property(property)
            || !self.inputs.index.plain_data_objects.contains(&object)
            || !self.state.world.object_properties_are_settled(object)
            || !self.input_field_prototype_absent(object, property)
        {
            return false;
        }
        self.state
            .world
            .surface
            .prefix_object_properties
            .get(&object)
            .is_some_and(|properties| {
                !properties.contains_key(&None)
                    && !properties.contains_key(&Some(property.to_owned()))
            })
    }

    pub(crate) fn accepts_deferred_global_write(
        &mut self,
        owner: SourceUnitOrdinal,
        env: &Context,
    ) -> bool {
        if !self
            .inputs
            .index
            .deferred_global_certificates
            .contains(&owner)
        {
            return false;
        }
        let Some(candidate) = &self.inputs.index.deferred_global_candidates[owner.0] else {
            return false;
        };
        let values = self.evaluate(&candidate.value, env, env);
        matches!(
            values.iter().collect::<Vec<_>>().as_slice(),
            [AbstractValue::Closure(_, _)]
                | [AbstractValue::Value(
                    Value::Alloc(Kind::Function, _) | Value::Alloc(Kind::Class, _)
                )]
        )
    }
    /// The payload heap retains accessor callbacks as capabilities, not as
    /// the result of reading their property. Only a data-tagged lookup may
    /// project that payload into an expression value.
    pub(crate) fn payload_lookup_is_data(&self, object: u32, property: Option<&str>) -> bool {
        if self
            .state
            .world
            .surface
            .applied_accessors
            .get(&object)
            .is_some_and(|accessors| {
                property.is_none_or(|property| accessors.contains_key(property))
            })
        {
            return false;
        }
        if let Some(shape) = self.inputs.index.ordinary_record_shape(object) {
            return match property {
                Some(property) => !matches!(
                    shape.properties.get(property),
                    Some(value_model::OwnPropertyDescriptorSyntax::Accessor { .. })
                ),
                None => shape.properties.values().all(|descriptor| {
                    matches!(descriptor, value_model::OwnPropertyDescriptorSyntax::Data)
                }),
            };
        }
        // Arrays and runtime-created data copies have their own shape proof.
        // An ambiguous source literal has neither, so its pooled payload does
        // not establish the descriptor selected by a later property lookup.
        self.inputs.index.array_elements.contains_key(&object)
            || self
                .state
                .world
                .surface
                .ordinary_spread_copies
                .contains(&object)
            || self
                .state
                .world
                .surface
                .descriptor_copy_results
                .contains(&object)
            || self
                .state
                .world
                .publication
                .parameter_initialized_objects
                .contains(&object)
    }

    /// Capabilities retained by the selected member, not its Get result.
    /// Called with receivers already evaluated at the current read prefix.
    pub(crate) fn selected_member_capabilities(
        &self,
        receivers: &Values,
        property: Option<&str>,
    ) -> Values {
        let mut selected = Values::new();
        for receiver in receivers {
            match receiver {
                AbstractValue::Value(Value::Alloc(Kind::Object, object)) => {
                    if let Some(properties) = self
                        .state
                        .world
                        .surface
                        .prefix_object_properties
                        .get(object)
                    {
                        if let Some(property) = property {
                            selected.extend(
                                properties
                                    .get(&Some(property.to_owned()))
                                    .into_iter()
                                    .flatten()
                                    .cloned(),
                            );
                            selected.extend(properties.get(&None).into_iter().flatten().cloned());
                        } else {
                            selected.extend(properties.values().flatten().cloned());
                        }
                    }
                }
                // These carriers already projected their retained capabilities
                // through Member. Preserve that policy without reevaluation.
                AbstractValue::Instance(_, _, _)
                | AbstractValue::DeferredCallbacks(_)
                | AbstractValue::ForeignDeferredRecord
                | AbstractValue::ArrayIntrinsic => {
                    selected.insert(receiver.clone());
                }
                AbstractValue::AuditedRetainedResult(_, captured) => {
                    selected.insert(AbstractValue::DeferredCallbacks(captured.clone()));
                }
                AbstractValue::Value(Value::Alloc(Kind::Class, key))
                | AbstractValue::ClassMembers(key) => {
                    selected.insert(AbstractValue::ClassMembers(*key));
                }
                AbstractValue::OwnDescriptorMap(site) => {
                    selected.extend(self.descriptor_map_callbacks(*site));
                }
                AbstractValue::Value(Value::Unavailable) => {}
                _ => {
                    selected.insert(AbstractValue::Value(Value::Top));
                }
            }
        }
        selected
    }

    pub(crate) fn known_local_instance_member(&self, key: u32, property: Option<&str>) -> bool {
        !self.state.world.classes.tainted.contains(&key)
            && property.is_some_and(|property| {
                self.inputs
                    .index
                    .class_values
                    .get(&key)
                    .is_some_and(|class| {
                        class.ordinary_instance_methods.contains(property)
                            || class
                                .instance_fields
                                .get(property)
                                .is_some_and(|value| !matches!(value, ValueExpr::Unknown))
                    })
                    || self
                        .inputs
                        .index
                        .local_constructors
                        .get(&key)
                        .is_some_and(|constructor| constructor.own_fields.contains(property))
            })
    }

    /// A private fresh instance has not been reachable through any other
    /// source use of its binding. Its checked class field remains an own data
    /// slot, so a different accessor on the class cannot receive this value.
    pub(crate) fn private_own_field_read(
        &self,
        receiver: &ValueExpr,
        property: Option<&str>,
        call: &Call,
        values: &Values,
    ) -> bool {
        let (ValueExpr::Binding(binding), Some(property)) = (receiver, property) else {
            return false;
        };
        if !self
            .state
            .world
            .classes
            .private_fresh_instance_bindings
            .contains(binding)
            || !self
                .inputs
                .index
                .sole_binding_reference_at(binding, call.span)
            || !self.inputs.index.single_eager_initializer_read(call)
            || !self.initialized_local_binding(binding)
        {
            return false;
        }
        let [AbstractValue::Instance(class, _, InstanceOrigin::Constructed)] =
            values.iter().collect::<Vec<_>>().as_slice()
        else {
            return false;
        };
        self.inputs
            .index
            .class_values
            .get(class)
            .is_some_and(|facts| {
                facts
                    .instance_fields
                    .get(property)
                    .is_some_and(|value| !matches!(value, ValueExpr::Unknown))
            })
    }

    pub(crate) fn ordinary_data_properties(
        &self,
        values: &Values,
    ) -> Option<BTreeMap<Option<String>, Values>> {
        let [AbstractValue::Value(Value::Alloc(Kind::Object, object))] =
            values.iter().collect::<Vec<_>>().as_slice()
        else {
            return None;
        };
        if !self.state.world.object_properties_are_settled(*object)
            || self
                .state
                .world
                .surface
                .applied_accessors
                .contains_key(object)
            || (!self.inputs.index.plain_data_objects.contains(object)
                && !self
                    .state
                    .world
                    .surface
                    .ordinary_spread_copies
                    .contains(object))
        {
            return None;
        }
        let properties = self
            .state
            .world
            .surface
            .prefix_object_properties
            .get(object)?;
        if properties.contains_key(&None) {
            return None;
        }
        Some(properties.clone())
    }

    pub(crate) fn validated_metadata_decoration(
        &mut self,
        application: &MetadataDecorateCall,
        env: &Context,
        captures: &Context,
    ) -> Option<ValidatedMetadataDecoration> {
        if !self.inputs.index.dynamic_scope_owners.is_empty()
            || !self
                .state
                .world
                .module
                .initialized_bindings
                .contains(&application.helper)
            || !self
                .state
                .world
                .module
                .initialized_bindings
                .contains(&application.target)
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
            Some(AbstractValue::Value(Value::Alloc(Kind::Function, key))) => *key,
            Some(AbstractValue::Closure(HeapAddress { callable: key, .. }, captured))
                if captured.values.is_empty() =>
            {
                *key
            }
            _ => return None,
        };
        let Some(AbstractValue::Value(Value::Alloc(Kind::Class, target_key))) = target.first()
        else {
            return None;
        };
        if application.stable_origins(self.inputs.flow) != Some((helper_key, *target_key)) {
            return None;
        }
        if application.member_decorators.iter().any(|root| {
            !self.inputs.index.imported_bindings.contains(root)
                || self.inputs.changed_member_roots.contains(root)
        }) {
            return None;
        }
        Some(ValidatedMetadataDecoration {
            target_owner: *self.inputs.binding_owner.get(&application.target)?,
        })
    }

    pub(crate) fn summarize_metadata_decoration(
        &mut self,
        application: &MetadataDecorateCall,
        validated: ValidatedMetadataDecoration,
        owner: SourceUnitOrdinal,
        outcome: &mut Outcome,
    ) {
        let result = self.request(RuntimeOperation::Model(
            super::super::runtime_models::ModelOperation::SummarizeMetadataDecoration {
                application: application.clone(),
                validated,
                owner,
                outcome: Box::new(std::mem::take(outcome)),
            },
        ));
        let RuntimeValue::Model(super::super::runtime_models::ModelResult::MetadataDecoration {
            outcome: model_outcome,
        }) = result
        else {
            unreachable!("metadata decoration models produce updated outcomes");
        };
        *outcome = *model_outcome;
    }

    pub(crate) fn summarize_metadata_decoration_transfer(
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
            if let Some(&binding_owner) = self.inputs.binding_owner.get(binding) {
                let (reads, rebinds) = self
                    .inputs
                    .source_unit_read_closure
                    .lazy_effects_for_source_unit(binding_owner);
                self.promote_lazy_effects(reads, rebinds, owner, outcome);
                outcome.executed_owners.insert(binding_owner);
            }
        }
        self.apply_rebind_effects(&outcome.effects.rebinds);
    }
}
