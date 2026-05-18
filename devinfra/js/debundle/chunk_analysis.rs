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

use crate::reports::owner_key;
use crate::{
    BindingId, BindingKind, BindingName, LogicalModule, LogicalModuleIndex, ModuleId, OwnerGraph,
    StatementFacts,
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
    owner_report_ids_by_binding: Vec<Vec<String>>,
    /// Pre-computed `binding → exported name` map. Built once per
    /// chunk so peelability's per-candidate `binding_reports` calls
    /// do a single hash lookup instead of re-walking `bindings` /
    /// `chunk_renames` / `logical_modules[idx].rename_map` per
    /// binding per candidate. Bindings absent from this map export
    /// under their own name.
    export_name_by_binding: HashMap<Id, Atom>,
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
        let export_name_by_binding =
            build_export_name_by_binding(&bindings, &chunk_renames, &logical_modules);
        Self {
            chunk_id,
            facts,
            bindings,
            logical_modules,
            chunk_renames,
            owner_graph,
            owner_report_ids_by_binding,
            export_name_by_binding,
        }
    }

    /// Pre-computed export name for a chunk binding, falling back
    /// to the binding's own name. Hot-path replacement for the
    /// previous `bindings` / `chunk_renames` / `rename_map` walk in
    /// peelability report generation.
    ///
    /// Looks up by `sym`-only since the report generators pass bare
    /// `BindingName` (no ctxt available at the call site). Within a
    /// chunk's top-level scope, syms are unique by construction, so
    /// the first sym match is unambiguous.
    pub(crate) fn export_name_for(&self, binding: &str) -> BindingName {
        self.export_name_by_binding
            .iter()
            .find(|(id, _)| id.0.as_ref() == binding)
            .map(|(_, atom)| atom.to_string())
            .unwrap_or_else(|| binding.to_string())
    }

    /// Render `id` to a human-readable label (used in cycle reports).
    pub fn module_name(&self, id: ModuleId) -> String {
        let LogicalModuleIndex(idx) = id.0;
        self.logical_modules
            .get(idx)
            .map(|m| m.id.clone())
            .unwrap_or_else(|| format!("<module#{idx}>"))
    }

    /// Which logical module owns a binding (by local name), if any.
    /// Returns `None` for names that aren't `Owned` in this analysis
    /// (e.g. globals, imported bindings, names not in the spec).
    ///
    /// Looks up by `sym`-only since most callers don't carry hygiene
    /// context. Top-level binding syms are unique within a chunk, so
    /// first sym match is unambiguous.
    pub fn owner_of(&self, name: &str) -> Option<ModuleId> {
        self.bindings
            .iter()
            .find(|(id, _)| id.0.as_ref() == name)
            .and_then(|(_, kind)| match kind {
                BindingKind::Owned { owner } => Some(*owner),
                BindingKind::Imported { .. } => None,
            })
    }

    /// Lookup a logical module by index.
    pub fn logical_module(&self, idx: LogicalModuleIndex) -> Option<&LogicalModule> {
        self.logical_modules.get(idx.0)
    }

    pub fn binding_name(&self, id: BindingId) -> &BindingName {
        self.owner_graph.binding_table.required_name(id)
    }

    pub fn owner_report_ids_for_bindings<'a>(
        &self,
        names: impl IntoIterator<Item = &'a str>,
    ) -> Vec<String> {
        names
            .into_iter()
            .filter_map(|name| self.owner_graph.binding_table.get(name))
            .filter_map(|binding| self.owner_report_ids_by_binding.get(binding.0))
            .flat_map(|ids| ids.iter().cloned())
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect()
    }
}

fn build_owner_report_ids_by_binding(owner_graph: &OwnerGraph) -> Vec<Vec<String>> {
    let mut by_binding = (0..owner_graph.binding_table.len())
        .map(|_| BTreeSet::<String>::new())
        .collect::<Vec<_>>();
    for node in owner_graph.iter_nodes() {
        let report_id = owner_key(node.id);
        for binding in &node.declared {
            if let Some(ids) = by_binding.get_mut(binding.0) {
                ids.insert(report_id.clone());
            }
        }
    }
    by_binding
        .into_iter()
        .map(|ids| ids.into_iter().collect())
        .collect()
}

/// Pre-compute every chunk binding's exported-name resolution so
/// peelability reporting (`reports::binding_reports`) becomes a
/// single hash lookup per binding instead of walking three maps.
/// Resolution rule:
/// - `Owned { Logical(idx) }` → `logical_modules[idx].rename_map[name]`
///   if present, else the binding's own name.
/// - Everything else → `chunk_renames[name]` if present, else the
///   binding's own name.
fn build_export_name_by_binding(
    bindings: &HashMap<Id, BindingKind>,
    chunk_renames: &HashMap<Id, Atom>,
    logical_modules: &[LogicalModule],
) -> HashMap<Id, Atom> {
    let mut out = HashMap::with_capacity(bindings.len() + chunk_renames.len());
    for (id, kind) in bindings {
        let export = match kind {
            BindingKind::Owned {
                owner: ModuleId(LogicalModuleIndex(idx)),
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
        if export != id.0 {
            out.insert(id.clone(), export);
        }
    }
    // Cover bindings that only show up in `chunk_renames` (no
    // `BindingKind` entry — e.g. names referenced by reports that
    // aren't first-class `Owned` / `Imported` bindings on the
    // analysis).
    for (id, export) in chunk_renames {
        if !bindings.contains_key(id) && export != &id.0 {
            out.insert(id.clone(), export.clone());
        }
    }
    out
}
