//! Escaped-callback promotion and possible-callback invocation: returned,
//! deferred and export-surface callbacks, the callback pool, and the bounded
//! invocation fallback.

use super::*;

impl<'source, 'rule> Executor<'source, 'rule> {
    /// [`Self::promote_raw_callbacks`] for values handed to opaque code, which
    /// may or may not invoke them: the promoted closure is assumed.
    pub(crate) fn promote_escaped_callbacks(&mut self, values: &Values, outcome: &mut Outcome) {
        self.assuming(AssumedEffectReason::EscapedCallbackClosure, |this| {
            this.promote_raw_callbacks(values, outcome)
        });
    }

    pub(crate) fn promote_raw_callbacks(&mut self, values: &Values, outcome: &mut Outcome) {
        let prior_outcome = std::mem::take(outcome);
        let result = self.request_callback_operation(CallbackOperation::PromoteRaw {
            values: values.clone(),
            outcome: Box::new(prior_outcome),
        });
        let CallbackResult::Outcome(result) = result else {
            unreachable!("raw callback promotion returns its updated outcome");
        };
        *outcome = *result;
    }

    pub(crate) fn promote_raw_callbacks_transfer(
        &mut self,
        values: &Values,
        outcome: &mut Outcome,
    ) {
        let effect_owner = self.state.current_top_owner.unwrap();
        let mut owners = BTreeSet::new();
        for callback in self.callback_values(values) {
            let owner = match callback {
                AbstractValue::ForeignDeferredRecord => {
                    self.promote_foreign_record(outcome);
                    None
                }
                AbstractValue::Value(Value::Alloc(Kind::Function, key))
                | AbstractValue::Closure(HeapAddress { callable: key, .. }, _) => self
                    .inputs
                    .index
                    .callables
                    .get(&key)
                    .map(|callable| callable.owner),
                AbstractValue::ClassMembers(key) => self
                    .inputs
                    .flow
                    .callable_source_unit
                    .get(&Value::Alloc(Kind::Class, key))
                    .or_else(|| self.inputs.index.class_owners.get(&key))
                    .copied(),
                _ => None,
            };
            owners.extend(owner);
        }
        if self.state.world.reachability.incomplete {
            for owner in owners {
                let (reads, rebinds) = self
                    .inputs
                    .source_unit_read_closure
                    .lazy_effects_for_source_unit(owner);
                let reads = reads.clone();
                let rebinds = rebinds.clone();
                let referenced: Values = reads
                    .iter()
                    .flat_map(|id| {
                        self.state
                            .world
                            .module
                            .prefix_values
                            .get(id)
                            .into_iter()
                            .flatten()
                            .cloned()
                    })
                    .collect();
                self.taint_objects(&referenced);
                self.promote_lazy_effects(&reads, &rebinds, effect_owner, outcome);
            }
            return;
        }
        let reached = SourceUnitReadClosure::extend_synchronous_source_units(
            &self.inputs.synchronous_owner_adjacency,
            owners,
            &mut BTreeSet::new(),
        );
        for owner in reached {
            // This coverage-checked union includes every retained callable's
            // synchronous prefix, including returned callbacks and getters.
            // Bodies past a definite await cannot run during this protocol.
            let (reads, rebinds) = self.inputs.synchronous_effects(owner, false);
            let reads = reads.clone();
            let rebinds = rebinds.clone();
            let referenced: Values = reads
                .iter()
                .flat_map(|id| {
                    self.state
                        .world
                        .module
                        .prefix_values
                        .get(id)
                        .into_iter()
                        .flatten()
                        .cloned()
                })
                .collect();
            self.taint_objects(&referenced);
            self.promote_lazy_effects(&reads, &rebinds, effect_owner, outcome);
        }
    }

    pub(crate) fn promote_deferred_callbacks(&mut self, captured: &Values, outcome: &mut Outcome) {
        let prior_outcome = std::mem::take(outcome);
        let result = self.assuming(AssumedEffectReason::EscapedCallbackClosure, |this| {
            this.request_callback_operation(CallbackOperation::PromoteDeferred {
                captured: captured.clone(),
                outcome: Box::new(prior_outcome),
            })
        });
        let CallbackResult::Outcome(result) = result else {
            unreachable!("deferred callback promotion returns its updated outcome");
        };
        *outcome = *result;
    }

    pub(crate) fn promote_deferred_callbacks_transfer(
        &mut self,
        captured: &Values,
        outcome: &mut Outcome,
    ) {
        let effect_owner = self.state.current_top_owner.unwrap();
        let mut roots = BTreeSet::new();
        for callback in self.callback_values(captured) {
            let key = match callback {
                AbstractValue::Value(Value::Alloc(Kind::Function, key))
                | AbstractValue::Closure(HeapAddress { callable: key, .. }, _) => key,
                AbstractValue::ClassMembers(key) => {
                    self.promote_class_members(key, outcome);
                    continue;
                }
                _ => continue,
            };
            if let Some(callable) = self.inputs.index.callables.get(&key) {
                roots.insert(callable.owner);
            }
        }
        if self.state.world.reachability.incomplete {
            for owner in roots {
                let (reads, rebinds) = self
                    .inputs
                    .source_unit_read_closure
                    .lazy_effects_for_source_unit(owner);
                self.promote_lazy_effects(reads, rebinds, effect_owner, outcome);
            }
        } else {
            for owner in roots {
                let (reads, rebinds) = self.inputs.closed_synchronous_effects(owner);
                self.promote_lazy_effects(reads, rebinds, effect_owner, outcome);
            }
        }
    }

    pub(super) fn promote_returned_callbacks(&mut self, returned: &Values) {
        let result = self.request_callback_operation(CallbackOperation::PromoteReturned {
            returned: returned.clone(),
        });
        let CallbackResult::Completed = result else {
            unreachable!("returned callback promotion has no value result");
        };
    }

    pub(crate) fn promote_returned_callbacks_transfer(&mut self, returned: &Values) {
        let callbacks: Values = self
            .callback_values(returned)
            .into_iter()
            .filter(|callback| {
                match callback {
                    AbstractValue::Value(Value::Alloc(Kind::Function, key))
                    | AbstractValue::Closure(HeapAddress { callable: key, .. }, _) => self
                        .inputs
                        .index
                        .callables
                        .get(key)
                        .is_some_and(|callable| {
                            let world = self.state.world_mut();
                            let incomplete = world.reachability.incomplete;
                            world
                                .publication
                                .returned_callback_owners
                                .insert((incomplete, callable.owner))
                        }),
                    // Keep class capability/superclass checks on their existing
                    // path. Other aggregate values have already been expanded by
                    // callback_values and are not callable roots themselves.
                    AbstractValue::ClassMembers(_) => true,
                    _ => false,
                }
            })
            .collect();
        if callbacks.is_empty() {
            return;
        }
        let mut outcome = std::mem::take(&mut self.state.fallback_outcome);
        let rebinds_before = outcome.effects.rebinds.len();
        outcome.unsupported(
            &self.inputs.metrics,
            ExecutionOperation::ReturnedCallback,
            self.state
                .current_top_owner
                .expect("returned callback has a top owner"),
            DUMMY_SP,
        );
        outcome.unknown_call = true;
        self.widen_native_effects();
        self.promote_deferred_callbacks(&callbacks, &mut outcome);
        // Class/superclass capability checks can invoke code and populate the
        // shared accumulator reentrantly. Preserve those contributions before
        // restoring the outer summary, including roots marked cached there.
        let mut reentrant = std::mem::take(&mut self.state.fallback_outcome);
        outcome.effects.reads.append(&mut reentrant.effects.reads);
        outcome
            .effects
            .rebinds
            .append(&mut reentrant.effects.rebinds);
        outcome
            .effects
            .uninitialized_reads
            .append(&mut reentrant.effects.uninitialized_reads);
        outcome
            .executed_owners
            .append(&mut reentrant.executed_owners);
        outcome.unknown_call |= reentrant.unknown_call;
        outcome.inherit_precision_losses(reentrant.effects.precision_losses);
        if outcome.effects.rebinds.len() != rebinds_before {
            self.apply_rebind_effects(&outcome.effects.rebinds);
        }
        // These effects belong to the active top-level prefix, regardless of
        // the lexical callee. Merge them once there, rather than copying the
        // same large read set through each cached nested invocation.
        self.state.fallback_outcome = outcome;
    }

    /// Opaque code holding a class or its prototype can retrieve and run any
    /// method, getter, field callback, or constructor synchronously. Cached
    /// owner traversal includes those bodies and their synchronous callees,
    /// while stopping reads after a definite await. Owners without complete
    /// callable coverage retain their whole lazy read set.
    pub(super) fn promote_class_members(&mut self, key: u32, outcome: &mut Outcome) {
        if !self.state.active_class_member_promotions.insert(key) {
            return;
        }
        let owner = self
            .inputs
            .flow
            .callable_source_unit
            .get(&Value::Alloc(Kind::Class, key))
            .copied()
            .or_else(|| self.inputs.index.class_owners.get(&key).copied());
        let Some(owner) = owner else {
            self.state.active_class_member_promotions.remove(&key);
            return;
        };
        let effect_owner = self.state.current_top_owner.unwrap_or(owner);
        // Class expressions may have only their containing statement owner,
        // while their constructor and methods still contribute lazy reads to
        // that owner's summary.
        if self.state.world.reachability.incomplete {
            let (reads, rebinds) = self
                .inputs
                .source_unit_read_closure
                .lazy_effects_for_source_unit(owner);
            self.promote_lazy_effects(reads, rebinds, effect_owner, outcome);
        } else {
            let (reads, rebinds) = self.inputs.closed_synchronous_effects(owner);
            self.promote_lazy_effects(reads, rebinds, effect_owner, outcome);
        }
        self.promote_captured_superclass(key, owner, outcome);
        // The opaque caller may invoke a method that changes a top-level
        // binding. Subsequent prefix calls must include its new possible
        // value, just as they do after an ordinary callback invocation.
        self.apply_rebind_effects(&outcome.effects.rebinds);
        self.state.active_class_member_promotions.remove(&key);
    }

    pub(super) fn promote_captured_superclass(
        &mut self,
        key: u32,
        owner: SourceUnitOrdinal,
        outcome: &mut Outcome,
    ) {
        let Some(class) = self.inputs.index.class_values.get(&key) else {
            return;
        };
        if class.super_class.is_none() {
            return;
        }
        let values = self
            .state
            .world
            .classes
            .superclasses
            .get(&key)
            .cloned()
            .unwrap_or_else(unknown);
        let mut has_unknown = values.is_empty();
        for value in values {
            let (function, captured) = match value {
                AbstractValue::Value(Value::Alloc(Kind::Class, base))
                | AbstractValue::ClassMembers(base) => {
                    self.promote_class_members(base, outcome);
                    continue;
                }
                AbstractValue::Value(Value::Alloc(Kind::Function, function)) => (function, None),
                AbstractValue::Closure(
                    HeapAddress {
                        callable: function, ..
                    },
                    captured,
                ) => (function, Some(captured)),
                value
                    if value.is_primitive()
                        || matches!(value, AbstractValue::Value(Value::Unavailable)) =>
                {
                    continue;
                }
                _ => {
                    has_unknown = true;
                    continue;
                }
            };
            let count = self
                .inputs
                .index
                .callables
                .get(&function)
                .map_or(0, |callable| callable.params.len());
            let mut nested = self.invoke(function, vec![unknown(); count], captured, false);
            self.apply_rebind_effects(&nested.effects.rebinds);
            outcome.absorb_call_effects(&mut nested.effects, self.state.assumption.as_ref());
            outcome.executed_owners.extend(nested.executed_owners);
            outcome.unknown_call |= nested.unknown_call;
            outcome.inherit_completeness(nested.complete, nested.effects.precision_losses);
        }
        if has_unknown {
            outcome.unsupported(
                &self.inputs.metrics,
                ExecutionOperation::SuperclassConstruction,
                owner,
                DUMMY_SP,
            );
            outcome.unknown_call = true;
            let effect_owner = self.state.current_top_owner.unwrap_or(owner);
            self.note_unknown_execution(effect_owner);
            self.invoke_possible_callbacks(&[unknown()], false, outcome);
        }
    }

    pub(crate) fn closure_captures(&mut self, key: u32, captures: &Context) -> CapturedContext {
        let captured = lexical_captures(
            self.inputs.index,
            &self.state.world.active_cells,
            captures,
            key,
            self.inputs.capture_policy(),
        );
        self.intern_captures(captured)
    }

    pub(super) fn available_export_callbacks(&self) -> Values {
        if !self.inputs.export_reentry_enabled {
            return Values::new();
        }
        self.export_surface_callback_values(ExportSurface::Reentry)
    }

    pub(super) fn export_callback_values(&self) -> Values {
        self.export_surface_callback_values(ExportSurface::Exported)
    }

    pub(super) fn export_surface_callback_values(&self, surface: ExportSurface) -> Values {
        let world: &World = &self.state.world;
        if let Some(callbacks) = self.state.export_callback_memos.get(surface, world) {
            return callbacks;
        }
        let (bindings, default) = match surface {
            ExportSurface::Reentry => (&self.inputs.reentry_bindings, self.inputs.reentry_default),
            ExportSurface::Exported => (&self.inputs.index.exported_bindings, true),
        };
        // Discover the whole export surface in one traversal. Exported aliases,
        // unknown values, and shared objects must not each re-expand the same
        // escaped callback/capture graph independently.
        let mut values = Values::new();
        self.visit_export_surface_callback_roots(bindings, default, |_, value| {
            values.insert(value);
        });
        let callbacks = self.callback_values(&values);
        self.state
            .export_callback_memos
            .store(surface, world, &callbacks);
        callbacks
    }

    pub(super) fn visit_export_surface_callback_roots(
        &self,
        bindings: &BTreeSet<Id>,
        default: bool,
        mut visit: impl FnMut(Option<&Id>, AbstractValue),
    ) {
        for binding in bindings {
            if let Some(exported) = self.state.world.module.prefix_values.get(binding) {
                for value in exported {
                    visit(Some(binding), value.clone());
                }
            }
        }
        if default {
            for key in &self.inputs.index.exported_hoisted_functions {
                visit(
                    None,
                    AbstractValue::Value(Value::Alloc(Kind::Function, *key)),
                );
            }
            for value in &self.state.world.publication.exported_runtime_values {
                visit(None, value.clone());
            }
        }
    }

    pub(super) fn visit_available_export_callback_roots(
        &self,
        visit: impl FnMut(Option<&Id>, AbstractValue),
    ) {
        if self.inputs.export_reentry_enabled {
            self.visit_export_surface_callback_roots(
                &self.inputs.reentry_bindings,
                self.inputs.reentry_default,
                visit,
            );
        }
    }

    pub(super) fn invoke_possible_callbacks(
        &mut self,
        args: &[Values],
        no_sync_arguments: bool,
        outcome: &mut Outcome,
    ) {
        self.invoke_possible_callbacks_at(args, no_sync_arguments, None, outcome);
    }

    pub(crate) fn invoke_possible_callbacks_at(
        &mut self,
        args: &[Values],
        no_sync_arguments: bool,
        publication_span: Option<Span>,
        outcome: &mut Outcome,
    ) {
        let prior_outcome = std::mem::take(outcome);
        // Opaque code may or may not invoke the pool: what it reaches is assumed.
        let result = self.assuming(AssumedEffectReason::OpaqueCallbackInvocation, |this| {
            this.request_callback_operation(CallbackOperation::InvokePossible {
                arguments: args.to_vec(),
                no_sync_arguments,
                publication_span,
                outcome: Box::new(prior_outcome),
            })
        });
        let CallbackResult::Outcome(result) = result else {
            unreachable!("possible callback invocation returns its updated outcome");
        };
        *outcome = *result;
    }

    pub(crate) fn invoke_possible_callbacks_transfer(
        &mut self,
        args: &[Values],
        no_sync_arguments: bool,
        publication_span: Option<Span>,
        outcome: &mut Outcome,
    ) {
        // Opaque code may run each available callback any number of times and
        // in any order. One fixed-order pass would let a member run before a
        // later member's write and miss the value it publishes. Repeat the
        // pass over the joined state until nothing a member can observe
        // changes. Each extra pass spends the shared invocation budget, whose
        // exhaustion takes the order-insensitive fallback summary.
        let mut foreign_promoted = false;
        loop {
            let callbacks = self.possible_callback_pool(
                args,
                no_sync_arguments,
                &mut foreign_promoted,
                outcome,
            );
            if self.summarize_read_only_callback_pool(&callbacks, outcome) {
                break;
            }
            let before = self.callback_pool_observation();
            if self.invoke_callback_pool_once(
                &callbacks,
                args,
                no_sync_arguments,
                publication_span,
                outcome,
            ) || self.callback_pool_observation() == before
            {
                break;
            }
            // Invocation memos keyed by the pre-pass revision describe the
            // smaller state; changes outside prefix values do not bump it.
            self.state.world_mut().revision.bump_prefix();
            self.state.invocation_count += 1;
        }
        // An opaque callee can retain any callback passed to it and invoke it
        // during a later prefix event.
        let values = ordered_values(args.iter().flatten().cloned());
        self.taint_objects(&values);
        let callbacks = self.callback_values(&values);
        self.insert_escaped_callback_values(
            &callbacks,
            ClassMembersPublicationWriter::PossibleCallbackRetention,
            publication_span,
            None,
        );
    }

    pub(super) fn possible_callback_pool(
        &mut self,
        args: &[Values],
        no_sync_arguments: bool,
        foreign_promoted: &mut bool,
        outcome: &mut Outcome,
    ) -> Values {
        let mut callbacks = self
            .state
            .world
            .publication
            .global_escaped_callbacks
            .clone();
        callbacks.extend_absent(&self.available_export_callbacks());
        // A callback-argument exemption does not certify foreign getters or
        // direct live-cell captures reachable through a returned record.
        let supplied = ordered_values(args.iter().flatten().cloned());
        let supplied_callbacks = self.callback_values(&supplied);
        if !*foreign_promoted
            && (callbacks.contains(&AbstractValue::ForeignDeferredRecord)
                || supplied_callbacks.contains(&AbstractValue::ForeignDeferredRecord))
        {
            self.promote_foreign_record(outcome);
            *foreign_promoted = true;
        }
        if !no_sync_arguments {
            callbacks.extend_absent(&supplied_callbacks);
        }
        // Activate the foreign pool before any callback-size fallback, which
        // otherwise only expands callables with local source owners.
        callbacks.remove(&AbstractValue::ForeignDeferredRecord);
        callbacks
    }

    /// Everything a pool member's invocation can depend on. The prefix
    /// revision covers binding values; heap maps, taints, escapes and native
    /// capability state can change without bumping it.
    pub(super) fn callback_pool_observation(&self) -> CallbackPoolObservation {
        let state = &*self.state;
        CallbackPoolObservation {
            prefix_revision: state.world.revision.prefix,
            reachability_revision: state.world.revision.reachability,
            prefix_object_properties: state.world.surface.prefix_object_properties.clone(),
            dense_arrays: state.world.arrays.dense.clone(),
            primitive_arrays: state.world.arrays.primitive.clone(),
            accumulated_values: state.world.arrays.accumulated_values.clone(),
            applied_accessors: state.world.surface.applied_accessors.clone(),
            class_superclasses: state.world.classes.superclasses.clone(),
            set_sizes: [
                state.world.publication.global_escaped_callbacks.len(),
                state.world.arrays.unknown_contents.len(),
                state.world.publication.exported_runtime_values.len(),
                state.world.surface.tainted_objects.len(),
                state.world.native.tainted_sets.len(),
                state.world.native.tainted_functions.len(),
                state.world.native.revoked_audited_results.len(),
                state.world.classes.tainted.len(),
                state.world.classes.escaped_capabilities.len(),
                state.world.surface.ordinary_spread_copies.len(),
                state.world.surface.descriptor_copy_results.len(),
                state
                    .world
                    .descriptors
                    .values()
                    .filter(|map| map.valid)
                    .count(),
            ],
            array_intrinsic_mutated: state.world.arrays.intrinsic_mutated,
            uncertain_native_execution: state.world.native.uncertain_execution,
        }
    }

    /// Runs every pool member once against the evolving state. Returns whether
    /// the bounded fallback summarized the pool instead.
    pub(super) fn invoke_callback_pool_once(
        &mut self,
        callbacks: &Values,
        source_arguments: &[Values],
        no_sync_arguments: bool,
        diagnostic_callsite_span: Option<Span>,
        outcome: &mut Outcome,
    ) -> bool {
        // Unknown host code may invoke any callback it received with another
        // currently available callback, so each formal gets the conservative
        // callback set plus a non-callable/unknown possibility.
        // Allocate only when an invoked function consumes a formal. The input
        // pool is immutable; constructing this set reads no live heap state.
        let possible_argument = std::cell::OnceCell::new();
        let args_for = |key: u32, index: &ExecutionIndex| {
            let count = index
                .callables
                .get(&key)
                .map_or(0, |callable| callable.params.len());
            if count == 0 {
                return Vec::new();
            }
            let possible_argument = possible_argument.get_or_init(|| {
                let mut values = callbacks.clone();
                values.insert(AbstractValue::Value(Value::Top));
                values
            });
            vec![possible_argument.clone(); count]
        };
        let reentrant_pool = callbacks.iter().any(|callback| {
            let key = match callback {
                AbstractValue::Value(Value::Alloc(Kind::Function, key))
                | AbstractValue::Closure(HeapAddress { callable: key, .. }, _) => *key,
                _ => return false,
            };
            self.state.active.contains(&ActiveInvocation {
                callable: key,
                top_owner: self.state.current_top_owner,
            })
        });
        for callback in callbacks.iter().cloned() {
            if reentrant_pool
                || callbacks.len() > MAX_SYNCHRONOUS_CALLBACK_POOL
                || self.state.invocation_count >= MAX_SYNCHRONOUS_INVOCATIONS
                || self.state.invocation_depth >= MAX_SYNCHRONOUS_INVOCATION_DEPTH
            {
                // A recursive member already falls back to a closure over the
                // entire pool in its arguments. Summarize that pool once rather
                // than enumerate permutations of callbacks around the cycle.
                // The same batch fallback bounds exhausted acyclic expansions.
                let reason = if reentrant_pool {
                    ExecutionPrecisionLossReason::RecursiveInvocation
                } else if callbacks.len() > MAX_SYNCHRONOUS_CALLBACK_POOL {
                    ExecutionPrecisionLossReason::CallbackPoolLimit {
                        limit: MAX_SYNCHRONOUS_CALLBACK_POOL,
                        pool_size: callbacks.len(),
                    }
                } else if self.state.invocation_count >= MAX_SYNCHRONOUS_INVOCATIONS {
                    ExecutionPrecisionLossReason::InvocationExpansionLimit {
                        limit: MAX_SYNCHRONOUS_INVOCATIONS,
                    }
                } else {
                    ExecutionPrecisionLossReason::InvocationDepthLimit {
                        limit: MAX_SYNCHRONOUS_INVOCATION_DEPTH,
                    }
                };
                let mut nested =
                    self.bounded_invocation_fallback(BoundedInvocationFallbackRequest {
                        owner: self
                            .state
                            .current_top_owner
                            .expect("callback call has a top owner"),
                        arguments: std::slice::from_ref(callbacks).to_vec(),
                        captured: Context::new(),
                        reason,
                        reach: HavocReach::Global {
                            cause: GlobalReachCause::EscapedCallbackPool,
                        },
                        span: DUMMY_SP,
                        diagnostics: BoundedInvocationFallbackDiagnostics {
                            callsite_span: diagnostic_callsite_span,
                            arguments: Some(if no_sync_arguments {
                                Vec::new()
                            } else {
                                source_arguments.to_vec()
                            }),
                        },
                    });
                self.apply_rebind_effects(&nested.effects.rebinds);
                outcome.absorb_call_effects(&mut nested.effects, self.state.assumption.as_ref());
                outcome.executed_owners.extend(nested.executed_owners);
                outcome.unknown_call |= nested.unknown_call;
                outcome.inherit_completeness(nested.complete, nested.effects.precision_losses);
                return true;
            }
            let writable_before = self.state.world.surface.writable_prefix.clone();
            let nested = match callback {
                AbstractValue::ClassMembers(key) => {
                    self.promote_class_members(key, outcome);
                    None
                }
                AbstractValue::DeferredCallbacks(captured)
                | AbstractValue::AuditedRetainedResult(_, captured) => {
                    self.promote_deferred_callbacks(&captured.values, outcome);
                    None
                }
                AbstractValue::Value(Value::Alloc(Kind::Function, key)) => {
                    Some(self.invoke(key, args_for(key, self.inputs.index), None, false))
                }
                AbstractValue::Closure(HeapAddress { callable: key, .. }, captured) => {
                    Some(self.invoke(key, args_for(key, self.inputs.index), Some(captured), false))
                }
                _ => None,
            };
            if let Some(mut nested) = nested {
                // Foreign code can retain and invoke a callback's synchronous
                // return value as well. This includes a descriptor getter that
                // returns another callable; stopping at the getter loses the
                // returned callable's importer reads. Prefix coverage preserves
                // the ordinary boundary at an unconditional await. Whatever the
                // member returns reaches that code too.
                self.taint_objects(&nested.returns);
                self.promote_returned_callbacks(&nested.returns);
                self.apply_rebind_effects(&nested.effects.rebinds);
                outcome.absorb_call_effects(&mut nested.effects, self.state.assumption.as_ref());
                outcome.executed_owners.extend(nested.executed_owners);
                outcome.unknown_call |= nested.unknown_call;
                outcome.inherit_completeness(nested.complete, nested.effects.precision_losses);
            }
            // Opaque code can omit or repeat this member. A changed exact
            // post-state cannot replace the entry witness as though the call
            // occurred once. Widen before another member or fixed-point pass
            // observes it; otherwise pushes grow exact lengths to the budget.
            if !self
                .state
                .world
                .surface
                .writable_prefix
                .ptr_eq(&writable_before)
            {
                let changed: Vec<_> = self
                    .state
                    .world
                    .surface
                    .writable_prefix
                    .iter()
                    .filter_map(|(object, state)| {
                        (writable_before.get(object) != Some(state)).then_some(*object)
                    })
                    .collect();
                for object in changed {
                    self.invalidate_writable_object(object);
                }
            }
        }
        false
    }

    pub(crate) fn apply_rebind_effects(&mut self, rebinds: &BTreeSet<(Id, SourceUnitOrdinal)>) {
        for (id, _) in rebinds {
            self.state
                .world_mut()
                .module
                .prefix_callable_selections
                .remove(id);
            if !self.inputs.top_bindings.contains(id) {
                continue;
            }
            let changed = self
                .state
                .world_mut()
                .module
                .prefix_values
                .entry_or_default(id)
                .insert(AbstractValue::Value(Value::Top));
            if changed {
                self.state.world_mut().revision.bump_prefix();
            }
        }
    }

    pub(super) fn bounded_invocation_fallback(
        &mut self,
        request: BoundedInvocationFallbackRequest,
    ) -> Outcome {
        let result = self
            .request_callback_operation(CallbackOperation::BoundedInvocationFallback { request });
        let CallbackResult::Outcome(result) = result else {
            unreachable!("bounded callback fallback returns an outcome");
        };
        *result
    }

    pub(crate) fn bounded_invocation_fallback_transfer(
        &mut self,
        request: BoundedInvocationFallbackRequest,
    ) -> Outcome {
        let BoundedInvocationFallbackRequest {
            owner,
            arguments,
            captured,
            reason,
            reach,
            span,
            diagnostics,
        } = request;
        let args = &arguments;
        let BoundedInvocationFallbackDiagnostics {
            callsite_span: diagnostic_callsite_span,
            arguments: diagnostic_arguments,
        } = diagnostics;
        let captured = &captured;
        let effect_owner = self.state.current_top_owner.unwrap_or(owner);
        if self.state.array_iteration_depth > 0 {
            self.state.array_iteration_unsupported = true;
        }
        // Within one source prefix initialization is fixed. Keep a monotone
        // summary so each reachable owner's lazy effects are promoted once,
        // even when recursion or changing abstract arguments hit the bound
        // repeatedly. Reusing earlier fallback roots only adds constraints.
        let mut outcome = std::mem::take(&mut self.state.fallback_outcome);
        let rebinds_before = outcome.effects.rebinds.len();
        outcome.lose_precision(
            &self.inputs.metrics,
            reason.clone(),
            reach.clone(),
            owner,
            span,
        );
        outcome.unknown_call = true;
        self.widen_native_effects();
        outcome.executed_owners.insert(owner);

        // The recursive summary is no longer being expanded. Preserve reads
        // from this callable and callbacks actually present in its arguments,
        // captures, exports, or escaped globals. Do not turn ordinary
        // recursion into a whole-chunk reachability root.
        let export_callbacks = self.available_export_callbacks();
        let possible_values = ordered_values(
            args.iter()
                .flatten()
                .chain(captured.values().flatten())
                .chain(&self.state.world.publication.global_escaped_callbacks)
                .chain(&export_callbacks)
                .cloned(),
        );

        let callbacks = self.callback_values(&possible_values);
        let first_class_candidates: BTreeSet<_> = callbacks
            .iter()
            .filter_map(|callback| match callback {
                AbstractValue::ClassMembers(key)
                    if !self
                        .state
                        .world
                        .publication
                        .global_escaped_callbacks
                        .contains(callback) =>
                {
                    Some(*key)
                }
                _ => None,
            })
            .collect();
        let input_paths = if first_class_candidates.is_empty() {
            BTreeMap::new()
        } else {
            // Only the first-publication diagnostic needs seed labels. Keep
            // this path-only work out of fallback calls with no new class
            // capability, and do not change the deduplicated value union.
            // Callback-pool truncation passes a synthetic `[callbacks]` value
            // to the semantic fallback; trace from the original call inputs
            // instead so the report never calls that synthetic vector arg 0.
            let path_arguments = diagnostic_arguments.as_deref().unwrap_or(args);
            let mut path_roots = Values::new();
            let mut fallback_seeds = BTreeMap::new();
            for (position, values) in path_arguments.iter().enumerate() {
                for value in values {
                    path_roots.insert(value.clone());
                    fallback_seeds
                        .entry(value.clone())
                        .or_insert(BoundedInvocationFallbackSeed::Argument { position });
                }
            }
            for (binding, values) in captured {
                for value in values {
                    path_roots.insert(value.clone());
                    fallback_seeds.entry(value.clone()).or_insert_with(|| {
                        BoundedInvocationFallbackSeed::CapturedBinding {
                            binding: binding.0.to_string(),
                        }
                    });
                }
            }
            for value in &self.state.world.publication.global_escaped_callbacks {
                path_roots.insert(value.clone());
                fallback_seeds
                    .entry(value.clone())
                    .or_insert(BoundedInvocationFallbackSeed::EscapedCallbackPool);
            }
            self.visit_available_export_callback_roots(|binding, value| {
                path_roots.insert(value.clone());
                fallback_seeds
                    .entry(value.clone())
                    .or_insert_with(|| match binding {
                        Some(binding) => BoundedInvocationFallbackSeed::AvailableExportBinding {
                            binding: binding.0.to_string(),
                        },
                        None => BoundedInvocationFallbackSeed::AvailableExportCallbacks,
                    });
            });
            let (_, paths) = self.callback_values_with_paths(
                &path_roots,
                &fallback_seeds,
                &first_class_candidates,
            );
            paths
        };
        let diagnostic_callsite = diagnostic_callsite_span
            .filter(|span| *span != DUMMY_SP)
            .map(|span| BoundedInvocationFallbackCallSite {
                top_level_owner: self.state.current_top_owner,
                span_start: span.lo.0,
                span_end: span.hi.0,
                source_location: None,
            });
        let publication_diagnostic = BoundedFallbackPublicationDiagnostic {
            input_paths,
            callsite: diagnostic_callsite,
        };

        if callbacks.contains(&AbstractValue::ForeignDeferredRecord) {
            self.promote_foreign_record(&mut outcome);
        }
        // The summarized bodies run on these inputs without the interpreter
        // following what they do to them.
        let reached = ordered_values(args.iter().flatten().chain(&callbacks).cloned());
        self.unsettle(&reached);
        let mut roots = BTreeSet::new();
        for callback in &callbacks {
            let key = match callback {
                AbstractValue::Value(Value::Alloc(Kind::Function, key))
                | AbstractValue::Closure(HeapAddress { callable: key, .. }, _) => Some(*key),
                _ => None,
            };
            if let Some(callback_owner) =
                key.and_then(|key| self.inputs.index.callables.get(&key).map(|c| c.owner))
            {
                roots.insert(callback_owner);
            }
            if let AbstractValue::ClassMembers(key) = callback
                && let Some(&owner) = self
                    .inputs
                    .flow
                    .callable_source_unit
                    .get(&Value::Alloc(Kind::Class, *key))
            {
                roots.insert(owner);
            }
        }
        // The summarized bodies can also reach a callable through the heap
        // rather than a lexical read: a stored closure they call or return,
        // or an accessor their property access runs. Storing it there took
        // an effect: a prior impure owner or a callable run earlier in this
        // statement, whose lexical closure covers the stored callable's body.
        // A pure owner's fresh object is reachable only through a lexical
        // read of its binding, which the closure already follows.
        roots.insert(effect_owner);
        roots.extend(outcome.executed_owners.iter().copied());
        roots.extend(self.state.statement_executed_owners.iter().copied());
        roots.extend(
            self.state
                .prior_effect_owners
                .iter()
                .copied()
                .filter(|candidate| {
                    !self
                        .state
                        .world
                        .publication
                        .accepted_deferred_global_writes
                        .contains(candidate)
                }),
        );
        self.extend_reachable_owners(roots.iter().copied().chain([owner]));
        let state = &mut *self.state;
        let mut reached = self.inputs.extend_available_source_units(
            roots.iter().copied(),
            &state.world.module.initialized_bindings,
            state.world.reachability.incomplete,
            &mut state.callback_fallback.expanded,
            &mut state.callback_fallback.deferred,
        );
        if self.state.world.reachability.incomplete {
            // Dynamic lexical scope defeats the normal binding-read closure.
            // Revisit even previously expanded owners: their cached prefix
            // effects may have omitted reads that eval or with can reach.
            if !self.state.callback_fallback.incomplete_expanded {
                self.state.callback_fallback.incomplete_expanded = true;
                for fact in self.inputs.facts {
                    self.state.callback_fallback.expanded.insert(fact.ordinal);
                    reached.push(fact.ordinal);
                }
            }
        }
        for reached_owner in reached {
            let (reads, rebinds) = self
                .inputs
                .synchronous_effects(reached_owner, self.state.world.reachability.incomplete);
            for id in reads
                .iter()
                .filter(|id| self.inputs.top_bindings.contains(*id))
            {
                if self.state.callback_fallback.reads.insert(id.clone()) {
                    self.state.callback_fallback.read_additions.push(id.clone());
                }
            }
            for id in rebinds
                .iter()
                .filter(|id| self.inputs.top_bindings.contains(*id))
            {
                if self.state.callback_fallback.rebinds.insert(id.clone()) {
                    self.state
                        .callback_fallback
                        .rebind_additions
                        .push(id.clone());
                }
            }
        }
        let callback_sizes = (
            self.state.callback_fallback.reads.len(),
            self.state.callback_fallback.rebinds.len(),
        );
        if let Some((reads_before, rebinds_before)) = self.state.fallback_callback_sizes {
            // New escaped roots can appear during a statement. Apply only
            // their newly discovered IDs, rather than repeatedly reapplying
            // the entire growing closure at the same initialization prefix.
            let reads = self.state.callback_fallback.read_additions[reads_before..]
                .iter()
                .cloned()
                .collect();
            let rebinds = self.state.callback_fallback.rebind_additions[rebinds_before..]
                .iter()
                .cloned()
                .collect();
            self.promote_lazy_effects(&reads, &rebinds, effect_owner, &mut outcome);
        } else {
            self.promote_lazy_effects(
                &self.state.callback_fallback.reads,
                &self.state.callback_fallback.rebinds,
                effect_owner,
                &mut outcome,
            );
        }
        self.state.fallback_callback_sizes = Some(callback_sizes);
        // A directly truncated callable need not escape. Follow its same
        // synchronous prefix graph without publishing private roots into the
        // persistent callback cache. Expand each local owner once per source
        // statement; earlier promotions remain in fallback_outcome.
        if !self.state.callback_fallback.expanded.contains(&owner) {
            let state = &mut *self.state;
            let reached = self.inputs.extend_available_source_units(
                [owner],
                &state.world.module.initialized_bindings,
                state.world.reachability.incomplete,
                &mut state.fallback_local_owners,
                &mut state.fallback_local_deferred,
            );
            for reached_owner in reached {
                let (reads, rebinds) = self
                    .inputs
                    .synchronous_effects(reached_owner, self.state.world.reachability.incomplete);
                let reads = reads
                    .difference(&self.state.callback_fallback.reads)
                    .cloned()
                    .collect();
                let rebinds = rebinds
                    .difference(&self.state.callback_fallback.rebinds)
                    .cloned()
                    .collect();
                self.promote_lazy_effects(&reads, &rebinds, effect_owner, &mut outcome);
            }
        }

        // The truncated body may have passed one of these callback values to
        // opaque code. Retain only this proven escape set for later calls.
        self.insert_escaped_callback_values(
            &callbacks,
            ClassMembersPublicationWriter::BoundedInvocationFallback,
            Some(span),
            Some(&publication_diagnostic),
        );
        if outcome.effects.rebinds.len() != rebinds_before {
            self.apply_rebind_effects(&outcome.effects.rebinds);
        }
        // Fallback effects are attributed to the active top-level statement,
        // not the lexical callee. Accumulate them there once instead of copying
        // and re-merging the whole summary through every nested invocation.
        self.state.fallback_outcome = outcome;
        let mut result = Outcome {
            returns: unknown(),
            unknown_call: true,
            executed_owners: BTreeSet::from([owner]),
            ..Outcome::default()
        };
        result.record_loss(ExecutionPrecisionLoss {
            reason,
            reach,
            statement_ordinal: owner,
            source_location: None,
            span,
        });
        result
    }
}
