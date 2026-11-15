//! Conditional invocation transitions. A complete witness precedes replay;
//! fresh graph nodes are renamed together, never copied as existing objects.

use super::*;
use crate::facts::{EffectCell, HeapContentAccesses, HeapContentCells};
use crate::program_execution::{RuntimeActivationId, effects::ProgramAllocationEvent};

#[derive(Clone)]
pub(in crate::facts::sync_effects) struct InvocationWitness {
    world: Arc<World>,
    controls: Arc<ExecutionControls>,
    observations: Observations,
    active: BTreeSet<ActiveInvocation>,
    invocation_count: usize,
    source_covered: bool,
    allocation_judgment: super::super::allocation_transfer::AllocationJudgment,
    native_candidate: Option<NativeConstructorCandidate>,
    native_transfer: Option<NativeConstructorTransfer>,
    query_cursor: super::super::runtime_inference::RuntimeQueryCursor,
    query_dependencies: ProofDependencies,
}

impl InvocationWitness {
    pub(super) fn capture(executor: &Executor<'_, '_>) -> Self {
        // Exhaustive classification: arenas and budgets stay live; every
        // readable semantic field is in World or the COW control payload.
        let ExecutionState {
            controls,
            world,
            capture_contexts: _,
            callback_sets: _,
            active,
            invocation_memos: _,
            export_callback_memos: _,
            foreign_callback_memos: _,
            invocation_depth: _,
            invocation_count,
            peak_invocation_depth: _,
        } = &*executor.state;
        Self {
            world: world.clone(),
            controls: controls.clone(),
            observations: executor.observations.clone(),
            active: active.clone(),
            invocation_count: *invocation_count,
            source_covered: executor.source_covered,
            allocation_judgment: executor.allocation_judgment.clone(),
            native_candidate: executor.native_initializer_candidate,
            native_transfer: executor.native_initializer_transfer.clone(),
            query_cursor: executor.queries.cursor(),
            query_dependencies: executor.queries.dependencies(),
        }
    }

    fn matches(&self, executor: &Executor<'_, '_>) -> bool {
        self.source_covered == executor.source_covered
            && self.query_cursor == executor.queries.cursor()
            && self.query_dependencies == executor.queries.dependencies()
            && self.allocation_judgment == executor.allocation_judgment
            && self.native_candidate == executor.native_initializer_candidate
            && self.native_transfer == executor.native_initializer_transfer
            && self.matches_state(&executor.state, &executor.observations)
    }
    fn matches_state(&self, state: &ExecutionState, observations: &Observations) -> bool {
        Arc::ptr_eq(&self.controls, &state.controls)
            && Arc::ptr_eq(&self.observations.data, &observations.data)
            && self.active == state.active
            && self.world.same_contents(&state.world)
    }
}

#[derive(Clone)]
pub(in crate::facts::sync_effects) struct InvocationMemo {
    before: InvocationWitness,
    after_world: Arc<World>,
    after_controls: Arc<ExecutionControls>,
    after_observations: Observations,
    pub(super) outcome: Outcome,
    expansions: usize,
    depth_bound: usize,
    event_prefix: Option<ProgramAllocationEvent>,
    allocation_judgment: super::super::allocation_transfer::AllocationJudgment,
    source_covered: bool,
    native_candidate: Option<NativeConstructorCandidate>,
    native_transfer: Option<NativeConstructorTransfer>,
    query_cursor: super::super::runtime_inference::RuntimeQueryCursor,
    query_dependencies: ProofDependencies,
}

impl InvocationMemo {
    pub(super) fn capture(
        executor: &Executor<'_, '_>,
        before: InvocationWitness,
        outcome: Outcome,
    ) -> Self {
        let expansions = executor.state.invocation_count - before.invocation_count;
        Self {
            before,
            after_world: executor.state.world.clone(),
            after_controls: executor.state.controls.clone(),
            after_observations: executor.observations.clone(),
            outcome,
            expansions,
            depth_bound: executor.state.peak_invocation_depth,
            event_prefix: executor.queries.allocation_prefix(),
            allocation_judgment: executor.allocation_judgment.clone(),
            source_covered: executor.source_covered,
            native_candidate: executor.native_initializer_candidate,
            native_transfer: executor.native_initializer_transfer.clone(),
            query_cursor: executor.queries.cursor(),
            query_dependencies: executor.queries.dependencies(),
        }
    }

    pub(super) fn replay(&self, executor: &mut Executor<'_, '_>) -> Option<Outcome> {
        if !self.before.matches(executor) {
            return None;
        }
        let prefix = executor.queries.allocation_prefix();
        let result = self.replay_state(&mut executor.state, &mut executor.observations, prefix)?;
        executor.allocation_judgment = self.allocation_judgment.clone();
        executor.source_covered = self.source_covered;
        executor.native_initializer_candidate = self.native_candidate;
        executor.native_initializer_transfer = self.native_transfer.clone();
        executor.queries.restore_replayed_cursor(self.query_cursor);
        executor.queries.assume(&self.query_dependencies);
        executor
            .evaluation_effects
            .extend(&result.effects.evaluation_effects);
        Some(result)
    }

    fn replay_state(
        &self,
        state: &mut ExecutionState,
        observations: &mut Observations,
        prefix: Option<ProgramAllocationEvent>,
    ) -> Option<Outcome> {
        if !self.before.matches_state(state, observations)
            || state.invocation_count.checked_add(self.expansions)? > MAX_SYNCHRONOUS_INVOCATIONS
            || state.invocation_depth.checked_add(self.depth_bound)?
                > MAX_SYNCHRONOUS_INVOCATION_DEPTH
        {
            return None;
        }
        let mut world = self.after_world.as_ref().clone();
        let mut controls = self.after_controls.as_ref().clone();
        let mut outcome = self.outcome.clone();
        let observations_after = self.rebased_observations(prefix.as_ref())?;
        let current_cells = state
            .world
            .cells
            .identities()
            .max(self.after_world.cells.identities());
        let before_cells = self.before.world.cells.identities();
        let cell_ids: BTreeMap<_, _> = self
            .after_world
            .cells
            .live_ids()
            .filter(|id| id.0 >= before_cells)
            .enumerate()
            .map(|(ordinal, id)| (id, LexicalCellId(current_cells + ordinal)))
            .collect();
        let allocations: BTreeMap<_, _> = self
            .after_world
            .allocation_events
            .keys()
            .filter(|id| !self.before.world.allocation_events.contains_key(*id))
            .map(|id| (*id, RuntimeActivationId::fresh()))
            .collect();
        let no_fresh_nodes = allocations.is_empty() && cell_ids.is_empty();
        let mut mapper = IdentityReplay {
            allocations,
            cells: cell_ids,
            contexts: BTreeMap::new(),
            sets: BTreeMap::new(),
            context_interner: &mut state.capture_contexts,
            set_interner: &mut state.callback_sets,
            old_prefix: self.event_prefix.as_ref(),
            new_prefix: prefix.as_ref(),
            portable_events: BTreeMap::new(),
        };
        for (old, event) in &world.allocation_events {
            if mapper.allocations.contains_key(old)
                && let Some(event) = event
            {
                let mapped = mapper.rebase_event(event)?;
                mapper.portable_events.insert(event.clone(), mapped);
            }
        }
        mapper.world(&mut world, current_cells);
        mapper.controls(&mut controls);
        mapper.outcome(&mut outcome);
        drop(mapper);
        state.world = if no_fresh_nodes {
            self.after_world.clone()
        } else {
            Arc::new(world)
        };
        state.controls = if no_fresh_nodes {
            self.after_controls.clone()
        } else {
            Arc::new(controls)
        };
        state.invocation_count += self.expansions;
        *observations = observations_after;
        Some(outcome)
    }

    fn rebased_observations(
        &self,
        prefix: Option<&ProgramAllocationEvent>,
    ) -> Option<Observations> {
        let start = self.before.observations.native_prefix_observations.len();
        if self.after_observations.native_prefix_observations.len() == start {
            return Some(self.after_observations.clone());
        }
        let old = self.event_prefix.as_ref()?;
        let new = prefix?;
        let mut observations = self.after_observations.clone();
        for observation in &mut observations.native_prefix_observations[start..] {
            let path = &mut observation.program_point_path;
            if path.len() < old.path.len()
                || !path
                    .iter()
                    .zip(&old.path)
                    .all(|(step, (kind, ordinal))| step.kind == *kind && step.ordinal == *ordinal)
            {
                return None;
            }
            *path = new
                .path
                .iter()
                .map(
                    |(kind, ordinal)| crate::reports::schema::NativeProgramPointStep {
                        kind: *kind,
                        ordinal: *ordinal,
                    },
                )
                .chain(path[old.path.len()..].iter().cloned())
                .collect();
        }
        Some(observations)
    }
}

pub(in crate::facts::sync_effects) struct IdentityReplay<'a> {
    allocations: BTreeMap<RuntimeActivationId, RuntimeActivationId>,
    cells: BTreeMap<LexicalCellId, LexicalCellId>,
    contexts: BTreeMap<usize, CapturedContext>,
    sets: BTreeMap<usize, Captured<Values>>,
    context_interner: &'a mut CaptureInterner<LexicalCaptures>,
    set_interner: &'a mut CaptureInterner<Values>,
    old_prefix: Option<&'a ProgramAllocationEvent>,
    new_prefix: Option<&'a ProgramAllocationEvent>,
    portable_events: BTreeMap<ProgramAllocationEvent, ProgramAllocationEvent>,
}

fn ord_map<K: Ord + Clone, V: Clone, L: Ord + Clone, W: Clone>(
    map: &mut OrdMap<K, V>,
    mut f: impl FnMut(K, V) -> (L, W),
) -> OrdMap<L, W> {
    map.iter()
        .map(|(key, value)| f(key.clone(), value.clone()))
        .collect()
}

impl IdentityReplay<'_> {
    fn allocation(&self, allocation: RuntimeAllocationId) -> RuntimeAllocationId {
        allocation
            .activation()
            .and_then(|id| self.allocations.get(&id))
            .map_or(allocation, |id| allocation.source_site().instantiated(*id))
    }
    fn cell(&self, cell: LexicalCellId) -> LexicalCellId {
        self.cells.get(&cell).copied().unwrap_or(cell)
    }
    fn allocation_set(&self, values: &mut BTreeSet<RuntimeAllocationId>) {
        *values = values.iter().map(|id| self.allocation(*id)).collect();
    }
    fn rebase_event(&self, event: &ProgramAllocationEvent) -> Option<ProgramAllocationEvent> {
        let old = self.old_prefix?;
        let new = self.new_prefix?;
        if event.owner != old.owner || !event.path.starts_with(&old.path) {
            return None;
        }
        let mut path = new.path.clone();
        path.extend_from_slice(&event.path[old.path.len()..]);
        Some(ProgramAllocationEvent {
            owner: new.owner.clone(),
            path,
            occurrence: event.occurrence,
        })
    }
    fn context(&mut self, captured: &CapturedContext) -> CapturedContext {
        if let Some(result) = self.contexts.get(&captured.cache_identity()) {
            return result.clone();
        }
        let mut value = captured.values.as_ref().clone();
        self.context_values(&mut value.snapshots);
        value.cells = value
            .cells
            .iter()
            .map(|(binding, id)| (binding.clone(), self.cell(*id)))
            .collect();
        value.home_object = value.home_object.map(|id| self.allocation(id));
        let result = self.context_interner.intern(value);
        self.contexts
            .insert(captured.cache_identity(), result.clone());
        result
    }
    fn retained(&mut self, captured: &Captured<Values>) -> Captured<Values> {
        if let Some(result) = self.sets.get(&captured.cache_identity()) {
            return result.clone();
        }
        let mut value = captured.values.as_ref().clone();
        self.values(&mut value);
        let result = self.set_interner.intern(value);
        self.sets.insert(captured.cache_identity(), result.clone());
        result
    }
    fn value(&mut self, value: AbstractValue) -> AbstractValue {
        match value {
            AbstractValue::Value(value) => {
                AbstractValue::Value(value.map_allocation(|id| self.allocation(id)))
            }
            AbstractValue::Closure(mut address, captured) => {
                address.callable = self.allocation(address.callable);
                AbstractValue::Closure(address, self.context(&captured))
            }
            AbstractValue::ClassMembers(id) => AbstractValue::ClassMembers(self.allocation(id)),
            AbstractValue::Instance(id, values, origin) => {
                AbstractValue::Instance(self.allocation(id), self.retained(&values), origin)
            }
            AbstractValue::DeferredCallbacks(values) => {
                AbstractValue::DeferredCallbacks(self.retained(&values))
            }
            AbstractValue::AuditedRetainedResult(kind, values) => {
                AbstractValue::AuditedRetainedResult(kind, self.retained(&values))
            }
            // These are instantiated module/host capabilities or source-only
            // carriers; none denotes an activation-local heap node.
            AbstractValue::OwnDescriptorMap(site) => {
                AbstractValue::OwnDescriptorMap(site.map_allocation(|id| self.allocation(id)))
            }
            other @ (AbstractValue::ModuleNamespace(_)
            | AbstractValue::SymbolicInput(_)
            | AbstractValue::CallbackFactory
            | AbstractValue::InlineCallbackFactory
            | AbstractValue::ForeignDeferredRecord
            | AbstractValue::ObjectAssign
            | AbstractValue::ArrayIntrinsic
            | AbstractValue::GlobalObject) => other,
        }
    }
    fn values(&mut self, values: &mut Values) {
        *values = values
            .iter()
            .cloned()
            .map(|value| self.value(value))
            .collect();
    }
    fn context_values(&mut self, values: &mut Context) {
        *values = ord_map(values, |key, mut value| {
            self.values(&mut value);
            (key, value)
        });
    }
    fn selection(&mut self, selection: &mut CallableSelection) {
        selection.target = self.value(selection.target.clone());
    }
    fn bindings(&mut self, bindings: &mut super::super::world::ModuleBindings) {
        let super::super::world::ModuleBindings {
            default_export,
            prefix_values,
            prefix_callable_selections,
            initialized_bindings: _,
            top_env,
            top_captures,
        } = bindings;
        if let Some(cell) = default_export {
            self.values(&mut cell.values);
            if let Some(selection) = &mut cell.selection {
                self.selection(selection);
            }
        }
        prefix_values.map_values(|values| self.values(values));
        *prefix_callable_selections = ord_map(prefix_callable_selections, |id, mut selection| {
            self.selection(&mut selection);
            (id, selection)
        });
        self.context_values(top_env);
        self.context_values(top_captures);
    }
    fn effect_cell(
        &self,
        cell: crate::program_execution::RuntimeEffectCell,
    ) -> crate::program_execution::RuntimeEffectCell {
        match cell {
            EffectCell::Binding(binding) => EffectCell::Binding(binding),
            EffectCell::GlobalProp(property) => EffectCell::GlobalProp(property),
            EffectCell::ArrayContents(id) => EffectCell::ArrayContents(self.allocation(id)),
            EffectCell::SetContents(id) => EffectCell::SetContents(self.allocation(id)),
            EffectCell::ObjectShape(id) => EffectCell::ObjectShape(self.allocation(id)),
            EffectCell::OwnProperty { origin, property } => EffectCell::OwnProperty {
                origin: self.allocation(origin),
                property,
            },
        }
    }
    fn heap(&self, heap: &mut crate::program_execution::RuntimeHeapContentCells) {
        let HeapContentCells {
            allocations,
            reads,
            writes,
            opaque: _,
            observes_external_heap: _,
        } = heap;
        for cells in [allocations, reads, writes] {
            *cells = cells
                .iter()
                .cloned()
                .map(|cell| self.effect_cell(cell))
                .collect();
        }
    }
    fn accesses(&self, accesses: &mut crate::program_execution::RuntimeHeapContentAccesses) {
        let HeapContentAccesses { reads, writes } = accesses;
        for cells in [reads, writes] {
            *cells = cells
                .iter()
                .cloned()
                .map(|cell| self.effect_cell(cell))
                .collect();
        }
    }
    pub(in crate::facts::sync_effects) fn outcome(&mut self, outcome: &mut Outcome) {
        let Outcome {
            effects,
            runtime_binding_effects: _,
            runtime_heap_frontiers: _,
            runtime_heap_contents,
            observed_runtime_heap_accesses,
            body_invoked: _,
            effect_coverage: _,
            root_binding_effect_identity: _,
            returns,
            return_selection,
            complete: _,
            executed_owners: _,
            unknown_call: _,
        } = outcome;
        self.values(returns);
        if let Some(selection) = return_selection {
            self.selection(selection);
        }
        self.heap(runtime_heap_contents);
        self.accesses(observed_runtime_heap_accesses);
        self.heap(&mut effects.runtime_heap_contents);
        self.accesses(&mut effects.observed_runtime_heap_accesses);
        let program = &mut effects.program_effects;
        for cells in [
            &mut program.heap_contents.allocations,
            &mut program.heap_contents.reads,
            &mut program.heap_contents.writes,
            &mut program.observed_heap_accesses.reads,
            &mut program.observed_heap_accesses.writes,
        ] {
            *cells = cells
                .iter()
                .cloned()
                .map(|cell| {
                    cell.map_allocation(|mut id| {
                        if let Some(event) = id
                            .event
                            .as_ref()
                            .and_then(|event| self.portable_events.get(event))
                        {
                            id.event = Some(event.clone());
                        }
                        id
                    })
                })
                .collect();
        }
    }
    fn world(&mut self, world: &mut World, current_cells: usize) {
        let World {
            scope: _,
            allocation_events,
            cells,
            active_cells,
            module,
            module_phase: _,
            inactive_modules,
            surface,
            descriptors,
            arrays,
            native,
            classes,
            publication,
            reachability: _,
            prerequisites: _,
            revision: _,
        } = world;
        *allocation_events = ord_map(allocation_events, |id, event| {
            let new_id = self.allocations.get(&id).copied().unwrap_or(id);
            let event =
                event.map(|event| self.portable_events.get(&event).cloned().unwrap_or(event));
            (new_id, event)
        });
        cells.map_identities(current_cells, &self.cells.clone(), |values| {
            self.values(values)
        });
        *active_cells = active_cells
            .iter()
            .map(|(binding, id)| (binding.clone(), self.cell(*id)))
            .collect();
        self.bindings(module);
        *inactive_modules = ord_map(inactive_modules, |scope, mut module| {
            self.bindings(&mut module.bindings);
            self.values(&mut module.exported_runtime_values);
            (scope, module)
        });
        surface.prefix_object_properties = ord_map(
            &mut surface.prefix_object_properties,
            |id, mut properties| {
                for values in properties.values_mut() {
                    self.values(values);
                }
                (self.allocation(id), properties)
            },
        );
        surface.writable_prefix = ord_map(&mut surface.writable_prefix, |id, value| {
            (self.allocation(id), value)
        });
        surface.applied_accessors =
            ord_map(&mut surface.applied_accessors, |id, mut properties| {
                for accessor in properties.values_mut() {
                    self.values(&mut accessor.getter);
                    self.values(&mut accessor.setter);
                }
                (self.allocation(id), properties)
            });
        for set in [
            &mut surface.ordinary_spread_copies,
            &mut surface.descriptor_copy_results,
            &mut surface.tainted_objects,
            &mut surface.unsettled_objects,
        ] {
            self.allocation_set(set);
        }
        *descriptors = ord_map(descriptors, |site, mut map| {
            map.source_object = self.allocation(map.source_object);
            self.values(&mut map.retained_values);
            for descriptor in map.properties.values_mut() {
                match descriptor {
                    super::super::property_inference::CapturedPropertyDescriptor::Data(values) => {
                        self.values(values)
                    }
                    super::super::property_inference::CapturedPropertyDescriptor::Accessor {
                        getter,
                        setter,
                    } => {
                        self.values(getter);
                        self.values(setter);
                    }
                }
            }
            (site.map_allocation(|id| self.allocation(id)), map)
        });
        arrays.dense = ord_map(&mut arrays.dense, |id, mut dense| {
            for values in &mut dense.slots {
                self.values(values);
            }
            (self.allocation(id), dense)
        });
        arrays.primitive = ord_map(&mut arrays.primitive, |id, value| {
            (self.allocation(id), value)
        });
        self.allocation_set(&mut arrays.unknown_contents);
        arrays.accumulated_values = ord_map(&mut arrays.accumulated_values, |id, mut values| {
            self.values(&mut values);
            (self.allocation(id), values)
        });
        self.allocation_set(&mut native.tainted_sets);
        self.allocation_set(&mut native.tainted_functions);
        native.revoked_audited_results = native
            .revoked_audited_results
            .values()
            .map(|values| {
                let value = self.retained(values);
                (value.cache_identity(), value)
            })
            .collect();
        classes.superclasses = ord_map(&mut classes.superclasses, |id, mut values| {
            self.values(&mut values);
            (self.allocation(id), values)
        });
        for set in [
            &mut classes.tainted,
            &mut classes.escaped_capabilities,
            &mut classes.checked_constructor_inputs,
        ] {
            self.allocation_set(set);
        }
        classes.first_members_publications = classes
            .first_members_publications
            .iter()
            .map(|(id, report)| (self.allocation(*id), report.clone()))
            .collect();
        self.values(&mut publication.global_escaped_callbacks);
        self.values(&mut publication.exported_runtime_values);
        publication.first_callback_publication_owners = publication
            .first_callback_publication_owners
            .iter()
            .map(|(id, owner)| (self.allocation(*id), *owner))
            .collect();
        self.allocation_set(&mut publication.parameter_initialized_objects);
    }
    fn controls(&mut self, controls: &mut ExecutionControls) {
        let ExecutionControls {
            current_program: _,
            effect_owner: _,
            source_controls: _,
            native_point_effects: _,
            activated_native_effects: _,
            author_host_contract_uses: _,
            class_member_effects: _,
            active_class_member_promotions,
            prior_reachability_roots: _,
            current_top_owner: _,
            proof_uses: _,
            returned_record_proof_uses: _,
            checking_record_factory_contract: _,
            nested_record_factory_premise,
            prior_effect_owners: _,
            statement_executed_owners: _,
            statement_invoked_callables,
            array_iteration_depth: _,
            iteration_element_records,
            array_iteration_protected_objects,
            array_iteration_unsupported: _,
            callback_fallback: _,
            assumption: _,
        } = controls;
        self.allocation_set(active_class_member_promotions);
        *nested_record_factory_premise =
            nested_record_factory_premise.map(|(a, b)| (self.allocation(a), self.allocation(b)));
        self.allocation_set(statement_invoked_callables);
        self.allocation_set(iteration_element_records);
        self.allocation_set(array_iteration_protected_objects);
    }
}

#[cfg(test)]
mod tests {
    use super::super::super::world::cells::LexicalCell;
    use super::*;
    use crate::facts::LocalClassConstructionShapes;
    use crate::program_execution::effects::ProgramSourceUnitId;
    use crate::reports::schema::NativeProgramPointKind;

    fn source() -> ExecutionIndex {
        let module =
            raw_js_test_support::parse("function make() { let held = {}; return () => held; }");
        let views = crate::facts::top_level_item_views(&module.body);
        ExecutionIndex::collect(
            &views,
            &AnalysisInputs::default(),
            None,
            &BTreeSet::new(),
            &LocalClassConstructionShapes::default(),
        )
    }
    fn state() -> ExecutionState {
        ExecutionState {
            controls: Arc::new(ExecutionControls::default()),
            world: Arc::new(World::new(WorldScope::Application)),
            capture_contexts: CaptureInterner::default(),
            callback_sets: CaptureInterner::default(),
            active: BTreeSet::new(),
            invocation_memos: OrdMap::new(),
            export_callback_memos: ExportCallbackMemos::default(),
            foreign_callback_memos: ForeignCallbackMemos::default(),
            invocation_depth: 0,
            invocation_count: 0,
            peak_invocation_depth: 0,
        }
    }
    fn memo(
        state: &ExecutionState,
        observations: &Observations,
        after: World,
        outcome: Outcome,
    ) -> InvocationMemo {
        InvocationMemo {
            before: InvocationWitness {
                world: state.world.clone(),
                controls: state.controls.clone(),
                observations: observations.clone(),
                active: state.active.clone(),
                invocation_count: state.invocation_count,
                source_covered: false,
                allocation_judgment:
                    super::super::super::allocation_transfer::AllocationJudgment::Opaque,
                native_candidate: None,
                native_transfer: None,
                query_cursor: Default::default(),
                query_dependencies: Default::default(),
            },
            after_world: Arc::new(after),
            after_controls: state.controls.clone(),
            after_observations: observations.clone(),
            outcome,
            expansions: 1,
            depth_bound: 1,
            event_prefix: None,
            allocation_judgment:
                super::super::super::allocation_transfer::AllocationJudgment::Opaque,
            source_covered: false,
            native_candidate: None,
            native_transfer: None,
            query_cursor: Default::default(),
            query_dependencies: Default::default(),
        }
    }

    // Snapshot/replay is an internal state seam: CLI output alone cannot
    // distinguish a real cache hit from executing an equivalent body again.
    #[test]
    fn unchanged_state_reuses_transition_and_charges_each_call() {
        let index = source();
        let scope = index.source_scope();
        let owner = index.callables.values().next().unwrap().owner;
        let mut state = state();
        let mut observations = Observations::default();
        let binding = ("held".into(), SyntaxContext::empty());
        let mut outcome = Outcome {
            complete: true,
            ..Default::default()
        };
        outcome.record_runtime_binding_read(
            scope,
            scope.binding(binding.clone()),
            scope.unit(owner),
            true,
            Some(&AssumedEffectReason::EscapedCallbackClosure),
        );
        let memo = memo(&state, &observations, state.world.as_ref().clone(), outcome);
        for _ in 0..2 {
            let replay = memo
                .replay_state(&mut state, &mut observations, None)
                .expect("unchanged pure state is reusable");
            assert!(
                replay
                    .runtime_binding_effects
                    .uninitialized_reads
                    .contains(&scope.binding(binding.clone()))
            );
            assert_eq!(replay.runtime_binding_effects.fallback_only.reads.len(), 1);
        }
        assert_eq!(state.invocation_count, 2);
        state.invocation_count = MAX_SYNCHRONOUS_INVOCATIONS;
        assert!(
            memo.replay_state(&mut state, &mut observations, None)
                .is_none()
        );
        state.invocation_count = 0;
        state.invocation_depth = MAX_SYNCHRONOUS_INVOCATION_DEPTH;
        assert!(
            memo.replay_state(&mut state, &mut observations, None)
                .is_none()
        );
        state.invocation_depth = 0;
        state.active.insert(ActiveInvocation {
            callable: index.allocation(*index.callables.keys().next().unwrap()),
            top_owner: None,
        });
        assert!(
            memo.replay_state(&mut state, &mut observations, None)
                .is_none()
        );
    }

    #[test]
    fn replay_rebases_new_native_observation_paths_and_keeps_prior_reports() {
        use crate::reports::schema::NativeProgramPointStep;
        let mut state = state();
        let observation = |ordinal| NativePrefixObservation {
            guard: NativeIntegrityGuard::NativeCallIdentity,
            program_point_path: vec![NativeProgramPointStep {
                kind: NativeProgramPointKind::Invoke,
                ordinal,
            }],
            property: None,
            prior_affected_roots: BTreeSet::new(),
            prior_blocking_prototype_mutations: 0,
            uncertain_execution: false,
        };
        let mut observations = Observations::default();
        observations.observe_native_prefix(observation(9));
        let mut memo = memo(
            &state,
            &observations,
            state.world.as_ref().clone(),
            Outcome::default(),
        );
        memo.event_prefix = Some(ProgramAllocationEvent {
            owner: ProgramSourceUnitId {
                module: "app.js".into(),
                ordinal: SourceUnitOrdinal(0),
            },
            path: vec![(NativeProgramPointKind::Invoke, 0)],
            occurrence: 0,
        });
        memo.after_observations
            .observe_native_prefix(observation(0));
        let prefix = ProgramAllocationEvent {
            path: vec![(NativeProgramPointKind::Invoke, 1)],
            ..memo.event_prefix.clone().unwrap()
        };
        memo.replay_state(&mut state, &mut observations, Some(prefix))
            .expect("native observations have checked request paths");
        assert_eq!(
            observations.native_prefix_observations,
            vec![observation(9), observation(1)]
        );
    }

    #[test]
    fn mutable_heap_cells_descriptors_native_state_and_controls_invalidate_reuse() {
        let index = source();
        let object = index.allocation(*index.own_descriptor_shapes.keys().next().unwrap());
        let mut state = state();
        let cell = state.world_mut().cells.insert(LexicalCell {
            values: Values::from([AbstractValue::UNDEFINED]),
            kind: LocalBinding::Let,
            initialization: CellInitialization::Initialized,
        });
        let mut observations = Observations::default();
        let memo = memo(
            &state,
            &observations,
            state.world.as_ref().clone(),
            Outcome::default(),
        );
        let base = state.world.clone();
        state.world_mut().surface.define_data_property(
            object,
            "value".into(),
            &Values::from([AbstractValue::PRIMITIVE]),
        );
        assert!(
            memo.replay_state(&mut state, &mut observations, None)
                .is_none()
        );
        state.world = base.clone();
        state
            .world_mut()
            .cells
            .store_declared(cell, &Values::from([AbstractValue::PRIMITIVE]));
        assert!(
            memo.replay_state(&mut state, &mut observations, None)
                .is_none()
        );
        state.world = base.clone();
        state.world_mut().surface.applied_accessors.insert(
            object,
            BTreeMap::from([("value".into(), AppliedAccessor::default())]),
        );
        assert!(
            memo.replay_state(&mut state, &mut observations, None)
                .is_none()
        );
        state.world = base.clone();
        state.world_mut().native.uncertain_execution = true;
        assert!(
            memo.replay_state(&mut state, &mut observations, None)
                .is_none()
        );
        state.world = base;
        state.array_iteration_unsupported = true;
        assert!(
            memo.replay_state(&mut state, &mut observations, None)
                .is_none()
        );
        state.controls = memo.before.controls.clone();
        observations.native_prototype_property_checks.insert(
            "value".into(),
            NativePrototypePropertyCheck {
                property: "value".into(),
                intact: false,
                blocking_mutation_count: 1,
            },
        );
        assert!(
            memo.replay_state(&mut state, &mut observations, None)
                .is_none()
        );
    }

    #[test]
    fn replay_renames_fresh_alias_graph_and_live_capture_cells_but_preserves_inputs() {
        let index = source();
        let scope = index.source_scope();
        let object_site = *index.own_descriptor_shapes.keys().next().unwrap();
        let function_site = *index.callables.keys().next_back().unwrap();
        let mut state = state();
        let mut observations = Observations::default();
        let input = scope
            .allocation(object_site)
            .instantiated(RuntimeActivationId::fresh());
        let object = scope
            .allocation(object_site)
            .instantiated(RuntimeActivationId::fresh());
        let closure = scope
            .allocation(function_site)
            .instantiated(RuntimeActivationId::fresh());
        let old_prefix = ProgramAllocationEvent {
            owner: ProgramSourceUnitId {
                module: "app.js".into(),
                ordinal: SourceUnitOrdinal(0),
            },
            path: vec![(NativeProgramPointKind::Invoke, 0)],
            occurrence: 0,
        };
        let new_prefix = ProgramAllocationEvent {
            path: vec![(NativeProgramPointKind::Invoke, 1)],
            ..old_prefix.clone()
        };
        state
            .world_mut()
            .allocation_events
            .insert(input.activation().unwrap(), None);
        let mut after = state.world.as_ref().clone();
        for (id, occurrence) in [(object, 0), (closure, 1)] {
            after.allocation_events.insert(
                id.activation().unwrap(),
                Some(ProgramAllocationEvent {
                    occurrence,
                    ..old_prefix.clone()
                }),
            );
        }
        after.surface.define_data_property(
            object,
            "input".into(),
            &Values::from([AbstractValue::Value(Value::Alloc(Kind::Object, input))]),
        );
        after.surface.define_data_property(
            object,
            "self".into(),
            &Values::from([AbstractValue::Value(Value::Alloc(Kind::Object, object))]),
        );
        let held = ("held".into(), SyntaxContext::empty());
        let cell = after.cells.insert(LexicalCell {
            values: Values::from([AbstractValue::Value(Value::Alloc(Kind::Object, object))]),
            kind: LocalBinding::Let,
            initialization: CellInitialization::Initialized,
        });
        let captured = state.capture_contexts.intern(LexicalCaptures {
            cells: [(held.clone(), cell)].into_iter().collect(),
            ..Default::default()
        });
        let mut outcome = Outcome {
            returns: Values::from([AbstractValue::Closure(
                HeapAddress {
                    scope: WorldScope::Application,
                    callable: closure,
                },
                captured,
            )]),
            complete: true,
            ..Default::default()
        };
        outcome
            .runtime_heap_contents
            .writes
            .insert(EffectCell::OwnProperty {
                origin: object,
                property: "self".into(),
            });
        outcome.observed_runtime_heap_accesses.writes =
            outcome.runtime_heap_contents.writes.clone();
        let mut memo = memo(&state, &observations, after, outcome);
        memo.event_prefix = Some(old_prefix);
        let replay = memo
            .replay_state(&mut state, &mut observations, Some(new_prefix.clone()))
            .expect("fresh graph replay");
        let AbstractValue::Closure(address, captured) = replay.returns.first().unwrap() else {
            panic!("closure");
        };
        assert_ne!(address.callable, closure);
        let new_cell = captured.values.cells[&held];
        assert_ne!(new_cell, cell);
        let AbstractValue::Value(Value::Alloc(Kind::Object, new_object)) = state
            .world
            .cells
            .get(new_cell)
            .unwrap()
            .values
            .first()
            .unwrap()
        else {
            panic!("captured object");
        };
        assert_ne!(*new_object, object);
        assert_eq!(
            state
                .world
                .surface
                .data_values(*new_object, "self")
                .unwrap(),
            &Values::from([AbstractValue::Value(Value::Alloc(
                Kind::Object,
                *new_object
            ))])
        );
        assert_eq!(
            state
                .world
                .surface
                .data_values(*new_object, "input")
                .unwrap(),
            &Values::from([AbstractValue::Value(Value::Alloc(Kind::Object, input))])
        );
        assert!(
            replay
                .observed_runtime_heap_accesses
                .writes
                .contains(&EffectCell::OwnProperty {
                    origin: *new_object,
                    property: "self".into()
                })
        );
        let event = state
            .world
            .allocation_events
            .get(&new_object.activation().unwrap())
            .unwrap()
            .as_ref()
            .unwrap();
        assert_eq!(event.path, new_prefix.path);
        assert!(
            !state
                .world
                .allocation_events
                .contains_key(&object.activation().unwrap())
        );
    }
}
