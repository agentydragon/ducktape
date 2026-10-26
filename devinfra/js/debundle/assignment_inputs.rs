//! Per-chunk analysis state: inputs, IR, and input-derived caches.
//!
//! [`AssignmentInputs`] is what's known about a chunk before propose
//! runs — the spec-supplied logical modules, the binding catalogue,
//! the dependency graph derived from the chunk facts, and the small
//! lookup caches that depend only on inputs. The propose algorithm
//! consumes an `AssignmentInputs` plus a default destination to produce a
//! [`crate::AssignmentAnalysis`] — the assignment-and-derived state
//! that depends on which logical-module assignment the spec chose.

use std::collections::{BTreeSet, HashMap};

use swc_atoms::Atom;
use swc_ecma_ast::Id;

use analysis::reports::source_unit_key;
use analysis::{BindingKind, DependencyGraph, ModuleId, PlannedModule};

/// Per-chunk inputs + IR + input-derived caches.
///
/// Constructed once per chunk and held by reference (typically via
/// `Arc<AssignmentInputs>`) by every [`crate::AssignmentAnalysis`]
/// candidate that explores an assignment over the same dependency graph.
/// All fields are private: the lookup tables are precomputed from the
/// inputs at [`AssignmentInputs::build`] time, so a mutable input field
/// would silently stale the caches. Read access goes through the
/// accessors below.
#[derive(Debug, Clone)]
pub struct AssignmentInputs {
    chunk_id: String,
    bindings: HashMap<Id, BindingKind>,
    logical_modules: Vec<PlannedModule>,
    dependency_graph: DependencyGraph,
    source_unit_report_ids_by_binding: HashMap<Id, Vec<String>>,
    binding_lookup_by_id: HashMap<Id, BindingLookupInfo>,
}

#[derive(Debug, Clone, Default)]
struct BindingLookupInfo {
    /// Exported name when it differs from the local binding name.
    export_name: Option<Atom>,
    /// Owning logical module for `Owned` bindings. Imported/global names
    /// are absent.
    owner: Option<ModuleId>,
}

impl AssignmentInputs {
    /// Build an `AssignmentInputs` reusing a caller-supplied dependency graph.
    /// `bindings` should already have every `Owned` binding the spec
    /// assigned and every `Imported` binding the spec re-exports.
    ///
    /// `chunk_renames` (in-place readability renames for bindings
    /// that stay in entry) feed the export-name lookup table but are
    /// not retained beyond construction.
    pub fn build(
        chunk_id: String,
        dependency_graph: DependencyGraph,
        bindings: HashMap<Id, BindingKind>,
        logical_modules: Vec<PlannedModule>,
        chunk_renames: &HashMap<Id, Atom>,
    ) -> Self {
        let source_unit_report_ids_by_binding =
            build_source_unit_report_ids_by_binding(&dependency_graph);
        let binding_lookup_by_id =
            build_binding_lookup_by_id(&bindings, chunk_renames, &logical_modules);
        Self {
            chunk_id,
            bindings,
            logical_modules,
            dependency_graph,
            source_unit_report_ids_by_binding,
            binding_lookup_by_id,
        }
    }

    pub fn chunk_id(&self) -> &str {
        &self.chunk_id
    }

    /// All top-level bindings of the chunk indexed by local name.
    /// Iteration order is undefined; consumers that need a
    /// deterministic order (emit sites, error messages) must sort
    /// the keys themselves.
    pub fn bindings(&self) -> &HashMap<Id, BindingKind> {
        &self.bindings
    }

    pub fn logical_modules(&self) -> &[PlannedModule] {
        &self.logical_modules
    }

    pub fn dependency_graph(&self) -> &DependencyGraph {
        &self.dependency_graph
    }

    /// Pre-computed export name for a chunk binding, falling back
    /// to the binding's own name. Hot-path replacement for the
    /// previous `bindings` / `chunk_renames` / `rename_map` walk.
    pub fn export_name_for(&self, binding: &Id) -> Atom {
        self.binding_lookup_by_id
            .get(binding)
            .and_then(|info| info.export_name.clone())
            .unwrap_or_else(|| binding.0.clone())
    }

    /// Canonical module identity for `id` — the [`spec::ModulePath`]
    /// every wire artifact (`dependency_graph.json` module table,
    /// `cycles.json`, `atomic_group_conflicts.json`) and diagnostic
    /// uses to denote this module. Parses the in-process
    /// `PlannedModule.id` (`"<chunk_id>::<path>"`) down to the clean
    /// path; panics on an out-of-range id or unparseable identity —
    /// both are pipeline bugs, not user errors.
    pub fn module_path(&self, id: ModuleId) -> spec::ModulePath {
        let idx = id.0;
        let module = self.logical_modules.get(idx).unwrap_or_else(|| {
            panic!(
                "ModuleId logical:{idx} out of range ({} logical modules)",
                self.logical_modules.len()
            )
        });
        spec::ModulePath::parse(&module.id, &self.chunk_id).unwrap_or_else(|e| {
            panic!(
                "logical module logical:{idx} has unparseable identity {:?}: {e}",
                module.id
            )
        })
    }

    /// Which logical module owns a binding, if any.
    /// Returns `None` for names that aren't `Owned` in this analysis
    /// (e.g. globals, imported bindings, names not in the spec).
    pub fn owner_of(&self, binding: &Id) -> Option<ModuleId> {
        self.binding_lookup_by_id
            .get(binding)
            .and_then(|info| info.owner)
    }

    /// Lookup a logical module by its dense identity.
    pub fn logical_module(&self, id: ModuleId) -> Option<&PlannedModule> {
        self.logical_modules.get(id.0)
    }

    pub fn source_unit_report_ids_for_bindings<'a>(
        &self,
        ids: impl IntoIterator<Item = &'a Id>,
    ) -> Vec<String> {
        ids.into_iter()
            .filter_map(|id| self.source_unit_report_ids_by_binding.get(id))
            .flat_map(|ids| ids.iter().cloned())
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect()
    }
}

/// Reverse-index `dependency_graph.nodes[].declared` so graph and planner reports
/// can resolve a binding `Id` → source units that declare it in a single hash
/// lookup. Most bindings come from exactly one source unit;
/// the `Vec<String>` shape accommodates the rare cases where the same
/// hygiene-identity ends up on multiple source units (anonymous statements
/// that share a synthetic source unit).
fn build_source_unit_report_ids_by_binding(
    dependency_graph: &DependencyGraph,
) -> HashMap<Id, Vec<String>> {
    let mut by_binding: HashMap<Id, BTreeSet<String>> = HashMap::new();
    for node in dependency_graph.iter_nodes() {
        let report_id = source_unit_key(node.id);
        for binding in &node.declared {
            by_binding
                .entry(binding.clone())
                .or_default()
                .insert(report_id.clone());
        }
    }
    by_binding
        .into_iter()
        .map(|(id, ids)| (id, ids.into_iter().collect()))
        .collect()
}

/// Pre-compute per-binding lookup data so reporting and reference
/// planning avoid repeated walks over `bindings`, `chunk_renames`, and
/// logical-module rename maps.
///
/// Export-name resolution rule:
/// - `Owned { Logical(idx) }` -> `logical_modules[idx].rename_map[id]`
///   if present, else the binding's own name.
/// - Everything else -> `chunk_renames[id]` if present, else the
///   binding's own name.
fn build_binding_lookup_by_id(
    bindings: &HashMap<Id, BindingKind>,
    chunk_renames: &HashMap<Id, Atom>,
    logical_modules: &[PlannedModule],
) -> HashMap<Id, BindingLookupInfo> {
    let mut out = HashMap::with_capacity(bindings.len() + chunk_renames.len());
    for (id, kind) in bindings {
        let owner = match kind {
            BindingKind::Owned { module } => Some(*module),
            BindingKind::Imported { .. } => None,
        };
        let export_name = match kind {
            BindingKind::Owned {
                module: ModuleId(idx),
            } => logical_modules
                .get(*idx)
                .and_then(|module| module.rename_map.get(id))
                .cloned()
                .unwrap_or_else(|| id.0.clone()),
            _ => chunk_renames
                .get(id)
                .cloned()
                .unwrap_or_else(|| id.0.clone()),
        };
        let export_name = (export_name != id.0).then_some(export_name);
        if owner.is_some() || export_name.is_some() {
            out.insert(id.clone(), BindingLookupInfo { export_name, owner });
        }
    }
    // Cover bindings that only show up in `chunk_renames` (no
    // `BindingKind` entry — e.g. names referenced by reports that
    // aren't first-class `Owned` / `Imported` bindings on the
    // analysis).
    for (id, export) in chunk_renames {
        if !bindings.contains_key(id) && export != &id.0 {
            out.entry(id.clone()).or_default().export_name = Some(export.clone());
        }
    }
    out
}
