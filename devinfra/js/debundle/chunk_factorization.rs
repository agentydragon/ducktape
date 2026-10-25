use std::collections::{BTreeSet, HashMap};
use std::sync::Arc;

use crate::report_builders::build_owner_graph_report;
use analysis::assignment_assembly::{AtomicUnitConflict, assemble_assignment};
use analysis::atomic_units::{AtomicUnit, OwnerGraphAndUnits};
use analysis::graph::{build_module_quotient, chunk_constraining_module_edges};
use analysis::module_assignment::ModuleAssignment;
use analysis::{LogicalModuleIndex, ModuleId, ModuleQuotient, OwnerGraphReport, PlannedModule};

use crate::FactorizationInputs;
use crate::esm_import_order::EsmImportOrder;
use crate::validation::{FactorizationReport, validate_factorization};

/// Per-chunk factorization output: the spec's assignment of the
/// owner graph, plus the realizability views derived from it
/// (`dep_graph`, `linker_order`, `assembly_conflicts`) and small
/// caches downstream consumers consult on the hot path.
///
/// Holds a reference to the [`FactorizationInputs`] it was factorized
/// from (`analysis`); report emission and downstream emit code reach
/// inputs (`bindings`, `logical_modules`, etc.) through `analysis()`.
/// Fields are private so the assignment cannot change independently of its
/// cached quotient, SCCs, and import order.
#[derive(Debug, Clone)]
pub struct ChunkFactorization {
    analysis: Arc<FactorizationInputs>,
    /// Module assignment per owner — the spec's assignment of the
    /// owner graph. Stored separately from the IR so the IR stays
    /// immutable across validation/report construction.
    assignment: ModuleAssignment,
    /// Atomic owner units computed from the owner graph's
    /// constraining-edge SCCs. Stored so report emission and
    /// downstream diagnostics reuse the same atomic-unit grouping that
    /// factor assembly validated.
    atomic_units: Vec<AtomicUnit>,
    /// Atomic-factor-unit splits the spec demands but the
    /// constraining-edge SCC analysis forbids — populated by
    /// `assignment_assembly` when YAML claims split an atomic unit across
    /// destination modules. Non-empty means the spec is unrealizable
    /// by construction; the materializer bails on these before
    /// emitting code.
    assembly_conflicts: Vec<AtomicUnitConflict>,
    dep_graph: ModuleQuotient,
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

/// An immutable assignment whose atomic-unit, schedule, and rebinding checks
/// passed. The borrow prevents changing the assignment while lowering uses it.
#[derive(Debug)]
pub struct ValidatedAssignment<'a> {
    factorization: &'a ChunkFactorization,
    report: FactorizationReport,
}

impl ValidatedAssignment<'_> {
    pub fn factorization(&self) -> &ChunkFactorization {
        self.factorization
    }

    pub fn report(&self) -> &FactorizationReport {
        &self.report
    }
}

impl ChunkFactorization {
    pub fn analysis(&self) -> &FactorizationInputs {
        &self.analysis
    }

    pub fn assignment(&self) -> &ModuleAssignment {
        &self.assignment
    }

    pub fn atomic_units(&self) -> &[AtomicUnit] {
        &self.atomic_units
    }

    pub fn dep_graph(&self) -> &ModuleQuotient {
        &self.dep_graph
    }

    /// Build a factorization from a caller-computed owner graph +
    /// atomic units (the materializer computes these once per chunk
    /// for mini-factor synthesis), the binding catalogue, and the
    /// spec-derived logical modules. `bindings` should already have
    /// every `Owned` binding the spec assigned and every `Imported`
    /// binding the spec re-exports.
    pub fn build_with(
        chunk_id: String,
        precomputed: OwnerGraphAndUnits,
        bindings: HashMap<swc_ecma_ast::Id, analysis::BindingKind>,
        logical_modules: Vec<PlannedModule>,
        chunk_renames: HashMap<swc_ecma_ast::Id, swc_atoms::Atom>,
        default_destination: ModuleId,
    ) -> Self {
        let OwnerGraphAndUnits {
            owner_graph,
            atomic_units,
        } = precomputed;
        let outcome = assemble_assignment(
            &owner_graph,
            &atomic_units,
            &bindings,
            &logical_modules,
            default_destination,
        );
        let assignment = outcome.assignment;
        let assembly_conflicts = outcome.conflicts;
        let dep_graph = build_module_quotient(&owner_graph, &assignment);
        // Cache the dep-graph SCC partition so downstream consumers
        // (`report_builders::build_quotient_scc_reports`, the validator's
        // verdict path) share one Tarjan walk instead of each
        // recomputing it.
        let dep_graph_sccs = dep_graph.sccs();
        // Drive Lemma-2 ordering through the canonical
        // `ChunkConstrainingEdgeSet` so the emitter and the gate's
        // simulator share one source of truth — see
        // `graph::chunk_constraining_module_edges` for the invariant doc.
        let canonical_edges = chunk_constraining_module_edges(&owner_graph, &assignment);
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
        let analysis = Arc::new(FactorizationInputs::build(
            chunk_id,
            owner_graph,
            bindings,
            logical_modules,
            &chunk_renames,
        ));
        Self {
            analysis,
            assignment,
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
            validate_factorization(self.analysis.owner_graph(), &self.assignment, &|id| {
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

    pub fn validate_for_emission(&self) -> Result<ValidatedAssignment<'_>, FactorizationReport> {
        let report = self.validate();
        if !report.atomic_unit_conflicts.is_empty()
            || !report.cycles.is_empty()
            || !report.cross_rebinds.is_empty()
        {
            return Err(report);
        }
        Ok(ValidatedAssignment {
            factorization: self,
            report,
        })
    }

    /// High-fidelity node-link view of the fine owner graph plus
    /// its current module quotient. Written as
    /// `<chunk_id>/owner_graph.json` for downstream peel tooling.
    pub fn owner_graph_report(&self) -> OwnerGraphReport {
        build_owner_graph_report(self)
    }
}
