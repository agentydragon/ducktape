use std::collections::{BTreeSet, HashMap};
use std::sync::Arc;

use crate::atomic_units::{AtomicUnit, OwnerGraphAndUnits, compute_owner_graph_and_units};
use crate::chunk_analysis::ChunkAnalysis;
use crate::factor_assembly::{AtomicUnitConflict, assemble_partition};
use crate::graph::{
    build_module_quotient, chunk_constraining_module_edges, chunk_linker_order,
    chunk_source_import_order,
};
use crate::partition::Partition;
use crate::reports::build_owner_graph_report;
use crate::validation::validate_factorization_with_quotient;

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
/// from (`analysis`); report emission and downstream emit code reach
/// inputs (`bindings`, `logical_modules`, etc.) through
/// `factorization.analysis.X`.
#[derive(Debug, Clone)]
pub struct ChunkFactorization {
    pub analysis: Arc<ChunkAnalysis>,
    /// Module assignment per owner — the spec's partition of the
    /// owner graph. Stored separately from the IR so the IR stays
    /// immutable across validation/report construction.
    pub partition: Partition,
    /// Atomic owner units computed from the owner graph's
    /// constraining-edge SCCs. Stored so report emission and
    /// downstream diagnostics reuse the same unit partition that
    /// factor assembly validated.
    pub atomic_units: Vec<AtomicUnit>,
    /// Atomic-factor-unit splits the spec demands but the
    /// constraining-edge SCC analysis forbids — populated by
    /// `factor_assembly` when YAML claims split an atomic unit across
    /// destination modules. Non-empty means the spec is unrealizable
    /// by construction; the materializer bails on these before
    /// emitting code.
    pub assembly_conflicts: Vec<AtomicUnitConflict>,
    pub dep_graph: ModuleQuotient,
    /// SCC partition of `dep_graph` in `tarjan_scc` reverse-
    /// topological order. Precomputed at build time so downstream
    /// consumers (`validation::validate_factorization` via the
    /// verdict's `scc_partition`, `reports::build_quotient_scc_reports`)
    /// share one walk.
    dep_graph_sccs: Vec<Vec<ModuleId>>,
    /// Topological linearization of `I ∪ S`, dependency-first
    /// (the module at index 0 must evaluate before any other; the
    /// last module — typically the residual entry — evaluates
    /// last). Empty when `dep_graph` has cycles (validation will
    /// reject the spec). Used by the emitter to author each
    /// module's `import` directive list in an order that steers
    /// ECMA-262's linker DFS toward an `I ∪ S`-respecting
    /// evaluation order; see docs/design.md "Lemma 2".
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
        facts: Vec<crate::StatementFacts>,
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
        facts: Vec<crate::StatementFacts>,
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
        // Cache the dep-graph SCC partition so downstream consumers
        // (`reports::build_quotient_scc_reports`, the validator's
        // verdict path) share one Tarjan walk instead of each
        // recomputing it.
        let dep_graph_sccs = dep_graph.sccs();
        // Drive Lemma-2 ordering through the canonical
        // `ChunkConstrainingEdgeSet` so the emitter and the gate's
        // simulator share one source of truth — see `graph.rs:
        // chunk_constraining_module_edges` for the invariant doc.
        let canonical_edges = chunk_constraining_module_edges(&owner_graph, &partition);
        // Canonical linker order (Vec<ModuleId>, dependency-first).
        // Modules absent from the canonical set are not present here.
        let linker_order: Vec<ModuleId> = chunk_linker_order(&canonical_edges);
        // O(1) position-lookup cache for `linker_position(id)` queries
        // — built once here so downstream code doesn't re-derive it.
        let linker_position_by_module: HashMap<ModuleId, usize> = linker_order
            .iter()
            .copied()
            .enumerate()
            .map(|(idx, id)| (id, idx))
            .collect();
        // Every logical module needs a deterministic source-order
        // slot for emit stability, even singletons that don't
        // participate in any canonical edge.
        let extra_nodes: BTreeSet<ModuleId> = (0..logical_modules.len())
            .map(|idx| ModuleId(LogicalModuleIndex(idx)))
            .collect();
        let source_import_order = chunk_source_import_order(&canonical_edges, &extra_nodes);
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
            atomic_units,
            assembly_conflicts,
            dep_graph,
            dep_graph_sccs,
            linker_order,
            linker_position_by_module,
            source_import_position_by_module,
        }
    }

    /// SCC partition of `dep_graph` (the module quotient), in
    /// `tarjan_scc` reverse-topological order. Precomputed at build
    /// time and shared with downstream consumers in lieu of a fresh
    /// Tarjan walk.
    pub fn dep_graph_sccs(&self) -> &[Vec<ModuleId>] {
        &self.dep_graph_sccs
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
    /// Per docs/design.md "The realizability theorem", Lemma 2: for
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
        let mut report = validate_factorization_with_quotient(
            &self.analysis.owner_graph,
            &self.partition,
            &self.dep_graph,
            &|id| self.analysis.module_name(id),
        );
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
    pub fn owner_graph_report(&self) -> OwnerGraphReport {
        build_owner_graph_report(self)
    }
}
