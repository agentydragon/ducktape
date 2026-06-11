//! Per-chunk analysis state: inputs, IR, and input-derived caches.
//!
//! [`ChunkAnalysis`] is what's known about a chunk before factorize
//! runs — the facts harvested from the AST, the spec-supplied
//! logical modules and chunk renames, the owner graph derived from
//! those facts, and the small lookup caches that depend only on
//! inputs. The factorize algorithm consumes a `ChunkAnalysis` plus
//! a default destination to produce a
//! [`crate::ChunkFactorization`] — the partition-and-derived state
//! that depends on which logical-module assignment the spec chose.

use std::collections::{BTreeSet, HashMap};

use swc_atoms::Atom;
use swc_ecma_ast::Id;

use analysis::reports::owner_key;
use analysis::{
    BindingKind, LogicalModule, LogicalModuleIndex, ModuleId, OwnerGraph, StatementFacts,
};

/// Per-chunk inputs + IR + input-derived caches.
///
/// Constructed once per chunk and held by reference (typically via
/// `Arc<ChunkAnalysis>`) by every [`crate::ChunkFactorization`]
/// candidate that explores a partition over the same owner graph.
#[derive(Debug, Clone)]
pub struct ChunkAnalysis {
    pub chunk_id: String,
    pub facts: Vec<StatementFacts>,
    /// All top-level bindings of the chunk indexed by local name.
    /// Iteration order is undefined; consumers that need a
    /// deterministic order (emit sites, error messages) must sort
    /// the keys themselves.
    pub bindings: HashMap<Id, BindingKind>,
    pub logical_modules: Vec<LogicalModule>,
    /// In-place readability renames for bindings that stay in
    /// entry. Iteration order is undefined; the
    /// `materialize_logical_modules` validation pass sorts the
    /// keys before iterating so any spec errors it emits stay
    /// deterministic.
    pub chunk_renames: HashMap<Id, Atom>,
    pub owner_graph: OwnerGraph,
    owner_report_ids_by_binding: HashMap<Id, Vec<String>>,
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

impl ChunkAnalysis {
    /// Build a `ChunkAnalysis` reusing a caller-supplied owner graph.
    /// `bindings` should already have every `Owned` binding the spec
    /// assigned and every `Imported` binding the spec re-exports.
    pub fn build(
        chunk_id: String,
        facts: Vec<StatementFacts>,
        owner_graph: OwnerGraph,
        bindings: HashMap<Id, BindingKind>,
        logical_modules: Vec<LogicalModule>,
        chunk_renames: HashMap<Id, Atom>,
    ) -> Self {
        let owner_report_ids_by_binding = build_owner_report_ids_by_binding(&owner_graph);
        let binding_lookup_by_id =
            build_binding_lookup_by_id(&bindings, &chunk_renames, &logical_modules);
        Self {
            chunk_id,
            facts,
            bindings,
            logical_modules,
            chunk_renames,
            owner_graph,
            owner_report_ids_by_binding,
            binding_lookup_by_id,
        }
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
    /// every wire artifact (`owner_graph.json` module table,
    /// `cycles.json`, `atomic_unit_conflicts.json`) and diagnostic
    /// uses to denote this module. Parses the in-process
    /// `LogicalModule.id` (`"<chunk_id>::<path>"`) down to the clean
    /// path; panics on an out-of-range id or unparseable identity —
    /// both are pipeline bugs, not user errors.
    pub fn module_path(&self, id: ModuleId) -> spec::ModulePath {
        let LogicalModuleIndex(idx) = id.0;
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

    /// Lookup a logical module by index.
    pub fn logical_module(&self, idx: LogicalModuleIndex) -> Option<&LogicalModule> {
        self.logical_modules.get(idx.0)
    }

    pub fn owner_report_ids_for_bindings<'a>(
        &self,
        ids: impl IntoIterator<Item = &'a Id>,
    ) -> Vec<String> {
        ids.into_iter()
            .filter_map(|id| self.owner_report_ids_by_binding.get(id))
            .flat_map(|ids| ids.iter().cloned())
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect()
    }
}

/// Reverse-index `owner_graph.nodes[].declared` so graph and planner reports
/// can resolve a binding `Id` → owners-that-declare-it in a single hash
/// lookup. Most bindings come from exactly one owner;
/// the `Vec<String>` shape accommodates the rare cases where the same
/// hygiene-identity ends up on multiple owners (anonymous statements
/// that share a synthetic owner).
fn build_owner_report_ids_by_binding(owner_graph: &OwnerGraph) -> HashMap<Id, Vec<String>> {
    let mut by_binding: HashMap<Id, BTreeSet<String>> = HashMap::new();
    for node in owner_graph.iter_nodes() {
        let report_id = owner_key(node.id);
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
    logical_modules: &[LogicalModule],
) -> HashMap<Id, BindingLookupInfo> {
    let mut out = HashMap::with_capacity(bindings.len() + chunk_renames.len());
    for (id, kind) in bindings {
        let owner = match kind {
            BindingKind::Owned { module } => Some(*module),
            BindingKind::Imported { .. } => None,
        };
        let export_name = match kind {
            BindingKind::Owned {
                module: ModuleId(LogicalModuleIndex(idx)),
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
