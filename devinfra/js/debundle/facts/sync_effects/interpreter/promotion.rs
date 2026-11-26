//! Effect and reachability promotion: lazy owners, top effects, fact roots, property writes and possible getters.

use super::*;

impl<'source, 'rule> Executor<'source, 'rule> {
    pub(super) fn all_owners(&self) -> impl Iterator<Item = SourceUnitOrdinal> + '_ {
        self.inputs.facts.iter().map(|fact| fact.ordinal)
    }

    pub(crate) fn promote_lazy_owner(
        &self,
        owner: SourceUnitOrdinal,
        effect_owner: SourceUnitOrdinal,
        outcome: &mut Outcome,
    ) {
        let (reads, rebinds) = self
            .inputs
            .synchronous_top_effects(owner, self.state.world.reachability.incomplete);
        self.promote_top_effects(reads, rebinds, effect_owner, outcome);
    }

    /// Attributes `reads` and `rebinds` to `effect_owner`, assumed when a
    /// conservative path is in force.
    pub(super) fn promote_lazy_effects(
        &self,
        reads: &BTreeSet<Id>,
        rebinds: &BTreeSet<Id>,
        effect_owner: SourceUnitOrdinal,
        outcome: &mut Outcome,
    ) {
        let promoted_reads = self.top_restricted(reads);
        let promoted_rebinds = self.top_restricted(rebinds);
        self.promote_top_effects(&promoted_reads, &promoted_rebinds, effect_owner, outcome);
    }

    /// The top-level bindings among sorted `ids`, in order.
    pub(super) fn top_restricted<'a>(&self, ids: &'a BTreeSet<Id>) -> Vec<&'a Id> {
        ids.iter()
            .filter(|id| self.inputs.top_bindings.contains(*id))
            .collect()
    }

    /// `promote_lazy_effects` for sorted, distinct top-level `promoted_reads`
    /// and `promoted_rebinds`.
    pub(crate) fn promote_top_effects(
        &self,
        promoted_reads: &[&Id],
        promoted_rebinds: &[&Id],
        effect_owner: SourceUnitOrdinal,
        outcome: &mut Outcome,
    ) {
        if self.promotes_nothing(promoted_reads, promoted_rebinds, effect_owner, outcome) {
            return;
        }
        let uninitialized: Vec<&Id> = promoted_reads
            .iter()
            .copied()
            .filter(|id| !self.state.world.module.initialized_bindings.contains(*id))
            .collect();
        let effects = &mut outcome.effects;
        let assumed = &mut effects.fallback_only;
        match self.state.assumption.as_ref() {
            Some(reason) => {
                let held = HeldBindings::ByOwner(&effects.reads);
                note_assumed(&mut assumed.reads, promoted_reads, held, reason);
                let held = HeldBindings::Plain(&effects.uninitialized_reads);
                note_assumed(
                    &mut assumed.uninitialized_reads,
                    &uninitialized,
                    held,
                    reason,
                );
                let held = HeldBindings::ByOwner(&effects.rebinds);
                note_assumed(&mut assumed.rebinds, promoted_rebinds, held, reason);
            }
            None => {
                note_observed(&mut assumed.reads, promoted_reads);
                note_observed(&mut assumed.uninitialized_reads, &uninitialized);
                note_observed(&mut assumed.rebinds, promoted_rebinds);
            }
        }
        insert_effects(&mut effects.reads, promoted_reads, |id| {
            (id.clone(), effect_owner)
        });
        insert_effects(&mut effects.uninitialized_reads, &uninitialized, Id::clone);
        insert_effects(&mut effects.rebinds, promoted_rebinds, |id| {
            (id.clone(), effect_owner)
        });
    }

    /// Whether `promote_top_effects` would leave `outcome` unchanged: every
    /// read and rebind is already held for `effect_owner`, every read that is
    /// not yet initialized is already an uninitialized read, and, when the
    /// promotion is observed, none of them carries an assumed-path tag to
    /// clear. Escaped and deferred callback promotion repeats the same owners
    /// at every opaque call, so most promotions are such repeats.
    pub(super) fn promotes_nothing(
        &self,
        promoted_reads: &[&Id],
        promoted_rebinds: &[&Id],
        effect_owner: SourceUnitOrdinal,
        outcome: &Outcome,
    ) -> bool {
        let effects = &outcome.effects;
        let assumed = &effects.fallback_only;
        let observed = self.state.assumption.is_none();
        promoted_reads.iter().all(|&id| {
            effects.reads.contains(&(id.clone(), effect_owner))
                && (self.state.world.module.initialized_bindings.contains(id)
                    || effects.uninitialized_reads.contains(id))
                && !(observed
                    && (assumed.reads.contains_key(id)
                        || assumed.uninitialized_reads.contains_key(id)))
        }) && promoted_rebinds.iter().all(|&id| {
            effects.rebinds.contains(&(id.clone(), effect_owner))
                && !(observed && assumed.rebinds.contains_key(id))
        })
    }

    pub(super) fn roots_for_owner(&self, owner: SourceUnitOrdinal) -> BTreeSet<SourceUnitOrdinal> {
        let mut roots = BTreeSet::from([owner]);
        if let Some(fact) = self.inputs.facts.get(owner.0) {
            roots.extend(
                self.inputs
                    .source_unit_read_closure
                    .source_units_for_bindings(
                        fact.reads.eager.iter().chain(fact.rebinds.eager.iter()),
                    ),
            );
        }
        roots
    }

    pub(crate) fn fact_roots(
        &mut self,
        fact: &StatementFacts,
        metadata_target: Option<SourceUnitOrdinal>,
        private_constructor_result_owners: &BTreeSet<SourceUnitOrdinal>,
        closed_callback_factory_result: Option<&PrivateCallbackFactoryResult>,
    ) -> BTreeSet<SourceUnitOrdinal> {
        let mut roots = BTreeSet::new();
        // Storing a known callable in an author-certified inert global data
        // slot does not make that callable executable during this prefix.
        // Its ordinary eager read and all graph ordering edges remain intact.
        if self
            .state
            .world
            .publication
            .accepted_deferred_global_writes
            .contains(&fact.ordinal)
        {
            return roots;
        }
        // Export lists link bindings; evaluating one does not expose a value
        // whose declaration has not run yet.
        if fact.export_linkage.is_some() {
            return roots;
        }
        if let Some(proof) = closed_callback_factory_result {
            self.consume_proof_dependencies(&proof.dependencies);
        }
        // The full metadata timing proof covers this whole statement. Its
        // local mutation still contributes graph edges, but the call does not
        // publish an executable capability through the target or its result.
        // Rooting the statement would traverse its eager target read in the
        // synchronous owner closure and undo the target filter below.
        if metadata_target.is_none()
            && (self.inputs.exported_owners.contains(&fact.ordinal)
                || !(closed_callback_factory_result.is_some()
                    || (self.queries.prepared_statement_is_pure(fact.ordinal)
                        && (fact.declares_direct_function
                            || matches!(fact.kind, StatementKind::ClassDecl)
                            || self.closed_local_record_factory_result(fact)))
                    || !private_constructor_result_owners.is_empty()))
        {
            roots.insert(fact.ordinal);
        }
        // Reading an initialized ordinary class as heritage does not invoke
        // its constructor or methods. This proof covers only that operation;
        // eager class work and publication retain their ordinary roots.
        let (inert_heritage, heritage_dependencies) =
            match self.noninvoking_class_heritage_read(fact) {
                Some(proof) => (Some(proof.binding.clone()), Some(proof.dependencies)),
                None => (None, None),
            };
        if let Some(dependencies) = &heritage_dependencies {
            self.consume_proof_dependencies(dependencies);
        }
        roots.extend(
            self.inputs
                .source_unit_read_closure
                .source_units_for_bindings(
                    fact.reads
                        .eager
                        .iter()
                        .filter(|id| Some(*id) != inert_heritage.as_ref())
                        .chain(fact.rebinds.eager.iter()),
                )
                .into_iter()
                .filter(|owner| {
                    Some(*owner) != metadata_target
                        && !private_constructor_result_owners.contains(owner)
                }),
        );
        roots
    }

    pub(super) fn noninvoking_class_heritage_read<'a>(
        &'a self,
        fact: &StatementFacts,
    ) -> Option<NonInvokingClassHeritageRead<'a>> {
        if !matches!(fact.kind, StatementKind::ClassDecl)
            || self.inputs.exported_owners.contains(&fact.ordinal)
            || self.state.world.reachability.incomplete
        {
            return None;
        }
        let (class_key, _) = self
            .inputs
            .index
            .class_bindings
            .iter()
            .find(|(key, binding)| {
                self.inputs.index.class_owners.get(key) == Some(&fact.ordinal)
                    && fact.declared.contains(binding)
            })?;
        let base = self
            .inputs
            .index
            .class_values
            .get(class_key)?
            .lexical_heritage
            .as_ref()?
            .binding();
        let values = self.state.world.module.prefix_values.get(base)?;
        let Some(AbstractValue::Value(Value::Alloc(Kind::Class, base_key))) = values.first() else {
            return None;
        };
        let base_owner = *self.inputs.index.class_owners.get(base_key)?;
        if !self.state.world.module.initialized_bindings.contains(base)
            || !fact.reads.eager.contains(base)
            || self.inputs.index.class_bindings.get(base_key) != Some(base)
            || base_owner.0 >= fact.ordinal.0
            || self.state.world.classes.tainted.contains(base_key)
            || self
                .state
                .world
                .classes
                .escaped_capabilities
                .contains(base_key)
            || values.len() != 1
        {
            return None;
        }
        // ClassDefinitionEvaluation checks IsConstructor and reads the own
        // non-writable, non-configurable `prototype` data property of this source
        // class. Neither operation executes its bodies. Effects from creating
        // either class are separate operations, not prerequisites of this read.
        Some(NonInvokingClassHeritageRead {
            binding: base,
            dependencies: self.state.world.prerequisites.clone(),
        })
    }

    pub(crate) fn extend_reachable_owners(
        &mut self,
        roots: impl IntoIterator<Item = SourceUnitOrdinal>,
    ) -> bool {
        let before = self.state.world.reachability.owners.len();
        // A reachable callback can read only its synchronous prefix while the
        // chunk is initializing. In particular, a binding used after a
        // definite await cannot make the body of its future value executable
        // during an opaque call at this prefix.
        let world = self.state.world_mut();
        let reached = self.inputs.extend_available_source_units(
            roots,
            &world.module.initialized_bindings,
            world.reachability.incomplete,
            &mut world.reachability.expanded,
            &mut world.reachability.deferred,
        );
        self.state
            .world_mut()
            .reachability
            .owners
            .extend(reached.iter().copied());
        let changed = self.state.world.reachability.owners.len() != before;
        if changed {
            self.state.world_mut().revision.bump_reachability();
        }
        if reached
            .iter()
            .any(|owner| self.inputs.index.dynamic_scope_owners.contains(owner))
        {
            self.mark_reachability_incomplete();
        }
        changed
    }

    pub(super) fn mark_reachability_incomplete(&mut self) {
        self.widen_native_effects();
        if !self.state.world.reachability.incomplete {
            self.state.world_mut().reachability.incomplete = true;
            self.state.world_mut().revision.bump_reachability();
        }
        // Incomplete reachability always falls back to the whole chunk.
        self.state.prior_reachability_roots = None;
    }

    pub(super) fn note_unknown_execution(&mut self, owner: SourceUnitOrdinal) {
        self.widen_native_effects();
        // Exports expose live capabilities only to code that can reenter the
        // module. Builtin iteration without such reentry remains precise.
        if self.inputs.export_reentry_enabled {
            let world = &self.state.world;
            let exported = ordered_values(
                self.inputs
                    .reentry_default
                    .then_some(&world.publication.exported_runtime_values)
                    .into_iter()
                    .flatten()
                    .chain(self.inputs.reentry_bindings.iter().flat_map(|binding| {
                        world
                            .module
                            .prefix_values
                            .get(binding)
                            .into_iter()
                            .flatten()
                    }))
                    .cloned(),
            );
            self.expose_class_capabilities(&exported);
            self.taint_objects(&exported);
        }
        let mut changed = !self.state.world.reachability.unknown_call_seen;
        self.state.world_mut().reachability.unknown_call_seen = true;
        let roots = self
            .state
            .prior_reachability_roots
            .take()
            .into_iter()
            .flatten()
            .chain(self.roots_for_owner(owner));
        changed |= self.extend_reachable_owners(roots);
        if changed {
            self.state.world_mut().revision.bump_reachability();
        }
    }

    pub(crate) fn note_unknown_call(&mut self, call: &Call) {
        self.note_unknown_execution(call.owner);
        if matches!(&call.callee, ValueExpr::Binding(id) if id.0.as_ref() == "eval") {
            self.mark_reachability_incomplete();
        }
    }

    pub(super) fn record_executed_owner(&mut self, owner: SourceUnitOrdinal) {
        if self
            .state
            .world
            .classes
            .private_constructor_result_owners
            .contains(&owner)
        {
            return;
        }
        self.state.statement_executed_owners.insert(owner);
        if self.state.world.reachability.unknown_call_seen {
            self.extend_reachable_owners([owner]);
        } else if let Some(roots) = &mut self.state.prior_reachability_roots {
            roots.insert(owner);
        }
    }

    pub(crate) fn promote_property_writes(
        &mut self,
        writes: &[(Option<Id>, Option<String>)],
        owner: SourceUnitOrdinal,
        outcome: &mut Outcome,
    ) {
        if writes.is_empty() {
            return;
        }
        let unknown_call = outcome.unknown_call
            || self.state.world.reachability.unknown_call_seen
            || self.state.fallback_outcome.unknown_call;
        if self.inputs.index.setter_owners.is_empty() && !unknown_call {
            return;
        }
        let mut possible_owners = outcome.executed_owners.clone();
        possible_owners.insert(owner);
        possible_owners.extend(self.state.prior_effect_owners.iter().copied().filter(
            |candidate| {
                !self
                    .state
                    .world
                    .publication
                    .accepted_deferred_global_writes
                    .contains(candidate)
            },
        ));
        for (receiver, _) in writes {
            if let Some(receiver) = receiver
                && let Some(binding_owner) = self.inputs.binding_owner.get(receiver)
            {
                possible_owners.insert(*binding_owner);
            }
        }
        let unknown_receiver = writes.iter().any(|(receiver, _)| receiver.is_none());
        if unknown_call {
            possible_owners.extend(
                self.state
                    .world
                    .reachability
                    .owners
                    .iter()
                    .copied()
                    .filter(|candidate| self.may_be_available_accessor_owner(*candidate)),
            );
            if self.state.world.reachability.incomplete {
                possible_owners.extend(self.all_owners());
            }
        } else if unknown_receiver {
            possible_owners.extend(
                self.all_owners()
                    .filter(|candidate| self.may_be_available_accessor_owner(*candidate)),
            );
        }
        // Setters declared for a written property on a possible receiver stay
        // observed. After an unknown call any available accessor owner may
        // have been installed on a receiver: those others are assumed.
        let mut named_owners = BTreeSet::new();
        for (_, property) in writes {
            if let Some(property) = property {
                named_owners.extend(
                    self.inputs
                        .index
                        .setter_owners
                        .get(&Some(property.clone()))
                        .into_iter()
                        .flatten()
                        .copied(),
                );
                named_owners.extend(
                    self.inputs
                        .index
                        .setter_owners
                        .get(&None)
                        .into_iter()
                        .flatten()
                        .copied(),
                );
            } else {
                named_owners.extend(
                    self.inputs
                        .index
                        .setter_owners
                        .values()
                        .flat_map(|owners| owners.iter().copied()),
                );
            }
        }
        let named_owners: BTreeSet<_> = named_owners
            .intersection(&possible_owners)
            .copied()
            .collect();
        let assumed_owners: BTreeSet<_> = if unknown_call {
            possible_owners.difference(&named_owners).copied().collect()
        } else {
            BTreeSet::new()
        };
        if !named_owners.is_empty() || !assumed_owners.is_empty() {
            let effect_owner = if unknown_call {
                self.state.current_top_owner.unwrap_or(owner)
            } else {
                owner
            };
            for accessor_owner in named_owners {
                self.promote_lazy_owner(accessor_owner, effect_owner, outcome);
            }
            self.assuming(AssumedEffectReason::PossibleSetter, |this| {
                for accessor_owner in assumed_owners {
                    this.promote_lazy_owner(accessor_owner, effect_owner, outcome);
                }
            });
            outcome.unsupported(
                &self.inputs.metrics,
                ExecutionOperation::PropertyWrite,
                owner,
                DUMMY_SP,
            );
        }
    }

    pub(super) fn may_be_available_accessor_owner(&self, candidate: SourceUnitOrdinal) -> bool {
        if self.state.world.reachability.incomplete {
            // Direct eval and `with` can manufacture references to lexical
            // values outside the statically known read closure.
            return true;
        }
        let Some(current) = self.state.current_top_owner else {
            return true;
        };
        // Reachability follows lexical reads, including declarations whose
        // initializer has not executed. Such a future let/const/class value
        // cannot yet have been installed as a setter. Function declarations
        // are hoisted, and a later var declaration can name a value assigned
        // earlier, so retain both. Directly executed owners are added by the
        // caller independently of this broad reachability filter.
        candidate.0 <= current.0
            || self
                .inputs
                .index
                .hoisted_function_owners
                .contains(&candidate)
            || !self.inputs.facts[candidate.0].var_declared.is_empty()
    }

    /// Follow only initialized, uniquely written canonical binding copies.
    /// A lost local result or any other initializer is not an external root.
    pub(super) fn external_binding_root(&self, binding: &Id) -> bool {
        let mut current = binding;
        let mut seen = BTreeSet::new();
        loop {
            if !seen.insert(current) || self.inputs.index.reassigned_bindings.contains(current) {
                return false;
            }
            if self.inputs.index.imported_bindings.contains(current)
                || (!self.inputs.flow.is_declared(current)
                    && !self.inputs.shadowed.contains(current.0.as_ref()))
            {
                return true;
            }
            if !self.inputs.single_writer_bindings.contains(current)
                || !self
                    .state
                    .world
                    .module
                    .initialized_bindings
                    .contains(current)
            {
                return false;
            }
            let Some(writes) = self.inputs.index.binding_declaration_writes.get(current) else {
                return false;
            };
            let [write] = writes.as_slice() else {
                return false;
            };
            let Some(ValueExpr::Binding(source)) = &write.initializer else {
                return false;
            };
            if !write.direct_top_level || !write.simple_identifier {
                return false;
            }
            current = source;
        }
    }

    /// An unknown descriptor or prototype can make a read invoke any accessor
    /// previously installed by reachable code. Its returned value is Top, but
    /// its synchronous binding effects still occur at this read's prefix.
    pub(crate) fn promote_possible_getter(
        &mut self,
        call: &Call,
        receivers: &Values,
        outcome: &mut Outcome,
    ) {
        let no_prior_skipped_execution = !self.state.world.native.uncertain_execution;
        if self.state.array_iteration_depth > 0 {
            self.state.array_iteration_unsupported = true;
        }
        self.note_unknown_call(call);
        self.taint_objects(receivers);
        // A direct unshadowed global is already an external graph. Source
        // callbacks installed into it are retained by publication/opaque-call
        // tracking. A local or derived Top may instead hide a private graph
        // lost by skipped execution and must retain the broader fallback.
        let external_root = matches!(&call.callee,
            ValueExpr::Member(receiver, _, _, _) if matches!(receiver.as_ref(),
                ValueExpr::Binding(binding)
                    if self.external_binding_root(binding)))
            && self.inputs.index.dynamic_scope_owners.is_empty()
            && !self.state.world.reachability.incomplete
            && no_prior_skipped_execution
            && !receivers
                .iter()
                .any(|value| matches!(value, AbstractValue::Value(Value::Alloc(Kind::Object, _))));
        let mut owners: BTreeSet<_> = if external_root {
            BTreeSet::new()
        } else {
            self.state
                .world
                .reachability
                .owners
                .iter()
                .copied()
                .filter(|owner| self.may_be_available_accessor_owner(*owner))
                .collect()
        };
        if !external_root {
            owners.extend(outcome.executed_owners.iter().copied());
            owners.insert(call.owner);
        }
        if self.state.world.reachability.incomplete {
            owners.extend(self.all_owners());
        }
        let effect_owner = self.state.current_top_owner.unwrap_or(call.owner);
        if !external_root {
            owners.insert(effect_owner);
        }
        let lookup = PossibleGetterLookupSite {
            lexical_owner: call.owner,
            span_start: call.span.lo.0,
            span_end: call.span.hi.0,
        };
        for owner in owners {
            let reason = AssumedEffectReason::PossibleGetter {
                lookup: lookup.clone(),
                promotion: PossibleGetterPromotion::LazyOwner {
                    source_owner: owner,
                },
            };
            self.assuming(reason, |this| {
                this.promote_lazy_owner(owner, effect_owner, outcome)
            });
        }
        let reason = AssumedEffectReason::PossibleGetter {
            lookup,
            promotion: PossibleGetterPromotion::RawCallbacks,
        };
        self.assuming(reason, |this| {
            this.promote_raw_callbacks(receivers, outcome)
        });
        // Promoted getters may mutate another captured object or rebind a
        // captured cell before the next operation in this same statement.
        let reached: Values = outcome
            .effects
            .reads
            .iter()
            .filter_map(|(binding, _)| self.state.world.module.prefix_values.get(binding))
            .flatten()
            .cloned()
            .collect();
        self.unsettle(&reached);
        self.apply_rebind_effects(&outcome.effects.rebinds);
        self.invoke_possible_callbacks_at(
            std::slice::from_ref(receivers),
            false,
            Some(call.span),
            outcome,
        );
        outcome.unsupported(
            &self.inputs.metrics,
            ExecutionOperation::MemberRead,
            call.owner,
            call.span,
        );
        outcome.unknown_call = true;
    }

    pub(super) fn safe_object_assign_target(&self, call: &Call, id: &Id) -> bool {
        if call.kind == CallKind::New
            || !call.plain_object_sources
            || !matches!(call.args.first(), Some(ValueExpr::Binding(target)) if target == id)
        {
            return false;
        }
        let ValueExpr::Member(object, Some(property), _, _) = &call.callee else {
            return false;
        };
        matches!(object.as_ref(), ValueExpr::Binding(object_id)
            if object_id.0.as_ref() == "Object"
                && !self.inputs.flow.is_declared(object_id)
                && !self.inputs.shadowed.contains("Object")
                && property == "assign")
    }

    pub(super) fn stable_fresh_function_target(
        &self,
        key: u32,
        callable: &Callable,
        id: &Id,
    ) -> bool {
        callable.local_functions.contains_key(id)
            && !callable.rebinds.contains(id)
            && !callable.literal_captures.contains(id)
            && !callable.escaped_bindings.contains(id)
            && !callable.dynamic_property_effect
            && callable
                .member_writes
                .iter()
                .all(|(receiver, property)| receiver != id || property != "__proto__")
            && !callable.local_assignments.iter().any(|(target, value)| {
                if target == id {
                    return false;
                }
                let mut mentioned = BTreeSet::new();
                value.bindings(&mut mentioned);
                mentioned.contains(id)
            })
            && !self
                .inputs
                .index
                .callables
                .iter()
                .any(|(other, nested)| *other != key && nested.reads.contains(id))
            && !callable.calls.iter().any(|call| {
                let mut mentioned = BTreeSet::new();
                call.callee.bindings(&mut mentioned);
                for arg in &call.args {
                    arg.bindings(&mut mentioned);
                }
                mentioned.contains(id) && !self.safe_object_assign_target(call, id)
            })
    }
}
