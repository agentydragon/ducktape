use std::collections::{BTreeSet, HashMap, HashSet};

use petgraph::algo::toposort;
use petgraph::graphmap::DiGraphMap;

use crate::atomic_units::compute_atomic_units;
use crate::factor_assembly::{AtomicUnitConflict, assemble_partition};
use crate::graph::{build_module_quotient, build_owner_graph};
use crate::partition::Partition;
use crate::reports::{build_owner_graph_report, owner_key};
use crate::validation::{
    render_assembly_conflict_summary, validate_cross_destination_assignments, validate_schedule,
};
use crate::{
    BindingId, BindingKind, BindingName, LogicalModule, LogicalModuleIndex, ModuleId,
    ModuleQuotient, OwnerGraph, OwnerGraphReport, RESIDUAL_ENTRY_LABEL, ScheduleReport,
    StatementFacts,
};

/// Single per-chunk schedule. Carries everything downstream code
/// needs to validate cycles and emit modules in an order that
/// respects `I ∪ S`.
#[derive(Debug, Clone)]
pub struct Schedule {
    pub chunk_id: String,
    pub facts: Vec<StatementFacts>,
    /// All top-level bindings of the chunk indexed by local name.
    /// Iteration order is undefined; consumers that need a
    /// deterministic order (emit sites, error messages) must sort
    /// the keys themselves.
    pub bindings: HashMap<BindingName, BindingKind>,
    pub logical_modules: Vec<LogicalModule>,
    /// In-place readability renames for bindings that stay in
    /// entry. Iteration order is undefined; the
    /// `materialize_logical_modules` validation pass sorts the
    /// keys before iterating so any spec errors it emits stay
    /// deterministic.
    pub chunk_renames: HashMap<BindingName, BindingName>,
    pub owner_graph: OwnerGraph,
    /// Module assignment per owner — the spec's partition of the
    /// owner graph. Stored separately from the IR so the IR stays
    /// immutable across hypothetical refinements during peelability.
    pub partition: Partition,
    /// Atomic-factor-unit splits the spec demands but the
    /// constraining-edge SCC analysis forbids — populated by
    /// `factor_assembly` when YAML claims split an atomic unit across
    /// destination modules. Non-empty means the spec is unrealizable
    /// by construction; the materializer bails on these before
    /// emitting code.
    pub assembly_conflicts: Vec<AtomicUnitConflict>,
    pub dep_graph: ModuleQuotient,
    owner_report_ids_by_binding: Vec<Vec<String>>,
    /// Topological linearization of `I ∪ S`, dependency-first
    /// (the module at index 0 must evaluate before any other; the
    /// last module — typically the residual entry — evaluates
    /// last). Empty when `dep_graph` has cycles (validation will
    /// reject the spec). Used by the emitter to author each
    /// module's `import` directive list in an order that steers
    /// ECMA-262's linker DFS toward an `I ∪ S`-respecting
    /// evaluation order; see DESIGN.md "Lemma 2".
    pub linker_order: Vec<ModuleId>,
    linker_position_by_module: HashMap<ModuleId, usize>,
    /// Entry's full post-Owned-resolution export set: the
    /// pre-existing source exports passed into
    /// [`Schedule::with_pre_existing_entry_exports`] union the
    /// bindings of every owner already assigned to a logical
    /// module. Computed once when the setter runs and reused
    /// across the ≥1500 peelability candidate evaluations per
    /// chunk that would otherwise rebuild it from scratch.
    /// `None` when the schedule was built without AST analysis
    /// (the test-helper case); peelability's emit-resolvability
    /// projection then silently skips the check.
    ///
    /// Consumed by the emit-resolvability projection in
    /// `peelability.rs` and the matching predicate in
    /// `materialize_logical_modules` (SSOT — see
    /// [`crate::graph::peel_emit_blocked_residual_bindings`]).
    entry_exported_binding_names_cache: Option<HashSet<BindingName>>,
    /// The pre-existing entry exports as passed in to
    /// [`Self::with_pre_existing_entry_exports`], stored verbatim
    /// (no folding in of moved-owner bindings). Used by
    /// [`Self::pre_existing_entry_exports`] so callers reconstructing
    /// "what does the upstream chunk source export" don't lose a
    /// binding that's both in the upstream export list *and* owned
    /// by a logical module (subtracting Owned bindings from
    /// `entry_exported_binding_names_cache` would drop those).
    pre_existing_entry_exports_input: BTreeSet<BindingName>,
    /// Pre-computed `binding → exported name` map. Built once per
    /// chunk in `Schedule::build` so peelability's per-candidate
    /// `binding_reports` calls do a single hash lookup instead of
    /// re-walking `bindings` / `chunk_renames` /
    /// `logical_modules[idx].rename_map` per binding per candidate.
    /// Bindings absent from this map export under their own name.
    export_name_by_binding: HashMap<BindingName, BindingName>,
}

impl Schedule {
    /// Build a schedule from chunk facts + the binding catalogue +
    /// spec-derived logical modules. `bindings` should already have
    /// every `Owned` binding the spec assigned and every `Imported`
    /// binding the spec re-exports.
    pub fn build(
        chunk_id: String,
        facts: Vec<StatementFacts>,
        bindings: HashMap<BindingName, BindingKind>,
        logical_modules: Vec<LogicalModule>,
        chunk_renames: HashMap<BindingName, BindingName>,
    ) -> Self {
        let owner_graph = build_owner_graph(&facts);
        let atomic_units = compute_atomic_units(&owner_graph);
        let outcome = assemble_partition(&owner_graph, &atomic_units, &bindings, &logical_modules);
        let partition = outcome.partition;
        let assembly_conflicts = outcome.conflicts;
        let owner_report_ids_by_binding = Self::build_owner_report_ids_by_binding(&owner_graph);
        let dep_graph = build_module_quotient(&owner_graph, &partition);
        let linker_order = compute_linker_order(&dep_graph, &logical_modules);
        let linker_position_by_module = linker_order
            .iter()
            .copied()
            .enumerate()
            .map(|(idx, id)| (id, idx))
            .collect();
        let export_name_by_binding =
            build_export_name_by_binding(&bindings, &chunk_renames, &logical_modules);
        Self {
            chunk_id,
            facts,
            bindings,
            logical_modules,
            chunk_renames,
            owner_graph,
            partition,
            assembly_conflicts,
            dep_graph,
            owner_report_ids_by_binding,
            linker_order,
            linker_position_by_module,
            entry_exported_binding_names_cache: None,
            pre_existing_entry_exports_input: BTreeSet::new(),
            export_name_by_binding,
        }
    }

    /// Attach the set of binding names that the source chunk's entry
    /// already exports. Folds them together with the names of every
    /// owner already assigned to a logical module into the cached
    /// post-Owned export set queried by the emit-resolvability
    /// projection in [`crate::graph::peel_emit_blocked_residual_bindings`].
    pub fn with_pre_existing_entry_exports(mut self, exports: BTreeSet<BindingName>) -> Self {
        let mut cache: HashSet<BindingName> = exports.iter().cloned().collect();
        for (name, kind) in &self.bindings {
            if let BindingKind::Owned {
                owner: ModuleId::Logical(_),
            } = kind
            {
                cache.insert(name.clone());
            }
        }
        self.pre_existing_entry_exports_input = exports;
        self.entry_exported_binding_names_cache = Some(cache);
        self
    }

    /// Set of binding names that entry exports under the schedule's
    /// current binding assignment — pre-existing source exports plus
    /// any binding that's already owned by a logical module
    /// (each gets an auto-added `export { name }` from entry; see
    /// `entry_exports_for_moved_bindings` in `materialize_logical_modules`).
    ///
    /// Returns `None` when AST analysis didn't populate the
    /// pre-existing set; peelability treats that as "skip the
    /// emit-resolvability projection" so non-pipeline test fixtures
    /// don't have to fake an export list.
    ///
    /// Cached on schedule construction; the underlying set is
    /// stable for the schedule's lifetime, so callers borrow it.
    pub fn entry_exported_binding_names(&self) -> Option<&HashSet<BindingName>> {
        self.entry_exported_binding_names_cache.as_ref()
    }

    /// Binding names entry exports via upstream source statements
    /// (verbatim what was passed to
    /// [`Self::with_pre_existing_entry_exports`]). Does NOT include
    /// the auto-added bindings of currently-moved logical-module
    /// owners — downstream planners (factorizer, etc.) that need to
    /// predict the post-promotion export set add their own
    /// hypothesized moved bindings on top.
    ///
    /// Empty `BTreeSet` when AST analysis didn't populate the input
    /// set (the test-helper case); same convention as
    /// [`Self::entry_exported_binding_names`].
    pub fn pre_existing_entry_exports(&self) -> &BTreeSet<BindingName> {
        &self.pre_existing_entry_exports_input
    }

    /// Pre-computed export name for a chunk binding, falling back
    /// to the binding's own name. Hot-path replacement for the
    /// previous `bindings` / `chunk_renames` / `rename_map` walk in
    /// peelability report generation.
    pub(crate) fn export_name_for(&self, binding: &str) -> BindingName {
        self.export_name_by_binding
            .get(binding)
            .cloned()
            .unwrap_or_else(|| binding.to_string())
    }

    /// Position of `id` in `linker_order`, if present. Used by the
    /// emitter to sort each module's `import` directives so that
    /// ECMA-262's depth-first link traversal evaluates dependencies
    /// before dependents.
    pub fn linker_position(&self, id: ModuleId) -> Option<usize> {
        self.linker_position_by_module.get(&id).copied()
    }

    /// Render `id` to a human-readable label (used in cycle reports).
    pub fn module_name(&self, id: ModuleId) -> String {
        match id {
            ModuleId::ResidualEntry => RESIDUAL_ENTRY_LABEL.to_string(),
            ModuleId::Logical(LogicalModuleIndex(idx)) => self
                .logical_modules
                .get(idx)
                .map(|m| m.id.clone())
                .unwrap_or_else(|| format!("<module#{idx}>")),
        }
    }

    /// Which logical module owns a binding (by local name), if any.
    /// Returns `None` for names that aren't `Owned` in this schedule
    /// (e.g. globals, imported bindings, names not in the spec).
    pub fn owner_of(&self, name: &str) -> Option<ModuleId> {
        self.bindings.get(name).and_then(|kind| match kind {
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

    /// Run SCC analysis over the dep graph. Spec authors consume the
    /// resulting report to fix any cycles or cross-destination
    /// rebinding writes.
    pub fn validate(&self) -> ScheduleReport {
        let mut report = validate_schedule(&self.dep_graph, &|id| self.module_name(id));
        report.cross_destination_assignments =
            validate_cross_destination_assignments(&self.owner_graph, &self.partition, &|id| {
                self.module_name(id)
            });
        report.atomic_unit_conflicts =
            render_assembly_conflict_summary(&self.assembly_conflicts, &self.owner_graph, &|id| {
                self.module_name(id)
            });
        report.linker_order = self
            .linker_order
            .iter()
            .map(|id| self.module_name(*id))
            .collect();
        report
    }

    /// High-fidelity node-link view of the fine owner graph plus
    /// its current module quotient. Written as
    /// `<chunk_id>/owner_graph.json` for downstream peel tooling.
    /// Always includes the factorizer report computed with
    /// [`crate::FactorizeOptions::default`]; downstream CLIs read
    /// the precomputed cells from disk rather than reproducing the
    /// algorithm against the serialized owner graph.
    pub fn owner_graph_report(&self) -> OwnerGraphReport {
        self.owner_graph_report_with_factorize_options(&crate::FactorizeOptions::default())
    }

    /// Like [`Self::owner_graph_report`] but with caller-chosen
    /// `FactorizeOptions` (e.g. to widen `size_cap_lines` for an
    /// exploratory pipeline run).
    pub fn owner_graph_report_with_factorize_options(
        &self,
        factorize_options: &crate::FactorizeOptions,
    ) -> OwnerGraphReport {
        let mut report = build_owner_graph_report(self);
        report.factorize = crate::build_factorize_report(self, factorize_options);
        report
    }
}

/// Pre-compute every chunk binding's exported-name resolution so
/// peelability reporting (`reports::binding_reports`) becomes a
/// single hash lookup per binding instead of walking three maps.
/// Mirrors the resolution rule in the previous
/// `reports::export_name_for_binding`:
/// - `Owned { Logical(idx) }` → `logical_modules[idx].rename_map[name]`
///   if present, else the binding's own name.
/// - Everything else → `chunk_renames[name]` if present, else the
///   binding's own name.
fn build_export_name_by_binding(
    bindings: &HashMap<BindingName, BindingKind>,
    chunk_renames: &HashMap<BindingName, BindingName>,
    logical_modules: &[LogicalModule],
) -> HashMap<BindingName, BindingName> {
    let mut out = HashMap::with_capacity(bindings.len() + chunk_renames.len());
    for (name, kind) in bindings {
        let export = match kind {
            BindingKind::Owned {
                owner: ModuleId::Logical(LogicalModuleIndex(idx)),
            } => logical_modules
                .get(*idx)
                .and_then(|module| module.rename_map.get(name))
                .cloned()
                .unwrap_or_else(|| name.clone()),
            _ => chunk_renames
                .get(name)
                .cloned()
                .unwrap_or_else(|| name.clone()),
        };
        if export != *name {
            out.insert(name.clone(), export);
        }
    }
    // Cover bindings that only show up in `chunk_renames` (no
    // `BindingKind` entry — e.g. names referenced by reports that
    // aren't first-class `Owned` / `Imported` bindings on the
    // schedule).
    for (name, export) in chunk_renames {
        if !bindings.contains_key(name) && export != name {
            out.insert(name.clone(), export.clone());
        }
    }
    out
}

/// Topological linearization of the dep graph, dependency-first.
/// Empty if the graph has cycles (`tarjan_scc` plus the validator
/// gate handle that case).
///
/// The dep-graph edge convention is `(M, M')` meaning `M` depends
/// on `M'`. `petgraph::algo::toposort` returns `u`-before-`v` for
/// every edge `(u, v)`, which under our convention puts dependents
/// before dependencies. The returned order is reversed so the
/// dependency comes first — matching the order ECMA-262's link
/// traversal needs to evaluate (deepest leaf first).
fn compute_linker_order(
    dep_graph: &ModuleQuotient,
    logical_modules: &[LogicalModule],
) -> Vec<ModuleId> {
    let mut graph = DiGraphMap::<ModuleId, ()>::new();
    // Add every module the schedule knows about so the order
    // covers them even if they have no dep-graph edges (singleton
    // leaves still need a deterministic position for emit ordering).
    graph.add_node(ModuleId::ResidualEntry);
    for idx in 0..logical_modules.len() {
        graph.add_node(ModuleId::Logical(LogicalModuleIndex(idx)));
    }
    for (from, to, _) in dep_graph.iter_edges() {
        graph.add_node(from);
        graph.add_node(to);
        graph.add_edge(from, to, ());
    }
    match toposort(&graph, None) {
        Ok(order) => order.into_iter().rev().collect(),
        Err(_) => Vec::new(),
    }
}
