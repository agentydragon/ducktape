//! Captured-context interning, value publication, unsettling, tainting and havoc reach.

use super::*;

impl<'source, 'rule> Executor<'source, 'rule> {
    pub(crate) fn intern_captures(&mut self, captures: LexicalCaptures) -> CapturedContext {
        self.state.capture_contexts.intern(captures)
    }

    pub(super) fn materialize_captures(&self, captures: Option<&LexicalCaptures>) -> Context {
        let Some(captures) = captures else {
            return Context::new();
        };
        let mut values = captures.snapshots.clone();
        for (binding, cell) in &captures.cells {
            values.insert(
                binding.clone(),
                self.state.world.cells[cell.0].values.clone(),
            );
        }
        values
    }

    pub(super) fn havoc_cells(&mut self, cells: impl IntoIterator<Item = LexicalCellId>) {
        let mut changed = false;
        for id in cells {
            let cell = &mut self.state.world_mut().cells[id.0];
            if matches!(cell.kind, LocalBinding::Const | LocalBinding::NamedSelf(_))
                && cell.initialization == CellInitialization::Initialized
            {
                continue;
            }
            if cell.initialization == CellInitialization::Uninitialized {
                // Skipped enclosing code can have initialized a lexical cell.
                // Keeping only TDZ would wrongly prune its later readers.
                cell.initialization = CellInitialization::MaybeInitialized;
                changed = true;
            }
            changed |= cell.values.insert(AbstractValue::Value(Value::Top));
        }
        if changed {
            self.state.world_mut().revision.bump_prefix();
        }
    }

    pub(super) fn intern_callback_set(&mut self, values: Values) -> Captured<Values> {
        self.state.callback_sets.intern(values)
    }

    pub(crate) fn deferred_callbacks(&mut self, values: Values) -> AbstractValue {
        AbstractValue::DeferredCallbacks(self.intern_callback_set(values))
    }

    /// A later store through a possibly published receiver exposes the new
    /// graph too. Taint its objects even if no callback exists yet: a later
    /// insertion through an alias must publish that callback.
    pub(super) fn publish_values(
        &mut self,
        values: &Values,
        writer: ClassMembersPublicationWriter,
        span: Option<Span>,
    ) {
        self.taint_objects(values);
        let callbacks = self.callback_values(values);
        if let Some(owner) = self.state.current_top_owner {
            for callback in &callbacks {
                let AbstractValue::Value(Value::Alloc(Kind::Function, key)) = callback else {
                    continue;
                };
                if !self
                    .state
                    .world
                    .publication
                    .global_escaped_callbacks
                    .contains(callback)
                {
                    self.state
                        .world_mut()
                        .publication
                        .first_callback_publication_owners
                        .entry(*key)
                        .or_insert(owner);
                }
            }
        }
        self.insert_escaped_callback_values(&callbacks, writer, span, None);
    }

    /// The only mutation point for the prefix-wide escaped callback set.
    /// Per-writer admission and taint decisions stay at their callers.
    pub(super) fn insert_escaped_callback_values(
        &mut self,
        callbacks: &Values,
        writer: ClassMembersPublicationWriter,
        span: Option<Span>,
        fallback_diagnostic: Option<&BoundedFallbackPublicationDiagnostic>,
    ) {
        let source_span = span.filter(|span| *span != DUMMY_SP);
        for callback in callbacks {
            let AbstractValue::ClassMembers(key) = callback else {
                continue;
            };
            if self
                .state
                .world
                .publication
                .global_escaped_callbacks
                .contains(callback)
            {
                continue;
            }
            let top_level_owner = self.state.current_top_owner;
            let span_start = source_span.map(|span| span.lo.0);
            let span_end = source_span.map(|span| span.hi.0);
            let bounded_fallback_input_path = (writer
                == ClassMembersPublicationWriter::BoundedInvocationFallback)
            .then(|| {
                fallback_diagnostic
                    .and_then(|diagnostic| diagnostic.input_paths.get(key))
                    .cloned()
                    .unwrap_or(BoundedInvocationFallbackInputPath::Unknown {
                        reason:
                            BoundedInvocationFallbackUnknownPathReason::RetainedReferencePathUnavailable,
                    })
            });
            let bounded_fallback_callsite = (writer
                == ClassMembersPublicationWriter::BoundedInvocationFallback)
                .then(|| fallback_diagnostic.and_then(|diagnostic| diagnostic.callsite.clone()))
                .flatten();
            self.state
                .world_mut()
                .classes
                .first_members_publications
                .entry(*key)
                .or_insert(FirstClassMembersPublication {
                    writer,
                    top_level_owner,
                    span_start,
                    span_end,
                    source_location: None,
                    bounded_fallback_input_path,
                    bounded_fallback_callsite,
                });
        }
        let escaped = &self.state.world.publication.global_escaped_callbacks;
        let escaping_classes: Vec<u32> = callbacks
            .iter()
            .filter_map(|callback| match callback {
                AbstractValue::ClassMembers(key) if !escaped.contains(callback) => Some(*key),
                _ => None,
            })
            .collect();
        if callbacks.iter().any(|callback| !escaped.contains(callback)) {
            let world = self.state.world_mut();
            world
                .publication
                .global_escaped_callbacks
                .extend_absent(callbacks);
            world.revision.bump_prefix();
        }
        // An escaped class, or an escaped instance of it, reaches what its
        // construction stored on the instance or the class. Each class
        // escapes once, so this terminates.
        for key in escaping_classes {
            let reached = self.class_reach(key);
            if !reached.is_empty() {
                self.publish_values(&reached, ClassMembersPublicationWriter::ClassReach, span);
            }
        }
    }

    pub(crate) fn audited_retained_result(
        &mut self,
        kind: RetainedResultKind,
        values: Values,
    ) -> AbstractValue {
        AbstractValue::AuditedRetainedResult(kind, self.intern_callback_set(values))
    }

    pub(crate) fn audited_result_is_clean(&self, captured: &Captured<Values>) -> bool {
        if self
            .state
            .world
            .native
            .revoked_audited_results
            .contains_key(&captured.cache_identity())
            || self
                .state
                .world
                .publication
                .global_escaped_callbacks
                .contains(&AbstractValue::AuditedRetainedResult(
                    RetainedResultKind::Factory,
                    captured.clone(),
                ))
            || self
                .state
                .world
                .publication
                .global_escaped_callbacks
                .contains(&AbstractValue::AuditedRetainedResult(
                    RetainedResultKind::Constructor,
                    captured.clone(),
                ))
            || self
                .state
                .world
                .publication
                .global_escaped_callbacks
                .contains(&AbstractValue::DeferredCallbacks(captured.clone()))
        {
            return false;
        }
        self.callback_values(captured.values.iter())
            .iter()
            .all(|value| {
                !self
                    .state
                    .world
                    .publication
                    .global_escaped_callbacks
                    .contains(value)
                    && match value {
                        AbstractValue::Value(Value::Alloc(Kind::Function, key))
                        | AbstractValue::Closure(HeapAddress { callable: key, .. }, _) => {
                            !self.state.world.native.tainted_functions.contains(key)
                        }
                        AbstractValue::AuditedRetainedResult(_, nested) => !self
                            .state
                            .world
                            .native
                            .revoked_audited_results
                            .contains_key(&nested.cache_identity()),
                        _ => true,
                    }
            })
    }

    pub(super) fn revoke_audited_result(&mut self, captured: &Captured<Values>) {
        // Equal capture sets can denote distinct runtime allocations. Sharing
        // revocation only loses precision; it never proves freshness or privacy.
        if self
            .state
            .world_mut()
            .native
            .revoked_audited_results
            .insert(captured.cache_identity(), captured.clone())
            .is_none()
        {
            self.state.world_mut().revision.bump_prefix();
        }
    }

    /// Any mutation or escape that can alter own descriptors invalidates the
    /// independent literal writability witness. The transition is monotonic
    /// and versions the execution prefix even when ordinary values do not
    /// change, so invocation memos cannot retain a stale descriptor premise.
    pub(super) fn invalidate_writable_object(&mut self, object: u32) {
        self.literal_heap().invalidate_writable(object);
    }

    /// Code the interpreter did not run may have reached `values`: every
    /// object they reach, through stored references or through what a
    /// reachable body reads, may since hold anything.
    pub(crate) fn unsettle<'v>(&mut self, values: impl IntoIterator<Item = &'v AbstractValue>) {
        let mut pending: Vec<_> = values.into_iter().cloned().collect();
        let mut visited = FxHashSet::default();
        // This pass does not change prefix values. Distinct closure contexts
        // still expand their own captures below, but share each binding's
        // identical prefix expansion. A later pass starts with an empty set.
        let mut expanded_prefix_reads = vec![false; self.inputs.prefix_binding_count];
        let mut changed = false;
        while let Some(value) = pending.pop() {
            if !visited.insert(value.clone()) {
                continue;
            }
            if let AbstractValue::Closure(_, captured) = &value {
                let cells: Vec<_> = captured.values.cells.values().copied().collect();
                self.havoc_cells(cells);
            }
            match &value {
                AbstractValue::DeferredCallbacks(captured)
                | AbstractValue::AuditedRetainedResult(_, captured) => {
                    self.revoke_audited_result(captured);
                }
                AbstractValue::Value(Value::Alloc(kind, object))
                    if kind.tracks_prefix_own_properties() =>
                {
                    changed |= self
                        .state
                        .world_mut()
                        .surface
                        .unsettled_objects
                        .insert(*object);
                    self.invalidate_writable_object(*object);
                }
                AbstractValue::Value(Value::Alloc(Kind::Function, key))
                | AbstractValue::Closure(HeapAddress { callable: key, .. }, _) => {
                    if let Some(reads) = self.inputs.callable_prefix_reads.get(key) {
                        pending.extend(
                            reads
                                .iter()
                                .filter(|binding| {
                                    !std::mem::replace(
                                        &mut expanded_prefix_reads[binding.index()],
                                        true,
                                    )
                                })
                                .filter_map(|binding| {
                                    self.state.world.module.prefix_values.get_ordinal(*binding)
                                })
                                .flatten()
                                .cloned(),
                        );
                    }
                }
                _ => {}
            }
            self.visit_stored_references(&value, true, |reference| {
                pending.push(reference.clone());
            });
        }
        if changed {
            // Invocation memos must not reuse property proofs from before
            // a bounded callback made these objects unknown.
            self.state.world_mut().revision.bump_prefix();
        }
    }

    pub(crate) fn taint_objects<'v>(
        &mut self,
        values: impl IntoIterator<Item = &'v AbstractValue>,
    ) {
        let mut pending: Vec<_> = values.into_iter().cloned().collect();
        let mut visited = FxHashSet::default();
        while let Some(value) = pending.pop() {
            if visited.contains(&value) {
                continue;
            }
            let mut expand_stored_references = true;
            match &value {
                AbstractValue::DeferredCallbacks(captured)
                | AbstractValue::AuditedRetainedResult(_, captured) => {
                    self.revoke_audited_result(captured);
                }
                AbstractValue::ArrayIntrinsic => {
                    self.state.world_mut().arrays.intrinsic_mutated = true;
                }
                AbstractValue::Value(Value::Alloc(kind, object))
                    if kind.tracks_prefix_own_properties() =>
                {
                    self.invalidate_writable_object(*object);
                    if !self
                        .state
                        .world_mut()
                        .surface
                        .tainted_objects
                        .insert(*object)
                    {
                        expand_stored_references = false;
                    }
                }
                AbstractValue::Value(Value::Alloc(Kind::Set, site))
                    if !self.state.world_mut().native.tainted_sets.insert(*site) =>
                {
                    expand_stored_references = false;
                }
                AbstractValue::OwnDescriptorMap(site) => {
                    self.invalidate_own_descriptor_map(*site);
                }
                AbstractValue::Value(Value::Alloc(Kind::Function, key)) => {
                    self.state.world_mut().native.tainted_functions.insert(*key);
                }
                AbstractValue::Closure(HeapAddress { callable: key, .. }, captured) => {
                    let cells: Vec<_> = captured.values.cells.values().copied().collect();
                    self.havoc_cells(cells);
                    self.state.world_mut().native.tainted_functions.insert(*key);
                }
                _ => {}
            }
            if expand_stored_references {
                self.visit_stored_references(&value, true, |reference| {
                    pending.push(reference.clone());
                });
            }
            // Retained children are queued and processed only after this loop
            // iteration, so recording the owned value here is cycle-safe and
            // avoids cloning it into the membership set.
            visited.insert(value);
        }
    }

    /// The foreign wrapper can invoke its captured callbacks and any values
    /// they return synchronously. It cannot resume them past an await.
    pub(crate) fn reachable_objects(&self, values: &Values) -> BTreeSet<u32> {
        let mut objects = BTreeSet::new();
        let mut pending: Vec<_> = values.iter().cloned().collect();
        let mut visited = BTreeSet::new();
        while let Some(value) = pending.pop() {
            if !visited.insert(value.clone()) {
                continue;
            }
            if let AbstractValue::Value(Value::Alloc(Kind::Object, object)) = &value {
                objects.insert(*object);
            }
            self.visit_stored_references(&value, true, |reference| {
                pending.push(reference.clone());
            });
        }
        objects
    }

    pub(crate) fn expose_class_capabilities<'v>(
        &mut self,
        values: impl IntoIterator<Item = &'v AbstractValue>,
    ) {
        let mut pending: Vec<_> = values.into_iter().cloned().collect();
        let mut visited = BTreeSet::new();
        while let Some(value) = pending.pop() {
            if !visited.insert(value.clone()) {
                continue;
            }
            if let AbstractValue::Value(Value::Alloc(Kind::Class, key))
            | AbstractValue::ClassMembers(key)
            | AbstractValue::Instance(key, _, _) = &value
            {
                self.state
                    .world_mut()
                    .classes
                    .escaped_capabilities
                    .insert(*key);
            }
            self.visit_stored_references(&value, true, |reference| {
                pending.push(reference.clone());
            });
        }
    }

    /// The reach of a rejected local construction (SPEC.md § Synchronous
    /// execution diagnostics): the class and the callables its arguments hold.
    pub(super) fn construction_reach(&self, key: u32, args: &[Values]) -> HavocReach {
        let class = AbstractValue::Value(Value::Alloc(Kind::Class, key));
        self.known_code_reach(None, std::iter::once(&class).chain(args.iter().flatten()))
    }

    /// The reach of an invocation truncated at a recursion, depth or
    /// expansion bound: the callee and the callables its inputs hold.
    pub(super) fn invocation_reach(
        &self,
        key: u32,
        owner: SourceUnitOrdinal,
        args: &[Values],
        captured: &Context,
    ) -> HavocReach {
        if self.state.world.native.tainted_functions.contains(&key) {
            return self.dynamic_scope_reach().unwrap_or(HavocReach::Global {
                cause: GlobalReachCause::BorrowedReceiver,
            });
        }
        self.known_code_reach(
            Some(owner),
            args.iter().flatten().chain(captured.values().flatten()),
        )
    }

    pub(super) fn dynamic_scope_reach(&self) -> Option<HavocReach> {
        (self.state.world.reachability.incomplete
            || !self.inputs.index.dynamic_scope_owners.is_empty())
        .then_some(HavocReach::Global {
            cause: GlobalReachCause::DynamicScope,
        })
    }

    /// Opaque code may hold the class or its prototype and install or borrow
    /// methods its constructor and fields run.
    pub(super) fn class_may_run_on_borrowed_receiver(&self, key: u32) -> bool {
        self.state.world.classes.tainted.contains(&key)
            || self.state.world.classes.escaped_capabilities.contains(&key)
            || self
                .state
                .world
                .publication
                .global_escaped_callbacks
                .contains(&AbstractValue::ClassMembers(key))
            || self
                .inputs
                .index
                .class_bindings
                .get(&key)
                .is_none_or(|binding| {
                    self.inputs.index.other_class_uses.contains(binding)
                        || (self.inputs.export_reentry_enabled
                            && self.inputs.reentry_bindings.contains(binding))
                })
    }

    /// Applies the collapse rules in order: dynamic scope, then the inputs
    /// (through stored properties, captures and accessors), then whether the
    /// synchronous read closure of `owner` and the inputs' callables and
    /// classes reaches a callee the source does not name.
    pub(super) fn known_code_reach<'a>(
        &self,
        owner: Option<SourceUnitOrdinal>,
        inputs: impl IntoIterator<Item = &'a AbstractValue>,
    ) -> HavocReach {
        if let Some(reach) = self.dynamic_scope_reach() {
            return reach;
        }
        let global = |cause| HavocReach::Global { cause };
        let mut roots: Vec<_> = owner.into_iter().collect();
        let mut pending: Vec<AbstractValue> = inputs.into_iter().cloned().collect();
        let mut visited = FxHashSet::default();
        while let Some(value) = pending.pop() {
            if !visited.insert(value.clone()) {
                continue;
            }
            if self
                .state
                .world
                .publication
                .global_escaped_callbacks
                .contains(&value)
            {
                return global(GlobalReachCause::TaintedInput);
            }
            match &value {
                AbstractValue::SymbolicInput(_) => return global(GlobalReachCause::TaintedInput),
                AbstractValue::DeferredCallbacks(captured)
                | AbstractValue::AuditedRetainedResult(_, captured)
                    if self
                        .state
                        .world
                        .native
                        .revoked_audited_results
                        .contains_key(&captured.cache_identity()) =>
                {
                    return global(GlobalReachCause::TaintedInput);
                }
                AbstractValue::Value(Value::Prim(_) | Value::Unavailable)
                | AbstractValue::ObjectAssign
                | AbstractValue::DeferredCallbacks(_)
                | AbstractValue::AuditedRetainedResult(_, _)
                | AbstractValue::OwnDescriptorMap(_) => {}
                AbstractValue::Value(Value::Top)
                | AbstractValue::GlobalObject
                | AbstractValue::ArrayIntrinsic => {
                    return global(GlobalReachCause::TaintedInput);
                }
                AbstractValue::CallbackFactory | AbstractValue::InlineCallbackFactory => {
                    return global(GlobalReachCause::UnknownCallee);
                }
                AbstractValue::ForeignDeferredRecord => {
                    return global(GlobalReachCause::ForeignRecord);
                }
                AbstractValue::Value(Value::Alloc(kind, object))
                    if kind.tracks_prefix_own_properties()
                        && !self.state.world.object_properties_are_settled(*object) =>
                {
                    return global(GlobalReachCause::TaintedInput);
                }
                AbstractValue::Value(Value::Alloc(Kind::Set, site))
                    if self.state.world.native.tainted_sets.contains(site) =>
                {
                    return global(GlobalReachCause::TaintedInput);
                }
                AbstractValue::Value(Value::Alloc(Kind::Function, key))
                | AbstractValue::Closure(HeapAddress { callable: key, .. }, _) => {
                    if self.state.world.native.tainted_functions.contains(key) {
                        return global(GlobalReachCause::TaintedInput);
                    }
                    let Some(callable) = self.inputs.index.callables.get(key) else {
                        return global(GlobalReachCause::UnknownCallee);
                    };
                    roots.push(callable.owner);
                }
                AbstractValue::Value(Value::Alloc(Kind::Class, key))
                | AbstractValue::ClassMembers(key)
                | AbstractValue::Instance(key, _, _) => {
                    if self.class_may_run_on_borrowed_receiver(*key) {
                        return global(GlobalReachCause::BorrowedReceiver);
                    }
                    let Some(&class_owner) = self
                        .inputs
                        .flow
                        .callable_source_unit
                        .get(&Value::Alloc(Kind::Class, *key))
                    else {
                        return global(GlobalReachCause::UnknownCallee);
                    };
                    roots.push(class_owner);
                }
                AbstractValue::Value(Value::Alloc(kind, _)) => {
                    if !kind.tracks_prefix_references() {
                        unreachable!("unsupported retained-reference allocation kind");
                    }
                }
            }
            self.visit_stored_references(&value, true, |reference| {
                pending.push(reference.clone());
            });
        }
        let reaches = self.inputs.reaches_unknown_callee.get_or_init(|| {
            owners_reaching(
                &self.inputs.synchronous_owner_adjacency,
                &self.inputs.unknown_callee_owners,
            )
        });
        if roots.iter().any(|root| reaches[root.0]) {
            return global(GlobalReachCause::UnknownCallee);
        }
        // Opaque code holding a class reached here may have replaced the
        // prototype methods its instances call.
        let closure: BTreeSet<_> = SourceUnitReadClosure::extend_synchronous_source_units(
            &self.inputs.synchronous_owner_adjacency,
            roots,
            &mut BTreeSet::new(),
        )
        .into_iter()
        .collect();
        let state = &*self.state;
        if state
            .world
            .classes
            .tainted
            .iter()
            .chain(&state.world.classes.escaped_capabilities)
            .chain(
                state
                    .world
                    .publication
                    .global_escaped_callbacks
                    .iter()
                    .filter_map(|value| match value {
                        AbstractValue::ClassMembers(key) => Some(key),
                        _ => None,
                    }),
            )
            .filter_map(|key| {
                self.inputs
                    .flow
                    .callable_source_unit
                    .get(&Value::Alloc(Kind::Class, *key))
            })
            .any(|owner| closure.contains(owner))
        {
            return global(GlobalReachCause::BorrowedReceiver);
        }
        HavocReach::Bounded
    }

    pub(crate) fn default_class_has_instance(&self, values: &Values) -> bool {
        let [AbstractValue::Value(Value::Alloc(Kind::Class, key))] =
            values.iter().collect::<Vec<_>>().as_slice()
        else {
            return false;
        };
        self.inputs
            .index
            .class_values
            .get(key)
            .is_some_and(|class| class.default_has_instance)
            && !self.state.world.classes.tainted.contains(key)
            && !self.state.world.classes.escaped_capabilities.contains(key)
            && !self
                .state
                .world
                .publication
                .global_escaped_callbacks
                .contains(&AbstractValue::ClassMembers(*key))
            && self
                .inputs
                .index
                .class_bindings
                .get(key)
                .is_some_and(|binding| {
                    !self.inputs.index.other_class_uses.contains(binding)
                        && !self.inputs.index.exported_bindings.contains(binding)
                })
    }

    pub(crate) fn ordinary_prototype_chain(&mut self, values: &Values) -> bool {
        !values.is_empty()
            && values.iter().all(|value| match value {
                AbstractValue::Value(Value::Prim(_)) => true,
                AbstractValue::Value(Value::Alloc(Kind::Object, object)) => {
                    self.state.world.object_properties_are_settled(*object)
                        && self.array_intrinsics_intact()
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
            })
    }

    pub(super) fn contains_dense_array(&self, values: &Values) -> bool {
        self.reachable_objects(values)
            .iter()
            .any(|object| self.state.world.arrays.dense.contains_key(object))
    }

    /// The summary's native constructions hold in every analyzed chunk's
    /// census, this one included.
    pub(crate) fn source_record_factory(&self, callee: &ValueExpr, args: &[ValueExpr]) -> bool {
        args.is_empty()
            && matches!(callee, ValueExpr::Binding(id)
            if self.inputs.index.returned_record_summaries.contains_key(id))
    }
}
