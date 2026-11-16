//! Module-local semantic state inside one shared program world.
//!
//! Entering a foreign body changes the lexical binding view. Heap objects,
//! captured cells, native integrity and published callbacks remain in the
//! same world; module evaluation never substitutes an isolated provider heap.

use super::super::value_model::Values;
use super::{ModuleBindings, ReachabilityState, World};
use source_identity::SourceUnitOrdinal;
use std::collections::BTreeSet;

/// A completed dependency is different from a module on the ESM DFS stack.
/// Hoisting belongs to instantiation; lexical initializers belong to evaluation.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub(in crate::facts::sync_effects) enum ModuleEvaluationPhase {
    #[default]
    Instantiated,
    Evaluating {
        next_owner: SourceUnitOrdinal,
    },
    Evaluated,
    /// Some path threw, or alternative paths disagree about evaluation.
    /// This state cannot authorize replaying a module body or using an
    /// eventual initializer as an already available value.
    Indeterminate,
}

/// The state whose raw binding ordinals and owner ordinals belong to one
/// source index. It is banked whenever another module is the lexical view.
#[derive(Debug, Clone, Default, PartialEq)]
pub(in crate::facts::sync_effects) struct ModuleSemanticState {
    pub(in crate::facts::sync_effects) bindings: ModuleBindings,
    pub(in crate::facts::sync_effects) reachability: ReachabilityState,
    pub(in crate::facts::sync_effects) phase: ModuleEvaluationPhase,
    pub(in crate::facts::sync_effects) exported_runtime_values: Values,
    pub(in crate::facts::sync_effects) private_constructor_result_owners:
        BTreeSet<SourceUnitOrdinal>,
    pub(in crate::facts::sync_effects) accepted_deferred_global_writes: BTreeSet<SourceUnitOrdinal>,
}

impl ModuleSemanticState {
    /// Exchange source-local cells and owner sets, preserving the caller's
    /// shared heap and identity allocators. Reentering a module observes its
    /// existing live cells, including a still-evaluating prefix.
    pub(in crate::facts::sync_effects) fn exchange(&mut self, world: &mut World) {
        std::mem::swap(&mut self.bindings, &mut world.module);
        std::mem::swap(&mut self.reachability, &mut world.reachability);
        std::mem::swap(&mut self.phase, &mut world.module_phase);
        std::mem::swap(
            &mut self.exported_runtime_values,
            &mut world.publication.exported_runtime_values,
        );
        std::mem::swap(
            &mut self.private_constructor_result_owners,
            &mut world.classes.private_constructor_result_owners,
        );
        std::mem::swap(
            &mut self.accepted_deferred_global_writes,
            &mut world.publication.accepted_deferred_global_writes,
        );
    }
}
