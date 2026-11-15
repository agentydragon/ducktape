//! Read dependencies of the retained-reference callback walk.
//!
//! The caller separately guards the evaluated roots and source inputs. These
//! COW snapshots cover the mutable graph traversed from those roots; they carry
//! neither source-owner effects nor permission to execute a callback.

use super::super::value_model::{AbstractValue, LexicalCellId, RuntimeCallSite, Values};
use super::super::world::World;
use super::super::world::descriptor_heap::OwnDescriptorMapState;
use super::super::world::object_surface::AppliedAccessor;
use super::super::world::prefix_values::PrefixValues;
use super::super::world::program_modules::ModuleSemanticState;
use crate::program_execution::{RuntimeAllocationId, RuntimeSourceScope};
use imbl::OrdMap;
use std::collections::{BTreeMap, BTreeSet};
use swc_ecma_ast::Id;

/// Sync with `walk_callback_values` and `visit_stored_references_with_edges`.
/// A persistent clone makes later mutation detach the collection, so pointer
/// equality proves its contents have not changed.
pub(in crate::facts::sync_effects) struct CallbackGraphReads {
    global_escaped_callbacks: Values,
    prefix_object_properties: OrdMap<RuntimeAllocationId, BTreeMap<Option<String>, Values>>,
    applied_accessors: OrdMap<RuntimeAllocationId, BTreeMap<String, AppliedAccessor>>,
    descriptors: OrdMap<RuntimeCallSite, OwnDescriptorMapState>,
    /// Every expanded closure is retained in the walk's result. Its immutable
    /// capture identifies all live cells read while expanding that closure.
    cells: BTreeMap<LexicalCellId, Values>,
    namespaces: Option<NamespaceReads>,
}

/// Namespace exports can resolve through reexports into any live module bank.
/// Keep the complete bank identity initially: a source activation can cause a
/// conservative cache miss even if its semantic export values did not change.
struct NamespaceReads {
    inactive_modules: OrdMap<RuntimeSourceScope, ModuleSemanticState>,
    active_prefix_values: PrefixValues,
    active_initialized_bindings: BTreeSet<Id>,
    active_default_export: Option<(bool, Values)>,
}

impl CallbackGraphReads {
    pub(in crate::facts::sync_effects) fn new(world: &World, callbacks: &Values) -> Self {
        let cells = callbacks
            .iter()
            .filter_map(|value| match value {
                AbstractValue::Closure(_, captured) => Some(captured),
                _ => None,
            })
            .flat_map(|captured| captured.values.cells.values())
            .map(|cell| (*cell, world.cells[cell.0].values.clone()))
            .collect();
        let namespaces = callbacks
            .iter()
            .any(|value| matches!(value, AbstractValue::ModuleNamespace(_)))
            .then(|| NamespaceReads {
                inactive_modules: world.inactive_modules.clone(),
                active_prefix_values: world.module.prefix_values.clone(),
                active_initialized_bindings: world.module.initialized_bindings.clone(),
                active_default_export: world
                    .module
                    .default_export
                    .as_ref()
                    .map(|cell| (cell.initialized, cell.values.clone())),
            });
        Self {
            global_escaped_callbacks: world.publication.global_escaped_callbacks.clone(),
            prefix_object_properties: world.surface.prefix_object_properties.clone(),
            applied_accessors: world.surface.applied_accessors.clone(),
            descriptors: world.descriptors.clone(),
            cells,
            namespaces,
        }
    }

    pub(in crate::facts::sync_effects) fn unchanged(&self, world: &World) -> bool {
        self.global_escaped_callbacks
            .ptr_eq(&world.publication.global_escaped_callbacks)
            && self
                .prefix_object_properties
                .ptr_eq(&world.surface.prefix_object_properties)
            && self
                .applied_accessors
                .ptr_eq(&world.surface.applied_accessors)
            && self.descriptors.ptr_eq(&world.descriptors)
            && self.cells.iter().all(|(id, values)| {
                world
                    .cells
                    .get(*id)
                    .is_some_and(|cell| cell.values.ptr_eq(values))
            })
            && self
                .namespaces
                .as_ref()
                .is_none_or(|reads| reads.unchanged(world))
    }
}

impl NamespaceReads {
    fn unchanged(&self, world: &World) -> bool {
        self.inactive_modules.ptr_eq(&world.inactive_modules)
            && self
                .active_prefix_values
                .ptr_eq(&world.module.prefix_values)
            && self.active_initialized_bindings == world.module.initialized_bindings
            && match (&self.active_default_export, &world.module.default_export) {
                (None, None) => true,
                (Some((initialized, values)), Some(cell)) => {
                    *initialized == cell.initialized && values.ptr_eq(&cell.values)
                }
                _ => false,
            }
    }
}

#[cfg(test)]
mod tests {
    use super::super::super::world::prefix_values::{PrefixValues, TopBindings};
    use super::super::super::world::{ModuleDefaultExport, WorldScope};
    use super::*;
    use std::sync::Arc;
    use swc_common::SyntaxContext;

    #[test]
    fn namespace_reads_invalidate_on_banked_bindings_and_default_initialization() {
        let scope = RuntimeSourceScope::fresh();
        let binding = ("held".into(), SyntaxContext::empty());
        let mut module = ModuleSemanticState::default();
        module.bindings.prefix_values =
            PrefixValues::new(Arc::new(TopBindings::new([binding.clone()])));
        module
            .bindings
            .prefix_values
            .insert(&binding, Values::from([AbstractValue::UNDEFINED]));
        let mut world = World::new(WorldScope::Application);
        world.inactive_modules.insert(scope, module);
        let callbacks = Values::from([AbstractValue::ModuleNamespace(scope)]);
        let reads = CallbackGraphReads::new(&world, &callbacks);
        assert!(reads.unchanged(&world));
        world
            .inactive_modules
            .get_mut(&scope)
            .unwrap()
            .bindings
            .prefix_values
            .insert(&binding, Values::from([AbstractValue::PRIMITIVE]));
        assert!(!reads.unchanged(&world));

        let reads = CallbackGraphReads::new(&world, &callbacks);
        world.module.initialized_bindings.insert(binding);
        assert!(!reads.unchanged(&world));

        world.module.default_export = Some(ModuleDefaultExport {
            initialized: false,
            values: Values::from([AbstractValue::UNDEFINED]),
            selection: None,
        });
        let reads = CallbackGraphReads::new(&world, &callbacks);
        world.module.default_export.as_mut().unwrap().initialized = true;
        assert!(!reads.unchanged(&world));
    }

    #[test]
    fn export_memo_rejects_changed_foreign_namespace_cells() {
        use super::super::{ExportCallbackMemos, ExportSurface};

        let scope = RuntimeSourceScope::fresh();
        let mut world = World::new(WorldScope::Application);
        world
            .inactive_modules
            .insert(scope, ModuleSemanticState::default());
        let callbacks = Values::from([AbstractValue::ModuleNamespace(scope)]);
        let memo = ExportCallbackMemos::default();
        memo.store(ExportSurface::Exported, &world, &callbacks);
        assert!(memo.get(ExportSurface::Exported, &world).is_some());
        world
            .inactive_modules
            .get_mut(&scope)
            .unwrap()
            .bindings
            .default_export = Some(ModuleDefaultExport {
            initialized: true,
            values: Values::from([AbstractValue::PRIMITIVE]),
            selection: None,
        });
        assert!(memo.get(ExportSurface::Exported, &world).is_none());
    }

    #[test]
    fn ordinary_graph_does_not_read_module_banks() {
        let mut world = World::new(WorldScope::Application);
        let callbacks = Values::from([AbstractValue::PRIMITIVE]);
        let reads = CallbackGraphReads::new(&world, &callbacks);
        world
            .inactive_modules
            .insert(RuntimeSourceScope::fresh(), ModuleSemanticState::default());
        assert!(reads.unchanged(&world));
    }
}
