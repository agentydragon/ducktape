//! Checked factory calls whose fresh returned graph the executor builds itself:
//! private callback, closed record and closed constructor factories, and the
//! closed inputs they admit.

use super::*;

impl<'source, 'rule> Executor<'source, 'rule> {
    /// A validated factory retains the inline callback only in its fresh
    /// returned graph. Creating that graph does not publish the callback;
    /// later calls, writes, and exports still flow through the value model.
    pub(crate) fn private_callback_factory_result(
        &self,
        fact: &StatementFacts,
    ) -> Option<PrivateCallbackFactoryResult> {
        if !matches!(fact.kind, StatementKind::VarDecl)
            || fact.declared.len() != 1
            || self.inputs.exported_owners.contains(&fact.ordinal)
            || self.inputs.index.opaque_top.contains(&fact.ordinal)
        {
            return None;
        }
        let [(binding, ValueExpr::Call(callee, _, site))] =
            self.inputs.index.top_assignments[fact.ordinal.0].as_slice()
        else {
            return None;
        };
        let [call] = self.inputs.index.top_calls[fact.ordinal.0].as_slice() else {
            return None;
        };
        let source = self
            .inputs
            .index
            .initialization_source_identities
            .get(&fact.ordinal)?;
        let ValueExpr::Binding(factory) = callee.as_ref() else {
            return None;
        };
        let summary = self.inputs.index.callback_factory_summaries.get(factory)?;
        if source.owner() != fact.ordinal
            || source.binding() != binding
            || !fact.declared.contains(binding)
            || !self.inputs.index.top_definite_initializers[fact.ordinal.0].contains(binding)
            || !summary.supports_callback_factory()
            || call.kind != CallKind::Call
            || call.has_spread
            || CallSite::from(call.span) != *site
            || source.initializer_span().lo > call.span.lo
            || source.initializer_span().hi < call.span.hi
            || self.inputs.index.direct_inline_callback_calls.get(site) != Some(factory)
            // The retention summary permits provider-private effects and is
            // not an evaluation-purity proof. Keep this classifier obligation
            // until an independent exact evaluation proof can replace it.
            || !self.queries.prepared_statement_is_pure(fact.ordinal)
        {
            return None;
        }
        Some(PrivateCallbackFactoryResult {
            proof_use: CallbackFactoryUseFact {
                imported_local: factory.0.to_string(),
                evidence: summary
                    .callback_factory_evidence()
                    .iter()
                    .cloned()
                    .collect(),
                kind: CallbackFactoryUseKind::ClosedResultRoot,
                lexical_statement_ordinal: fact.ordinal,
                source_location: None,
                span: call.span,
            },
            dependencies: self.settled_prefix_dependencies(),
        })
    }

    /// A return-only local function can put its arguments into a fresh
    /// ordinary record without publishing or calling them. The initializer
    /// still runs normally; only its result owner's speculative root is
    /// omitted until a later read, call, write, or export exposes that value.
    pub(super) fn closed_local_record_factory_result(&self, fact: &StatementFacts) -> bool {
        if !matches!(fact.kind, StatementKind::VarDecl)
            || !self.queries.prepared_statement_is_pure(fact.ordinal)
            || fact.declared.len() != 1
            || self.state.world.reachability.incomplete
            || !self.inputs.index.dynamic_scope_owners.is_empty()
            || self.inputs.index.opaque_top.contains(&fact.ordinal)
            || !self.inputs.index.top_property_writes[fact.ordinal.0].is_empty()
            || !self.inputs.index.top_member_value_writes[fact.ordinal.0].is_empty()
            || !self.inputs.index.top_callback_result_writes[fact.ordinal.0].is_empty()
            || !self.inputs.index.top_exported_values[fact.ordinal.0].is_empty()
        {
            return false;
        }
        let [(binding, ValueExpr::Call(callee, args, site))] =
            self.inputs.index.top_assignments[fact.ordinal.0].as_slice()
        else {
            return false;
        };
        let [call] = self.inputs.index.top_calls[fact.ordinal.0].as_slice() else {
            return false;
        };
        let ValueExpr::Binding(factory) = callee.as_ref() else {
            return false;
        };
        let Some(&key) = self.inputs.index.hoisted_bindings.get(factory) else {
            return false;
        };
        if !fact.declared.contains(binding)
            || !self.inputs.index.top_definite_initializers[fact.ordinal.0].contains(binding)
            || self
                .inputs
                .flow
                .values_of_binding(factory)
                .iter()
                .ne([Value::Alloc(Kind::Function, key)])
            || call.kind != CallKind::Call
            || call.has_spread
            || CallSite::from(call.span) != *site
            || !args.iter().all(|arg| {
                matches!(
                    arg,
                    ValueExpr::Alloc(Kind::Function, _) | ValueExpr::NonCallable(_)
                )
            })
        {
            return false;
        }
        let Some(callable) = self.inputs.index.callables.get(&key) else {
            return false;
        };
        if self.fresh_local_record_receiver(key, callable).is_some() {
            return true;
        }
        if callable.opaque
            || callable.generator
            || self.inputs.index.async_callables.contains(&key)
            || callable.may_fall_through
            || !callable.calls.is_empty()
            || !callable.rebinds.is_empty()
            || !callable.member_writes.is_empty()
            || !callable.member_value_writes.is_empty()
            || !callable.callback_result_writes.is_empty()
            || callable.dynamic_property_effect
            || !callable.escaped_bindings.is_empty()
            || !callable.namespace_escape_values.is_empty()
            || callable
                .params
                .iter()
                .any(|param| param.binding.is_none() || param.default.is_some())
            || callable.local_assignments.iter().any(|(id, value)| {
                !callable.local_functions.contains_key(id)
                    || !matches!(value, ValueExpr::Alloc(Kind::Function, _))
            })
            || callable.body_facts.value_flow().returns().len() != 1
            || !matches!(
                callable.body_facts.value_flow().returns()[0],
                ValueExpr::ObjectLiteral(..)
            )
        {
            return false;
        }
        let allowed: BTreeSet<_> = callable
            .params
            .iter()
            .filter_map(|param| param.binding.clone())
            .chain(callable.local_functions.keys().cloned())
            .collect();
        callable.reads.is_subset(&allowed)
            && self
                .closed_local_record_value(&callable.body_facts.value_flow().returns()[0], &allowed)
    }

    /// Check that the shared ordered record certificate is fully represented
    /// by the execution index before treating its writes as private own-data
    /// writes. Incomplete index coverage retains ordinary promotion.
    pub(super) fn fresh_local_record_receiver(&self, key: u32, callable: &Callable) -> Option<u32> {
        let proof = self.inputs.graph.fresh_record_proof(key)?;
        let allowed: BTreeSet<_> = callable
            .params
            .iter()
            .filter_map(|param| param.binding.clone())
            .chain(std::iter::once(proof.local.clone()))
            .collect();
        if callable.opaque
            || callable.generator
            || self.inputs.index.async_callables.contains(&key)
            || callable.may_fall_through
            || !callable.calls.is_empty()
            || !callable.rebinds.is_empty()
            || callable.dynamic_property_effect
            || !callable.reads.is_subset(&allowed)
            || callable.local_assignments.len() != 1
            || !matches!(&callable.local_assignments[0], (id, ValueExpr::ObjectLiteral(object, _))
                if id == &proof.local && object == &proof.object)
            || callable.body_facts.value_flow().returns().len() != 1
            || !matches!(&callable.body_facts.value_flow().returns()[0], ValueExpr::Binding(id) if id == &proof.local)
            || callable.escaped_bindings != BTreeSet::from([proof.local.clone()])
            || !self.inputs.index.plain_data_objects.contains(&proof.object)
            || callable.callback_result_writes.len() != proof.write_spans.len()
            || callable.member_value_writes.len() != proof.write_spans.len()
            || callable.member_writes.len() != proof.write_spans.len()
            || callable.callback_result_writes.iter().any(|write| {
                !write.simple_assignment
                    || !write.data_read_operands
                    || !matches!(&write.receiver, ValueExpr::Binding(id) if id == &proof.local)
                    || write.property.is_none()
            })
            || callable
                .member_value_writes
                .iter()
                .any(|write| write.receiver != proof.local || write.property.is_none())
            || callable
                .member_writes
                .iter()
                .any(|(id, _)| id != &proof.local)
        {
            return None;
        }
        Some(proof.object)
    }

    pub(super) fn closed_local_record_value(
        &self,
        value: &ValueExpr,
        allowed: &BTreeSet<Id>,
    ) -> bool {
        match value {
            ValueExpr::ObjectLiteral(key, properties) => {
                self.inputs.index.plain_data_objects.contains(key)
                    && properties.iter().all(|(name, value)| {
                        name.is_some() && self.closed_local_record_value(value, allowed)
                    })
            }
            ValueExpr::Binding(id) => allowed.contains(id),
            ValueExpr::Alloc(Kind::Function, _)
            | ValueExpr::NonCallable(_)
            | ValueExpr::Undefined => true,
            _ => false,
        }
    }

    /// An author assertion is attached to the canonical top-level factory,
    /// and only supplies premises at an exact, syntactically fresh call site.
    /// `Err` names the refused argument shape of a direct annotated call.
    pub(crate) fn annotated_record_factory(
        &mut self,
        fact: &StatementFacts,
    ) -> Option<Result<(Id, Span), LocalRecordFactoryRejection>> {
        let [(_, ValueExpr::Call(callee, _, site))] =
            self.inputs.index.top_assignments[fact.ordinal.0].as_slice()
        else {
            return None;
        };
        let ValueExpr::Binding(factory) = callee.as_ref() else {
            return None;
        };
        if !self.inputs.top_bindings.contains(factory)
            || !self
                .inputs
                .index
                .retained_record_factories
                .contains(factory.0.as_ref())
        {
            return None;
        }
        let argument = self.inputs.index.record_factory_calls.get(site)?;
        let call = self.inputs.index.top_calls[fact.ordinal.0]
            .iter()
            .find(|call| CallSite::from(call.span) == *site)?;
        let rejection = |reason, property| LocalRecordFactoryRejection {
            factory_binding: factory.0.to_string(),
            reason,
            property,
            construction: None,
            first_callback_publication_owner: None,
            first_class_members_publication: None,
            lexical_statement_ordinal: fact.ordinal,
            source_location: None,
            span: call.span,
        };
        let record = match argument {
            Ok(record) => record,
            Err((reason, property)) => return Some(Err(rejection(*reason, property.clone()))),
        };
        // `ToString` of a proven primitive runs no user code (none of the
        // admitted primitives is a Symbol, whose `ToString` throws).
        if let Some((property, _)) = record
            .template_substitutions
            .iter()
            .find(|(_, value)| !self.closed_primitive_input(value))
        {
            return Some(Err(rejection(
                RecordFactoryRejectionReason::TemplateSubstitution,
                Some(property.clone()),
            )));
        }
        Some(Ok((factory.clone(), call.span)))
    }

    /// The first record property the checked constructor proof refused, for
    /// an annotated call whose argument passed the syntax check.
    pub(crate) fn record_factory_input_rejection(
        &mut self,
        fact: &StatementFacts,
    ) -> Option<(RecordFactoryRejectionReason, Option<String>)> {
        let [(_, ValueExpr::Call(_, args, _))] =
            self.inputs.index.top_assignments[fact.ordinal.0].as_slice()
        else {
            return None;
        };
        let [ValueExpr::ObjectLiteral(_, properties)] = args.as_ref() else {
            return None;
        };
        properties.iter().find_map(|(property, value)| {
            self.unclosed_constructor_input(value)
                .map(|reason| (reason, property.clone()))
        })
    }

    /// The failed check of the factory's returned top-level allocation, with
    /// its class identity when the source resolves one.
    pub(crate) fn returned_class_construction_rejection(
        &mut self,
        factory: &Id,
    ) -> Option<(Option<u32>, ConstructionRejection)> {
        let key = self.inputs.index.hoisted_bindings.get(factory)?;
        let [ValueExpr::New(class, args, _)] = self
            .inputs
            .index
            .callables
            .get(key)?
            .body_facts
            .value_flow()
            .returns()
        else {
            return None;
        };
        let ValueExpr::Binding(class) = class.as_ref() else {
            return None;
        };
        let Some(class) = self.stable_local_class_key(class) else {
            return Some((None, ConstructionRejection::UncheckedConstructor));
        };
        Some((
            Some(class),
            self.eligible_local_constructor(class, args.len()).err()?,
        ))
    }

    pub(super) fn unclosed_constructor_input(
        &mut self,
        value: &ValueExpr,
    ) -> Option<RecordFactoryRejectionReason> {
        if self.closed_constructor_input(value) {
            return None;
        }
        Some(match value {
            ValueExpr::Binding(_) => RecordFactoryRejectionReason::UnprovenBinding,
            ValueExpr::Member(..) => RecordFactoryRejectionReason::UnprovenMemberRead,
            ValueExpr::ObjectLiteral(_, properties) => properties
                .iter()
                .find_map(|(_, value)| self.unclosed_constructor_input(value))
                .unwrap_or(RecordFactoryRejectionReason::UnsupportedValue),
            _ => RecordFactoryRejectionReason::UnsupportedValue,
        })
    }

    /// The annotation's prototype assertion may pass through a wrapper only
    /// at the exact selected factory call. Its evaluated operand must still
    /// be the source literal checked for accessors, spreads and computed keys.
    /// All wrapper and argument effects have already run in the ordinary lane.
    pub(super) fn checked_nested_record_factory_call(
        &mut self,
        call: &Call,
        args: &[Values],
        env: &Context,
        captures: &Context,
    ) -> Option<(u32, u32, u32, Vec<String>)> {
        if call.caller.is_none() || call.kind != CallKind::Call || call.has_spread {
            return None;
        }
        let ValueExpr::Binding(factory) = &call.callee else {
            return None;
        };
        let &factory_key = self.inputs.index.hoisted_bindings.get(factory)?;
        if !self
            .inputs
            .index
            .retained_record_factories
            .contains(factory.0.as_ref())
            || self.inputs.index.exported_bindings.contains(factory)
            || self
                .state
                .world
                .publication
                .global_escaped_callbacks
                .contains(&AbstractValue::Value(Value::Alloc(
                    Kind::Function,
                    factory_key,
                )))
        {
            return None;
        }
        let record = self
            .inputs
            .index
            .record_factory_calls
            .get(&CallSite::from(call.span))?
            .as_ref()
            .ok()?;
        if record.template_substitutions.iter().any(|(_, value)| {
            if !matches!(value, ValueExpr::Binding(_)) {
                return true;
            }
            let evaluated = self.evaluate(value, env, captures);
            evaluated.is_empty() || !evaluated.iter().all(AbstractValue::is_primitive)
        }) {
            return None;
        }
        let ([ValueExpr::ObjectLiteral(object, _)], [values]) = (call.args.as_slice(), args) else {
            return None;
        };
        if values.len() != 1
            || !values.contains(&AbstractValue::Value(Value::Alloc(Kind::Object, *object)))
            || !self.inputs.index.plain_data_objects.contains(object)
            || !self.state.world.object_properties_are_settled(*object)
            || !self.inputs.index.dynamic_scope_owners.is_empty()
        {
            return None;
        }
        let callable = self.inputs.index.callables.get(&factory_key)?.clone();
        if callable.opaque
            || callable.generator
            || callable.may_fall_through
            || self.inputs.index.async_callables.contains(&factory_key)
            || !callable.rebinds.is_empty()
            || !callable.local_assignments.is_empty()
            || !callable.member_writes.is_empty()
            || !callable.member_value_writes.is_empty()
            || !callable.callback_result_writes.is_empty()
            || callable.dynamic_property_effect
            || !callable.escaped_bindings.is_empty()
            || !callable.namespace_escape_values.is_empty()
            || callable.params.len() != 1
            || callable.params[0].binding.is_none()
            || callable.params[0].default.is_some()
        {
            return None;
        }
        let [returned @ ValueExpr::New(callee, _, _)] = callable.body_facts.value_flow().returns()
        else {
            return None;
        };
        let ValueExpr::Binding(class_binding) = callee.as_ref() else {
            return None;
        };
        let class_key = self.stable_local_class_key(class_binding)?;
        let params = BTreeMap::from([(callable.params[0].binding.clone()?, &call.args[0])]);
        let premises_before = self
            .observations
            .record_factory_prototype_premises()
            .clone();
        let previous = self
            .state
            .nested_record_factory_premise
            .replace((class_key, *object));
        let mut class_reads = BTreeSet::new();
        let mut allocations = BTreeMap::new();
        let checked = self.checked_constructor_return(
            returned,
            &params,
            false,
            &mut class_reads,
            &mut allocations,
        );
        let required_prototype_properties = self
            .observations
            .record_factory_prototype_premises()
            .properties_for(class_key, *object);
        self.state.nested_record_factory_premise = previous;
        *self.observations.record_factory_prototype_premises_mut() = premises_before;
        if !checked
            || !callable
                .reads
                .is_subset(&params.keys().cloned().chain(class_reads).collect())
        {
            return None;
        }
        let mut indexed_allocations = BTreeMap::new();
        for nested in &callable.calls {
            let ValueExpr::Binding(class) = &nested.callee else {
                return None;
            };
            let key = self.stable_local_class_key(class)?;
            if nested.kind != CallKind::New || nested.has_spread {
                return None;
            }
            *indexed_allocations
                .entry((key, nested.args.len()))
                .or_insert(0) += 1;
        }
        (allocations == indexed_allocations).then_some((
            factory_key,
            class_key,
            *object,
            required_prototype_properties,
        ))
    }

    /// A checked constructor can retain callbacks in a fresh instance without
    /// exposing them during the initializer. The ordinary executor still runs
    /// the wrapper and constructor. Exported results require a closure that
    /// rules out synchronous importer reentry during provider initialization.
    pub(crate) fn closed_local_constructor_factory_result(
        &mut self,
        fact: &StatementFacts,
    ) -> Result<CheckedLocalConstructorFactory, RecordFactoryRejectionReason> {
        if self.state.world.reachability.incomplete {
            return Err(RecordFactoryRejectionReason::IncompleteExecution);
        }
        if self.inputs.exported_owners.contains(&fact.ordinal) {
            return Err(RecordFactoryRejectionReason::ExportedResult);
        }
        if !matches!(fact.kind, StatementKind::VarDecl)
            || fact.declared.len() != 1
            || self.inputs.index.opaque_top.contains(&fact.ordinal)
            || !self.inputs.index.top_property_writes[fact.ordinal.0].is_empty()
            || !self.inputs.index.top_member_value_writes[fact.ordinal.0].is_empty()
            || !self.inputs.index.top_callback_result_writes[fact.ordinal.0].is_empty()
            || !self.inputs.index.top_exported_values[fact.ordinal.0].is_empty()
        {
            return Err(RecordFactoryRejectionReason::StatementShape);
        }
        let [(binding, ValueExpr::Call(callee, args, site))] =
            self.inputs.index.top_assignments[fact.ordinal.0].as_slice()
        else {
            return Err(RecordFactoryRejectionReason::StatementShape);
        };
        // Proven enum own-field reads, and template `ToString` of proven
        // primitives, are retained in the normal top-level executor and eager
        // facts, but cannot invoke a getter or callback.
        let calls: Vec<_> = self.inputs.index.top_calls[fact.ordinal.0]
            .iter()
            .filter(|call| match call.kind {
                CallKind::LocalMemberRead => !self.closed_enum_member_read(&call.callee),
                CallKind::ImplicitCoercion => !matches!(call.args.as_slice(),
                    [argument] if self.closed_primitive_input(argument)),
                _ => true,
            })
            .collect();
        if !args.iter().all(|arg| self.closed_constructor_input(arg)) {
            return Err(RecordFactoryRejectionReason::UnsupportedValue);
        }
        let [call] = calls.as_slice() else {
            return Err(RecordFactoryRejectionReason::ArgumentEffects);
        };
        let ValueExpr::Binding(factory) = callee.as_ref() else {
            return Err(RecordFactoryRejectionReason::FactoryCalleeShape);
        };
        let &key = self
            .inputs
            .index
            .hoisted_bindings
            .get(factory)
            .ok_or(RecordFactoryRejectionReason::FactoryNotHoisted)?;
        if !fact.declared.contains(binding)
            || !self.inputs.index.top_definite_initializers[fact.ordinal.0].contains(binding)
        {
            return Err(RecordFactoryRejectionReason::InitializerBinding);
        }
        if self.inputs.index.exported_bindings.contains(factory) {
            return Err(RecordFactoryRejectionReason::FactoryExported);
        }
        if self
            .state
            .world
            .publication
            .global_escaped_callbacks
            .contains(&AbstractValue::Value(Value::Alloc(Kind::Function, key)))
        {
            return Err(RecordFactoryRejectionReason::FactoryEscaped);
        }
        if self
            .inputs
            .flow
            .values_of_binding(factory)
            .iter()
            .ne([Value::Alloc(Kind::Function, key)])
        {
            return Err(RecordFactoryRejectionReason::FactoryFlowIdentity);
        }
        if !self
            .state
            .world
            .module
            .prefix_values
            .get(factory)
            .is_some_and(|values| {
                values.len() == 1
                    && values.contains(&AbstractValue::Value(Value::Alloc(Kind::Function, key)))
            })
        {
            return Err(RecordFactoryRejectionReason::FactoryPrefixIdentity);
        }
        if call.kind != CallKind::Call || call.has_spread || CallSite::from(call.span) != *site {
            return Err(RecordFactoryRejectionReason::ArgumentEffects);
        }
        let callable = self
            .inputs
            .index
            .callables
            .get(&key)
            .ok_or(RecordFactoryRejectionReason::FactoryBody)?;
        if callable.opaque
            || callable.generator
            || self.inputs.index.async_callables.contains(&key)
            || callable.may_fall_through
            || !callable.rebinds.is_empty()
            || !callable.local_assignments.is_empty()
            || !callable.member_writes.is_empty()
            || !callable.member_value_writes.is_empty()
            || !callable.callback_result_writes.is_empty()
            || callable.dynamic_property_effect
            || !callable.escaped_bindings.is_empty()
            || !callable.namespace_escape_values.is_empty()
            || callable.body_facts.value_flow().returns().len() != 1
            || callable.params.len() != args.len()
            || callable
                .params
                .iter()
                .any(|param| param.binding.is_none() || param.default.is_some())
        {
            return Err(RecordFactoryRejectionReason::FactoryBody);
        }
        let params: BTreeMap<_, _> = callable
            .params
            .iter()
            .zip(args.iter())
            .map(|(param, arg)| (param.binding.clone().unwrap(), arg))
            .collect();
        let mut class_reads = BTreeSet::new();
        let mut allocations = BTreeMap::new();
        let checked_return = self.checked_constructor_return(
            &callable.body_facts.value_flow().returns()[0],
            &params,
            false,
            &mut class_reads,
            &mut allocations,
        );
        if !checked_return {
            return Err(RecordFactoryRejectionReason::ConstructorProof);
        }
        let allowed_reads: BTreeSet<_> = params.keys().cloned().chain(class_reads).collect();
        if !callable.reads.is_subset(&allowed_reads) {
            return Err(RecordFactoryRejectionReason::FactoryBody);
        }
        let mut indexed_allocations = BTreeMap::new();
        for nested in &callable.calls {
            let ValueExpr::Binding(class) = &nested.callee else {
                return Err(RecordFactoryRejectionReason::FactoryBody);
            };
            let class_key = self
                .stable_local_class_key(class)
                .ok_or(RecordFactoryRejectionReason::ConstructorProof)?;
            if nested.kind != CallKind::New || nested.has_spread {
                return Err(RecordFactoryRejectionReason::FactoryBody);
            }
            *indexed_allocations
                .entry((class_key, nested.args.len()))
                .or_insert(0) += 1;
        }
        if allocations != indexed_allocations {
            return Err(RecordFactoryRejectionReason::FactoryBody);
        }
        let mut owners = BTreeSet::from([callable.owner]);
        for (class_key, _) in allocations.keys() {
            owners.insert(
                *self
                    .inputs
                    .index
                    .class_owners
                    .get(class_key)
                    .ok_or(RecordFactoryRejectionReason::ConstructorProof)?,
            );
        }
        Ok(CheckedLocalConstructorFactory {
            call_span: call.span,
            checked_owners: owners,
            heap_local: args
                .iter()
                .any(|arg| matches!(arg, ValueExpr::ObjectLiteral(..)))
                && allocations.keys().all(|(class, _)| {
                    self.inputs
                        .index
                        .local_constructors
                        .get(class)
                        .is_some_and(|constructor| {
                            constructor.inert_allocation
                                || (!constructor.checked_default_fields
                                    && constructor.key.is_some())
                        })
                }),
        })
    }

    pub(super) fn closed_constructor_input(&mut self, value: &ValueExpr) -> bool {
        match value {
            ValueExpr::ObjectLiteral(key, properties) => {
                if self
                    .state
                    .nested_record_factory_premise
                    .is_some_and(|(_, checked)| checked == *key)
                {
                    return self.inputs.index.plain_data_objects.contains(key);
                }
                if let Some(elements) = self.inputs.index.array_elements.get(key) {
                    return self.inputs.index.dense_array_literals.contains_key(key)
                        && elements.iter().all(|element| {
                            element.as_ref().is_some_and(|element| {
                                element.spread.is_none()
                                    && self.closed_constructor_input(&element.value)
                            })
                        });
                }
                self.inputs.index.plain_data_objects.contains(key)
                    && properties
                        .iter()
                        .all(|(name, value)| name.is_some() && self.closed_constructor_input(value))
            }
            ValueExpr::Member(..) => self.closed_enum_member_read(value),
            ValueExpr::Binding(binding) => {
                self.closed_primitive_binding(binding)
                    || self.closed_function_binding(binding)
                    || self.closed_primitive_data_binding(binding)
            }
            ValueExpr::Primitive(operands) => operands
                .iter()
                .all(|operand| matches!(operand, ValueExpr::NonCallable(_) | ValueExpr::Undefined)),
            ValueExpr::Void(operand) | ValueExpr::LogicalNot(operand) => {
                matches!(
                    operand.as_ref(),
                    ValueExpr::NonCallable(_) | ValueExpr::Undefined
                )
            }
            ValueExpr::StrictEquality(left, right) => [left.as_ref(), right.as_ref()]
                .into_iter()
                .all(|operand| matches!(operand, ValueExpr::NonCallable(_) | ValueExpr::Undefined)),
            ValueExpr::Alloc(Kind::Function, _)
            | ValueExpr::NonCallable(_)
            | ValueExpr::Undefined => true,
            _ => false,
        }
    }

    /// A proven primitive: an initialized literal `const` or a closed enum's
    /// own primitive member.
    pub(super) fn closed_primitive_input(&mut self, value: &ValueExpr) -> bool {
        match value {
            ValueExpr::Binding(binding) => self.closed_primitive_binding(binding),
            ValueExpr::Member(..) => self.closed_enum_member_read(value),
            _ => false,
        }
    }

    pub(super) fn closed_primitive_binding(&self, binding: &Id) -> bool {
        self.inputs
            .index
            .direct_primitive_const_bindings
            .contains(binding)
            && self
                .state
                .world
                .module
                .initialized_bindings
                .contains(binding)
            && self
                .state
                .world
                .module
                .prefix_values
                .get(binding)
                .is_some_and(|values| {
                    !values.is_empty() && values.iter().all(AbstractValue::is_primitive)
                })
    }

    /// An initialized top-level binding whose every whole-chunk value is one
    /// source function. Reading it runs no code, and the record retains the
    /// function exactly as it retains an inline callback: storing is not a call.
    pub(super) fn closed_function_binding(&self, binding: &Id) -> bool {
        if !self.inputs.top_bindings.contains(binding)
            || !self.inputs.index.dynamic_scope_owners.is_empty()
            || !self
                .state
                .world
                .module
                .initialized_bindings
                .contains(binding)
        {
            return false;
        }
        let [Value::Alloc(Kind::Function, key)] = self
            .inputs
            .flow
            .values_of_binding(binding)
            .iter()
            .collect::<Vec<_>>()[..]
        else {
            return false;
        };
        self.state
            .world
            .module
            .prefix_values
            .get(binding)
            .is_some_and(|values| {
                values.len() == 1
                    && values.iter().all(|value| {
                        matches!(value, AbstractValue::Value(Value::Alloc(Kind::Function, function))
                        | AbstractValue::Closure(HeapAddress { callable: function, .. }, _) if *function == key)
                    })
            })
    }

    /// An initialized top-level binding to one untainted literal array or
    /// plain-data object whose every current slot holds a primitive. The
    /// shared identity runs no code when stored, holds no callback, and later
    /// writes through any alias are tracked on the same allocation.
    pub(super) fn closed_primitive_data_binding(&self, binding: &Id) -> bool {
        if !self.inputs.top_bindings.contains(binding)
            || !self.inputs.index.dynamic_scope_owners.is_empty()
            || !self
                .state
                .world
                .module
                .initialized_bindings
                .contains(binding)
        {
            return false;
        }
        let [Value::Alloc(Kind::Object, object)] = self
            .inputs
            .flow
            .values_of_binding(binding)
            .iter()
            .collect::<Vec<_>>()[..]
        else {
            return false;
        };
        let literal = if self.inputs.index.array_elements.contains_key(&object) {
            self.inputs.index.dense_array_literals.contains_key(&object)
        } else {
            self.inputs.index.plain_data_objects.contains(&object)
        };
        literal
            && self.state.world.object_properties_are_settled(object)
            && self
                .state
                .world
                .module
                .prefix_values
                .get(binding)
                .is_some_and(|values| {
                    values.len() == 1
                        && values
                            .contains(&AbstractValue::Value(Value::Alloc(Kind::Object, object)))
                })
            && self
                .state
                .world
                .surface
                .prefix_object_properties
                .get(&object)
                .is_some_and(|properties| {
                    properties
                        .values()
                        .flatten()
                        .all(AbstractValue::is_primitive)
                })
            && self
                .state
                .world
                .arrays
                .dense
                .get(&object)
                .is_none_or(|dense| {
                    dense
                        .slots
                        .iter()
                        .flatten()
                        .all(AbstractValue::is_primitive)
                })
    }

    pub(super) fn stable_local_class_key(&self, binding: &Id) -> Option<u32> {
        let values = self.state.world.module.prefix_values.get(binding)?;
        let [AbstractValue::Value(Value::Alloc(Kind::Class, key))] =
            values.iter().collect::<Vec<_>>().as_slice()
        else {
            return None;
        };
        (self
            .state
            .world
            .module
            .initialized_bindings
            .contains(binding)
            && self.inputs.index.class_bindings.get(key) == Some(binding))
        .then_some(*key)
    }

    pub(super) fn checked_constructor_return(
        &mut self,
        value: &ValueExpr,
        params: &BTreeMap<Id, &ValueExpr>,
        nested: bool,
        class_reads: &mut BTreeSet<Id>,
        allocations: &mut BTreeMap<(u32, usize), usize>,
    ) -> bool {
        let ValueExpr::New(callee, args, _) = value else {
            return false;
        };
        let ValueExpr::Binding(class) = callee.as_ref() else {
            return false;
        };
        let Some(key) = self.stable_local_class_key(class) else {
            return false;
        };
        let Ok(constructor) = self.eligible_local_constructor(key, args.len()) else {
            return false;
        };
        if nested && !constructor.inert_allocation {
            return false;
        }
        let property_reads = constructor.property_reads.clone();
        class_reads.insert(class.clone());
        *allocations.entry((key, args.len())).or_insert(0) += 1;
        for arg in args.iter() {
            match arg {
                ValueExpr::New(..) => {
                    if !self.checked_constructor_return(arg, params, true, class_reads, allocations)
                    {
                        return false;
                    }
                }
                ValueExpr::Binding(binding) => {
                    if !params
                        .get(binding)
                        .is_some_and(|value| self.closed_constructor_input(value))
                    {
                        return false;
                    }
                }
                other if self.closed_constructor_input(other) => {}
                _ => return false,
            }
        }
        for (index, property) in &property_reads {
            let Some(arg) = args.get(*index) else {
                return false;
            };
            let source = match arg {
                ValueExpr::Binding(binding) => {
                    let Some(value) = params.get(binding) else {
                        return false;
                    };
                    *value
                }
                other => other,
            };
            let ValueExpr::ObjectLiteral(object, properties) = source else {
                return false;
            };
            if !self.inputs.index.plain_data_objects.contains(object)
                || (!properties
                    .iter()
                    .any(|(name, _)| name.as_deref() == Some(property))
                    && !self.input_field_prototype_absent(*object, property))
            {
                return false;
            }
        }
        true
    }
}
