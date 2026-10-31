use std::collections::{BTreeSet, HashMap};
use std::sync::Arc;

use thiserror::Error;

use crate::report_builders::build_dependency_graph_report;
use analysis::assignment_assembly::{AtomicGroupConflict, assemble_assignment};
use analysis::atomic_groups::{AtomicGroup, DependencyGraphAndAtomicGroups};
use analysis::graph::{build_module_quotient, chunk_constraining_module_edges};
use analysis::module_assignment::ModuleAssignment;
use analysis::{
    BindingCatalogue, BindingScopeError, ChunkId, DependencyGraphReport, EntryDestination,
    ModuleId, ModuleQuotient, PlannedModule,
};

use crate::AssignmentInputs;
use crate::esm_import_order::EsmImportOrder;
use crate::validation::{AssignmentReport, validate_assignment};

#[derive(Debug, Error)]
pub enum AssignmentAnalysisBuildError {
    #[error(transparent)]
    BindingScope(#[from] BindingScopeError),
    #[error(
        "entry destination logical:{entry} must follow the {planned_module_count} planned output modules"
    )]
    InvalidEntryDestination {
        entry: usize,
        planned_module_count: usize,
    },
}

/// Per-chunk assignment analysis: the spec's assignment of the
/// dependency graph, plus the realizability views derived from it
/// (`module_quotient`, `import_order`, `assembly_conflicts`) and small
/// caches downstream consumers consult on the hot path.
///
/// Holds a reference to the [`AssignmentInputs`] it was built
/// from (`inputs`); report emission and downstream emit code reach
/// inputs (`bindings`, `planned_modules`, etc.) through `inputs()`.
/// Fields are private so the assignment cannot change independently of its
/// cached quotient, SCCs, and import order.
#[derive(Debug, Clone)]
pub struct AssignmentAnalysis {
    inputs: Arc<AssignmentInputs>,
    /// Module assignment per source unit — the spec's assignment of the
    /// dependency graph. Stored separately from the IR so the IR stays
    /// immutable across validation/report construction.
    assignment: ModuleAssignment,
    /// Atomic groups computed from the dependency graph's
    /// constraining-edge SCCs. Stored so report emission and
    /// downstream diagnostics reuse the same atomic group grouping that
    /// factor assembly validated.
    atomic_groups: Vec<AtomicGroup>,
    /// Atomic-group conflicts the spec demands but the
    /// constraining-edge SCC analysis forbids — populated by
    /// `assignment_assembly` when YAML claims split an atomic group across
    /// destination modules. Non-empty means the spec is unrealizable
    /// by construction; the materializer bails on these before
    /// emitting code.
    assembly_conflicts: Vec<AtomicGroupConflict>,
    module_quotient: ModuleQuotient,
    /// SCC partition of `module_quotient` in `tarjan_scc` reverse-
    /// topological order. Precomputed at build time for
    /// `report_builders::build_quotient_scc_reports`.
    module_quotient_sccs: Vec<Vec<ModuleId>>,
    /// Shared per-module ESM import ordering — the single source of
    /// truth the emitter renders import declarations from and the
    /// realizability gate's evaluation simulator mirrors. See
    /// `esm_import_order::EsmImportOrder`.
    import_order: EsmImportOrder,
}

/// An immutable assignment whose atomic-group, schedule, and rebinding checks
/// passed. The borrow prevents changing the assignment while lowering uses it.
#[derive(Debug)]
pub struct ValidatedAssignment<'a> {
    assignment_analysis: &'a AssignmentAnalysis,
    report: AssignmentReport,
}

impl ValidatedAssignment<'_> {
    pub fn assignment_analysis(&self) -> &AssignmentAnalysis {
        self.assignment_analysis
    }

    pub fn report(&self) -> &AssignmentReport {
        &self.report
    }
}

/// An immutable assignment the gate rejected. It still projects to syntax
/// placement so `debundle run --emit-on-rejection` can lower it for reading;
/// the report says why the result is not runnable.
#[derive(Debug)]
pub struct RejectedAssignment<'a> {
    assignment_analysis: &'a AssignmentAnalysis,
    report: AssignmentReport,
}

impl RejectedAssignment<'_> {
    pub fn assignment_analysis(&self) -> &AssignmentAnalysis {
        self.assignment_analysis
    }

    pub fn report(&self) -> &AssignmentReport {
        &self.report
    }
}

impl AssignmentAnalysis {
    pub fn inputs(&self) -> &AssignmentInputs {
        &self.inputs
    }

    pub fn assignment(&self) -> &ModuleAssignment {
        &self.assignment
    }

    pub fn atomic_groups(&self) -> &[AtomicGroup] {
        &self.atomic_groups
    }

    pub fn module_quotient(&self) -> &ModuleQuotient {
        &self.module_quotient
    }

    /// Canonical report path for a destination. Real output paths come from
    /// their `PlannedModule`; the entry keeps the historic report label at
    /// this serialization boundary without becoming a planned output.
    pub fn module_path(&self, id: ModuleId) -> spec::ModulePath {
        if id == self.assignment.entry_destination().module_id() {
            let report_id = format!("{}::anon_residual_sentinel", self.inputs.chunk_id());
            return spec::ModulePath::parse(&report_id, self.inputs.chunk_id())
                .expect("entry destination report path is canonical");
        }
        self.inputs.planned_module_path(id)
    }

    /// Build an assignment analysis from a caller-computed dependency graph +
    /// atomic groups (the materializer computes these once per chunk
    /// for atomic-group module synthesis), the binding catalogue, and the
    /// real planned output modules. The entry destination follows those
    /// modules in this analysis. The catalogue should already have
    /// every `Owned` binding the spec assigned and every `Imported`
    /// binding the spec re-exports. `chunk_identity` must match the
    /// catalogue's interned source chunk; `chunk_id` remains the diagnostic
    /// name.
    pub fn build_with(
        chunk_id: String,
        chunk_identity: ChunkId,
        precomputed: DependencyGraphAndAtomicGroups,
        binding_catalogue: BindingCatalogue,
        planned_modules: Vec<PlannedModule>,
        chunk_renames: HashMap<swc_ecma_ast::Id, swc_atoms::Atom>,
        entry_destination: EntryDestination,
    ) -> Result<Self, AssignmentAnalysisBuildError> {
        let entry_module_id = entry_destination.module_id();
        if entry_module_id != ModuleId(planned_modules.len()) {
            return Err(AssignmentAnalysisBuildError::InvalidEntryDestination {
                entry: entry_module_id.0,
                planned_module_count: planned_modules.len(),
            });
        }
        let DependencyGraphAndAtomicGroups {
            dependency_graph,
            atomic_groups,
        } = precomputed;
        let outcome = {
            let local_bindings = binding_catalogue.local_view(chunk_identity)?;
            assemble_assignment(
                &dependency_graph,
                &atomic_groups,
                &local_bindings,
                &planned_modules,
                entry_destination,
            )
        };
        let assignment = outcome.assignment;
        let assembly_conflicts = outcome.conflicts;
        let module_quotient = build_module_quotient(&dependency_graph, &assignment);
        // Cache the module-quotient SCC partition so downstream consumers
        // (`report_builders::build_quotient_scc_reports`, the validator's
        // verdict path) share one Tarjan walk instead of each
        // recomputing it.
        let module_quotient_sccs = module_quotient.sccs();
        // Drive Lemma-2 ordering through the canonical
        // `ChunkConstrainingEdgeSet` so the emitter and the gate's
        // simulator share one source of truth — see
        // `graph::chunk_constraining_module_edges` for the invariant doc.
        let canonical_edges = chunk_constraining_module_edges(&dependency_graph, &assignment);
        // Every output module and the entry destination need a deterministic
        // source-order slot for emit stability, even singletons that don't
        // participate in any canonical edge.
        let mut extra_nodes: BTreeSet<ModuleId> =
            (0..planned_modules.len()).map(ModuleId).collect();
        extra_nodes.insert(entry_module_id);
        let constraining_pairs: BTreeSet<(ModuleId, ModuleId)> = canonical_edges.pairs().collect();
        let import_order = EsmImportOrder::build(
            &constraining_pairs,
            &canonical_edges.i_successors,
            &extra_nodes,
        );
        let inputs = Arc::new(AssignmentInputs::build(
            chunk_id,
            dependency_graph,
            chunk_identity,
            binding_catalogue,
            planned_modules,
            &chunk_renames,
        )?);
        Ok(Self {
            inputs,
            assignment,
            atomic_groups,
            assembly_conflicts,
            module_quotient,
            module_quotient_sccs,
            import_order,
        })
    }

    /// SCC partition of `module_quotient` (the module quotient), in
    /// `tarjan_scc` reverse-topological order. Precomputed at build
    /// time and shared with downstream consumers in lieu of a fresh
    /// Tarjan walk.
    pub fn module_quotient_sccs(&self) -> &[Vec<ModuleId>] {
        &self.module_quotient_sccs
    }

    /// Shared per-module ESM import ordering. The emitter renders
    /// every import-directive list through this; the realizability
    /// gate's evaluation simulator applies the same ordering rules
    /// (see `esm_import_order::EsmImportOrder` for the contract).
    pub fn import_order(&self) -> &EsmImportOrder {
        &self.import_order
    }

    /// Validate the assignment and its module quotient. Spec authors consume the
    /// resulting report to fix any cycles or atomic-group conflicts.
    pub fn validate(&self) -> AssignmentReport {
        let mut report =
            validate_assignment(self.inputs.dependency_graph(), &self.assignment, &|id| {
                self.module_path(id)
            });
        report.atomic_group_conflicts = self.assembly_conflicts.clone();
        report.linker_order = self
            .import_order
            .linker_order()
            .iter()
            .map(|id| self.module_path(*id))
            .collect();
        report
    }

    pub fn validate_for_emission(&self) -> Result<ValidatedAssignment<'_>, RejectedAssignment<'_>> {
        let report = self.validate();
        if !report.atomic_group_conflicts.is_empty()
            || !report.cycles.is_empty()
            || !report.cross_rebinds.is_empty()
        {
            return Err(RejectedAssignment {
                assignment_analysis: self,
                report,
            });
        }
        Ok(ValidatedAssignment {
            assignment_analysis: self,
            report,
        })
    }

    /// High-fidelity node-link view of the fine dependency graph plus
    /// its current module quotient. Written as
    /// `<chunk_id>/dependency_graph.json` for downstream peel tooling;
    /// `program_graph` is the run's program graph relative to that file.
    pub fn dependency_graph_report(
        &self,
        program_graph: std::path::PathBuf,
    ) -> DependencyGraphReport {
        build_dependency_graph_report(self, program_graph)
    }
}
