//! Realizability gate and assignment validation.
//!
//! Builds on the `analysis` core (dependency graph, assignment, reports):
//! quotient the dependency graph into the module dependency graph used by
//! ESM import emission and linker-order reasoning, validate
//! realizability (`realizability`, `validation`), and assemble the
//! per-chunk assignment analysis (`chunk_analysis`,
//! `assignment_analysis`). The shared ESM import ordering consumed by
//! both the gate's simulator and the emitter lives in
//! `esm_import_order`.

use std::path::PathBuf;

use serde::Serialize;

mod assignment_analysis;
mod assignment_inputs;
#[cfg(test)]
mod counted_digraph;
mod esm_import_order;
mod esm_schedule;
mod realizability;
mod report_builders;
mod validation;

pub use assignment_analysis::{
    AssignmentAnalysis, AssignmentAnalysisBuildError, AtomicGroupPlacementClass,
    AtomicGroupPlacementObligation, CandidateEvaluation, CandidatePreflight,
    CandidateRelocationAssessment, CandidateRelocationAssessmentReport, CandidateRelocationStatus,
    CrossDestinationRebindObligation, RejectedAssignment, SemanticOrderSccObligation,
    StructuralPlacementAssessment, StructuralPlacementCoverage, StructuralPlacementStatus,
    ValidatedAssignment, tv_audit_dir,
};
pub use assignment_inputs::AssignmentInputs;
pub use esm_import_order::EsmImportOrder;
#[cfg(test)]
pub use esm_schedule::ScheduleObstruction;
pub use esm_schedule::{EsmImportSchedule, ImportScheduleError};
pub use realizability::{
    AssignmentConstraints, CrossRebindEdge, EffectConflictCell, EffectConflictDiagnosticRow,
    EffectConflictDisposition, EffectConflictRow, EffectCoverageReport, EffectOpacity,
    EffectRelocationAcceptanceScope, EffectRelocationAssessment, EffectRelocationConflict,
    EffectRelocationDecision, EffectTranslationValidationReport, EffectTranslationValidationResult,
    LegacyEdgeComparisonCounts, RealizabilityVerdict, SccDiagnosis, SccRejection,
    TranslationValidationEvidenceReport, TranslationValidationReport, TranslationValidationResult,
    assignment_constraining_cycles, check_realizability_with_priority,
    check_realizability_with_schedule, project_assignment_constraints,
};
#[cfg(test)]
pub use realizability::{
    LadderDecision, ModuleAssignmentBatch, ModuleAssignmentDelta, QuotientConstraints,
    QuotientTopologyDelta, RealizabilityIndex, check_realizability, check_realizability_touching,
};
pub use validation::{
    AssignmentReport, AssignmentValidation, AtInitReadPosition, BlockingRejection,
    BlockingSccEntry, CutEdgeExplanation, CycleEdge, CycleReport,
    render_atomic_group_conflict_summary, render_cycle_summary, render_explanations,
    validate_assignment_with_priority, validate_assignment_with_schedule,
};
#[cfg(test)]
pub use validation::{validate_assignment, validate_assignment_with_verdict};

/// A `define_logical_module` member whose `binding.name` did not
/// resolve to a top-level declaration in the chunk. Collected across chunks
/// and rejected at final validation before accepted output is written.
#[derive(Debug, Clone, Serialize)]
pub struct UnmatchedSpecClaim {
    pub chunk_id: String,
    /// Claiming module, by canonical [`spec::ModulePath`].
    pub module_path: spec::ModulePath,
    pub binding_name: String,
    pub export_name: String,
}

/// Which realizability-gate check rejected a chunk's assignment.
#[derive(Debug, Clone, Copy, PartialEq, Eq, strum::Display)]
pub enum GateVerdict {
    /// Members of one atomic group were claimed by different modules.
    #[strum(to_string = "{count} atomic-group conflict(s)")]
    AtomicGroupConflicts { count: usize },
    /// Module-quotient SCCs with at-init / side-effect edges between members.
    #[strum(to_string = "{count} unrealizable module SCC(s)")]
    UnrealizableCycles { count: usize },
}

/// A chunk the realizability gate rejected but lowering continued for, under
/// `debundle run --emit-on-rejection`. Its files are readable, not runnable.
#[derive(Debug, Clone)]
pub struct ChunkGateRejection {
    pub chunk_id: String,
    pub verdict: GateVerdict,
    /// `cycles.json` / `atomic_group_conflicts.json`, when the run has a
    /// report directory.
    pub evidence_report: Option<PathBuf>,
    pub dependency_graph_report: Option<PathBuf>,
    /// The rendered rejection the run fails with.
    pub diagnostic: String,
}

impl ChunkGateRejection {
    pub fn summary_line(&self) -> String {
        let evidence = match &self.evidence_report {
            Some(path) => format!("evidence: {}", path.display()),
            None => "no report directory configured; see the run's stderr".to_string(),
        };
        format!(
            "chunk {chunk}: {verdict}; {evidence}",
            chunk = self.chunk_id,
            verdict = self.verdict,
        )
    }
}
