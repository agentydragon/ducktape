use std::collections::{BTreeSet, HashMap};
use std::sync::Arc;

use crate::report_builders::build_owner_graph_report;
use analysis::atomic_units::{AtomicUnit, OwnerGraphAndUnits, compute_owner_graph_and_units};
use analysis::factor_assembly::{AtomicUnitConflict, assemble_partition};
use analysis::graph::{build_module_quotient, chunk_constraining_module_edges};
use analysis::partition::Partition;
use analysis::{LogicalModule, LogicalModuleIndex, ModuleId, ModuleQuotient, OwnerGraphReport};

use crate::chunk_analysis::ChunkAnalysis;
use crate::esm_import_order::EsmImportOrder;
use crate::validation::{FactorizationReport, validate_factorization};

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
    /// topological order. Precomputed at build time for
    /// `report_builders::build_quotient_scc_reports`.
    dep_graph_sccs: Vec<Vec<ModuleId>>,
    /// Shared per-module ESM import ordering — the single source of
    /// truth the emitter renders import declarations from and the
    /// realizability gate's evaluation simulator mirrors. See
    /// `esm_import_order::EsmImportOrder`.
    import_order: EsmImportOrder,
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
        facts: Vec<analysis::StatementFacts>,
        bindings: HashMap<swc_ecma_ast::Id, analysis::BindingKind>,
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
        facts: Vec<analysis::StatementFacts>,
        precomputed: OwnerGraphAndUnits,
        bindings: HashMap<swc_ecma_ast::Id, analysis::BindingKind>,
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
        // (`report_builders::build_quotient_scc_reports`, the validator's
        // verdict path) share one Tarjan walk instead of each
        // recomputing it.
        let dep_graph_sccs = dep_graph.sccs();
        // Drive Lemma-2 ordering through the canonical
        // `ChunkConstrainingEdgeSet` so the emitter and the gate's
        // simulator share one source of truth — see `graph.rs:
        // chunk_constraining_module_edges` for the invariant doc.
        let canonical_edges = chunk_constraining_module_edges(&owner_graph, &partition);
        // Every logical module needs a deterministic source-order
        // slot for emit stability, even singletons that don't
        // participate in any canonical edge.
        let extra_nodes: BTreeSet<ModuleId> = (0..logical_modules.len())
            .map(|idx| ModuleId(LogicalModuleIndex(idx)))
            .collect();
        let constraining_pairs: BTreeSet<(ModuleId, ModuleId)> = canonical_edges.pairs().collect();
        let import_order = EsmImportOrder::build(
            &constraining_pairs,
            &canonical_edges.i_successors,
            &extra_nodes,
        );
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
            import_order,
        }
    }

    /// SCC partition of `dep_graph` (the module quotient), in
    /// `tarjan_scc` reverse-topological order. Precomputed at build
    /// time and shared with downstream consumers in lieu of a fresh
    /// Tarjan walk.
    pub fn dep_graph_sccs(&self) -> &[Vec<ModuleId>] {
        &self.dep_graph_sccs
    }

    /// Shared per-module ESM import ordering. The emitter renders
    /// every import-directive list through this; the realizability
    /// gate's evaluation simulator applies the same ordering rules
    /// (see `esm_import_order::EsmImportOrder` for the contract).
    pub fn import_order(&self) -> &EsmImportOrder {
        &self.import_order
    }

    /// Run SCC analysis over the dep graph. Spec authors consume the
    /// resulting report to fix any cycles or atomic-unit conflicts.
    pub fn validate(&self) -> FactorizationReport {
        let mut report =
            validate_factorization(&self.analysis.owner_graph, &self.partition, &|id| {
                self.analysis.module_path(id)
            });
        report.atomic_unit_conflicts = self.assembly_conflicts.clone();
        report.linker_order = self
            .import_order
            .linker_order()
            .iter()
            .map(|id| self.analysis.module_path(*id))
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
