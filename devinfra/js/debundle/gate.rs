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

mod assignment_analysis;
mod assignment_inputs;
mod counted_digraph;
mod esm_import_order;
mod realizability;
mod report_builders;
mod validation;

pub use assignment_analysis::{
    ApplicationAssessmentError, ApplicationRelocationAssessmentReport, AssignmentAnalysis,
    AssignmentAnalysisBuildError, AtomicGroupPlacementClass, AtomicGroupPlacementObligation,
    CandidateEvaluation, CandidateModuleInitializationEvidence, CandidatePreflight,
    CandidateProviderTargetEvidence, CandidateRelocationAssessment,
    CandidateRelocationAssessmentReport, CandidateRelocationStatus,
    ConditionalProviderTargetAddress, ConditionalProviderTargetAssessment,
    CrossDestinationRebindObligation, ModuleCellAddress, ModuleInitializationAssessment,
    ModuleInitializationRefusal, ProviderTargetRefusal, RejectedAssignment,
    SemanticOrderSccObligation, StructuralPlacementAssessment, StructuralPlacementCoverage,
    StructuralPlacementStatus, ValidatedAssignment,
};
pub use assignment_inputs::AssignmentInputs;
pub use esm_import_order::EsmImportOrder;
pub use realizability::{
    CrossRebindEdge, EffectConflictCell, EffectConflictDiagnosticRow, EffectConflictDisposition,
    EffectConflictRow, EffectCoverageReport, EffectOpacity, EffectRelocationAcceptanceScope,
    EffectRelocationAssessment, EffectRelocationConflict, EffectRelocationDecision,
    EffectTranslationValidationReport, EffectTranslationValidationResult, LadderDecision,
    LegacyEdgeComparisonCounts, ModuleAssignmentDelta, RealizabilityIndex, RealizabilityVerdict,
    SccDiagnosis, SccRejection, TranslationValidationEvidenceReport, TranslationValidationReport,
    TranslationValidationResult, check_realizability, check_realizability_touching,
    simulated_evaluation_post_order,
};
pub use validation::{
    AssignmentReport, AssignmentValidation, AtInitReadPosition, BlockingRejection,
    BlockingSccEntry, CutEdgeExplanation, CycleEdge, CycleReport,
    render_atomic_group_conflict_summary, render_cycle_summary, render_explanations,
    validate_assignment, validate_assignment_with_verdict,
};

#[cfg(test)]
mod analysis_tests;
