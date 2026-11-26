//! Checks analysis inputs against the index before execution: native
//! integrity, `Set` constructions, semantic facts, selected value and
//! native-operation inputs, and the source mutations that would void them.

use super::*;

impl ExecutionIndex {
    pub(crate) fn validated_native_operation_inputs(&self) -> Vec<ResolvedNativeOperationInput> {
        self.resolved_native_operation_inputs.clone()
    }

    pub(super) fn apply_native_integrity(&mut self, integrity: &NativeIntegrity) {
        self.mutated_intrinsic_roots = integrity.mutated.clone();
        self.native_integrity_witnesses = integrity.witnesses.clone();
        self.native_integrity_value_input_refs = integrity.consumed_value_inputs.clone();
        self.native_integrity_host_behavior = integrity.consumed_host_behavior.clone();
        self.never_written = integrity.never_written.clone();
        self.never_written_overrode = !integrity.never_written_overrides.is_empty();
        self.integrity = self.integrity_flags();
    }

    /// Literal operands are primitive without executing source. An identifier
    /// operand is admitted only when the completed census proves the same
    /// canonical binding has one earlier direct primitive initializer, with
    /// no dynamic-scope access or recorded write. This establishes primitive
    /// type, not an exact value; ordinary reads still carry order and TDZ.
    pub(super) fn has_stable_primitive_set_array(
        &self,
        declaration: &BindingDeclarationWrite,
    ) -> bool {
        if !self.dynamic_scope_owners.is_empty() {
            return false;
        }
        declaration
            .set_from_primitive_array_aliases
            .as_ref()
            .is_some_and(|aliases| {
                aliases.iter().all(|binding| {
                    !self.reassigned_bindings.contains(binding)
                        && !self.binding_target_writes.contains(binding)
                        && matches!(
                            self.binding_declaration_writes.get(binding).map(Vec::as_slice),
                            Some([source]) if source.direct_top_level
                                && source.simple_identifier
                                && source.primitive_literal_initializer
                                && source.owner < declaration.owner
                        )
                })
            })
    }

    /// Source-only construction proofs. Every protocol premise comes from the
    /// completed census; this operation must run before its roots seed flow.
    pub(super) fn infer_set_constructions(
        &self,
        integrity: &IntegrityFlags,
    ) -> BTreeMap<u32, InferredSetConstruction> {
        use IntrinsicSlot::{ArrayIteratorPrototype, Global, Prototype, SetIteratorPrototype};
        use NativeIntrinsic::{Array, Set};
        use analysis_vocabulary::provenance::InferenceRule;
        if !self.dynamic_scope_owners.is_empty() {
            return BTreeMap::new();
        }
        let declarations: BTreeMap<_, _> = self
            .binding_declaration_writes
            .iter()
            .filter_map(|(binding, writes)| {
                let [write] = writes.as_slice() else {
                    return None;
                };
                (write.direct_top_level
                    && write.simple_identifier
                    && !self.reassigned_bindings.contains(binding))
                .then_some((write.owner, (binding, write)))
            })
            .collect();
        let set_binding = ("Set".into(), swc_common::SyntaxContext::empty());
        if self.declared_bindings().iter().any(|id| id.0 == "Set")
            || self.property_key_declarations.contains(&set_binding)
        {
            return BTreeMap::new();
        }
        let slots = [
            Global(Set),
            Prototype(Set),
            Prototype(Array),
            ArrayIteratorPrototype,
        ];
        let mut proofs: BTreeMap<u32, InferredSetConstruction> = BTreeMap::new();
        let mut sites_by_binding = BTreeMap::new();
        for (owner, (binding, declaration)) in &declarations {
            let Some(ValueExpr::New(callee, _, site)) = &declaration.initializer else {
                continue;
            };
            if !matches!(callee.as_ref(), ValueExpr::Binding(id) if id.0 == "Set") {
                continue;
            }
            let operation = if self.has_stable_primitive_set_array(declaration) {
                InferredSetOperation::PrimitiveArray
            } else if let Some((source, source_span, spread_token_span)) =
                &declaration.set_from_set_spread_source
            {
                let Some(seed_site) = sites_by_binding.get(source) else {
                    continue;
                };
                let Some(seed) = proofs.get(seed_site) else {
                    continue;
                };
                if !matches!(seed.operation, InferredSetOperation::PrimitiveArray) {
                    continue;
                }
                let Some((seed_owner, _)) = declarations.iter().find(|(_, (id, _))| *id == source)
                else {
                    continue;
                };
                if seed_owner >= owner {
                    continue;
                }
                let mut interval_bindings = Vec::new();
                let mut dependencies = ProofDependencies::default();
                let mut valid = true;
                for ordinal in seed_owner.0..owner.0 {
                    let Some((id, write)) = declarations.get(&SourceUnitOrdinal(ordinal)) else {
                        valid = false;
                        break;
                    };
                    let Some(proof) = sites_by_binding.get(*id).and_then(|site| proofs.get(site))
                    else {
                        valid = false;
                        break;
                    };
                    if !self.has_stable_primitive_set_array(write)
                        || self.original_statement_spans[ordinal]
                            != self.original_statement_spans[owner.0]
                    {
                        valid = false;
                        break;
                    }
                    interval_bindings.push((*id).clone());
                    dependencies.extend(&proof.dependencies);
                }
                if !valid {
                    continue;
                }
                interval_bindings.push((*binding).clone());
                InferredSetOperation::ProtectedSpread {
                    source: source.clone(),
                    interval: Box::new(analysis_vocabulary::inputs::StableSetIterableUse {
                        source_span: *source_span,
                        spread_token_span: *spread_token_span,
                        interval_bindings,
                        dependencies,
                    }),
                }
            } else {
                continue;
            };
            let required_slots = match operation {
                InferredSetOperation::PrimitiveArray => slots.to_vec(),
                InferredSetOperation::ProtectedSpread { .. } => {
                    [slots.as_slice(), &[SetIteratorPrototype]].concat()
                }
            };
            let Some(mut premise) = native_premise(
                integrity,
                &required_slots,
                None,
                InferenceRule::PrimitiveSetIteration,
            ) else {
                continue;
            };
            let mut operation = operation;
            if let InferredSetOperation::ProtectedSpread { interval, .. } = &mut operation {
                premise.dependencies.extend(&interval.dependencies);
                interval.dependencies = premise.dependencies.clone();
            }
            sites_by_binding.insert((*binding).clone(), site.allocation_id());
            proofs.insert(
                site.allocation_id(),
                InferredSetConstruction {
                    binding: (*binding).clone(),
                    operation,
                    dependencies: premise.dependencies,
                    inference: premise.inference_use,
                },
            );
        }
        proofs
    }

    /// Build one immutable context from the canonical source index and the
    /// completed native-write census. Callable candidates are input facts;
    /// their returned-record constructors participate in the same native
    /// identity and integrity proofs as local-class fields.
    pub(super) fn completed_native_rules(&self) -> SemanticRules {
        SemanticRules {
            native: NativePremises {
                integrity: self.integrity.clone(),
                bindings: NativeBindingContext::default(),
            },
            dynamic_scope: !self.dynamic_scope_owners.is_empty(),
            ..SemanticRules::default()
        }
    }

    /// The author and host premises the completed census consumed.
    pub(crate) fn native_integrity_dependencies(&self) -> ProofDependencies {
        let bundle_globals = self
            .native_integrity_host_behavior
            .contains(&HostBehaviorKey::PredefinedGlobals);
        ProofDependencies {
            value_inputs: self.native_integrity_value_input_refs.clone(),
            host_behavior: self.native_integrity_host_behavior.clone(),
            no_accessor_install_callables: if bundle_globals {
                self.census_globals
                    .bundle
                    .no_accessor_install_callables
                    .clone()
            } else {
                BTreeSet::new()
            },
            program_premises: if self.never_written_overrode {
                BTreeSet::from([ProgramPremiseKey::IntrinsicsNeverWritten])
            } else {
                BTreeSet::new()
            },
            ..Default::default()
        }
    }

    /// The completed census as slot flags, with the `IntrinsicIntact` axioms
    /// of every validated native input.
    pub(crate) fn integrity_flags(&self) -> IntegrityFlags {
        IntegrityFlags::new(
            self.dynamic_scope_owners.is_empty(),
            self.mutated_intrinsic_roots.clone(),
            &self.native_integrity_witnesses,
            self.native_integrity_dependencies(),
            self.intrinsic_axioms(),
            PrototypeWrites::new(
                self.native_prototype_mutations
                    .iter()
                    .map(|mutation| mutation.properties.as_ref()),
            ),
        )
        .with_never_written(self.never_written.clone())
    }

    /// Each validated input asserts the slots its operation reads at the
    /// exact call or construction site its consumer queries.
    pub(super) fn intrinsic_axioms(&self) -> BTreeMap<u32, IntrinsicIntact> {
        use IntrinsicSlot::{ArrayIteratorPrototype, Global, Prototype, Static};
        use NativeIntrinsic::{Array, Object, Set, Symbol, WeakMap};
        use spec::{NativeCallIdentityKind as Callee, NativeOperationKind as Operation};
        const SET_FROM_ARRAY: &[IntrinsicSlot] = &[
            Global(Set),
            Prototype(Set),
            Prototype(Array),
            ArrayIteratorPrototype,
        ];
        self.resolved_native_operation_inputs
            .iter()
            .map(|input| {
                let (site, slots): (Span, Vec<IntrinsicSlot>) = match input.evidence.operation {
                    // The builtin `Symbol` row at the call. A zero-argument
                    // or literal-string call also makes the source unit pure
                    // (`symbol_literal_proof`).
                    NativeInputKind::Operation(Operation::Symbol) => {
                        (input.call_span, vec![Global(Symbol)])
                    }
                    // A fresh Set value-flow origin and Set-content allocation
                    // cell at the constructor, and constructor purity. Later
                    // Set methods and iteration keep their own checks.
                    NativeInputKind::Operation(Operation::SetFromPrimitiveArray) => {
                        (input.call_span, SET_FROM_ARRAY.to_vec())
                    }
                    // Timing, purity and heap ordering of this construction;
                    // the platform result has no ordinary-object certificate.
                    NativeInputKind::Operation(Operation::TextEncoderEmpty) => {
                        (input.call_span, vec![Global(NativeIntrinsic::TextEncoder)])
                    }
                    NativeInputKind::Operation(Operation::MapEmpty) => {
                        (input.call_span, vec![Global(NativeIntrinsic::Map)])
                    }
                    NativeInputKind::Operation(Operation::WeakMapEmpty) => {
                        (input.call_span, vec![Global(WeakMap)])
                    }
                    NativeInputKind::Operation(Operation::SetEmpty) => {
                        (input.call_span, vec![Global(Set)])
                    }
                    // The `Object.freeze` row at that call. A fresh plain-data
                    // literal argument also drops the call's opaque heap
                    // barrier; the returned value gains no value-flow origin.
                    NativeInputKind::CallIdentity(Callee::ObjectFreeze) => (
                        input.call_span,
                        vec![Global(Object), Static(Object, "freeze")],
                    ),
                };
                (
                    site.lo.0,
                    IntrinsicIntact {
                        slots: slots.into_iter().collect(),
                        reference: input.evidence.reference.clone(),
                    },
                )
            })
            .collect()
    }

    pub(crate) fn prepare_semantic_facts(
        &self,
        flow: &ChunkValueFlow,
        shadowed: &BTreeSet<&'static str>,
        shapes: SemanticShapeInputs<'_>,
        source_facts: Option<&Arc<SemanticFacts>>,
    ) -> Arc<SemanticFacts> {
        let SemanticShapeInputs {
            classes,
            sets,
            array_spreads,
            callables,
        } = shapes;
        let mut collection_bindings = BTreeMap::new();
        let constructors = classes
            .classes
            .values()
            .flatten()
            .map(|(binding, _)| binding.clone())
            .chain(
                callables
                    .namespace_wrappers
                    .values()
                    .map(|candidate| candidate.weak_map.clone()),
            );
        for binding in constructors {
            let Some(row) = builtins::lookup(binding.0.as_ref(), None, Invocation::Construct)
            else {
                continue;
            };
            if row.allocates.is_some_and(Kind::is_keyed_collection) {
                collection_bindings.insert(binding, row);
            }
        }
        // Rules read binding identity only for these candidate callees.
        let declared_bindings = collection_bindings
            .keys()
            .chain(
                callables
                    .namespace_wrappers
                    .values()
                    .map(|candidate| &candidate.object),
            )
            .filter(|binding| flow.is_declared(binding))
            .cloned()
            .collect();
        let completed = self.completed_native_rules();
        Arc::new(SemanticFacts::solve_with_source(
            SemanticRules {
                classes,
                sets,
                array_spreads: array_spreads.by_initializer.keys().copied().collect(),
                collection_bindings,
                native: NativePremises {
                    bindings: NativeBindingContext {
                        declared_bindings,
                        shadowed_globals: shadowed.clone(),
                        global_object_names: self.global_object_names.clone(),
                    },
                    ..completed.native
                },
                descriptor_copy_providers: self.descriptor_copy_factories.clone(),
                descriptor_copy_calls: self
                    .descriptor_copy_calls
                    .iter()
                    .filter_map(|(site, candidate)| {
                        let shape = self.own_descriptor_shapes.get(&candidate.source_object)?;
                        let data_keys = shape
                            .properties
                            .iter()
                            .filter_map(|(key, descriptor)| {
                                matches!(descriptor, OwnPropertyDescriptorSyntax::Data)
                                    .then_some(key.clone())
                            })
                            .collect();
                        let accessor_keys = shape
                            .properties
                            .iter()
                            .filter_map(|(key, descriptor)| {
                                matches!(descriptor, OwnPropertyDescriptorSyntax::Accessor { .. })
                                    .then_some(key.clone())
                            })
                            .collect();
                        Some((
                            site.source_span().lo.0,
                            SemanticDescriptorCopyCallCandidate {
                                source_object: candidate.source_object,
                                provider_binding: candidate.provider_binding.clone(),
                                signature: candidate.signature.clone(),
                                data_keys,
                                accessor_keys,
                            },
                        ))
                    })
                    .collect(),
                callables,
                dynamic_scope: completed.dynamic_scope,
                ..SemanticRules::default()
            },
            source_facts,
        ))
    }

    pub(super) fn validate_value_inputs(
        &mut self,
        selected: &[SelectedBindingValueInput],
        resolved: &[ResolvedBindingValueInput],
        raw_aliases: &BTreeMap<Id, NativeReceiverRoots>,
        declared: &BTreeSet<Id>,
    ) {
        for input in selected {
            let request = &input.evidence.spec_location;
            if !self.dynamic_scope_owners.is_empty() {
                self.value_input_errors.push(format!(
                    "{request} cannot refine `{}` because provider analysis contains dynamic-scope contamination",
                    input.selected_binding
                ));
                continue;
            }
            let candidates: Vec<_> = self
                .binding_declaration_writes
                .iter()
                .filter(|(binding, _)| binding.0.as_ref() == input.selected_binding)
                .flat_map(|(binding, writes)| {
                    writes.iter().filter_map(move |write| {
                        (write.simple_identifier
                            && write.direct_top_level
                            && input.selected_statement_span.contains(write.binding_span))
                        .then_some((binding.clone(), write.clone()))
                    })
                })
                .collect();
            let [(binding, declaration)] = candidates.as_slice() else {
                self.value_input_errors.push(format!(
                    "{request}.source_match selected `{}`, but the canonical execution index found {} direct simple top-level declarations in that source item; exactly one is required",
                    input.selected_binding,
                    candidates.len()
                ));
                continue;
            };
            let binding = (*binding).clone();
            let declaration = (*declaration).clone();
            if self
                .binding_declaration_writes
                .get(&binding)
                .is_none_or(|writes| writes.len() != 1)
            {
                self.value_input_errors.push(format!(
                    "{request} cannot refine `{}` because its canonical binding has repeated or pattern declaration sites",
                    input.selected_binding
                ));
                continue;
            }
            if self
                .property_key_assignments
                .get(&binding)
                .is_none_or(|writes| writes.len() != 1)
            {
                self.value_input_errors.push(format!(
                    "{request} cannot refine `{}` because the canonical write index found a possible reassignment, update, destructuring write, or deferred write",
                    input.selected_binding
                ));
                continue;
            }
            let Some(initializer) = declaration.initializer.as_ref() else {
                self.value_input_errors.push(format!(
                    "{request} cannot refine `{}` because the selected declaration has no initializer",
                    input.selected_binding
                ));
                continue;
            };
            let check_known_identity =
                matches!(initializer, ValueExpr::Binding(_) | ValueExpr::Member(..));
            if check_known_identity {
                let mut roots = intrinsic_roots(
                    initializer,
                    raw_aliases,
                    &self.census_globals,
                    declared,
                    &BTreeMap::new(),
                    &CallableReturnInputs::default(),
                );
                roots.remove(&NativeReceiverRoot::Intrinsic(
                    NativeIntrinsic::UnknownGlobalValue,
                ));
                let contradictions: BTreeSet<_> = roots
                    .iter()
                    .filter_map(NativeReceiverRoot::intrinsic_root)
                    .collect();
                if !contradictions.is_empty() {
                    self.value_input_errors.push(format!(
                        "{request} contradicts recognized native/global identity for `{}`: {contradictions:?}",
                        input.selected_binding
                    ));
                    continue;
                }
            }
            if !is_opaque_member_slot(initializer) {
                self.value_input_errors.push(format!(
                    "{request} uses an unsupported initializer category for `{}`; this version accepts only opaque binding-rooted member reads",
                    input.selected_binding
                ));
                continue;
            }
            let raw_roots = intrinsic_roots(
                initializer,
                raw_aliases,
                &self.census_globals,
                declared,
                &BTreeMap::new(),
                &CallableReturnInputs::default(),
            );
            if !raw_roots.iter().any(|root| {
                matches!(
                    root,
                    NativeReceiverRoot::Intrinsic(NativeIntrinsic::UnknownGlobalValue)
                )
            }) {
                self.value_input_errors.push(format!(
                    "{request} cannot refine `{}` because source-only analysis did not establish an existing unknown-global receiver root",
                    input.selected_binding
                ));
                continue;
            }
            if self.ordinary_input_refs.contains_key(&binding) {
                self.value_input_errors.push(format!(
                    "{request} duplicates a value input for canonical binding `{}`",
                    input.selected_binding
                ));
                continue;
            }
            let Some(initializer_span) = declaration.initializer_span else {
                self.value_input_errors.push(format!(
                    "{request} cannot refine `{}` because its initializer has no source span",
                    input.selected_binding
                ));
                continue;
            };
            let mut evidence = input.evidence.clone();
            evidence.selected_binding = input.selected_binding.clone();
            self.ordinary_input_refs.insert(
                binding.clone(),
                (evidence.reference.clone(), declaration.owner),
            );
            self.value_input_evidence
                .insert(evidence.reference.clone(), evidence.clone());
            self.resolved_value_inputs.push(ResolvedBindingValueInput {
                evidence,
                binding,
                declaration_span: declaration.binding_span,
                initializer_span,
                initializer_ordinal: declaration.owner,
            });
        }
        for input in resolved {
            let request = &input.evidence.spec_location;
            if !self.dynamic_scope_owners.is_empty() {
                self.value_input_errors.push(format!(
                    "{request} cannot refine `{}` because provider analysis contains dynamic-scope contamination",
                    input.evidence.selected_binding
                ));
                continue;
            }
            let Some(writes) = self.binding_declaration_writes.get(&input.binding) else {
                self.value_input_errors.push(format!(
                    "{request} no longer resolves to one canonical declaration in this prepared AST"
                ));
                continue;
            };
            let [declaration] = writes.as_slice() else {
                self.value_input_errors.push(format!(
                    "{request} no longer resolves to one canonical declaration in this prepared AST"
                ));
                continue;
            };
            if !declaration.direct_top_level
                || !declaration.simple_identifier
                || declaration.binding_span != input.declaration_span
                || declaration.initializer_span != Some(input.initializer_span)
                || declaration.owner != input.initializer_ordinal
                || input.binding.0.as_ref() != input.evidence.selected_binding
                || self
                    .property_key_assignments
                    .get(&input.binding)
                    .is_none_or(|writes| writes.len() != 1)
            {
                self.value_input_errors.push(format!(
                    "{request} no longer identifies the same stable simple top-level binding and complete write summary"
                ));
                continue;
            }
            let Some(initializer) = declaration.initializer.as_ref() else {
                self.value_input_errors.push(format!(
                    "{request} no longer has the source initializer used by the value input"
                ));
                continue;
            };
            if !is_opaque_member_slot(initializer) {
                self.value_input_errors.push(format!(
                    "{request} now uses an unsupported initializer category; expected a binding-rooted opaque member read"
                ));
                continue;
            }
            let roots = intrinsic_roots(
                initializer,
                raw_aliases,
                &self.census_globals,
                declared,
                &BTreeMap::new(),
                &CallableReturnInputs::default(),
            );
            let contradiction = roots
                .iter()
                .filter_map(NativeReceiverRoot::intrinsic_root)
                .any(|root| root != NativeIntrinsic::UnknownGlobalValue);
            if contradiction {
                self.value_input_errors.push(format!(
                    "{request} contradicts a recognized native/global initializer in this prepared AST"
                ));
                continue;
            }
            if !roots.contains(&NativeReceiverRoot::Intrinsic(
                NativeIntrinsic::UnknownGlobalValue,
            )) {
                self.value_input_errors.push(format!(
                    "{request} cannot refine this binding because source-only analysis did not establish an existing unknown-global receiver root"
                ));
                continue;
            }
            if self.ordinary_input_refs.contains_key(&input.binding) {
                self.value_input_errors.push(format!(
                    "{request} duplicates a captured value input for canonical binding `{}`",
                    input.evidence.selected_binding
                ));
                continue;
            }
            self.ordinary_input_refs.insert(
                input.binding.clone(),
                (input.evidence.reference.clone(), input.initializer_ordinal),
            );
            self.value_input_evidence
                .insert(input.evidence.reference.clone(), input.evidence.clone());
            self.resolved_value_inputs.push(input.clone());
        }
    }

    pub(super) fn validate_native_operation_inputs(
        &mut self,
        selected: &[SelectedNativeOperationInput],
        resolved: &[ResolvedNativeOperationInput],
    ) {
        if selected.is_empty() && resolved.is_empty() {
            return;
        }
        if !self.dynamic_scope_owners.is_empty() {
            for evidence in selected
                .iter()
                .map(|input| &input.evidence)
                .chain(resolved.iter().map(|input| &input.evidence))
            {
                self.native_operation_input_errors.push(format!(
                    "{} cannot assert native operation identity because provider analysis contains dynamic-scope contamination",
                    evidence.spec_location
                ));
            }
            return;
        }
        for input in selected {
            let request = &input.evidence.spec_location;
            if let Some(error) = self.native_operation_mutation_error(&input.evidence) {
                self.native_operation_input_errors.push(error);
                continue;
            }
            if input.evidence.operation
                == NativeInputKind::CallIdentity(spec::NativeCallIdentityKind::ObjectFreeze)
            {
                let candidates: Vec<_> = self
                    .hoisted_bindings
                    .keys()
                    .filter(|binding| binding.0.as_ref() == input.selected_binding)
                    .filter(|binding| {
                        self.hoisted_bindings
                            .get(*binding)
                            .and_then(|key| self.callable_spans.get(key))
                            .is_some_and(|span| input.selected_statement_span.contains(*span))
                    })
                    .collect();
                let [binding] = candidates.as_slice() else {
                    self.native_operation_input_errors.push(format!(
                        "{request} must select exactly one direct top-level function declaration"
                    ));
                    continue;
                };
                let Some(call) =
                    self.direct_freeze_return_call(binding, input.selected_statement_span)
                else {
                    self.native_operation_input_errors.push(format!(
                        "{request} requires a nonasync top-level function whose sole statement directly returns one unshadowed Object.freeze call"
                    ));
                    continue;
                };
                let mut evidence = input.evidence.clone();
                evidence.selected_binding = input.selected_binding.clone();
                self.native_operation_input_evidence
                    .insert(call.span.into(), evidence.clone());
                self.resolved_native_operation_inputs
                    .push(ResolvedNativeOperationInput {
                        evidence,
                        binding: (*binding).clone(),
                        selected_statement_span: input.selected_statement_span,
                        declaration_span: self.callable_spans[&self.hoisted_bindings[*binding]],
                        initializer_span: call.span,
                        call_span: call.span,
                        initializer_ordinal: call.owner,
                    });
                continue;
            }
            let candidates: Vec<_> = self
                .binding_declaration_writes
                .iter()
                .filter(|(binding, _)| binding.0.as_ref() == input.selected_binding)
                .flat_map(|(binding, writes)| {
                    writes.iter().filter_map(move |write| {
                        (write.simple_identifier
                            && write.direct_top_level
                            && input.selected_statement_span.contains(write.binding_span))
                        .then_some((binding.clone(), write.clone()))
                    })
                })
                .collect();
            let [(binding, declaration)] = candidates.as_slice() else {
                self.native_operation_input_errors.push(format!(
                    "{request}.source_match selected `{}`, but the canonical execution index found {} direct simple top-level declarations in that source item; exactly one is required",
                    input.selected_binding,
                    candidates.len()
                ));
                continue;
            };
            if matches!(
                input.evidence.operation,
                NativeInputKind::Operation(spec::NativeOperationKind::SetFromPrimitiveArray)
            ) {
                let Some(ValueExpr::New(callee, _, call_site)) = declaration.initializer.as_ref()
                else {
                    self.native_operation_input_errors.push(format!(
                        "{request} must select a direct supported Set constructor initializer"
                    ));
                    continue;
                };
                let ValueExpr::Binding(constructor) = callee.as_ref() else {
                    self.native_operation_input_errors.push(format!(
                        "{request} must select a direct uncomputed Set identifier"
                    ));
                    continue;
                };
                if !self.is_unshadowed_primitive_set_initializer(declaration, constructor) {
                    self.native_operation_input_errors
                        .push(format!(
                            "{request} requires a direct unshadowed new Set([primitive literals or earlier stable primitive bindings]) operation with one dense nonspread array literal"
                        ));
                    continue;
                }
                let Some(call) = self.top_calls[declaration.owner.0]
                    .iter()
                    .find(|call| CallSite::from(call.span) == *call_site)
                else {
                    self.native_operation_input_errors.push(format!(
                        "{request} no longer resolves to a direct top-level constructor site"
                    ));
                    continue;
                };
                if call.kind != CallKind::New || call.has_spread || call.caller.is_some() {
                    self.native_operation_input_errors.push(format!(
                        "{request} must select an ordinary direct nonspread constructor site"
                    ));
                    continue;
                }
                let mut evidence = input.evidence.clone();
                evidence.selected_binding = input.selected_binding.clone();
                self.native_operation_input_evidence
                    .insert(*call_site, evidence.clone());
                self.resolved_native_operation_inputs
                    .push(ResolvedNativeOperationInput {
                        evidence,
                        binding: binding.clone(),
                        selected_statement_span: input.selected_statement_span,
                        declaration_span: declaration.binding_span,
                        initializer_span: declaration.initializer_span.unwrap_or(call.span),
                        call_span: call.span,
                        initializer_ordinal: declaration.owner,
                    });
                continue;
            }
            let binding = (*binding).clone();
            let declaration = (*declaration).clone();
            if declaration.initializer.is_none() {
                self.native_operation_input_errors.push(format!(
                    "{request} selected `{}` without an initializer",
                    input.selected_binding
                ));
                continue;
            }
            let Some(initializers) =
                native_operation_initializers(&declaration, input.evidence.operation)
            else {
                let object_shape = if input.evidence.operation
                    == NativeInputKind::Operation(spec::NativeOperationKind::MapEmpty)
                {
                    ", or an object containing only direct empty Map data fields"
                } else {
                    ""
                };
                self.native_operation_input_errors.push(format!(
                    "{request} must select a direct top-level binding initialized by a {}{object_shape}",
                    native_operation_call_name(input.evidence.operation)
                ));
                continue;
            };
            if declaration.empty_map_object_sites.is_some()
                && self.binding_declaration_writes[&binding].len() != 1
            {
                self.native_operation_input_errors.push(format!(
                    "{request} must select one unambiguous canonical object binding declaration"
                ));
                continue;
            }
            for (callee, args, call_site) in initializers {
                let call = match self.direct_native_initializer_call(
                    input.evidence.operation,
                    &declaration,
                    call_site,
                ) {
                    Ok(call) => call,
                    Err(reason) => {
                        self.native_operation_input_errors
                            .push(format!("{request} {reason}"));
                        continue;
                    }
                };
                if let Some(reason) = self.native_operation_call_shape_error(
                    input.evidence.operation,
                    callee,
                    args,
                    call_site,
                    call,
                ) {
                    self.native_operation_input_errors
                        .push(format!("{request} {reason}"));
                    continue;
                }
                let call_span = call.span;
                let mut evidence = input.evidence.clone();
                evidence.selected_binding = input.selected_binding.clone();
                self.native_operation_input_evidence
                    .insert(call_site, evidence.clone());
                self.resolved_native_operation_inputs
                    .push(ResolvedNativeOperationInput {
                        evidence,
                        binding: binding.clone(),
                        selected_statement_span: input.selected_statement_span,
                        declaration_span: declaration.binding_span,
                        initializer_span: declaration.initializer_span.unwrap_or(call_span),
                        call_span,
                        initializer_ordinal: declaration.owner,
                    });
            }
        }

        for input in resolved {
            let request = &input.evidence.spec_location;
            if let Some(error) = self.native_operation_mutation_error(&input.evidence) {
                self.native_operation_input_errors.push(error);
                continue;
            }
            if input.evidence.operation
                == NativeInputKind::CallIdentity(spec::NativeCallIdentityKind::ObjectFreeze)
            {
                let is_same = self
                    .direct_freeze_return_call(&input.binding, input.selected_statement_span)
                    .is_some_and(|call| {
                        call.span == input.call_span
                            && call.owner == input.initializer_ordinal
                            && input.declaration_span
                                == self.callable_spans[&self.hoisted_bindings[&input.binding]]
                            && input.initializer_span == call.span
                            && input.binding.0.as_ref() == input.evidence.selected_binding
                    });
                if !is_same {
                    self.native_operation_input_errors.push(format!(
                        "{request} no longer identifies the same direct unshadowed Object.freeze return call"
                    ));
                    continue;
                }
                self.native_operation_input_evidence
                    .insert(input.call_span.into(), input.evidence.clone());
                self.resolved_native_operation_inputs.push(input.clone());
                continue;
            }
            let Some([declaration]) = self
                .binding_declaration_writes
                .get(&input.binding)
                .map(Vec::as_slice)
            else {
                self.native_operation_input_errors.push(format!(
                    "{request} no longer resolves to one canonical binding declaration"
                ));
                continue;
            };
            if matches!(
                input.evidence.operation,
                NativeInputKind::Operation(spec::NativeOperationKind::SetFromPrimitiveArray)
            ) {
                let Some(ValueExpr::New(callee, _, call_site)) = declaration.initializer.as_ref()
                else {
                    self.native_operation_input_errors.push(format!(
                        "{request} no longer selects a direct Set constructor initializer"
                    ));
                    continue;
                };
                let is_same = matches_resolved_native_initializer(input, declaration, *call_site)
                    && matches!(callee.as_ref(), ValueExpr::Binding(constructor)
                        if self.is_unshadowed_primitive_set_initializer(declaration, constructor));
                if !is_same {
                    self.native_operation_input_errors.push(format!(
                        "{request} no longer identifies the same direct unshadowed Set initializer"
                    ));
                    continue;
                }
                self.native_operation_input_evidence
                    .insert(*call_site, input.evidence.clone());
                self.resolved_native_operation_inputs.push(input.clone());
                continue;
            }
            let Some((callee, args, call_site)) =
                native_operation_initializers(declaration, input.evidence.operation).and_then(
                    |initializers| {
                        initializers
                            .into_iter()
                            .find(|(_, _, site)| *site == CallSite::from(input.call_span))
                    },
                )
            else {
                self.native_operation_input_errors.push(format!(
                    "{request} no longer selects the same direct native initializer site"
                ));
                continue;
            };
            let call = match self.direct_native_initializer_call(
                input.evidence.operation,
                declaration,
                call_site,
            ) {
                Ok(call) => call,
                Err(reason) => {
                    self.native_operation_input_errors
                        .push(format!("{request} {reason}"));
                    continue;
                }
            };
            if let Some(reason) = self.native_operation_call_shape_error(
                input.evidence.operation,
                callee,
                args,
                call_site,
                call,
            ) {
                self.native_operation_input_errors.push(format!(
                    "{request} no longer identifies the selected operation: {reason}"
                ));
                continue;
            }
            let is_same = matches_resolved_native_initializer(input, declaration, call_site);
            if !is_same {
                self.native_operation_input_errors.push(format!(
                    "{request} no longer identifies the same direct unshadowed native initializer"
                ));
                continue;
            }
            self.native_operation_input_evidence
                .insert(call_site, input.evidence.clone());
            self.resolved_native_operation_inputs.push(input.clone());
        }

        let original_len = self.resolved_native_operation_inputs.len();
        let mut references = BTreeMap::new();
        let mut sites = BTreeSet::new();
        self.resolved_native_operation_inputs.retain(|input| {
            let same_initializer = references.get(&input.evidence.reference).is_none_or(
                |previous: &ResolvedNativeOperationInput| {
                    input.evidence.operation
                        == NativeInputKind::Operation(spec::NativeOperationKind::MapEmpty)
                        && previous.evidence.operation == input.evidence.operation
                        && previous.binding == input.binding
                        && previous.selected_statement_span == input.selected_statement_span
                        && previous.declaration_span == input.declaration_span
                        && previous.initializer_span == input.initializer_span
                        && previous.initializer_ordinal == input.initializer_ordinal
                        && input.initializer_span != input.call_span
                },
            );
            if !same_initializer || !sites.insert(CallSite::from(input.call_span)) {
                return false;
            }
            references.insert(input.evidence.reference.clone(), input.clone());
            true
        });
        if self.resolved_native_operation_inputs.len() != original_len {
            self.native_operation_input_errors.push(
                "native_operation_inputs contain conflicting assertion references or duplicate call sites"
                    .to_owned(),
            );
        }
    }

    pub(super) fn native_operation_mutation_error(
        &self,
        evidence: &NativeOperationInputEvidence,
    ) -> Option<String> {
        let request = &evidence.spec_location;
        let operation = evidence.operation;
        match operation {
            NativeInputKind::CallIdentity(spec::NativeCallIdentityKind::ObjectFreeze) => {
                let owner = self.known_object_freeze_mutation()?;
                Some(format!(
                    "{request} is contradicted by a positively identified required Object.freeze slot write in source unit {}",
                    owner.0
                ))
            }
            NativeInputKind::Operation(spec::NativeOperationKind::SetFromPrimitiveArray) => {
                let owner = self.known_set_protocol_mutation()?;
                Some(format!(
                    "{request} is contradicted by a positively identified required native slot write in source unit {}",
                    owner.0
                ))
            }
            _ => {
                let (name, owner) = self.known_global_native_operation_mutation(operation)?;
                Some(format!(
                    "{request} is contradicted by a positively identified global {name} replacement or accessor installation (source unit {})",
                    owner.0
                ))
            }
        }
    }

    pub(super) fn is_unshadowed_primitive_set_initializer(
        &self,
        declaration: &BindingDeclarationWrite,
        constructor: &Id,
    ) -> bool {
        self.has_stable_primitive_set_array(declaration)
            && constructor.0.as_ref() == "Set"
            && !self.property_key_declarations.contains(constructor)
            && !self.imported_bindings.contains(constructor)
    }

    pub(super) fn direct_native_initializer_call(
        &self,
        operation: NativeInputKind,
        declaration: &BindingDeclarationWrite,
        site: CallSite,
    ) -> Result<&Call, &'static str> {
        let call = self.top_calls[declaration.owner.0]
            .iter()
            .find(|call| CallSite::from(call.span) == site)
            .ok_or("no longer resolves to a direct top-level call site")?;
        if operation.empty_constructor_name().is_some()
            && declaration.empty_map_object_sites.is_none()
            && declaration.initializer_span != Some(call.span)
        {
            return Err(
                "must select the direct constructor initializer without a sequence or assignment wrapper",
            );
        }
        Ok(call)
    }

    pub(super) fn source_slot_overlap(&self, slot: &NativeWriteSlot) -> NativeSlotOverlap {
        source_slot_overlap(
            self.native_write_census
                .witnesses
                .iter()
                .chain(
                    self.native_write_census
                        .conditional
                        .iter()
                        .map(|write| &write.witness),
                )
                .chain(self.native_integrity_witnesses.iter()),
            slot,
        )
    }

    pub(super) fn native_operation_call_shape_error(
        &self,
        operation: NativeInputKind,
        callee: &ValueExpr,
        args: &[ValueExpr],
        call_site: CallSite,
        call: &Call,
    ) -> Option<&'static str> {
        if let Some(constructor) = operation.empty_constructor_name() {
            if call.kind != CallKind::New
                || call.has_spread
                || call.caller.is_some()
                || !args.is_empty()
                || !self.bare_constructor_sites.contains(&call_site)
            {
                return Some("must select direct zero-argument native construction without spread");
            }
            let ValueExpr::Binding(binding) = callee else {
                return Some("must select the direct bare global constructor");
            };
            if binding.0.as_ref() != constructor || self.declared_bindings().contains(binding) {
                return Some(
                    "must select the original unshadowed global constructor binding matching the requested operation",
                );
            }
            return None;
        }
        if call.kind != CallKind::Call || call.has_spread || call.caller.is_some() {
            return Some(match operation {
                NativeInputKind::Operation(spec::NativeOperationKind::Symbol) => {
                    "must select a nonoptional, nonspread, direct top-level Symbol call"
                }
                _ => "must select an ordinary direct top-level native call",
            });
        }
        if !self.ordinary_top_call_sites.contains(&call_site) {
            return Some("must select an ordinary direct top-level call");
        }
        match operation {
            NativeInputKind::Operation(spec::NativeOperationKind::Symbol) => {
                let ValueExpr::Binding(symbol) = callee else {
                    return Some("must select a direct uncomputed Symbol identifier call");
                };
                if symbol.0.as_ref() != "Symbol" {
                    return Some("operation does not match the selected direct Symbol initializer");
                }
                if self.declared_bindings().contains(symbol) {
                    return Some(
                        "selects a shadowed Symbol binding; only the original unshadowed global Symbol binding is supported",
                    );
                }
                if !self.primitive_native_symbol_calls.contains(&call_site) || args.len() > 1 {
                    return Some(
                        "has an unsupported Symbol call shape; use one ordinary direct call with zero or one syntactically primitive argument and no spread",
                    );
                }
                let Some((row, _)) = native_call_identity_key(CallKind::Call, callee) else {
                    return Some("does not resolve through the exact native Symbol call row");
                };
                (row.family() == Some(NativeIntrinsic::Symbol))
                    .then_some(())
                    .is_none()
                    .then_some("selected an unsupported native Symbol call row")
            }
            _ => Some("operation does not match the selected native call"),
        }
    }

    pub(super) fn known_global_native_operation_mutation(
        &self,
        operation: NativeInputKind,
    ) -> Option<(&'static str, SourceUnitOrdinal)> {
        let name = match operation {
            NativeInputKind::Operation(spec::NativeOperationKind::Symbol) => "Symbol",
            _ => operation.empty_constructor_name()?,
        };
        match self.source_slot_overlap(&NativeWriteSlot {
            receiver: NativeReceiverPath::GlobalObject,
            key: NativePropertyKey::String(name.to_owned()),
        }) {
            NativeSlotOverlap::Known(owner) => Some((name, owner)),
            NativeSlotOverlap::Opaque | NativeSlotOverlap::Absent => None,
        }
    }

    pub(super) fn known_object_freeze_mutation(&self) -> Option<SourceUnitOrdinal> {
        [
            NativeWriteSlot {
                receiver: NativeReceiverPath::GlobalObject,
                key: NativePropertyKey::String("Object".to_owned()),
            },
            NativeWriteSlot {
                receiver: NativeReceiverPath::Constructor(NativeIntrinsic::Object),
                key: NativePropertyKey::String("freeze".to_owned()),
            },
        ]
        .iter()
        .find_map(|slot| match self.source_slot_overlap(slot) {
            NativeSlotOverlap::Known(owner) => Some(owner),
            NativeSlotOverlap::Opaque | NativeSlotOverlap::Absent => None,
        })
    }

    pub(super) fn known_descriptor_copy_mutation(
        &self,
        assigned_keys: &BTreeSet<String>,
    ) -> Option<SourceUnitOrdinal> {
        let slots = ["Object"]
            .into_iter()
            .map(|key| NativeWriteSlot {
                receiver: NativeReceiverPath::GlobalObject,
                key: NativePropertyKey::String(key.to_owned()),
            })
            .chain(
                [
                    "getOwnPropertyDescriptors",
                    "defineProperties",
                    "assign",
                    "prototype",
                ]
                .into_iter()
                .map(|key| NativeWriteSlot {
                    receiver: NativeReceiverPath::Constructor(NativeIntrinsic::Object),
                    key: NativePropertyKey::String(key.to_owned()),
                }),
            )
            .chain(assigned_keys.iter().map(|key| NativeWriteSlot {
                receiver: NativeReceiverPath::Prototype(NativeIntrinsic::Object),
                key: NativePropertyKey::String(key.clone()),
            }));
        slots
            .into_iter()
            .find_map(|slot| match self.source_slot_overlap(&slot) {
                NativeSlotOverlap::Known(owner) => Some(owner),
                NativeSlotOverlap::Opaque | NativeSlotOverlap::Absent => None,
            })
    }

    pub(crate) fn direct_freeze_return_call(
        &self,
        binding: &Id,
        selected_span: Span,
    ) -> Option<Call> {
        if !self.direct_top_level_functions.contains(binding) {
            return None;
        }
        let key = *self.hoisted_bindings.get(binding)?;
        let declaration = *self.callable_spans.get(&key)?;
        if !selected_span.contains(declaration) || self.async_callables.contains(&key) {
            return None;
        }
        let callable = self.callables.get(&key)?;
        if callable.generator {
            return None;
        }
        let site = CallSite::from(*self.direct_return_calls.get(binding)?);
        let call = callable
            .calls
            .iter()
            .find(|call| call.kind == CallKind::Call && CallSite::from(call.span) == site)?;
        if call.kind != CallKind::Call
            || call.caller != Some(key)
            || call.has_spread
            || CallSite::from(call.span) != site
            || call.args.len() != 1
        {
            return None;
        }
        let ValueExpr::Member(receiver, Some(member), MemberKey::Static, _) = &call.callee else {
            return None;
        };
        let ValueExpr::Binding(object) = receiver.as_ref() else {
            return None;
        };
        if object.0.as_ref() != "Object"
            || member != "freeze"
            || self.declared_bindings().contains(object)
        {
            return None;
        }
        let (row, _) = native_call_identity_key(CallKind::Call, &call.callee)?;
        (row.op == IntrinsicOp::ObjectFreeze).then(|| call.as_ref().clone())
    }

    pub(super) fn known_array_iterator_protocol_mutation_with(
        &self,
        integrity: &ArraySpreadProtocolIntegrity,
    ) -> Option<SourceUnitOrdinal> {
        [
            NativeWriteSlot {
                receiver: NativeReceiverPath::Prototype(NativeIntrinsic::Array),
                key: NativePropertyKey::SymbolIterator,
            },
            NativeWriteSlot {
                receiver: NativeReceiverPath::ArrayIteratorPrototype,
                key: NativePropertyKey::String("next".to_owned()),
            },
        ]
        .iter()
        .find_map(|slot| {
            match array_spread_protocol_slot_overlap(
                self.native_write_census.witnesses.iter().chain(
                    self.native_write_census
                        .conditional
                        .iter()
                        .map(|write| &write.witness),
                ),
                slot,
                &integrity.mutated,
                integrity.global_array_binding_mutated,
                &integrity.premise_authorized_next_writes,
                ArraySpreadNativeIteratorPremise,
            ) {
                NativeSlotOverlap::Known(owner) => Some(owner),
                NativeSlotOverlap::Opaque | NativeSlotOverlap::Absent => None,
            }
        })
    }

    pub(super) fn known_set_protocol_mutation(&self) -> Option<SourceUnitOrdinal> {
        [
            NativeWriteSlot {
                receiver: NativeReceiverPath::GlobalObject,
                key: NativePropertyKey::String("Set".to_owned()),
            },
            NativeWriteSlot {
                receiver: NativeReceiverPath::Prototype(NativeIntrinsic::Set),
                key: NativePropertyKey::String("add".to_owned()),
            },
            NativeWriteSlot {
                receiver: NativeReceiverPath::Prototype(NativeIntrinsic::Array),
                key: NativePropertyKey::SymbolIterator,
            },
            NativeWriteSlot {
                receiver: NativeReceiverPath::ArrayIteratorPrototype,
                key: NativePropertyKey::String("next".to_owned()),
            },
        ]
        .iter()
        .find_map(|slot| match self.source_slot_overlap(slot) {
            NativeSlotOverlap::Known(owner) => Some(owner),
            NativeSlotOverlap::Opaque | NativeSlotOverlap::Absent => None,
        })
    }

    pub(super) fn validate_captured_callable_returns(
        &mut self,
        assignments: &[&(Id, ValueExpr)],
        flow: Option<&ChunkValueFlow>,
        raw_aliases: &BTreeMap<Id, NativeReceiverRoots>,
    ) {
        if self.resolved_value_inputs.is_empty() {
            return;
        }
        let closure = close_receiver_flow(self, assignments, flow, raw_aliases);
        let mut rejected = Vec::new();
        for input in &self.resolved_value_inputs {
            let unsupported = self.callables.iter().any(|(key, callable)| {
                callable.reads.contains(&input.binding)
                    && (callable.generator
                        || self.async_callables.contains(key)
                        || closure
                            .facts
                            .get(&ReceiverFlowKey::Callable(*key))
                            .is_none_or(|fact| {
                                fact.outcomes.is_empty()
                                    || fact.outcomes.contains(&CallableReturnOutcome::Unknown)
                            }))
            });
            if unsupported {
                rejected.push((
                    input.binding.clone(),
                    input.evidence.reference.clone(),
                    format!(
                        "{} cannot refine `{}` because a capturing synchronous return path is unsupported, unresolved, or opaque",
                        input.evidence.spec_location, input.evidence.selected_binding
                    ),
                ));
            }
        }
        for (binding, reference, message) in rejected {
            self.value_input_errors.push(message);
            self.ordinary_input_refs.remove(&binding);
            self.value_input_evidence.remove(&reference);
            self.resolved_value_inputs
                .retain(|input| input.binding != binding);
        }
    }

    pub(super) fn stable_top_level_copy_owner(
        &self,
        binding: &Id,
        initializer: &ValueExpr,
    ) -> Option<SourceUnitOrdinal> {
        if !matches!(initializer, ValueExpr::Binding(_))
            || self
                .property_key_assignments
                .get(binding)
                .is_none_or(|writes| writes.len() != 1)
        {
            return None;
        }
        let [declaration] = self.binding_declaration_writes.get(binding)?.as_slice() else {
            return None;
        };
        let same_direct_binding = matches!(
            (declaration.initializer.as_ref(), initializer),
            (Some(ValueExpr::Binding(declared)), ValueExpr::Binding(candidate))
                if declared == candidate
        );
        (declaration.direct_top_level && declaration.simple_identifier && same_direct_binding)
            .then_some(declaration.owner)
    }
}
