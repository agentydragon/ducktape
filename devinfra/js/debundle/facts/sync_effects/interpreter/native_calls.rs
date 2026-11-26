//! Native call resolution: certify a builtin row for a call site's intrinsic
//! identity, admit its operands and iteration, and execute the resolved row.

use super::*;

impl<'source, 'rule> Executor<'source, 'rule> {
    /// Return a builtin row only after proving the source-level intrinsic
    /// identity: the callee names the original global and the row's slots are
    /// intact, or asserted at this exact call. Operand eligibility is checked
    /// separately.
    pub(crate) fn exact_native_call_row(
        &mut self,
        kind: CallKind,
        callee: &ValueExpr,
        site: CallSite,
    ) -> Option<(&'static Row, NativeIdentitySource)> {
        match self.native_call_identity(kind, callee, site)? {
            NativeCallIdentity::FailedGuard(slots) => {
                self.record_failed_native_guard(NativeIntegrityGuard::NativeCallIdentity, slots);
                None
            }
            NativeCallIdentity::Proved(row, source) => {
                match &source {
                    NativeIdentitySource::AuthorInput { dependencies, .. } => self
                        .record_proof_dependencies(
                            NativeIntegrityGuard::NativeCallIdentity,
                            dependencies,
                        ),
                    NativeIdentitySource::Inferred { premise } => self.record_inference_use(
                        NativeIntegrityGuard::NativeCallIdentity,
                        premise.inference_use.clone(),
                        &premise.dependencies,
                    ),
                }
                Some((row, source))
            }
        }
    }

    /// The identity decision of `exact_native_call_row` without recording
    /// its premise use; `None` when the callee names no intrinsic row.
    pub(super) fn native_call_identity(
        &self,
        kind: CallKind,
        callee: &ValueExpr,
        site: CallSite,
    ) -> Option<NativeCallIdentity> {
        let (row, callee) = super::super::execution_index::native_call_identity_key(kind, callee)?;
        if !names_intrinsic(
            &callee,
            row.family()?,
            self.inputs.flow.is_declared(&callee.binding),
            self.inputs.shadowed,
            &self.inputs.index.global_object_names,
        ) {
            return None;
        }
        let Some(premise) = native_premise(
            &self.inputs.index.integrity,
            row.requires,
            Some(site.allocation_id()),
            InferenceRule::NativeCallIdentity,
        ) else {
            return Some(NativeCallIdentity::FailedGuard(row.requires));
        };
        if !premise.census {
            let evidence = self
                .inputs
                .index
                .native_operation_input_evidence
                .get(&site)?
                .clone();
            return Some(NativeCallIdentity::Proved(
                row,
                NativeIdentitySource::AuthorInput {
                    evidence: Box::new(evidence),
                    dependencies: Box::new(premise.dependencies),
                },
            ));
        }
        Some(NativeCallIdentity::Proved(
            row,
            NativeIdentitySource::Inferred {
                premise: Box::new(premise),
            },
        ))
    }

    pub(crate) fn native_member_lookup_is_data(
        &mut self,
        callee: &ValueExpr,
        site: Option<u32>,
    ) -> bool {
        if !self.inputs.index.dynamic_scope_owners.is_empty() {
            return false;
        }
        let Some(slots) = native_own_data_read_slots(callee, |binding| {
            self.inputs.flow.is_declared(binding)
                || self.inputs.shadowed.contains(binding.0.as_ref())
        }) else {
            return false;
        };
        let Some(premise) = native_premise(
            &self.inputs.index.integrity,
            &slots,
            site,
            InferenceRule::NativeOwnDataRead,
        ) else {
            return false;
        };
        // Identity discharges this lookup only. Heap read analysis records
        // the exact read use; invocation uses require separate admission.
        self.record_proof_dependencies(
            NativeIntegrityGuard::NativeCallIdentity,
            &premise.dependencies,
        );
        self.observations
            .record_inference_use(premise.inference_use);
        true
    }

    /// Reuse the exact identity/lookup proof of the canonical outer call.
    /// Neither this lookup nor native identity certifies the invocation's
    /// inputs, timing, purity or throws.
    pub(crate) fn callee_lookup_is_certified(&mut self, lookup: &Call) -> bool {
        if self.native_member_lookup_is_data(&lookup.callee, None) {
            return true;
        }
        if let ValueExpr::Member(receiver, Some(property), MemberKey::Static, _) = &lookup.callee
            && let ValueExpr::Binding(namespace) = receiver.as_ref()
            && self
                .inputs
                .index
                .audited_factory_members
                .get(namespace)
                .into_iter()
                .chain(self.inputs.index.no_reentry_members.get(namespace))
                .chain(self.inputs.index.no_reentry_record_members.get(namespace))
                .any(|members| members.contains(property))
        {
            // These maps contain only the completed namespace identity,
            // publication, mutation and namespace_independent_members proofs.
            return true;
        }
        let Some(call) = self
            .inputs
            .index
            .accessor_callee_calls
            .get(&lookup.span.into())
        else {
            return false;
        };
        let site = CallSite::from(call.span);
        if self.native_member_lookup_is_data(&lookup.callee, Some(site.allocation_id())) {
            return true;
        }
        let Some((row, _)) = self.exact_native_call_row(call.kind, &call.callee, site) else {
            return false;
        };
        let premise = native_premise(
            &self.inputs.index.integrity,
            row.requires,
            Some(site.allocation_id()),
            InferenceRule::NativeCallIdentity,
        )
        .expect("exact native call retained its identity premise");
        // Identity discharges this lookup only. An operation-use record is
        // reserved for the separately admitted invocation.
        self.record_proof_dependencies(
            NativeIntegrityGuard::NativeCallIdentity,
            &premise.dependencies,
        );
        self.observations
            .record_inference_use(premise.inference_use);
        true
    }

    /// An admitted row certifies the operation after its inputs have
    /// evaluated. It does not infer purity or exception freedom.
    pub(crate) fn resolve_native_call(
        &mut self,
        kind: CallKind,
        callee: &ValueExpr,
        site: CallSite,
        expressions: &[ValueExpr],
        args: &[Values],
    ) -> Option<NativeCallResolution> {
        let RuntimeValue::Heap(HeapResult::Native(resolution)) =
            self.request(RuntimeOperation::Heap(HeapOperation::ResolveNative {
                kind,
                callee: callee.clone(),
                site,
                expressions: expressions.to_vec(),
                arguments: args.to_vec(),
            }))
        else {
            unreachable!("native model resolution produces an optional model");
        };
        *resolution
    }

    /// Admit a row for the call's operands. The row's writes are proved
    /// first; each proof names the operands it shows to be exact ordinary
    /// objects, on which the listed internal methods run no user code. Every
    /// remaining coercion must land on a primitive operand.
    pub(crate) fn resolve_native_call_transfer(
        &mut self,
        kind: CallKind,
        callee: &ValueExpr,
        site: CallSite,
        expressions: &[ValueExpr],
        args: &[Values],
    ) -> Option<NativeCallResolution> {
        let (row, source) = self.exact_native_call_row(kind, callee, site)?;
        if !row.arity.contains(&args.len()) || !row.invokes.is_empty() {
            return None;
        }
        let iterable = self.admit_set_iteration(row, kind, callee, site, expressions, args);
        let native_call = match &source {
            NativeIdentitySource::Inferred { premise } => Some(premise.as_ref().clone()),
            NativeIdentitySource::AuthorInput { .. } => None,
        };
        let mut ordinary_operands = BTreeSet::new();
        let mut own_property_projection = None;
        let mut projected_reads = Vec::new();
        let mut application = None;
        let mut definition = None;
        for mutation in row.mutates {
            match *mutation {
                // SetIntegrityLevel runs only ordinary internal methods: it is
                // silent on a primitive and on every exact object allocation.
                // An allocation another statement already holds keeps its
                // conservative barrier, since the attribute change would have
                // to be ordered against that statement's writes.
                Mutation::Integrity(ArgSel::Arg(index)) => {
                    let values = args.get(index)?;
                    let literal = match expressions.get(index) {
                        Some(ValueExpr::ObjectLiteral(object, _)) => Some(*object),
                        _ => None,
                    };
                    if values.is_empty()
                        || !values.iter().all(|value| {
                            value.is_primitive()
                                || matches!(value, AbstractValue::Value(Value::Alloc(kind, object))
                                    if kind.tracks_prefix_own_properties()
                                        && (literal == Some(*object)
                                            || self.statement_literal(*object)))
                        })
                    {
                        return None;
                    }
                    ordinary_operands.insert(index);
                }
                Mutation::DefineProperty {
                    target: ArgSel::Arg(target),
                    key: ArgSel::Arg(key),
                    descriptor: ArgSel::Arg(descriptor),
                } => {
                    let (Some(target_values), Some(key_values), Some(descriptor_values)) =
                        (args.get(target), args.get(key), args.get(descriptor))
                    else {
                        return None;
                    };
                    definition = Some(descriptor_heap::dynamic_key_definition(
                        self,
                        target_values,
                        key_values,
                        descriptor_values,
                    )?);
                    // The key stays with the primitive coercion check below.
                    ordinary_operands.extend([target, descriptor]);
                }
                Mutation::DefineProperties {
                    target: ArgSel::Arg(target),
                    descriptors: ArgSel::Arg(descriptors),
                } => {
                    let (Some(target_values), Some(map_values)) =
                        (args.get(target), args.get(descriptors))
                    else {
                        return None;
                    };
                    application = Some(descriptor_heap::application(
                        self,
                        target_values,
                        map_values,
                    )?);
                    ordinary_operands.extend([target, descriptors]);
                }
                Mutation::FreshOwnDescriptorsOf(ArgSel::Arg(index)) => {
                    let proof = self.own_property_projection(
                        args.get(index)?,
                        OwnPropertyRequest::Descriptors {
                            native_call: native_call.clone()?,
                        },
                    )?;
                    let OwnPropertyProjection::Descriptors(projection) = &proof.projection else {
                        return None;
                    };
                    let source = projection.source_object;
                    projected_reads.push(EffectCell::ObjectShape(source));
                    projected_reads.extend(projection.ordered_names.iter().map(|name| {
                        EffectCell::OwnProperty {
                            origin: source,
                            property: name.clone(),
                        }
                    }));
                    own_property_projection = Some(proof);
                    ordinary_operands.insert(index);
                }
                Mutation::FreshOwnKeysOf(ArgSel::Arg(index)) => {
                    let source_values = args.get(index)?;
                    // The projection admits only a singleton ordinary record.
                    let Some(AbstractValue::Value(Value::Alloc(Kind::Object, source))) =
                        source_values.first()
                    else {
                        return None;
                    };
                    projected_reads.push(EffectCell::ObjectShape(*source));
                    own_property_projection = Some(self.own_property_projection(
                        source_values,
                        OwnPropertyRequest::Names {
                            native_call: native_call.clone()?,
                        },
                    )?);
                    ordinary_operands.insert(index);
                }
                // Retention needs one known callable; the slot stores it
                // without invoking it.
                Mutation::FreshRetains(ArgSel::Arg(index)) => {
                    let values = args.get(index)?;
                    if values.len() != 1
                        || !values.iter().next().is_some_and(|value| {
                            matches!(
                                value,
                                AbstractValue::Value(Value::Alloc(Kind::Function, _))
                                    | AbstractValue::Closure(_, _)
                            )
                        })
                    {
                        return None;
                    }
                }
                // A write through a `Rest` selector is not modeled.
                _ => return None,
            }
        }
        for &(selector, operation) in row.coerces {
            let ArgSel::Arg(index) = selector else {
                return None;
            };
            if iterable.is_some() && index == 0 && operation == CoerceOp::Iterate {
                continue;
            }
            if ordinary_operands.contains(&index) {
                continue;
            }
            // An absent argument is `undefined`.
            let Some(values) = args.get(index) else {
                continue;
            };
            if !operation.silent_on_primitive()
                || values.is_empty()
                || !values.iter().all(AbstractValue::is_primitive)
            {
                return None;
            }
        }
        if let NativeIdentitySource::AuthorInput { evidence, .. } = source {
            let source_span = site.source_span();
            self.observations.record_native_operation_input_use(
                source_span,
                analysis::reports::schema::NativeOperationInputUse {
                    evidence: *evidence,
                    source_location: None,
                    span: source_span,
                },
            );
        }
        if let Some(proof) = &own_property_projection {
            self.observations
                .record_inference_use(proof.inference_use());
        }
        let operation_coverage = if kind == CallKind::New
            && matches!(row.op, IntrinsicOp::Map | IntrinsicOp::WeakMap)
            && expressions.is_empty()
        {
            let mut dependencies = self.observations.dependencies().clone();
            dependencies.extend(&self.queries.dependencies());
            super::super::allocation_transfer::keyed_collection_new(row, site, dependencies)
                .map(|result| result.effects)
        } else {
            None
        };
        let plan = native_operation::plan(
            row,
            site,
            args,
            own_property_projection.as_ref(),
            application,
            definition,
            self.state.world.revision.prefix,
        )?;
        let mut dependencies = self.observations.dependencies().clone();
        dependencies.extend(&self.queries.dependencies());
        Some(NativeCallResolution {
            row,
            plan,
            iterable,
            operation_coverage,
            projected_reads,
            dependencies,
        })
    }

    /// Preserve the exact native row contract; the shared iterable operation
    /// owns protocol/content admission for both spread and construction.
    pub(super) fn admit_set_iteration(
        &mut self,
        row: &'static Row,
        kind: CallKind,
        callee: &ValueExpr,
        site: CallSite,
        expressions: &[ValueExpr],
        arguments: &[Values],
    ) -> Option<NativeIterableReceipt> {
        if kind != CallKind::New
            || row.op != IntrinsicOp::Set
            || row.invocation != [Invocation::Construct]
            || row.arity != (0..=1)
            || row.coerces != [(ArgSel::Arg(0), CoerceOp::Iterate)]
            || row.allocates != Some(Kind::Set)
            || !row.mutates.is_empty()
            || row.returns != Return::Fresh
            || row.throws != Throws::Never
            || !matches!(callee, ValueExpr::Binding(binding) if binding.0.as_ref() == "Set")
        {
            return None;
        }
        let [expression] = expressions else {
            return None;
        };
        let [values] = arguments else {
            return None;
        };
        let input = if let Some((projection, _)) = self
            .inputs
            .set_iterable_proof
            .native_iterable_operand(site.source_span())
        {
            let NativeIterableOperandOrigin::ImmediateArrayLiteral { allocation_site } =
                projection.operand_origin();
            if Some(projection.owner()) != self.state.current_top_owner
                || !matches!(expression, ValueExpr::ObjectLiteral(array, _) if *array == allocation_site)
            {
                return None;
            }
            NativeIterableInput::ImmediateArrayLiteral(allocation_site)
        } else {
            match expression {
                ValueExpr::ObjectLiteral(array, _)
                    if self.inputs.index.array_elements.contains_key(array) =>
                {
                    NativeIterableInput::ImmediateArrayLiteral(*array)
                }
                _ => NativeIterableInput::Borrowed,
            }
        };
        self.native_iterable(NativeIterableUse::SetConstruction { site, input }, values)
    }

    pub(crate) fn native_iterable(
        &mut self,
        operation: NativeIterableUse,
        operand: &Values,
    ) -> Option<NativeIterableReceipt> {
        let RuntimeValue::Heap(HeapResult::Iteration(receipt)) =
            self.request(RuntimeOperation::Heap(HeapOperation::IterateNative {
                operation,
                operand: operand.clone(),
            }))
        else {
            unreachable!("native iterable requests return their operation receipt");
        };
        *receipt
    }

    pub(crate) fn transfer_native_iterable(
        &mut self,
        operation: NativeIterableUse,
        operand: &Values,
    ) -> Option<NativeIterableReceipt> {
        if let NativeIterableUse::ArraySpread(span) = operation
            && let Some((input, source_index, source)) =
                self.inputs.set_iterable_proof.imported_array_spread(span)
        {
            // The selected source and its author lifetime premise are bound
            // to this exact spread token. This transfers only finite
            // primitive yields; it deliberately creates no provider heap
            // identity or provider-content read cell.
            if !self.array_slots_intact(ARRAY_PROTOTYPE_SLOTS) {
                return None;
            }
            if source.primitive_slots.len() > MAX_EXACT_ARRAY_SPREAD_SLOTS {
                return None;
            }
            let mut dependencies = self.settled_prefix_dependencies();
            dependencies
                .imported_primitive_array_spread_inputs
                .insert(input.evidence.reference.clone());
            self.record_proof_dependencies(NativeIntegrityGuard::ArrayIntrinsics, &dependencies);
            self.observations
                .record_inference_use(analysis::analysis_provenance::InferenceUse {
                    conclusion: InferenceRule::ImportedPrimitiveArraySpread,
                    derivation_rules: BTreeSet::from([
                        InferenceRule::ImportedPrimitiveArraySpread,
                        InferenceRule::NativeIntegrity,
                    ]),
                    value_input_refs: BTreeSet::new(),
                    descriptor_copy_author_refs: BTreeSet::new(),
                    argument_input_refs: BTreeSet::new(),
                    native_operation_input_refs: BTreeSet::new(),
                    imported_primitive_array_spread_input_refs: BTreeSet::from([input
                        .evidence
                        .reference
                        .clone()]),
                });
            self.observations
                .record_imported_primitive_array_spread_input_use(
                    analysis::analysis_inputs::ImportedPrimitiveArraySpreadInputUse {
                        evidence: input.evidence.clone(),
                        operand_index: source_index,
                    },
                );
            let yields = source
                .primitive_slots
                .iter()
                .map(|kind| Values::from([AbstractValue::Value(Value::Prim(*kind))]))
                .collect();
            return Some(NativeIterableReceipt {
                source: None,
                yields: NativeIterableYields::ExactSlots(yields),
                heap_contents: analysis::facts::HeapContentCells::default(),
                input: NativeIterableInput::Borrowed,
                coverage: EffectCoverageFragment::native_iterable_protocol(&dependencies),
            });
        }
        let [AbstractValue::Value(Value::Alloc(kind, object))] =
            operand.iter().collect::<Vec<_>>().as_slice()
        else {
            return None;
        };
        if !self
            .state
            .world
            .surface
            .object_accessors_are_settled(*object)
            || self.state.world.arrays.unknown_contents.contains(object)
            || self
                .state
                .world
                .surface
                .applied_accessors
                .contains_key(object)
            || self.state.world.native.tainted_sets.contains(object)
        {
            return None;
        }
        if matches!(operation, NativeIterableUse::ArraySpread(_))
            && self
                .settled_dense_array_slot_count(*object)
                .is_some_and(|length| length > MAX_EXACT_ARRAY_SPREAD_SLOTS)
        {
            return None;
        }
        let proof = self.inputs.set_iterable_proof;
        let mut dependencies = self.settled_prefix_dependencies();
        let yields = match operation {
            NativeIterableUse::ArraySpread(span) if *kind == Kind::Set => {
                if proof.stable_spread_source(span) != Some(*object) {
                    return None;
                }
                let premise = proof.stable_spread_dependencies(span)?;
                dependencies.extend(premise);
                self.record_proof_dependencies(NativeIntegrityGuard::SetIterable, premise);
                NativeIterableYields::PrimitiveOnly
            }
            NativeIterableUse::ArraySpread(span) if *kind == Kind::Object => {
                if let Some(premise) = proof.stable_array_spread_dependencies(span) {
                    if proof.stable_array_spread_source(span) != Some(*object) {
                        return None;
                    }
                    dependencies.extend(premise);
                    let inference = proof.stable_array_spread_inference_use(span)?;
                    self.record_inference_use(
                        NativeIntegrityGuard::ArrayIntrinsics,
                        inference,
                        premise,
                    );
                } else if !self.array_slots_intact(ARRAY_PROTOTYPE_SLOTS) {
                    return None;
                }
                if let Some(slots) = self.settled_dense_array_slots(*object) {
                    NativeIterableYields::ExactSlots(slots)
                } else {
                    let premise = self.state.world.arrays.primitive.get(object)?;
                    dependencies.extend(premise);
                    NativeIterableYields::PrimitiveOnly
                }
            }
            NativeIterableUse::SetConstruction { site, input } if *kind == Kind::Object => {
                if matches!(input, NativeIterableInput::ImmediateArrayLiteral(origin) if origin != *object)
                {
                    return None;
                }
                let span = site.source_span();
                let premise =
                    if let Some((projection, premise)) = proof.native_iterable_operand(span) {
                        if Some(projection.owner()) != self.state.current_top_owner
                            || !matches!(input,
                            NativeIterableInput::ImmediateArrayLiteral(origin)
                                if matches!(
                                    projection.operand_origin(),
                                    NativeIterableOperandOrigin::ImmediateArrayLiteral {
                                        allocation_site
                                    } if allocation_site == origin && allocation_site == *object
                                ))
                        {
                            return None;
                        }
                        premise
                    } else {
                        proof.dependencies_for_constructor(span)?
                    };
                dependencies.extend(premise);
                let yields = if let Some(slots) = self.settled_dense_array_slots(*object) {
                    if !slots.iter().all(|slot| !slot.is_empty() && slot.iter().all(|value| {
                        matches!(value, AbstractValue::Value(Value::Prim(kinds)) if [PrimKinds::STRING, PrimKinds::NUMBER, PrimKinds::BOOLEAN, PrimKinds::NULL, PrimKinds::BIGINT].contains(kinds))
                    })) { return None; }
                    NativeIterableYields::ExactSlots(slots)
                } else {
                    let array_dependencies = self.state.world.arrays.primitive.get(object)?;
                    dependencies.extend(array_dependencies);
                    NativeIterableYields::PrimitiveOnly
                };
                if let Some(evidence) = proof.author_input_for_constructor(span).cloned() {
                    self.record_proof_dependencies(NativeIntegrityGuard::SetIterable, premise);
                    self.observations.record_native_operation_input_use(
                        span,
                        analysis::reports::schema::NativeOperationInputUse {
                            evidence,
                            source_location: None,
                            span,
                        },
                    );
                } else {
                    self.record_inference_use(
                        NativeIntegrityGuard::SetIterable,
                        proof.inference_use_for_constructor(span)?,
                        premise,
                    );
                }
                yields
            }
            _ => return None,
        };
        dependencies.extend(&self.settled_prefix_dependencies());
        let mut heap_contents = analysis::facts::HeapContentCells::default();
        heap_contents.reads.insert(match kind {
            Kind::Set => analysis::facts::EffectCell::SetContents(*object),
            Kind::Object => analysis::facts::EffectCell::ArrayContents(*object),
            _ => unreachable!("native iterable admission only models Set or Array sources"),
        });
        let input = if let NativeIterableUse::SetConstruction { site, input } = operation {
            heap_contents
                .allocations
                .insert(analysis::facts::EffectCell::SetContents(
                    site.allocation_id(),
                ));
            input
        } else {
            NativeIterableInput::Borrowed
        };
        Some(NativeIterableReceipt {
            source: Some(*object),
            yields,
            heap_contents,
            input,
            coverage: EffectCoverageFragment::native_iterable_protocol(&dependencies),
        })
    }

    /// Author-asserted timing for two exact operations in the pinned Bun host.
    /// This does not supply a return value or a purity/throw summary.
    pub(super) fn host_call_does_not_reenter(&mut self, call: &Call) -> bool {
        if call.has_spread
            || !call.args.is_empty()
            || !self.inputs.index.dynamic_scope_owners.is_empty()
        {
            return false;
        }
        let mut dependencies = analysis::analysis_inputs::ProofDependencies::default();
        // Both arms below hold only when the index saw the matching
        // `reentry_free_operations` entry; an unaudited host selects neither.
        let operation = match (call.kind, &call.callee) {
            (CallKind::New, ValueExpr::Binding(binding))
                if self
                    .inputs
                    .index
                    .host_async_local_storage_imports
                    .contains(binding) =>
            {
                spec::ReentryFreeOperation::AsyncLocalStorageConstruct
            }
            (CallKind::Call, ValueExpr::Member(receiver, Some(member), MemberKey::Static, _))
                if call.no_reentry_candidate && member == "resolve" =>
            {
                let ValueExpr::Binding(binding) = receiver.as_ref() else {
                    return false;
                };
                if !self.inputs.index.host_promise_resolve
                    || binding.0.as_ref() != "Promise"
                    || self.inputs.flow.is_declared(binding)
                    || self.inputs.shadowed.contains("Promise")
                {
                    return false;
                }
                if !self.native_guard_intact(
                    NativeIntegrityGuard::HostPromiseResolve,
                    &[
                        IntrinsicSlot::Global(NativeIntrinsic::Promise),
                        IntrinsicSlot::Static(NativeIntrinsic::Promise, "resolve"),
                    ],
                ) {
                    return false;
                }
                spec::ReentryFreeOperation::PromiseResolve
            }
            _ => return false,
        };
        dependencies
            .host_behavior
            .insert(analysis::analysis_inputs::HostBehaviorKey::ReentryFreeOperation(operation));
        self.consume_proof_dependencies(&dependencies);
        let proof_use = analysis::reports::schema::ReentryFreeOperationUse {
            operation,
            source_location: None,
            span: call.span,
        };
        if !self.state.author_host_contract_uses.contains(&proof_use) {
            self.state.author_host_contract_uses.push(proof_use);
        }
        true
    }

    /// Commit the admitted row's heap mutations to this prefix, then project
    /// its normal return value. The mutations happen whether or not the
    /// caller uses the value.
    pub(crate) fn execute_native_resolution(
        &mut self,
        resolution: NativeCallResolution,
        args: &[Values],
        may_throw: bool,
        site: CallSite,
    ) -> Values {
        let NativeCallResolution {
            row,
            plan,
            iterable,
            operation_coverage,
            projected_reads: _,
            dependencies: _,
        } = resolution;
        let fresh = match native_operation::execute(self, plan) {
            Ok(fresh) => fresh,
            // The prefix moved between admission and execution: nothing was
            // committed and no row fact describes this call's result.
            Err(native_operation::StalePlan) => {
                let mut values = unknown();
                values.insert(AbstractValue::Value(Value::Unavailable));
                return values;
            }
        };
        let mut values = match row.returns {
            Return::Unknown => unknown(),
            Return::Prim(kinds) => Values::from([AbstractValue::Value(Value::Prim(kinds))]),
            Return::Arg(ArgSel::Arg(index)) => args.get(index).cloned().unwrap_or_else(unknown),
            Return::Arg(ArgSel::Rest(_)) => unknown(),
            Return::Fresh if row.allocates == Some(Kind::Set) && iterable.is_some() => {
                // The common operation modeled construction from primitive
                // iteration. Preserve only its allocation kind/origin; this
                // is no Set contents, size, properties or singleton proof.
                Values::from([AbstractValue::Value(Value::Alloc(
                    Kind::Set,
                    site.allocation_id(),
                ))])
            }
            Return::Fresh if operation_coverage.is_some() => unknown(),
            Return::Fresh => fresh.unwrap_or_else(|| self.fresh_allocation_values(row, args, site)),
        };
        if may_throw {
            values.insert(AbstractValue::Value(Value::Unavailable));
        }
        values
    }

    pub(super) fn record_native_initializer_transfer(
        &mut self,
        callee: &ValueExpr,
        expressions: &[ValueExpr],
        arguments: &[Values],
        may_throw: bool,
        site: CallSite,
        resolution: &NativeCallResolution,
    ) {
        let Some(candidate) = self.native_initializer_candidate else {
            return;
        };
        let kind = candidate.kind();
        let (expected_op, expected_allocation, expected_return) = match kind {
            NativeConstructorKind::Set => (IntrinsicOp::Set, Some(Kind::Set), Return::Fresh),
            NativeConstructorKind::TextEncoder => (IntrinsicOp::TextEncoder, None, Return::Unknown),
        };
        // `resolution` comes from this New dispatch's resolve_native_call,
        // which already proves the exact unshadowed global identity through
        // its census or exact-site input. The builtin binding itself stays
        // opaque in value flow, so its abstract value is not an identity test.
        let row = resolution.row;
        let dispatch_form = match (kind, expressions) {
            (NativeConstructorKind::Set | NativeConstructorKind::TextEncoder, []) => {
                NativeConstructorForm::Empty
            }
            (NativeConstructorKind::Set, [ValueExpr::ObjectLiteral(array, _)]) => {
                NativeConstructorForm::SetPrimitiveArray { array: *array }
            }
            _ => return,
        };
        let names_expected_global = matches!(callee, ValueExpr::Binding(binding)
            if binding.0.as_ref() == kind.global_name());
        // Empty collection forms cannot run coercions of absent arguments.
        // TextEncoder's host row has exact zero arity and no coercions.
        let root_call_shape_matches = match (kind, dispatch_form) {
            (NativeConstructorKind::Set, NativeConstructorForm::Empty) => {
                row.arity.contains(&0)
                    && row
                        .coerces
                        .iter()
                        .all(|(selector, _)| matches!(selector, ArgSel::Arg(_)))
            }
            (NativeConstructorKind::TextEncoder, NativeConstructorForm::Empty) => {
                row.arity == (0..=0) && row.coerces.is_empty()
            }
            (NativeConstructorKind::Set, NativeConstructorForm::SetPrimitiveArray { array }) => {
                row.arity.contains(&1)
                    && row.coerces == [(ArgSel::Arg(0), CoerceOp::Iterate)]
                    && matches!(expressions, [ValueExpr::ObjectLiteral(lowered, _)] if *lowered == array)
                    && matches!(arguments, [values]
                        if matches!(values.iter().collect::<Vec<_>>().as_slice(),
                            [AbstractValue::Value(Value::Alloc(Kind::Object, evaluated))]
                                if *evaluated == array))
            }
            _ => false,
        };
        if candidate.matches_dispatch(site, kind, dispatch_form)
            && names_expected_global
            && !may_throw
            && row.op == expected_op
            && row.invocation == [Invocation::Construct]
            && root_call_shape_matches
            && row.invokes.is_empty()
            && row.allocates == expected_allocation
            && row.mutates.is_empty()
            && row.returns == expected_return
            && row.throws == Throws::Never
        {
            let mut dependencies = self.observations.dependencies().clone();
            dependencies.extend(&self.queries.dependencies());
            self.native_initializer_transfer =
                Some(NativeConstructorTransfer::applied(candidate, dependencies));
        }
    }
}
