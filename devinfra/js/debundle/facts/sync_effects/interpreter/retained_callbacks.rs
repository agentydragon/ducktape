//! Stored-reference traversal, retained callbacks and member-write application.

use super::*;

impl<'source, 'rule> Executor<'source, 'rule> {
    /// Enumerate one value's retained references without executing any
    /// callable capability. Object properties can be skipped after the
    /// callback walk has already expanded that object's property map; applied
    /// accessor capabilities remain visible on every visit.
    pub(super) fn visit_stored_references(
        &self,
        value: &AbstractValue,
        include_object_properties: bool,
        mut visit: impl FnMut(&AbstractValue),
    ) {
        self.visit_stored_references_with_edges(
            value,
            include_object_properties,
            |reference, _| {
                visit(reference);
            },
        );
    }

    /// The single inventory of retained-reference carriers. Edge labels are
    /// borrowed and inexpensive for ordinary callback, taint and exposure
    /// walks; diagnostic paths allocate labels only when explicitly collected.
    pub(super) fn visit_stored_references_with_edges(
        &self,
        value: &AbstractValue,
        include_object_properties: bool,
        mut visit: impl FnMut(&AbstractValue, StoredReferenceEdge<'_>),
    ) {
        match value {
            AbstractValue::Value(Value::Alloc(kind, object)) => {
                if kind.tracks_prefix_references() {
                    // RegExp identity is retained so references stored on a
                    // literal remain reachable. This is containment only; it
                    // does not make regex properties or prototype behavior
                    // an ordinary-record fact.
                    if include_object_properties
                        && let Some(properties) = self
                            .state
                            .world
                            .surface
                            .prefix_object_properties
                            .get(object)
                    {
                        for (property, values) in properties {
                            let edge = if matches!(kind, Kind::Array) {
                                StoredReferenceEdge::ArrayProperty(property.as_deref())
                            } else {
                                StoredReferenceEdge::ObjectProperty(property.as_deref())
                            };
                            for value in values {
                                visit(value, edge);
                            }
                        }
                    }
                    if let Some(accessors) = self.state.world.surface.applied_accessors.get(object)
                    {
                        for (property, accessor) in accessors {
                            for getter in &accessor.getter {
                                visit(getter, StoredReferenceEdge::AppliedGetter(property));
                            }
                            for setter in &accessor.setter {
                                visit(setter, StoredReferenceEdge::AppliedSetter(property));
                            }
                        }
                    }
                } else if !matches!(kind, Kind::Function | Kind::Class) {
                    unreachable!("unsupported retained-reference allocation kind");
                }
            }
            AbstractValue::Closure(_, captured) => {
                if let Some(home_object) = captured.values.home_object {
                    visit(
                        &AbstractValue::Value(Value::Alloc(Kind::Object, home_object)),
                        StoredReferenceEdge::ClosureHomeObject,
                    );
                }
                for (binding, values) in &captured.values.snapshots {
                    for value in values {
                        visit(
                            value,
                            StoredReferenceEdge::ClosureSnapshot(binding.0.as_ref()),
                        );
                    }
                }
                for (binding, cell) in &captured.values.cells {
                    for value in &self.state.world.cells[cell.0].values {
                        visit(value, StoredReferenceEdge::ClosureCell(binding.0.as_ref()));
                    }
                }
            }
            AbstractValue::Instance(_, captured, _) => {
                for value in captured.values.iter() {
                    visit(value, StoredReferenceEdge::InstanceRetainedValue);
                }
            }
            AbstractValue::DeferredCallbacks(captured) => {
                for value in captured.values.iter() {
                    visit(value, StoredReferenceEdge::DeferredCallbackRetainedValue);
                }
            }
            AbstractValue::AuditedRetainedResult(_, captured) => {
                for value in captured.values.iter() {
                    visit(value, StoredReferenceEdge::AuditedResultRetainedValue);
                }
            }
            AbstractValue::OwnDescriptorMap(site) => {
                if let Some(map) = self.state.world.descriptors.get(site) {
                    for value in &map.retained_values {
                        visit(value, StoredReferenceEdge::DescriptorMapRetainedValue);
                    }
                }
            }
            // Keep the carrier inventory exhaustive: when AbstractValue gains
            // another retained-reference shape, this visitor must classify it.
            AbstractValue::Value(Value::Prim(_) | Value::Unavailable | Value::Top)
            | AbstractValue::ClassMembers(_)
            | AbstractValue::CallbackFactory
            | AbstractValue::InlineCallbackFactory
            | AbstractValue::ForeignDeferredRecord
            | AbstractValue::ObjectAssign
            | AbstractValue::ArrayIntrinsic
            | AbstractValue::GlobalObject
            | AbstractValue::SymbolicInput(_) => {}
        }
    }

    pub(crate) fn callback_values<'v>(
        &self,
        values: impl IntoIterator<Item = &'v AbstractValue>,
    ) -> Values {
        let pending: Vec<_> = values.into_iter().cloned().collect();
        self.walk_callback_values(pending, None).0
    }

    pub(super) fn callback_values_with_paths(
        &self,
        values: &Values,
        seeds: &BTreeMap<AbstractValue, BoundedInvocationFallbackSeed>,
        targets: &BTreeSet<u32>,
    ) -> (Values, BTreeMap<u32, BoundedInvocationFallbackInputPath>) {
        let pending = values
            .iter()
            .cloned()
            .map(|value| TracedCallbackValue {
                path: seeds.get(&value).cloned().map_or(
                    BoundedInvocationFallbackInputPath::Unknown {
                        reason: BoundedInvocationFallbackUnknownPathReason::SeedUnavailable,
                    },
                    |seed| BoundedInvocationFallbackInputPath::Retained {
                        seed,
                        edges: Vec::new(),
                    },
                ),
                value,
            })
            .collect();
        self.walk_callback_values(pending, Some(targets))
    }

    pub(super) fn walk_callback_values<T: CallbackTraversalItem>(
        &self,
        mut pending: Vec<T>,
        trace_targets: Option<&BTreeSet<u32>>,
    ) -> (Values, BTreeMap<u32, BoundedInvocationFallbackInputPath>) {
        // Also the walk's visited set: probed once per reached value. A
        // transient hash set keeps those probes off the persistent `Values`
        // tree, which is built once, in order, from the result.
        let mut callbacks = FxHashSet::default();
        let mut paths = BTreeMap::new();
        let mut visited_objects = BTreeSet::new();
        let mut visited_descriptor_maps = BTreeSet::new();
        let mut expanded_escaped = false;
        while let Some(work) = pending.pop() {
            let value = work.value();
            let mut expand_stored_references = true;
            let mut expand_object_properties = true;
            match value {
                AbstractValue::Value(Value::Alloc(Kind::Function, _)) => {
                    callbacks.insert(value.clone());
                }
                AbstractValue::Closure(_, _) => {
                    // Capture snapshots form a DAG: multiple bindings can
                    // contain the same closure. Expand each closure once,
                    // rather than revisit it along every path through captures.
                    expand_stored_references = callbacks.insert(value.clone());
                }
                AbstractValue::Value(Value::Alloc(Kind::Class, key))
                | AbstractValue::ClassMembers(key) => {
                    if callbacks.insert(AbstractValue::ClassMembers(*key))
                        && trace_targets.is_some_and(|targets| targets.contains(key))
                        && let Some(path) = work.class_members_path()
                    {
                        paths.entry(*key).or_insert(path);
                    }
                }
                AbstractValue::Instance(key, captured, _) => {
                    if callbacks.insert(AbstractValue::ClassMembers(*key))
                        && trace_targets.is_some_and(|targets| targets.contains(key))
                        && let Some(path) = work.class_members_path()
                    {
                        paths.entry(*key).or_insert(path);
                    }
                    // Capability reach never needs the constructed-kind receipt.
                    // Coalesce a constructed instance and a projected member
                    // with the same class and retained arguments in the pool.
                    expand_stored_references = callbacks.insert(AbstractValue::Instance(
                        *key,
                        captured.clone(),
                        InstanceOrigin::Projected,
                    ));
                }
                AbstractValue::ForeignDeferredRecord => {
                    callbacks.insert(value.clone());
                }
                AbstractValue::DeferredCallbacks(_)
                | AbstractValue::AuditedRetainedResult(_, _) => {
                    expand_stored_references = callbacks.insert(value.clone());
                }
                AbstractValue::OwnDescriptorMap(site) => {
                    expand_stored_references = visited_descriptor_maps.insert(*site);
                }
                AbstractValue::Value(Value::Alloc(Kind::Object, object)) => {
                    expand_object_properties = visited_objects.insert(*object);
                }
                AbstractValue::GlobalObject | AbstractValue::Value(Value::Top)
                    if !expanded_escaped =>
                {
                    // Unknown external data cannot expose an unescaped local
                    // callback. Preserve tracked escapes; exports are added
                    // separately at opaque call sites, and known object
                    // identities are expanded through their property maps.
                    expanded_escaped = true;
                    for escaped in self
                        .state
                        .world
                        .publication
                        .global_escaped_callbacks
                        .iter()
                        .filter(|value| !matches!(value, AbstractValue::Value(Value::Top)))
                    {
                        pending.push(work.retained_child(
                            escaped,
                            StoredReferenceEdge::EscapedCallbackPoolExpansion,
                        ));
                    }
                }
                _ => {}
            }
            if expand_stored_references {
                self.visit_stored_references_with_edges(
                    value,
                    expand_object_properties,
                    |reference, edge| pending.push(work.retained_child(reference, edge)),
                );
            }
        }
        (callbacks.into_iter().collect(), paths)
    }

    /// A direct call or construction of an imported callable carrying a
    /// derived foreign call summary (SPEC.md § Derived foreign call summaries).
    pub(crate) fn foreign_call_summary(
        &self,
        kind: CallKind,
        callee: &ValueExpr,
    ) -> Option<analysis_vocabulary::inputs::ForeignCallSummary> {
        use analysis_vocabulary::inputs::ForeignInvocation;
        let ValueExpr::Binding(binding) = callee else {
            return None;
        };
        let summary = self.inputs.index.foreign_call_imports.get(binding)?;
        matches!(
            (kind, summary.invocation),
            (CallKind::Call, ForeignInvocation::Call)
                | (CallKind::New, ForeignInvocation::Construct)
        )
        .then(|| summary.clone())
    }

    /// Retained arguments stay reachable through the fresh result: code that
    /// later holds it may invoke their callbacks or write through them.
    pub(super) fn retain_foreign_arguments(
        &mut self,
        summary: &analysis_vocabulary::inputs::ForeignCallSummary,
        args: &[Values],
    ) -> Values {
        let retained: Values = if summary.retained_provider_captures.is_empty() {
            summary
                .retained_arguments
                .iter()
                .filter_map(|position| args.get(*position))
                .flatten()
                .cloned()
                .collect()
        } else {
            // Unknown captured values may alias an actual argument. The
            // summary proves no separation between those inputs and captures.
            args.iter().flatten().cloned().collect()
        };
        self.expose_class_capabilities(&retained);
        self.taint_objects(&retained);
        retained
    }

    pub(crate) fn foreign_call_result(
        &mut self,
        summary: &analysis_vocabulary::inputs::ForeignCallSummary,
        args: &[Values],
    ) -> Values {
        use analysis_vocabulary::inputs::{ForeignInvocation, ForeignReturn};
        let mut result = Values::new();
        for returned in &summary.returns {
            match returned {
                ForeignReturn::Argument { index } => result.extend(
                    args.get(*index)
                        .cloned()
                        .unwrap_or_else(|| Values::from([AbstractValue::UNDEFINED])),
                ),
                ForeignReturn::FreshAllocation => {
                    let mut retained = self.retain_foreign_arguments(summary, args);
                    if !summary.retained_provider_captures.is_empty() {
                        // Captures need not be primitive or unescaped. A later
                        // use retains arbitrary foreign/importer-cell effects.
                        retained.insert(AbstractValue::ForeignDeferredRecord);
                    }
                    // Keep live input identities, including containers that
                    // acquire callbacks after this call. The carrier remains
                    // opaque; its interned captures prove no result identity.
                    result.insert(self.deferred_callbacks(retained));
                }
                ForeignReturn::Primitive => {
                    result.insert(AbstractValue::PRIMITIVE);
                }
            }
        }
        // A class binding stays in its TDZ until the provider evaluates, which
        // an ESM cycle can delay past this call. A function declaration is
        // initialized at instantiation.
        if summary.invocation == ForeignInvocation::Construct {
            result.insert(AbstractValue::Value(Value::Unavailable));
        }
        result
    }

    pub(crate) fn record_foreign_call_use(
        &mut self,
        summary: &analysis_vocabulary::inputs::ForeignCallSummary,
    ) {
        use analysis_vocabulary::provenance::{InferenceRule, InferenceUse};
        let mut derivation_rules = BTreeSet::from([InferenceRule::ForeignCallSummary]);
        if !summary.native_constructions.is_empty() {
            derivation_rules.insert(InferenceRule::NativeIntegrity);
        }
        self.record_inference_use(
            NativeIntegrityGuard::ForeignCallSummary,
            InferenceUse {
                conclusion: InferenceRule::ForeignCallSummary,
                derivation_rules,
                value_input_refs: summary.dependencies.value_inputs.clone(),
                descriptor_copy_author_refs: BTreeSet::new(),
                argument_input_refs: summary.dependencies.argument_inputs.clone(),
                native_operation_input_refs: summary.dependencies.native_operation_inputs.clone(),
                imported_primitive_array_spread_input_refs: summary
                    .dependencies
                    .imported_primitive_array_spread_inputs
                    .clone(),
            },
            &summary.dependencies,
        );
    }

    pub(crate) fn retained_factory_callbacks(
        &self,
        site: CallSite,
        expressions: &[ValueExpr],
        args: &[Values],
    ) -> Option<Values> {
        self.retained_callbacks(
            &self.inputs.index.retained_callback_calls.get(&site)?.call,
            expressions,
            args,
            RetainedResultKind::Factory,
        )
    }

    pub(super) fn retained_constructor_callbacks(
        &self,
        site: CallSite,
        expressions: &[ValueExpr],
        args: &[Values],
    ) -> Option<Values> {
        self.retained_callbacks(
            self.inputs.index.retained_constructor_calls.get(&site)?,
            expressions,
            args,
            RetainedResultKind::Constructor,
        )
    }

    pub(super) fn no_reentry_callbacks(
        &self,
        site: CallSite,
        expressions: &[ValueExpr],
        args: &[Values],
    ) -> Option<Values> {
        let contract = self.inputs.index.no_reentry_callback_calls.get(&site)?;
        // This contract admits ordinary local callables only. An opaque result
        // or a returned callback graph does not establish that input shape.
        let signatures: Vec<_> = contract
            .signatures
            .iter()
            .filter(|signature| {
                signature.callback_arguments.iter().all(|position| {
                    let Some(values) = args.get(*position) else {
                        return false;
                    };
                    let is_ordinary_callable = |value: &AbstractValue| {
                        matches!(
                            value,
                            AbstractValue::Value(Value::Alloc(Kind::Function, _))
                                | AbstractValue::Closure(_, _)
                        )
                    };
                    if contract.require_single_callback_identity {
                        values.len() == 1 && values.iter().all(is_ordinary_callable)
                    } else {
                        values.iter().all(is_ordinary_callable)
                    }
                })
            })
            .cloned()
            .collect();
        let contract = RetainedCallbackCall {
            namespace_member: None,
            signatures,
            argument_count: expressions.len(),
            callback_argument_exprs: BTreeMap::new(),
            require_single_callback_identity: contract.require_single_callback_identity,
        };
        let callbacks =
            self.retained_callbacks(&contract, expressions, args, RetainedResultKind::Factory)?;
        // Independently exported aliases and already exposed callbacks keep
        // ordinary conservative timing; the provider audit cannot hide them.
        if !callbacks.is_disjoint(&self.export_callback_values()) {
            return None;
        }
        Some(callbacks)
    }

    /// Check a source-selected member callee's runtime callback identities
    /// before suppressing callback promotion from its synthetic accessor-read
    /// event. Only admitted identifiers/function literals and inert primitive
    /// expressions reach this path, so the abstract reads do not execute or
    /// skip JavaScript argument evaluation; the real call transfer evaluates
    /// all receiver and argument expressions in source order afterward.
    pub(crate) fn no_reentry_member_lookup_admitted(
        &mut self,
        site: CallSite,
        env: &Context,
        captures: &Context,
    ) -> bool {
        let Some(contract) = self.inputs.index.no_reentry_callback_calls.get(&site) else {
            return false;
        };
        let mut expressions = vec![ValueExpr::Unknown; contract.argument_count];
        let mut args = vec![Values::new(); contract.argument_count];
        for (position, expression) in &contract.callback_argument_exprs {
            let Some(expression_slot) = expressions.get_mut(*position) else {
                return false;
            };
            let Some(argument_slot) = args.get_mut(*position) else {
                return false;
            };
            *expression_slot = expression.clone();
            *argument_slot = self.evaluate(expression, env, captures);
        }
        self.no_reentry_callbacks(site, &expressions, &args)
            .is_some()
    }

    pub(super) fn retained_callbacks(
        &self,
        contract: &RetainedCallbackCall,
        expressions: &[ValueExpr],
        args: &[Values],
        admission: RetainedResultKind,
    ) -> Option<Values> {
        if let Some((namespace, member)) = &contract.namespace_member
            && !self
                .inputs
                .index
                .audited_factory_members
                .get(namespace)
                .is_some_and(|members| members.contains(member))
        {
            return None;
        }
        for signature in &contract.signatures {
            let mut retained = Values::new();
            let admitted = signature.callback_arguments.iter().all(|position| {
                let expression = &expressions[*position];
                if let ValueExpr::Binding(binding) = expression {
                    let rebound = self.inputs.facts.iter().any(|fact| {
                        fact.rebinds.eager.contains(binding) || fact.rebinds.lazy.contains(binding)
                    });
                    let multiple_origins =
                        self.inputs.flow.values_of_binding(binding).iter().count() > 1;
                    // Whole-chunk flow may retain conservative alternatives
                    // for an imported factory result. At this call, the
                    // synchronous value is still an audited factory carrier;
                    // its cleanliness and non-mutation are checked below.
                    // Do not let that carrier erase binding rebind evidence
                    // or widen ordinary/mixed callback values.
                    let all_factory_carriers = !args[*position].is_empty()
                        && args[*position].iter().all(|value| {
                            matches!(
                                value,
                                AbstractValue::AuditedRetainedResult(
                                    RetainedResultKind::Factory,
                                    _
                                )
                            )
                        });
                    if rebound || (multiple_origins && !all_factory_carriers) {
                        return false;
                    }
                }
                let values = &args[*position];
                if values.is_empty()
                    || !values.iter().all(|value| match value {
                        AbstractValue::Value(Value::Alloc(Kind::Function, key))
                        | AbstractValue::Closure(HeapAddress { callable: key, .. }, _) => {
                            self.inputs.index.callables.contains_key(key)
                                && !self.state.world.native.tainted_functions.contains(key)
                        }
                        AbstractValue::AuditedRetainedResult(kind, captured) => {
                            (*kind == RetainedResultKind::Factory
                                || admission == RetainedResultKind::Constructor)
                                && self.audited_result_is_clean(captured)
                        }
                        _ => false,
                    })
                {
                    return false;
                }
                retained.extend(self.callback_values(values));
                true
            });
            if !admitted {
                continue;
            }
            let functions: BTreeSet<_> = retained
                .iter()
                .filter_map(|value| match value {
                    AbstractValue::Value(Value::Alloc(Kind::Function, key))
                    | AbstractValue::Closure(HeapAddress { callable: key, .. }, _) => Some(*key),
                    _ => None,
                })
                .collect();
            let mutating_receiver = |expression: &ValueExpr| {
                let mut root = expression;
                while let ValueExpr::Member(receiver, _, _, _) = root {
                    root = receiver;
                }
                matches!(root, ValueExpr::Binding(binding) if self.inputs.flow.values_of_binding(binding).iter()
                    .any(|origin| matches!(origin, Value::Alloc(Kind::Function, key) if functions.contains(&key))))
            };
            // Existing callable own/prototype surfaces are outside the fresh
            // input proof. Reject recognized writes through all local aliases,
            // even if they occur after this call or in a lazy callable.
            let mutated = self
                .inputs
                .index
                .intrinsic_writes
                .iter()
                .map(|write| &write.receiver)
                .any(mutating_receiver)
                || self
                    .inputs
                    .index
                    .top_callback_result_writes
                    .iter()
                    .flatten()
                    .chain(
                        self.inputs
                            .index
                            .callables
                            .values()
                            .flat_map(|callable| &callable.callback_result_writes),
                    )
                    .any(|write| mutating_receiver(&write.receiver));
            if mutated
                || retained.iter().any(|value| {
                    self.state
                        .world
                        .publication
                        .global_escaped_callbacks
                        .contains(value)
                })
            {
                continue;
            }
            return Some(retained);
        }
        None
    }

    pub(super) fn fresh_primitive_write_receivers(
        &mut self,
        callable: &Callable,
        env: &Context,
    ) -> BTreeSet<u32> {
        let writes = &callable.callback_result_writes;
        if self.state.array_iteration_depth == 0
            || self.state.array_iteration_unsupported
            || !self.array_intrinsics_intact()
            || callable.opaque
            || writes.is_empty()
            || writes.len() != callable.member_value_writes.len()
            || writes.len()
                != callable.member_writes.len() + callable.dynamic_property_receivers.len()
            || callable
                .calls
                .iter()
                .any(|call| call.kind != CallKind::LocalMemberRead)
        {
            return BTreeSet::new();
        }
        let mut objects = BTreeSet::new();
        for write in writes {
            if !write.simple_assignment
                || !write.data_read_operands
                || !matches!(write.receiver, ValueExpr::Binding(_))
            {
                return BTreeSet::new();
            }
            for expression in [&write.property_key, &write.value] {
                let values = self.evaluate(expression, env, env);
                if values.is_empty() || !values.iter().all(AbstractValue::is_primitive) {
                    return BTreeSet::new();
                }
            }
            let receivers = self.evaluate(&write.receiver, env, env);
            let [AbstractValue::Value(Value::Alloc(Kind::Object, object))] =
                receivers.iter().collect::<Vec<_>>().as_slice()
            else {
                return BTreeSet::new();
            };
            if !self.state.world.object_properties_are_settled(*object)
                || self
                    .state
                    .array_iteration_protected_objects
                    .contains(object)
                || (!self.inputs.index.plain_data_objects.contains(object)
                    && !self
                        .state
                        .world
                        .surface
                        .ordinary_spread_copies
                        .contains(object))
                || self.state.world.arrays.dense.contains_key(object)
            {
                return BTreeSet::new();
            }
            objects.insert(*object);
        }
        objects
    }

    /// Whether `callable` is a parameter-mutation initializer of a fresh
    /// object: it calls nothing, returns its one parameter, and every
    /// property write stores a primitive under a primitive key into that
    /// parameter, which holds only untainted plain-data object literals. Such
    /// a write creates or overwrites a data property of an object no accessor
    /// or callback can reach (A11 covers `%Object.prototype%`), so it
    /// publishes nothing. A primitive receiver throws before writing.
    pub(super) fn fresh_data_writes(
        &mut self,
        callable: &Callable,
        env: &Context,
    ) -> Option<BTreeSet<u32>> {
        let writes = &callable.callback_result_writes;
        let [
            Parameter {
                binding: Some(parameter),
                default: None,
                ..
            },
        ] = callable.params.as_slice()
        else {
            return None;
        };
        if callable.opaque
            || !callable.calls.is_empty()
            || !matches!(callable.body_facts.value_flow().returns(), [ValueExpr::Binding(returned)] if returned == parameter)
            || writes.is_empty()
            || writes.len() != callable.member_value_writes.len()
            || writes.len()
                != callable.member_writes.len() + callable.dynamic_property_receivers.len()
            || writes.iter().any(
                |write| !matches!(&write.receiver, ValueExpr::Binding(receiver) if receiver == parameter),
            )
        {
            return None;
        }
        let primitive = |executor: &mut Self, expression: &ValueExpr| {
            let values = executor.evaluate(expression, env, env);
            !values.is_empty() && values.iter().all(AbstractValue::is_primitive)
        };
        let mut objects = BTreeSet::new();
        let mut fresh_receiver = |executor: &mut Self, expression: &ValueExpr| {
            let receivers = executor.evaluate(expression, env, env);
            let mut any_object = false;
            receivers.iter().all(|receiver| match receiver {
                AbstractValue::Value(Value::Prim(_)) => true,
                AbstractValue::Value(Value::Alloc(Kind::Object, object)) => {
                    any_object = true;
                    objects.insert(*object);
                    executor.inputs.index.plain_data_objects.contains(object)
                        && executor.state.world.object_properties_are_settled(*object)
                        && !executor.state.world.arrays.dense.contains_key(object)
                }
                _ => false,
            }) && any_object
        };
        for write in writes {
            if !write.simple_assignment
                || !primitive(self, &write.property_key)
                || !primitive(self, &write.value)
                || !fresh_receiver(self, &write.receiver)
            {
                return None;
            }
        }
        for receiver in &callable.dynamic_property_receivers {
            if !fresh_receiver(self, receiver) {
                return None;
            }
        }
        Some(objects)
    }

    pub(crate) fn apply_member_value_writes(
        &mut self,
        writes: &[MemberValueWrite],
        env: &Context,
        captures: &Context,
        certified_data_receivers: &BTreeSet<u32>,
        deferred_global_write: bool,
    ) {
        if writes.is_empty() {
            return;
        }
        let RuntimeValue::Heap(HeapResult::Updated) =
            self.request(RuntimeOperation::Heap(HeapOperation::WriteMembers {
                writes: writes.to_vec(),
                environment: env.clone(),
                captures: captures.clone(),
                certified_data_receivers: certified_data_receivers.clone(),
                deferred_global_write,
            }))
        else {
            unreachable!("member writes produce a successor heap");
        };
    }

    pub(crate) fn apply_member_value_writes_transfer(
        &mut self,
        writes: &[MemberValueWrite],
        env: &Context,
        captures: &Context,
        certified_data_receivers: &BTreeSet<u32>,
        deferred_global_write: bool,
    ) {
        for write in writes {
            let MemberValueWrite {
                receiver,
                property,
                value: expression,
                ..
            } = write;
            let receivers = self.evaluate(&ValueExpr::Binding(receiver.clone()), env, captures);
            let values = self.evaluate(expression, env, captures);
            self.publish_accessor_receivers(&receivers, write.site.source_span());
            for receiver in receivers {
                match receiver {
                    AbstractValue::GlobalObject | AbstractValue::ArrayIntrinsic => {
                        if receiver == AbstractValue::GlobalObject
                            && let Some(property) = property
                        {
                            self.evaluation_effects.record(
                                write.owner,
                                write.site.source_span(),
                                GlobalAccessKind::Write,
                                property,
                            );
                        }
                        if deferred_global_write && receiver == AbstractValue::GlobalObject {
                            continue;
                        }
                        self.state.world_mut().arrays.intrinsic_mutated |=
                            receiver == AbstractValue::ArrayIntrinsic;
                        self.taint_objects(&values);
                        let callbacks = self.callback_values(&values);
                        self.insert_escaped_callback_values(
                            &callbacks,
                            ClassMembersPublicationWriter::GlobalOrIntrinsicMemberWrite,
                            Some(write.site.source_span()),
                            None,
                        );
                    }
                    AbstractValue::Value(Value::Alloc(kind, object))
                        if kind.tracks_prefix_own_properties() =>
                    {
                        let may_be_published =
                            self.state.world.surface.tainted_objects.contains(&object)
                                || self.state.world.surface.unsettled_objects.contains(&object);
                        if !certified_data_receivers.contains(&object) {
                            self.state
                                .world_mut()
                                .surface
                                .tainted_objects
                                .insert(object);
                        }
                        // RegExp slots share this heap only for callback
                        // containment. Ordinary-record shape and member-read
                        // proofs remain restricted to `Kind::Object`.
                        self.literal_heap()
                            .write_property(object, property.clone(), &values);
                        if may_be_published {
                            self.publish_values(
                                &values,
                                ClassMembersPublicationWriter::PublishedObjectMemberWrite,
                                Some(write.site.source_span()),
                            );
                        }
                    }
                    // A class, or its prototype: every instance inherits
                    // the latter, including a refused construction's
                    // unknown instance, and anything holding the class
                    // reaches the former.
                    AbstractValue::Value(Value::Alloc(Kind::Class, _))
                    | AbstractValue::ClassMembers(_) => {
                        self.publish_values(
                            &values,
                            ClassMembersPublicationWriter::ClassSurfaceMemberWrite,
                            Some(write.site.source_span()),
                        );
                    }
                    AbstractValue::Instance(key, captured, _) => {
                        self.state.world_mut().classes.tainted.insert(key);
                        self.taint_objects(captured.values.iter());
                        self.taint_objects(&values);
                        let callbacks = self.callback_values(&values);
                        self.insert_escaped_callback_values(
                            &callbacks,
                            ClassMembersPublicationWriter::InstanceMemberWrite,
                            Some(write.site.source_span()),
                            None,
                        );
                    }
                    AbstractValue::OwnDescriptorMap(site) => {
                        self.invalidate_own_descriptor_map(site);
                        self.taint_objects(&values);
                        let callbacks = self.callback_values(&values);
                        self.insert_escaped_callback_values(
                            &callbacks,
                            ClassMembersPublicationWriter::OwnDescriptorMapMemberWrite,
                            Some(write.site.source_span()),
                            None,
                        );
                    }
                    AbstractValue::Value(Value::Top)
                    | AbstractValue::DeferredCallbacks(_)
                    | AbstractValue::AuditedRetainedResult(_, _)
                    | AbstractValue::ForeignDeferredRecord => {
                        self.taint_objects(&values);
                        // A write through an untracked object may publish the
                        // callback through that object before it is passed to
                        // opaque code. Retain it as an escaped callback root.
                        let callbacks = self.callback_values(&values);
                        self.insert_escaped_callback_values(
                            &callbacks,
                            ClassMembersPublicationWriter::UntrackedReceiverMemberWrite,
                            Some(write.site.source_span()),
                            None,
                        );
                    }
                    _ => {}
                }
            }
        }
    }

    pub(crate) fn promote_callback_result_writes(
        &mut self,
        writes: &[CallbackResultWrite],
        env: &Context,
        captures: &Context,
        owner: SourceUnitOrdinal,
        outcome: &mut Outcome,
        certified_data_receivers: &BTreeSet<u32>,
    ) {
        for write in writes {
            let receivers = self.evaluate(&write.receiver, env, captures);
            let uncertified: Values = receivers.iter().filter(|value| !matches!(value,
                AbstractValue::Value(Value::Alloc(Kind::Object, object)) if certified_data_receivers.contains(object))).cloned().collect();
            self.taint_objects(&uncertified);
            let mut callbacks = self.callback_values(&receivers);
            let mut root = &write.receiver;
            while let ValueExpr::Member(receiver, _, _, _) = root {
                root = receiver;
            }
            let root_values = self.evaluate(root, env, captures);
            // A bare binding receiver is `apply_member_value_writes`' lane.
            if !matches!(write.receiver, ValueExpr::Binding(_)) {
                self.store_through_member_chain(
                    write,
                    root,
                    &receivers,
                    &root_values,
                    env,
                    captures,
                );
            }
            let callable_surface = root_values.iter().any(|value| {
                matches!(
                    value,
                    AbstractValue::Value(Value::Alloc(Kind::Function, _))
                        | AbstractValue::Closure(_, _)
                )
            });
            if !callable_surface
                && !callbacks.iter().any(|value| {
                    matches!(
                        value,
                        AbstractValue::DeferredCallbacks(_)
                            | AbstractValue::AuditedRetainedResult(_, _)
                    )
                })
            {
                continue;
            }
            callbacks.extend(self.callback_values(&root_values));
            if write
                .property
                .as_deref()
                .is_some_and(|property| property != "__proto__")
                && matches!(
                    write.value,
                    ValueExpr::NonCallable(_) | ValueExpr::Undefined
                )
            {
                continue;
            }
            // Callable own/prototype fields are not modeled as an ordinary
            // object graph. Installing an opaque value there, or in a retained
            // factory result, can add callbacks/accessors. Conservatively
            // include their effects now, before a later opaque consumer or
            // member projection can execute an otherwise untracked callback.
            outcome.unsupported(
                &self.inputs.metrics,
                ExecutionOperation::PropertyWrite,
                owner,
                DUMMY_SP,
            );
            outcome.unknown_call = true;
            self.note_unknown_execution(owner);
            let values = self.evaluate(&write.value, env, captures);
            self.promote_deferred_callbacks(&values, outcome);
            self.invoke_possible_callbacks_at(
                &[callbacks, values],
                false,
                Some(write.site.source_span()),
                outcome,
            );
        }
    }

    /// A value stored through a member chain (`a.b.c = f`) lands where a
    /// later read of the same chain finds it: in a tracked receiver's
    /// properties, or, under a global root or intrinsic, in the escaped
    /// pool every opaque call may invoke.
    pub(super) fn store_through_member_chain(
        &mut self,
        write: &CallbackResultWrite,
        root: &ValueExpr,
        receivers: &Values,
        root_values: &Values,
        env: &Context,
        captures: &Context,
    ) {
        let values = self.evaluate(&write.value, env, captures);
        let global_root = matches!(root, ValueExpr::Binding(id)
            if !self.inputs.flow.is_declared(id)
                && !env.contains_key(id)
                && !captures.contains_key(id))
            || root_values.iter().chain(receivers.iter()).any(|value| {
                matches!(
                    value,
                    AbstractValue::GlobalObject | AbstractValue::ArrayIntrinsic
                )
            });
        let mut changed = false;
        if global_root
            || receivers
                .iter()
                .chain(root_values)
                .any(|receiver| match receiver {
                    AbstractValue::Value(Value::Alloc(kind, object))
                        if kind.tracks_prefix_own_properties() =>
                    {
                        self.state.world.surface.tainted_objects.contains(object)
                            || self.state.world.surface.unsettled_objects.contains(object)
                    }
                    AbstractValue::Value(Value::Top)
                    | AbstractValue::ForeignDeferredRecord
                    | AbstractValue::DeferredCallbacks(_)
                    | AbstractValue::AuditedRetainedResult(_, _)
                    | AbstractValue::Instance(_, _, _) => true,
                    // A class's prototype is inherited by every instance,
                    // including the unknown instance of a refused
                    // construction.
                    AbstractValue::ClassMembers(_)
                    | AbstractValue::Value(Value::Alloc(Kind::Class, _)) => true,
                    _ => false,
                })
        {
            self.publish_values(
                &values,
                ClassMembersPublicationWriter::MemberChainStore,
                Some(write.site.source_span()),
            );
        }
        for receiver in receivers {
            if let AbstractValue::Value(Value::Alloc(kind, object)) = receiver
                && kind.tracks_prefix_own_properties()
            {
                let stored = self
                    .state
                    .world_mut()
                    .surface
                    .prefix_object_properties
                    .entry(*object)
                    .or_default()
                    .entry(write.property.clone())
                    .or_default();
                let before = stored.len();
                stored.extend(values.iter().cloned());
                changed |= stored.len() != before;
            }
        }
        if changed {
            self.state.world_mut().revision.bump_prefix();
        }
    }
}
