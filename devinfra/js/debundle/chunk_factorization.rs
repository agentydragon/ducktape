use std::collections::HashMap;
use std::sync::Arc;

use petgraph::algo::{tarjan_scc, toposort};
use petgraph::graphmap::DiGraphMap;

use crate::atomic_units::{OwnerGraphAndUnits, compute_owner_graph_and_units};
use crate::chunk_analysis::ChunkAnalysis;
use crate::factor_assembly::{AtomicUnitConflict, assemble_partition};
use crate::graph::build_module_quotient;
use crate::partition::Partition;
use crate::reports::build_owner_graph_report;
use crate::validation::validate_factorization;

use crate::{
    FactorizationReport, LogicalModule, LogicalModuleIndex, ModuleId, ModuleQuotient,
    OwnerGraphReport,
};

/// Per-chunk factorization output: the spec's partition of the
/// owner graph, plus the realizability views derived from it
/// (`dep_graph`, `linker_order`, `assembly_conflicts`) and small
/// caches downstream consumers consult on the hot path.
///
/// Holds a reference to the [`ChunkAnalysis`] it was factorized
/// from (`analysis`); peelability and downstream emit code reach
/// inputs (`bindings`, `logical_modules`, etc.) through
/// `factorization.analysis.X`.
#[derive(Debug, Clone)]
pub struct ChunkFactorization {
    pub analysis: Arc<ChunkAnalysis>,
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
    source_import_position_by_module: HashMap<ModuleId, usize>,
}

impl ChunkFactorization {
    /// Build a factorization from chunk facts plus the binding
    /// catalogue plus spec-derived logical modules. `bindings` should
    /// already have every `Owned` binding the spec assigned and every
    /// `Imported` binding the spec re-exports.
    ///
    /// Convenience constructor: computes the owner graph and atomic
    /// units internally. Call sites that already have those precomputed
    /// (e.g. the materializer reuses them for mini-factor synthesis)
    /// should call [`Self::build_with`] instead.
    pub fn build(
        chunk_id: String,
        facts: Vec<StatementFactsInput>,
        bindings: HashMap<swc_ecma_ast::Id, crate::BindingKind>,
        logical_modules: Vec<LogicalModule>,
        chunk_renames: HashMap<swc_ecma_ast::Id, swc_atoms::Atom>,
        default_destination: ModuleId,
    ) -> Self {
        let precomputed = compute_owner_graph_and_units(&facts);
        Self::build_with(
            chunk_id,
            facts,
            precomputed,
            bindings,
            logical_modules,
            chunk_renames,
            default_destination,
        )
    }

    /// Build a factorization reusing a caller-computed owner graph +
    /// atomic units. The materializer computes these once per chunk
    /// for mini-factor synthesis and passes them in here so the
    /// factorization doesn't redo the work.
    pub fn build_with(
        chunk_id: String,
        facts: Vec<StatementFactsInput>,
        precomputed: OwnerGraphAndUnits,
        bindings: HashMap<swc_ecma_ast::Id, crate::BindingKind>,
        logical_modules: Vec<LogicalModule>,
        chunk_renames: HashMap<swc_ecma_ast::Id, swc_atoms::Atom>,
        default_destination: ModuleId,
    ) -> Self {
        let OwnerGraphAndUnits {
            owner_graph,
            atomic_units,
        } = precomputed;
        let outcome = assemble_partition(
            &owner_graph,
            &atomic_units,
            &bindings,
            &logical_modules,
            default_destination,
        );
        let partition = outcome.partition;
        let assembly_conflicts = outcome.conflicts;
        let dep_graph = build_module_quotient(&owner_graph, &partition);
        let linker_order = compute_linker_order(&dep_graph, &logical_modules);
        let linker_position_by_module: HashMap<ModuleId, usize> = linker_order
            .iter()
            .copied()
            .enumerate()
            .map(|(idx, id)| (id, idx))
            .collect();
        let source_import_order =
            compute_source_import_order(&dep_graph, &logical_modules, &linker_position_by_module);
        let source_import_position_by_module: HashMap<ModuleId, usize> = source_import_order
            .iter()
            .copied()
            .enumerate()
            .map(|(idx, id)| (id, idx))
            .collect();
        let analysis = Arc::new(ChunkAnalysis::build(
            chunk_id,
            facts,
            owner_graph,
            bindings,
            logical_modules,
            chunk_renames,
        ));
        Self {
            analysis,
            partition,
            assembly_conflicts,
            dep_graph,
            linker_order,
            linker_position_by_module,
            source_import_position_by_module,
        }
    }

    /// Position of `id` in `linker_order`, if present. Used by the
    /// emitter to sort each module's `import` directives so that
    /// ECMA-262's depth-first link traversal evaluates dependencies
    /// before dependents.
    pub fn linker_position(&self, id: ModuleId) -> Option<usize> {
        self.linker_position_by_module.get(&id).copied()
    }

    /// Position of `id` in the emitted entry's source-import order
    /// — the order in which entry's `import` directives must appear
    /// in source so the ESM linker's depth-first instantiation lands
    /// on a Phase-2 evaluation order matching `linker_order`.
    ///
    /// Per DESIGN.md "The realizability theorem", Lemma 2: for
    /// acyclic shapes this coincides with `linker_position`
    /// (dependency-first source order). For cyclic-I shapes accepted
    /// by the relaxed clause-3 rule, SCC members are reverse-sorted
    /// — DFS into the dependent unwinds the dependency first in
    /// post-order, so the dependent must appear first in entry's
    /// source for post-DFS evaluation to put the dependency first.
    pub fn source_import_position(&self, id: ModuleId) -> Option<usize> {
        self.source_import_position_by_module.get(&id).copied()
    }

    /// Run SCC analysis over the dep graph. Spec authors consume the
    /// resulting report to fix any cycles or atomic-unit conflicts.
    pub fn validate(&self) -> FactorizationReport {
        let mut report =
            validate_factorization(&self.analysis.owner_graph, &self.partition, &|id| {
                self.analysis.module_name(id)
            });
        report.atomic_unit_conflicts = self.assembly_conflicts.clone();
        report.linker_order = self
            .linker_order
            .iter()
            .map(|id| self.analysis.module_name(*id))
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

type StatementFactsInput = crate::StatementFacts;

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
    // Add every module the factorization knows about so the order
    // covers them even if they have no dep-graph edges (singleton
    // leaves still need a deterministic position for emit ordering).
    for idx in 0..logical_modules.len() {
        graph.add_node(ModuleId(LogicalModuleIndex(idx)));
    }
    for (from, to, weight) in dep_graph.all_edges() {
        if !weight.constrains_init_order() {
            continue;
        }
        graph.add_node(from);
        graph.add_node(to);
        graph.add_edge(from, to, ());
    }
    match toposort(&graph, None) {
        Ok(order) => order.into_iter().rev().collect(),
        Err(_) => Vec::new(),
    }
}

/// Compute the source order in which entry's emitted `import`
/// directives must appear so the ESM linker's depth-first
/// instantiation lands on a Phase-2 evaluation order matching
/// `linker_order`. Implements DESIGN.md "The realizability theorem"
/// Lemma 2.
///
/// The idea: ESM Phase-2 evaluates a module's source-order imports
/// recursively, then runs the module's own init code. For acyclic
/// imports graphs, post-DFS-order matches source order — so
/// dependency-first source produces dependency-first evaluation.
/// For cyclic imports graphs accepted by the relaxed clause-3 rule
/// (the constraining-edge subgraph is acyclic even though `I` has
/// the cycle), DFS into the first imported SCC member recurses to
/// its dependency, hits the cycle back-edge, and finalizes the
/// dependency LAST in post-order — wrong. The fix: within each SCC
/// of `I`, reverse the order so the most-dependent member is first
/// in source, the cycle unwinds through its dependencies, and
/// post-order matches the dependency-first ordering the
/// constraining-edge subgraph defines.
///
/// Returns a `Vec<ModuleId>` ordered such that index 0 should be
/// entry's first import.
fn compute_source_import_order(
    dep_graph: &ModuleQuotient,
    logical_modules: &[LogicalModule],
    linker_position_by_module: &HashMap<ModuleId, usize>,
) -> Vec<ModuleId> {
    let sccs = tarjan_scc(&dep_graph.0);
    let mut scc_of: HashMap<ModuleId, usize> = HashMap::new();
    let mut scc_rank: Vec<usize> = Vec::with_capacity(sccs.len());
    for (idx, scc) in sccs.iter().enumerate() {
        let min_pos = scc
            .iter()
            .filter_map(|m| linker_position_by_module.get(m).copied())
            .min()
            .unwrap_or(usize::MAX);
        scc_rank.push(min_pos);
        for m in scc {
            scc_of.insert(*m, idx);
        }
    }
    // Start from every module the factorization knows about. Modules
    // with no dep-graph edges (singletons) still need a deterministic
    // source-order slot for emit stability.
    let mut nodes: Vec<ModuleId> = (0..logical_modules.len())
        .map(|idx| ModuleId(LogicalModuleIndex(idx)))
        .collect();
    for (from, to, _) in dep_graph.all_edges() {
        if !nodes.contains(&from) {
            nodes.push(from);
        }
        if !nodes.contains(&to) {
            nodes.push(to);
        }
    }
    nodes.sort_by(|a, b| {
        let a_scc = scc_of.get(a).copied();
        let b_scc = scc_of.get(b).copied();
        let a_rank = a_scc
            .and_then(|i| scc_rank.get(i).copied())
            .unwrap_or(usize::MAX);
        let b_rank = b_scc
            .and_then(|i| scc_rank.get(i).copied())
            .unwrap_or(usize::MAX);
        let a_pos = linker_position_by_module
            .get(a)
            .copied()
            .unwrap_or(usize::MAX);
        let b_pos = linker_position_by_module
            .get(b)
            .copied()
            .unwrap_or(usize::MAX);
        // (SCC rank ASC, intra-SCC linker_position DESC). DESC
        // reverses within each SCC; the rank ASC keeps SCCs
        // dependency-first relative to each other. For singleton
        // SCCs, the DESC reverse is a no-op (only one member).
        a_rank.cmp(&b_rank).then_with(|| b_pos.cmp(&a_pos))
    });
    nodes
}
