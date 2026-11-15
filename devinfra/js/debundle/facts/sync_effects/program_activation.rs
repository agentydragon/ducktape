//! Source-local control caches banked beside a program's shared execution.
//!
//! These ordinals name owners in one source index. Heap/capture identities,
//! invocation budgets, native mutation state and the assumption stack stay
//! live across a call into another module.

use super::{CallbackFallbackSummary, ExecutionState, Executor, Outcome};
use crate::facts::abstract_domain::value::Value;
use crate::facts::sync_effects::value_model::{AbstractValue, Values};
use crate::facts::sync_effects::world::program_modules::ModuleSemanticState;
use crate::facts::{CallbackFactoryUseFact, ReturnedRecordUseFact};
use crate::program_execution::{ProgramValueSource, RuntimeAllocationId, RuntimeSourceScope};
use source_identity::SourceUnitOrdinal;
use std::collections::{BTreeMap, BTreeSet};
use swc_ecma_ast::Id;

pub(in crate::facts::sync_effects) struct ProgramMemberRead {
    pub(in crate::facts::sync_effects) values: Values,
    pub(in crate::facts::sync_effects) initialized: bool,
    pub(in crate::facts::sync_effects) target: Option<ProgramCellTarget>,
}

#[derive(Clone)]
pub(in crate::facts::sync_effects) enum ProgramCellTarget {
    Binding(RuntimeSourceScope, Id),
    Default(crate::program_execution::RuntimeSourceUnitId),
}

#[derive(Clone, Default)]
pub(in crate::facts::sync_effects) struct ModuleControlFrame {
    activated_native_effects: BTreeSet<(SourceUnitOrdinal, Option<u32>)>,
    class_member_effects: BTreeMap<SourceUnitOrdinal, (BTreeSet<Id>, BTreeSet<Id>)>,
    prior_reachability_roots: Option<BTreeSet<SourceUnitOrdinal>>,
    current_top_owner: Option<SourceUnitOrdinal>,
    proof_uses: Vec<CallbackFactoryUseFact>,
    returned_record_proof_uses: Vec<ReturnedRecordUseFact>,
    checking_record_factory_contract: bool,
    prior_effect_owners: BTreeSet<SourceUnitOrdinal>,
    statement_executed_owners: BTreeSet<SourceUnitOrdinal>,
    callback_fallback: CallbackFallbackSummary,
}

impl ModuleControlFrame {
    pub(in crate::facts::sync_effects) fn exchange(&mut self, state: &mut ExecutionState) {
        std::mem::swap(
            &mut self.activated_native_effects,
            &mut state.activated_native_effects,
        );
        std::mem::swap(
            &mut self.class_member_effects,
            &mut state.class_member_effects,
        );
        std::mem::swap(
            &mut self.prior_reachability_roots,
            &mut state.prior_reachability_roots,
        );
        std::mem::swap(&mut self.current_top_owner, &mut state.current_top_owner);
        std::mem::swap(&mut self.proof_uses, &mut state.proof_uses);
        std::mem::swap(
            &mut self.returned_record_proof_uses,
            &mut state.returned_record_proof_uses,
        );
        std::mem::swap(
            &mut self.checking_record_factory_contract,
            &mut state.checking_record_factory_contract,
        );
        std::mem::swap(
            &mut self.prior_effect_owners,
            &mut state.prior_effect_owners,
        );
        std::mem::swap(
            &mut self.statement_executed_owners,
            &mut state.statement_executed_owners,
        );
        std::mem::swap(&mut self.callback_fallback, &mut state.callback_fallback);
    }

    /// An inactive source has the same suspended control cursor on every
    /// alternative. Cached may-effects are unions; an expansion can be
    /// skipped later only if every alternative already performed it.
    pub(in crate::facts::sync_effects) fn join(alternatives: &[&Self]) -> Option<Self> {
        let first = *alternatives.first()?;
        if alternatives.iter().any(|frame| {
            frame.current_top_owner != first.current_top_owner
                || frame.checking_record_factory_contract != first.checking_record_factory_contract
        }) {
            return None;
        }
        let mut joined = Self {
            activated_native_effects: intersection(
                alternatives
                    .iter()
                    .map(|frame| &frame.activated_native_effects),
            ),
            prior_reachability_roots: None,
            current_top_owner: first.current_top_owner,
            checking_record_factory_contract: first.checking_record_factory_contract,
            ..Self::default()
        };
        // A prior root delta is a prefix cache. Recompute it after a join;
        // a root reached on only one leaf is not already consumed on another.
        joined.callback_fallback.expanded = intersection(
            alternatives
                .iter()
                .map(|frame| &frame.callback_fallback.expanded),
        );
        joined.callback_fallback.incomplete_expanded = alternatives
            .iter()
            .all(|frame| frame.callback_fallback.incomplete_expanded);
        for frame in alternatives {
            // Source-only memo values are equal where both alternatives
            // populated them. An entry missing on one leaf remains a memo
            // of the same immutable source, not a prefix fact.
            joined
                .class_member_effects
                .extend(frame.class_member_effects.clone());
            joined
                .prior_effect_owners
                .extend(&frame.prior_effect_owners);
            joined
                .statement_executed_owners
                .extend(&frame.statement_executed_owners);
            joined
                .callback_fallback
                .deferred
                .extend(frame.callback_fallback.deferred.clone());
            joined
                .callback_fallback
                .reads
                .extend(frame.callback_fallback.reads.iter().cloned());
            joined
                .callback_fallback
                .rebinds
                .extend(frame.callback_fallback.rebinds.iter().cloned());
            for proof in &frame.proof_uses {
                if !joined.proof_uses.contains(proof) {
                    joined.proof_uses.push(proof.clone());
                }
            }
            for proof in &frame.returned_record_proof_uses {
                if !joined.returned_record_proof_uses.contains(proof) {
                    joined.returned_record_proof_uses.push(proof.clone());
                }
            }
        }
        Some(joined)
    }
}

fn intersection<'a, T: Ord + Clone + 'a>(
    mut sets: impl Iterator<Item = &'a BTreeSet<T>>,
) -> BTreeSet<T> {
    let Some(first) = sets.next() else {
        return BTreeSet::new();
    };
    let mut result = first.clone();
    for set in sets {
        result.retain(|value| set.contains(value));
    }
    result
}

impl ExecutionState {
    /// Instantiation supplies live module cells and hoisted values. It does
    /// not run a body, declare lexical cells initialized, or import a
    /// completed world computed independently of this caller.
    pub(crate) fn install_source(
        &mut self,
        scope: RuntimeSourceScope,
        mut initial: ExecutionState,
    ) {
        let mut semantics = ModuleSemanticState::default();
        semantics.exchange(initial.world_mut());
        let mut controls = ModuleControlFrame::default();
        controls.exchange(&mut initial);
        self.world_mut().inactive_modules.insert(scope, semantics);
        self.source_controls.insert(scope, controls);
        // These are the source census's initial native hazards, not a
        // capability from a provider's eventual execution.
        self.native_point_effects
            .extend(&initial.native_point_effects);
    }
}

impl<'source, 'rule> Executor<'source, 'rule> {
    /// Summary writes invalidate the actual owning module's live cell. The
    /// scheduling owner can be in another source and is never reinterpreted
    /// as an ordinal in this cell's declaration index.
    pub(in crate::facts::sync_effects) fn apply_runtime_rebind_effects(
        &mut self,
        effects: &super::RuntimeBindingEffects,
    ) {
        let active = self.source_index().source_scope();
        let mut changed = false;
        for binding in effects.rebinds.keys() {
            let Some(source) = self.inputs.source_for_scope(binding.source) else {
                continue;
            };
            let Some(local) = binding.source.local_binding(binding) else {
                continue;
            };
            if !source.top_bindings.contains(local) {
                continue;
            }
            let world = self.state.world_mut();
            let cells = if binding.source == active {
                Some(&mut world.module)
            } else {
                world
                    .inactive_modules
                    .get_mut(&binding.source)
                    .map(|module| &mut module.bindings)
            };
            if let Some(cells) = cells {
                cells.prefix_callable_selections.remove(local);
                changed |= cells
                    .prefix_values
                    .entry_or_default(local)
                    .insert(AbstractValue::Value(Value::Top));
            }
        }
        if changed {
            self.state.world_mut().revision.bump_prefix();
        }
    }

    /// Resolve an import to its live module cell without granting readiness.
    pub(in crate::facts::sync_effects) fn imported_cell_target(
        &self,
        binding: &Id,
    ) -> Option<ProgramCellTarget> {
        if !self.source_index().imported_bindings.contains(binding) {
            return None;
        }
        let program = self.inputs.root.executables?;
        let importer = self.source_inputs().module_id?;
        let source_identity::ProgramLinkResolution::Resolved(target) =
            program.imported_source(importer, binding.0.as_ref(), None)
        else {
            return None;
        };
        self.program_cell_target(&target)
    }

    fn program_cell_target(&self, target: &ProgramValueSource) -> Option<ProgramCellTarget> {
        if let ProgramValueSource::DefaultValue { module, .. } = target {
            let source = if Some(module.as_str()) == self.inputs.root.module_id {
                self.inputs.root
            } else {
                self.inputs.program_sources?.get(module)?
            };
            let (owner, _, _) = source.index.default_export_value.as_ref()?;
            return Some(ProgramCellTarget::Default(
                source.index.source_scope().unit(*owner),
            ));
        }
        let (scope, binding) = self.program_binding_target(target)?;
        Some(ProgramCellTarget::Binding(scope, binding))
    }

    fn program_binding_target(
        &self,
        target: &ProgramValueSource,
    ) -> Option<(RuntimeSourceScope, Id)> {
        let ProgramValueSource::Binding { module, local } = target else {
            return None;
        };
        let source = if Some(module.as_str()) == self.inputs.root.module_id {
            self.inputs.root
        } else {
            self.inputs.program_sources?.get(module)?
        };
        let binding = source.top_bindings_by_name.get(local.as_str())?.as_ref()?;
        Some((source.index.source_scope(), binding.clone()))
    }

    pub(in crate::facts::sync_effects) fn module_bindings(
        &self,
        scope: RuntimeSourceScope,
    ) -> Option<&crate::facts::sync_effects::world::ModuleBindings> {
        if scope == self.source_index().source_scope() {
            Some(&self.state.world.module)
        } else {
            self.state
                .world
                .inactive_modules
                .get(&scope)
                .map(|module| &module.bindings)
        }
    }

    pub(in crate::facts::sync_effects) fn imported_binding_values(
        &self,
        binding: &Id,
    ) -> Option<(Values, bool)> {
        if !self.source_index().imported_bindings.contains(binding) {
            return None;
        }
        let program = self.inputs.root.executables?;
        let importer = self.source_inputs().module_id?;
        let source_identity::ProgramLinkResolution::Resolved(target) =
            program.imported_source(importer, binding.0.as_ref(), None)
        else {
            return None;
        };
        self.program_value(&target)
    }

    fn program_value(&self, target: &ProgramValueSource) -> Option<(Values, bool)> {
        if let Some(ProgramCellTarget::Default(owner)) = self.program_cell_target(target) {
            let cell = self
                .module_bindings(owner.source)?
                .default_export
                .as_ref()?;
            let mut values = cell.values.clone();
            if !cell.initialized {
                values.insert(AbstractValue::Value(Value::Unavailable));
            }
            return Some((values, cell.initialized));
        }
        if let ProgramValueSource::Namespace { module } = target {
            let source = if Some(module.as_str()) == self.inputs.root.module_id {
                self.inputs.root
            } else {
                self.inputs.program_sources?.get(module)?
            };
            // An incomplete namespace is handled by the ordinary conservative
            // import fallback, never as a partial exhaustive property surface.
            let source_identity::ProgramLinkResolution::Resolved(_) =
                self.inputs.root.executables?.namespace_exports(module)
            else {
                return None;
            };
            self.module_bindings(source.index.source_scope())?;
            return Some((
                Values::from([AbstractValue::ModuleNamespace(source.index.source_scope())]),
                true,
            ));
        }
        let (scope, local) = self.program_binding_target(target)?;
        let source = self.inputs.source_for_scope(scope)?;
        let cells = self.module_bindings(scope)?;
        let initialized = cells.initialized_bindings.contains(&local);
        let is_var = source.var_bindings.contains(&local);
        let mut values = cells.prefix_values.get(&local).cloned().unwrap_or_default();
        if !initialized {
            values.insert(if is_var {
                AbstractValue::UNDEFINED
            } else {
                AbstractValue::Value(Value::Unavailable)
            });
        }
        if values.is_empty() {
            return None;
        }
        Some((values, initialized || is_var))
    }

    pub(in crate::facts::sync_effects) fn namespace_member(
        &self,
        scope: RuntimeSourceScope,
        name: &str,
    ) -> Option<ProgramMemberRead> {
        let source = self.inputs.source_for_scope(scope)?;
        let program = self.inputs.root.executables?;
        match program.exported_source(source.module_id?, name) {
            source_identity::ProgramLinkResolution::Resolved(target) => {
                let (values, initialized) = self.program_value(&target)?;
                Some(ProgramMemberRead {
                    values,
                    initialized,
                    target: self.program_cell_target(&target),
                })
            }
            source_identity::ProgramLinkResolution::Missing => Some(ProgramMemberRead {
                values: Values::from([AbstractValue::UNDEFINED]),
                initialized: true,
                target: None,
            }),
            _ => None,
        }
    }

    /// Revisit the live export cells whenever a retained namespace is
    /// expanded. Keeping the namespace carrier in the escape pool also
    /// retains exports initialized after its first publication.
    pub(in crate::facts::sync_effects) fn namespace_references(
        &self,
        scope: RuntimeSourceScope,
        mut visit: impl FnMut(&AbstractValue, &str),
    ) {
        let Some(module) = self
            .inputs
            .source_for_scope(scope)
            .and_then(|source| source.module_id)
        else {
            visit(&AbstractValue::Value(Value::Top), "*");
            return;
        };
        let Some(program) = self.inputs.root.executables else {
            visit(&AbstractValue::Value(Value::Top), "*");
            return;
        };
        let source_identity::ProgramLinkResolution::Resolved(names) =
            program.namespace_exports(module)
        else {
            visit(&AbstractValue::Value(Value::Top), "*");
            return;
        };
        for name in names {
            if let Some(read) = self.namespace_member(scope, &name) {
                for value in &read.values {
                    visit(value, &name);
                }
            } else {
                visit(&AbstractValue::Value(Value::Top), &name);
            }
        }
    }

    pub(in crate::facts::sync_effects) fn promote_namespace_reads(
        &self,
        scope: RuntimeSourceScope,
        outcome: &mut Outcome,
    ) {
        let Some(module) = self
            .inputs
            .source_for_scope(scope)
            .and_then(|source| source.module_id)
        else {
            outcome.unknown_call = true;
            return;
        };
        let Some(program) = self.inputs.root.executables else {
            outcome.unknown_call = true;
            return;
        };
        let source_identity::ProgramLinkResolution::Resolved(names) =
            program.namespace_exports(module)
        else {
            outcome.unknown_call = true;
            return;
        };
        let projection = self.source_index().source_scope();
        let Some(owner) = self.state.effect_owner.or_else(|| {
            self.state
                .current_top_owner
                .map(|owner| projection.unit(owner))
        }) else {
            outcome.unknown_call = true;
            return;
        };
        for name in names {
            if let Some(ProgramMemberRead {
                initialized,
                target: Some(target),
                ..
            }) = self.namespace_member(scope, &name)
            {
                self.record_program_cell_read(&target, owner, !initialized, outcome);
            }
        }
    }

    pub(in crate::facts::sync_effects) fn initialized_import_binding(&self, binding: &Id) -> bool {
        self.imported_binding_values(binding)
            .is_some_and(|(_, initialized)| initialized)
    }

    pub(in crate::facts::sync_effects) fn imported_callable_selection(
        &self,
        binding: &Id,
        values: &Values,
    ) -> Option<crate::facts::sync_effects::kernel::frame::CallableSelection> {
        if !self.initialized_import_binding(binding) {
            return None;
        }
        self.program_cell_selection(&self.imported_cell_target(binding)?, values)
    }

    pub(in crate::facts::sync_effects) fn record_program_cell_read(
        &self,
        target: &ProgramCellTarget,
        owner: crate::program_execution::RuntimeSourceUnitId,
        uninitialized: bool,
        outcome: &mut Outcome,
    ) {
        use crate::program_execution::effects::{AvailabilityState, RuntimeAvailabilityCell};

        let states = if self.state.assumption.is_some() {
            BTreeSet::from([AvailabilityState::Assumed])
        } else {
            self.program_cell_availability(target, uninitialized)
        };
        let cell = match target {
            ProgramCellTarget::Binding(source, binding) => {
                RuntimeAvailabilityCell::Binding(source.binding(binding.clone()))
            }
            ProgramCellTarget::Default(cell) => RuntimeAvailabilityCell::Default(*cell),
        };
        let point = (
            owner,
            self.queries
                .point_path()
                .into_iter()
                .map(|step| (step.kind, step.ordinal))
                .collect(),
            cell,
        );
        outcome
            .runtime_binding_effects
            .availability_points
            .entry(point)
            .or_default()
            .push(&states);
        match target {
            ProgramCellTarget::Binding(source, binding) => {
                let cell = source.binding(binding.clone());
                outcome.record_runtime_binding_read(
                    self.source_index().source_scope(),
                    cell.clone(),
                    owner,
                    uninitialized,
                    self.state.assumption.as_ref(),
                );
                outcome
                    .runtime_binding_effects
                    .availability_reads
                    .entry(RuntimeAvailabilityCell::Binding(cell))
                    .or_default()
                    .entry(owner)
                    .or_default()
                    .extend(states);
            }
            ProgramCellTarget::Default(cell) => {
                outcome.record_runtime_default_read(
                    *cell,
                    owner,
                    uninitialized,
                    self.state.assumption.as_ref(),
                );
                outcome
                    .runtime_binding_effects
                    .availability_reads
                    .entry(RuntimeAvailabilityCell::Default(*cell))
                    .or_default()
                    .entry(owner)
                    .or_default()
                    .extend(states);
            }
        }
    }

    /// Record an actual read of a named declaration or import and attach its
    /// readiness at this execution prefix. Import aliases are not cells in
    /// the importing module: only a resolved provider target is recorded.
    pub(in crate::facts::sync_effects) fn record_named_program_cell_read(
        &self,
        binding: &Id,
        owner: crate::program_execution::RuntimeSourceUnitId,
        outcome: &mut Outcome,
    ) {
        let imported = self.source_index().imported_bindings.contains(binding);
        let target = if imported {
            self.imported_cell_target(binding)
        } else if self.source_inputs().top_bindings.contains(binding) {
            let scope = self.source_index().source_scope();
            Some(ProgramCellTarget::Binding(scope, binding.clone()))
        } else {
            None
        };
        let Some(target) = target else {
            if imported {
                // A missing or unsupported import has no canonical cell to
                // classify. The strict module schedule owns the refusal; do
                // not let this partial trace claim completeness.
                outcome.runtime_binding_effects.availability_complete = false;
            }
            return;
        };
        let initialized = if imported {
            self.initialized_import_binding(binding)
        } else {
            self.initialized_local_binding(binding)
        };
        self.record_program_cell_read(&target, owner, !initialized, outcome);
    }

    fn program_cell_availability(
        &self,
        target: &ProgramCellTarget,
        uninitialized: bool,
    ) -> BTreeSet<crate::program_execution::effects::AvailabilityState> {
        use crate::program_execution::effects::AvailabilityState;

        match target {
            ProgramCellTarget::Binding(scope, binding) => {
                let Some(source) = self.inputs.source_for_scope(*scope) else {
                    return BTreeSet::from([AvailabilityState::Unknown]);
                };
                let Some(cells) = self.module_bindings(*scope) else {
                    return BTreeSet::from([AvailabilityState::Unknown]);
                };
                if cells.initialized_bindings.contains(binding) {
                    return cells
                        .prefix_values
                        .get(binding)
                        .map(availability_states)
                        .unwrap_or_else(|| BTreeSet::from([AvailabilityState::Unknown]));
                }
                if source.var_bindings.contains(binding) {
                    return match cells.prefix_values.get(binding) {
                        Some(values) => availability_states(values),
                        None => BTreeSet::from([AvailabilityState::InitializedUndefined]),
                    };
                }
                if uninitialized {
                    BTreeSet::from([AvailabilityState::TemporalDeadZone])
                } else {
                    BTreeSet::from([AvailabilityState::Unknown])
                }
            }
            ProgramCellTarget::Default(owner) => {
                let Some(cell) = self
                    .module_bindings(owner.source)
                    .and_then(|cells| cells.default_export.as_ref())
                else {
                    return BTreeSet::from([AvailabilityState::Unknown]);
                };
                if !cell.initialized {
                    BTreeSet::from([AvailabilityState::TemporalDeadZone])
                } else {
                    availability_states(&cell.values)
                }
            }
        }
    }

    pub(in crate::facts::sync_effects) fn program_cell_selection(
        &self,
        target: &ProgramCellTarget,
        values: &Values,
    ) -> Option<crate::facts::sync_effects::kernel::frame::CallableSelection> {
        match target {
            ProgramCellTarget::Binding(source, binding) => {
                self.program_callable_selection(*source, binding, values)
            }
            ProgramCellTarget::Default(owner) => self
                .module_bindings(owner.source)?
                .default_export
                .as_ref()
                .filter(|cell| cell.initialized)?
                .selection
                .as_ref()
                .filter(|selection| selection.matches(values))
                .cloned(),
        }
    }

    pub(in crate::facts::sync_effects) fn program_callable_selection(
        &self,
        scope: RuntimeSourceScope,
        local: &Id,
        values: &Values,
    ) -> Option<crate::facts::sync_effects::kernel::frame::CallableSelection> {
        use crate::facts::sync_effects::kernel::frame::CallableSelection;
        let source = self.inputs.source_for_scope(scope)?;
        if !self
            .module_bindings(scope)?
            .initialized_bindings
            .contains(local)
        {
            return None;
        }
        if let Some(selection) = self
            .module_bindings(scope)?
            .prefix_callable_selections
            .get(local)
            .filter(|selection| selection.matches(values))
        {
            return Some(selection.clone());
        }
        let key = source.index.hoisted_bindings.get(local)?;
        if !source.index.direct_top_level_functions.contains(local)
            || !self
                .local_call_target(values)
                .is_some_and(|(target, _)| source.index.local_origin(target) == Some(*key))
        {
            return None;
        }
        CallableSelection::observed(values, self.boxed_settled_prefix_dependencies())
    }

    pub(in crate::facts::sync_effects) fn callable_inputs(
        &self,
        callable: RuntimeAllocationId,
    ) -> Option<&'source super::ExecutionInputs<'source>> {
        self.inputs.source_for_scope(callable.source)
    }

    /// Enter the defining source's lexical cells while retaining the caller's
    /// heap, captures, mutation history and resource budget. The module bank
    /// itself supplies readiness; resolving a source scope never grants it.
    pub(in crate::facts::sync_effects) fn in_source<T>(
        &mut self,
        scope: RuntimeSourceScope,
        action: impl FnOnce(&mut Self) -> T,
    ) -> Option<T> {
        let current_scope = self.source_index().source_scope();
        if current_scope == scope {
            return Some(action(self));
        }
        let source = self.inputs.source_for_scope(scope)?;
        let module = source.module_id?.to_owned();
        if source.index.source_scope() != scope
            || !self.state.world.inactive_modules.contains_key(&scope)
            || !self.state.source_controls.contains_key(&scope)
        {
            return None;
        }
        let outer_program = self.state.current_program.clone();
        let mut semantics = self.state.world_mut().inactive_modules.remove(&scope)?;
        let mut controls = self.state.source_controls.remove(&scope)?;
        semantics.exchange(self.state.world_mut());
        controls.exchange(&mut self.state);
        self.state
            .world_mut()
            .inactive_modules
            .insert(current_scope, semantics);
        self.state.source_controls.insert(current_scope, controls);
        self.state.current_program = if scope == self.inputs.root.index.source_scope() {
            None
        } else {
            Some(module)
        };
        let outer_query_source = self.queries.exchange_source(scope);
        let result = action(self);
        self.queries.exchange_source(outer_query_source);
        // A source activation always restores its cursor before a path join.
        // The bank retains every instantiated module across every leaf.
        let mut semantics = self
            .state
            .world_mut()
            .inactive_modules
            .remove(&current_scope)
            .expect("a balanced source activation retains its suspended module cells");
        let mut controls = self
            .state
            .source_controls
            .remove(&current_scope)
            .expect("a balanced source activation retains its suspended control cursor");
        semantics.exchange(self.state.world_mut());
        controls.exchange(&mut self.state);
        self.state
            .world_mut()
            .inactive_modules
            .insert(scope, semantics);
        self.state.source_controls.insert(scope, controls);
        self.state.current_program = outer_program;
        Some(result)
    }
}

fn availability_states(
    values: &Values,
) -> BTreeSet<crate::program_execution::effects::AvailabilityState> {
    use crate::facts::abstract_domain::value::Value;
    use crate::facts::sync_effects::value_model::AbstractValue;
    use crate::program_execution::effects::AvailabilityState;
    use analysis_model::PrimKinds;

    if values.is_empty() {
        return BTreeSet::from([AvailabilityState::Unknown]);
    }
    let mut states = BTreeSet::new();
    for value in values.iter() {
        match value {
            AbstractValue::Value(Value::Top) => {
                return BTreeSet::from([AvailabilityState::Unknown]);
            }
            // Symbolic inputs stand for an externally supplied value whose
            // `undefined` possibility is not ruled out by the source facts.
            // Treating one as initialized non-undefined would hide a
            // difference between a ready module cell and a TDZ/uninitialized
            // read in another schedule.
            AbstractValue::SymbolicInput(_) => {
                return BTreeSet::from([AvailabilityState::Unknown]);
            }
            AbstractValue::Value(Value::Unavailable) => {
                states.insert(AvailabilityState::TemporalDeadZone);
            }
            AbstractValue::Value(Value::Prim(kinds)) => {
                if kinds.intersects(PrimKinds::UNDEFINED) {
                    states.insert(AvailabilityState::InitializedUndefined);
                }
                if !kinds.without(PrimKinds::UNDEFINED).is_empty() {
                    states.insert(AvailabilityState::InitializedValue);
                }
                if kinds.is_empty() {
                    states.insert(AvailabilityState::Unknown);
                }
            }
            AbstractValue::Value(_)
            | AbstractValue::ModuleNamespace(_)
            | AbstractValue::Closure(..)
            | AbstractValue::ClassMembers(_)
            | AbstractValue::Instance(..)
            | AbstractValue::CallbackFactory
            | AbstractValue::InlineCallbackFactory
            | AbstractValue::DeferredCallbacks(_)
            | AbstractValue::AuditedRetainedResult(..)
            | AbstractValue::ForeignDeferredRecord
            | AbstractValue::ObjectAssign
            | AbstractValue::ArrayIntrinsic
            | AbstractValue::GlobalObject
            | AbstractValue::OwnDescriptorMap(_) => {
                states.insert(AvailabilityState::InitializedValue);
            }
        }
    }
    if states.is_empty() {
        states.insert(AvailabilityState::Unknown);
    }
    states
}

#[cfg(test)]
mod availability_tests {
    use super::availability_states;
    use crate::facts::abstract_domain::value::Value;
    use crate::facts::sync_effects::value_model::{
        AbstractValue, ProviderInput, ProviderScopeId, SymbolicInputId, Values,
    };
    use crate::program_execution::effects::AvailabilityState;
    use analysis_model::PrimKinds;
    use std::collections::BTreeSet;

    #[test]
    fn availability_distinguishes_var_undefined_and_lexical_tdz() {
        assert_eq!(
            availability_states(&Values::from([AbstractValue::UNDEFINED])),
            BTreeSet::from([AvailabilityState::InitializedUndefined]),
        );
        assert_eq!(
            availability_states(&Values::from([AbstractValue::Value(Value::Unavailable)])),
            BTreeSet::from([AvailabilityState::TemporalDeadZone]),
        );
        assert_eq!(
            availability_states(&Values::from([AbstractValue::Value(Value::Top)])),
            BTreeSet::from([AvailabilityState::Unknown]),
        );
    }

    #[test]
    fn availability_preserves_joined_undefined_and_defined_values() {
        let value =
            AbstractValue::Value(Value::Prim(PrimKinds::UNDEFINED.union(PrimKinds::NUMBER)));
        assert_eq!(
            availability_states(&Values::from([value])),
            BTreeSet::from([
                AvailabilityState::InitializedUndefined,
                AvailabilityState::InitializedValue,
            ]),
        );
    }

    #[test]
    fn symbolic_input_does_not_claim_non_undefined_availability() {
        let value = AbstractValue::SymbolicInput(SymbolicInputId {
            scope: ProviderScopeId::fresh().unwrap(),
            input: ProviderInput::Argument(0),
        });
        assert_eq!(
            availability_states(&Values::from([value])),
            BTreeSet::from([AvailabilityState::Unknown]),
        );
    }
}
