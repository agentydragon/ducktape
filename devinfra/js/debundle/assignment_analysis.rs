use analysis_model::DepKind;
use source_identity::ChunkId;
use std::collections::{BTreeSet, HashMap};
use std::sync::{Arc, OnceLock};

use serde::Serialize;
use thiserror::Error;

use crate::report_builders::build_dependency_graph_report;
use analysis::assignment_assembly::{AtomicGroupConflict, assemble_assignment};
use analysis::atomic_groups::{AtomicGroup, DependencyGraphAndAtomicGroups};
use analysis::graph::{build_module_quotient, chunk_constraining_module_edges};
use analysis::module_assignment::ModuleAssignment;
use analysis::{
    BindingCatalogue, BindingScopeError, DependencyGraphReport, EntryDestination, ModuleId,
    ModuleQuotient, PlannedModule, StructuralPlacementCheck,
};
use source_identity::SourceUnitId;

use crate::AssignmentInputs;
use crate::esm_import_order::EsmImportOrder;
use crate::realizability::CrossRebindEdge;
use crate::realizability::{
    CandidateEmittedOrder, EffectConflictDiagnosticRow, EffectRelocationAssessment,
    EffectTranslationValidationInputs, EffectTranslationValidationResult, RealizabilityVerdict,
    SccRejection, TranslationValidationEvidenceReport, TranslationValidationReport,
    TranslationValidationResult, check_realizability, effect_translation_assess,
    translation_validate,
};
use crate::validation::{AssignmentReport, AssignmentValidation, validation_from_verdict};

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
    /// Atomic-group conflicts populated by `assignment_assembly` when YAML
    /// claims split a group across destinations. The final assessment separates
    /// structural blockers from semantic-order comparison constraints.
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
    /// Planning and final validation share this verdict for the immutable
    /// graph/assignment pair. A rebuilt assignment starts with an empty cache.
    realizability: OnceLock<RealizabilityVerdict>,
}

/// An immutable assignment whose complete effect and structural assessment
/// accepted the exact emitted order. The borrow keeps that assignment fixed.
#[derive(Debug)]
pub struct ValidatedAssignment<'a> {
    assignment_analysis: &'a AssignmentAnalysis,
    validation: AssignmentValidation,
    assessment: CandidateRelocationAssessment,
}

impl<'a> ValidatedAssignment<'a> {
    pub fn assignment_analysis(&self) -> &'a AssignmentAnalysis {
        self.assignment_analysis
    }

    pub fn report(&self) -> &AssignmentReport {
        &self.validation.report
    }

    pub fn assessment(&self) -> &CandidateRelocationAssessment {
        &self.assessment
    }
}

/// An immutable assignment the gate rejected. It still projects to syntax
/// placement so `debundle run --emit-on-rejection` can lower it for reading;
/// the report says why the result is not runnable.
#[derive(Debug)]
pub struct RejectedAssignment<'a> {
    assignment_analysis: &'a AssignmentAnalysis,
    validation: Box<AssignmentValidation>,
}

impl<'a> RejectedAssignment<'a> {
    pub fn assignment_analysis(&self) -> &'a AssignmentAnalysis {
        self.assignment_analysis
    }

    pub fn report(&self) -> &AssignmentReport {
        &self.validation.report
    }
}

/// One final candidate assessment over the exact immutable assignment. Legacy
/// graph and edge validation are retained only as diagnostic comparisons.
#[derive(Debug)]
pub struct CandidateEvaluation<'a> {
    assignment_analysis: &'a AssignmentAnalysis,
    legacy_validation: AssignmentValidation,
    translation_validation: TranslationValidationResult,
    candidate_assessment: CandidateRelocationAssessment,
}

impl<'a> CandidateEvaluation<'a> {
    pub fn assignment_analysis(&self) -> &'a AssignmentAnalysis {
        self.assignment_analysis
    }

    pub fn is_rejected(&self) -> bool {
        self.candidate_assessment.status() != CandidateRelocationStatus::AcceptedWithinCheckedScope
    }

    pub fn report(&self) -> &AssignmentReport {
        &self.legacy_validation.report
    }

    pub fn translation_validation(&self) -> &TranslationValidationResult {
        &self.translation_validation
    }

    pub fn candidate_assessment(&self) -> &CandidateRelocationAssessment {
        &self.candidate_assessment
    }

    pub fn into_validation(self) -> Result<ValidatedAssignment<'a>, RejectedAssignment<'a>> {
        if self.is_rejected() {
            Err(RejectedAssignment {
                assignment_analysis: self.assignment_analysis,
                validation: Box::new(self.legacy_validation),
            })
        } else {
            Ok(ValidatedAssignment {
                assignment_analysis: self.assignment_analysis,
                validation: self.legacy_validation,
                assessment: self.candidate_assessment,
            })
        }
    }

    /// Assess every final candidate, regardless of report output. The visitors
    /// export the same sweeps without retaining a second copy of their rows.
    pub fn evaluate<E>(
        assignment_analysis: &'a AssignmentAnalysis,
        facts: &[analysis::facts::StatementFacts],
        projection: &analysis::PlacementProjection<'_>,
        translation_validation_visitor: Option<
            &mut impl FnMut(&TranslationValidationEvidenceReport) -> Result<(), E>,
        >,
        effect_visitor: Option<&mut impl FnMut(&EffectConflictDiagnosticRow<'_>) -> Result<(), E>>,
    ) -> Result<Self, E> {
        let legacy_validation = assignment_analysis.validate_with_verdict();
        let emitted_order = assignment_analysis.candidate_emitted_order();
        let translation_validation = assignment_analysis.translation_validation(
            &legacy_validation,
            &emitted_order,
            translation_validation_visitor,
        )?;
        let candidate_assessment = assignment_analysis.candidate_relocation_assessment(
            &legacy_validation,
            facts,
            &translation_validation,
            &emitted_order,
            projection,
            effect_visitor,
        )?;
        Ok(Self {
            assignment_analysis,
            legacy_validation,
            translation_validation,
            candidate_assessment,
        })
    }
}

/// The reason a split atomic group is surfaced to the placement pass. Pure
/// eager-use/sequenced SCCs are semantic order constraints; the remaining
/// recognized causes preserve source-declarator and other structural
/// co-location obligations. Mixed or empty cause sets stay explicit blockers.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum AtomicGroupPlacementClass {
    SemanticOrderScc,
    StructuralConstraint,
    MixedOrUnclassified,
}

#[derive(Debug, Clone, Serialize)]
pub struct AtomicGroupPlacementObligation {
    pub classification: AtomicGroupPlacementClass,
    pub conflict: AtomicGroupConflict,
}

/// A typed cross-destination write edge that must be placed with its binding
/// owner before the emitted ESM is writable.
#[derive(Debug, Clone, Serialize)]
pub struct CrossDestinationRebindObligation {
    pub edge: CrossRebindEdge,
    pub source_unit: SourceUnitId,
    pub target_unit: SourceUnitId,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum StructuralPlacementStatus {
    Blocked,
    NoKnownBlocker,
}

#[derive(Debug, Clone, Serialize)]
pub struct StructuralPlacementCoverage {
    pub unresolved_checks: BTreeSet<StructuralPlacementCheck>,
}

impl StructuralPlacementCoverage {
    pub fn is_complete(&self) -> bool {
        self.unresolved_checks.is_empty()
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct StructuralPlacementAssessment {
    pub status: StructuralPlacementStatus,
    /// Reads the same typed projection consumed by emission; diagnostic counts
    /// do not reconstruct the emitter's decision.
    pub emission_mappable: bool,
    pub coverage: StructuralPlacementCoverage,
    pub projection_violations: Vec<analysis::PlacementProjectionViolation>,
    pub cross_destination_rebinds: Vec<CrossDestinationRebindObligation>,
    pub atomic_group_obligations: Vec<AtomicGroupPlacementObligation>,
    pub semantic_order_sccs: Vec<SemanticOrderSccObligation>,
}

#[derive(Debug, Clone, Serialize)]
pub struct SemanticOrderSccObligation {
    pub rejection: SccRejection,
    pub modules: Vec<analysis::ModuleId>,
    pub dependency_edges: Vec<usize>,
}

/// A same-assignment candidate result. The effect assessment and its report
/// are retained together from one sweep, beside typed structural placement
/// obligations derived from the validation that admitted this exact analysis.
#[derive(Debug, Clone)]
pub struct CandidateRelocationAssessment {
    pub placement: StructuralPlacementAssessment,
    pub effect: EffectTranslationValidationResult,
}

#[derive(Debug, Clone, Serialize)]
pub struct CandidateRelocationAssessmentReport {
    #[serde(flatten)]
    pub effect: EffectRelocationAssessment,
    pub status: CandidateRelocationStatus,
    pub legacy_gate_accepts: bool,
    /// Diagnostic disagreement: the legacy graph rejects semantic-order SCCs
    /// independently of the final effect/structural verdict.
    pub legacy_gate_ordering_veto: bool,
    pub placement: StructuralPlacementAssessment,
}

/// One application verdict with every required materialized chunk's actual
/// effect/placement assessment. Construction rejects partial or repeated sets.
#[derive(Debug, Clone, Serialize)]
pub struct ApplicationRelocationAssessmentReport {
    pub status: CandidateRelocationStatus,
    pub chunks: BTreeMap<String, CandidateRelocationAssessmentReport>,
}

#[derive(Debug, Error)]
pub enum ApplicationAssessmentError {
    #[error("candidate assessment contains unexpected materialized chunk {0:?}")]
    UnexpectedChunk(String),
    #[error("candidate assessment repeats materialized chunk {0:?}")]
    DuplicateChunk(String),
    #[error("candidate assessment is missing materialized chunks {0:?}")]
    MissingChunks(Vec<String>),
}

impl ApplicationRelocationAssessmentReport {
    pub fn from_chunks(
        expected: &BTreeSet<String>,
        assessments: Vec<(String, CandidateRelocationAssessmentReport)>,
    ) -> Result<Self, ApplicationAssessmentError> {
        let mut chunks = BTreeMap::new();
        for (chunk, report) in assessments {
            if !expected.contains(&chunk) {
                return Err(ApplicationAssessmentError::UnexpectedChunk(chunk));
            }
            if chunks.insert(chunk.clone(), report).is_some() {
                return Err(ApplicationAssessmentError::DuplicateChunk(chunk));
            }
        }
        let missing = expected
            .iter()
            .filter(|chunk| !chunks.contains_key(*chunk))
            .cloned()
            .collect::<Vec<_>>();
        if !missing.is_empty() {
            return Err(ApplicationAssessmentError::MissingChunks(missing));
        }
        let mut effect_blocked = false;
        let mut placement_blocked = false;
        let mut incomplete = false;
        for report in chunks.values() {
            match report.status {
                CandidateRelocationStatus::BlockedByEffect => effect_blocked = true,
                CandidateRelocationStatus::BlockedByPlacement => placement_blocked = true,
                CandidateRelocationStatus::BlockedByEffectAndPlacement => {
                    effect_blocked = true;
                    placement_blocked = true;
                }
                CandidateRelocationStatus::Incomplete => incomplete = true,
                CandidateRelocationStatus::AcceptedWithinCheckedScope => {}
            }
        }
        let status = match (effect_blocked, placement_blocked, incomplete) {
            (true, true, _) => CandidateRelocationStatus::BlockedByEffectAndPlacement,
            (true, false, _) => CandidateRelocationStatus::BlockedByEffect,
            (false, true, _) => CandidateRelocationStatus::BlockedByPlacement,
            (false, false, true) => CandidateRelocationStatus::Incomplete,
            (false, false, false) => CandidateRelocationStatus::AcceptedWithinCheckedScope,
        };
        Ok(Self { status, chunks })
    }
}

/// Final relocation verdict for the assessed order. Acceptance requires complete
/// effect and structural obligations and actual emitter placement maps.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum CandidateRelocationStatus {
    BlockedByEffect,
    BlockedByPlacement,
    BlockedByEffectAndPlacement,
    Incomplete,
    AcceptedWithinCheckedScope,
}

impl CandidateRelocationAssessment {
    pub fn status(&self) -> CandidateRelocationStatus {
        let effect_blocked = matches!(
            self.effect.assessment.decision,
            crate::realizability::EffectRelocationDecision::Conflict { .. }
        );
        let effect_accepted = matches!(
            self.effect.assessment.decision,
            crate::realizability::EffectRelocationDecision::Accepted { .. }
        );
        candidate_relocation_status(effect_blocked, effect_accepted, &self.placement)
    }

    pub fn report(&self) -> CandidateRelocationAssessmentReport {
        CandidateRelocationAssessmentReport {
            status: self.status(),
            legacy_gate_accepts: self.effect.report.legacy_gate_accepts,
            legacy_gate_ordering_veto: has_legacy_gate_ordering_veto(&self.placement),
            effect: self.effect.assessment.clone(),
            placement: self.placement.clone(),
        }
    }
}

fn candidate_relocation_status(
    effect_blocked: bool,
    effect_accepted: bool,
    placement: &StructuralPlacementAssessment,
) -> CandidateRelocationStatus {
    match (
        effect_blocked,
        placement.status == StructuralPlacementStatus::Blocked,
    ) {
        (true, true) => CandidateRelocationStatus::BlockedByEffectAndPlacement,
        (true, false) => CandidateRelocationStatus::BlockedByEffect,
        (false, true) => CandidateRelocationStatus::BlockedByPlacement,
        (false, false)
            if !effect_accepted
                || !placement.coverage.is_complete()
                || !placement.emission_mappable =>
        {
            CandidateRelocationStatus::Incomplete
        }
        (false, false) => CandidateRelocationStatus::AcceptedWithinCheckedScope,
    }
}

fn has_legacy_gate_ordering_veto(placement: &StructuralPlacementAssessment) -> bool {
    !placement.semantic_order_sccs.is_empty()
        || placement.atomic_group_obligations.iter().any(|obligation| {
            obligation.classification == AtomicGroupPlacementClass::SemanticOrderScc
        })
}

fn structural_placement_assessment(
    graph: &analysis::DependencyGraph,
    validation: &AssignmentValidation,
    projection: &analysis::PlacementProjection<'_>,
) -> StructuralPlacementAssessment {
    let cross_destination_rebinds: Vec<CrossDestinationRebindObligation> = validation
        .verdict
        .cross_rebinds
        .iter()
        .map(|edge| {
            let dependency = graph
                .iter_edges()
                .find(|dependency| dependency.id == edge.dependency_edge)
                .expect("typed cross-rebind edge must belong to the validated dependency graph");
            CrossDestinationRebindObligation {
                edge: edge.clone(),
                source_unit: dependency.from,
                target_unit: dependency.to,
            }
        })
        .collect();
    let atomic_group_obligations: Vec<AtomicGroupPlacementObligation> = validation
        .report
        .atomic_group_conflicts
        .iter()
        .cloned()
        .map(|conflict| AtomicGroupPlacementObligation {
            classification: classify_atomic_group_conflict(&conflict),
            conflict,
        })
        .collect();
    let semantic_order_sccs: Vec<SemanticOrderSccObligation> = validation
        .verdict
        .unrealizable_sccs
        .iter()
        .map(|diagnosis| SemanticOrderSccObligation {
            rejection: diagnosis.rejection,
            modules: diagnosis.core.modules.iter().copied().collect(),
            dependency_edges: diagnosis
                .core
                .constraining_dependency_edges
                .iter()
                .map(|edge| edge.0)
                .collect(),
        })
        .collect();
    let blocked = projection.assessment().has_blockers()
        || !cross_destination_rebinds.is_empty()
        || atomic_group_obligations.iter().any(|obligation| {
            obligation.classification != AtomicGroupPlacementClass::SemanticOrderScc
        });
    StructuralPlacementAssessment {
        status: if blocked {
            StructuralPlacementStatus::Blocked
        } else {
            StructuralPlacementStatus::NoKnownBlocker
        },
        emission_mappable: projection.emission_maps().is_some(),
        coverage: StructuralPlacementCoverage {
            unresolved_checks: projection.assessment().unresolved_checks().clone(),
        },
        projection_violations: projection.assessment().violations().to_vec(),
        cross_destination_rebinds,
        atomic_group_obligations,
        semantic_order_sccs,
    }
}

fn classify_atomic_group_conflict(conflict: &AtomicGroupConflict) -> AtomicGroupPlacementClass {
    let causes = &conflict.causes;
    if !causes.is_empty()
        && causes
            .iter()
            .all(|cause| matches!(cause, DepKind::EagerUse | DepKind::Sequenced))
    {
        AtomicGroupPlacementClass::SemanticOrderScc
    } else if !causes.is_empty()
        && causes.iter().all(|cause| {
            matches!(
                cause,
                DepKind::EagerRebind
                    | DepKind::LazyRebind
                    | DepKind::DeferredRebind
                    | DepKind::CoDeclaration
                    | DepKind::CoLocation
                    | DepKind::LocalEffect
            )
        })
    {
        AtomicGroupPlacementClass::StructuralConstraint
    } else {
        AtomicGroupPlacementClass::MixedOrUnclassified
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
        let import_order = EsmImportOrder::build_for_assignment(
            &canonical_edges,
            &dependency_graph,
            &assignment,
            &emitted_modules(planned_modules.len(), entry_module_id),
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
            realizability: OnceLock::new(),
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
    /// every import-directive list through this; the candidate's exact
    /// emitted-order simulation consumes this same object
    /// (see `esm_import_order::EsmImportOrder` for the contract).
    pub fn import_order(&self) -> &EsmImportOrder {
        &self.import_order
    }

    /// Validate the assignment and its module quotient. Spec authors consume the
    /// resulting report to fix any cycles or atomic-group conflicts.
    pub fn validate(&self) -> AssignmentReport {
        self.validate_with_verdict().report
    }

    /// Retain the typed realizability result beside the rendered report for
    /// callers that must make a typed gate decision or pair it with another
    /// in-memory analysis of this assignment.
    pub fn validate_with_verdict(&self) -> AssignmentValidation {
        let mut validation = validation_from_verdict(
            self.inputs.dependency_graph(),
            &self.assignment,
            &|id| self.module_path(id),
            self.realizability_verdict().clone(),
        );
        validation.report.atomic_group_conflicts = self.assembly_conflicts.clone();
        validation.report.linker_order = self
            .import_order
            .linker_order()
            .iter()
            .map(|id| self.module_path(*id))
            .collect();
        validation
    }

    pub fn realizability_verdict(&self) -> &RealizabilityVerdict {
        self.realizability
            .get_or_init(|| check_realizability(self.inputs.dependency_graph(), &self.assignment))
    }

    /// Project one immutable candidate through the emitter's exact ESM loader
    /// order before either translation sweep consumes it.
    fn candidate_emitted_order(&self) -> CandidateEmittedOrder {
        CandidateEmittedOrder::from_assignment(
            self.inputs.dependency_graph(),
            &self.assignment,
            &emitted_modules(
                self.inputs.planned_modules().len(),
                self.assignment.entry_destination().module_id(),
            ),
            &self.import_order,
        )
    }

    /// Diagnostic edge translation validation beside the legacy graph verdict.
    /// `DEBUNDLE_TV_ORACLE` compares these two legacy checks, independently of
    /// the final effect/placement status.
    fn translation_validation<E>(
        &self,
        validation: &AssignmentValidation,
        emitted_order: &CandidateEmittedOrder,
        mut visitor: Option<&mut impl FnMut(&TranslationValidationEvidenceReport) -> Result<(), E>>,
    ) -> Result<TranslationValidationResult, E> {
        let dependency_graph = self.inputs.dependency_graph();
        let exporting = visitor.is_some();
        let mut project = |evidence| {
            visitor.as_mut().expect("active evidence visitor")(
                &TranslationValidationEvidenceReport::new(
                    evidence,
                    dependency_graph,
                    &self.assignment,
                    &|id| self.module_path(id),
                ),
            )
        };
        let verdict = translation_validate(
            dependency_graph,
            &self.assignment,
            emitted_order,
            exporting.then_some(&mut project),
        )?;
        let wire = TranslationValidationReport::new(
            &verdict,
            validation.verdict.is_realizable()
                && validation.report.atomic_group_conflicts.is_empty(),
            dependency_graph,
            &self.assignment,
            &|id| self.module_path(id),
        );
        assert!(
            wire.accepts == wire.gate_accepts || !tv_oracle_enabled(),
            "DEBUNDLE_TV_ORACLE: chunk {}: translation validation disagrees with the gate: {wire:#?}",
            self.inputs.chunk_id(),
        );
        Ok(TranslationValidationResult {
            report: wire,
            conflict_pairs: verdict.conflict_pairs,
        })
    }

    /// Assess structural placement and candidate-order effects together for
    /// this exact assignment/import order. The effect report and typed effect
    /// assessment come from one sweep; placement consumes the already-computed
    /// canonical validation instead of rerunning realizability.
    fn candidate_relocation_assessment<E>(
        &self,
        validation: &AssignmentValidation,
        facts: &[analysis::facts::StatementFacts],
        legacy: &TranslationValidationResult,
        emitted_order: &CandidateEmittedOrder,
        projection: &analysis::PlacementProjection<'_>,
        visitor: Option<&mut impl FnMut(&EffectConflictDiagnosticRow<'_>) -> Result<(), E>>,
    ) -> Result<CandidateRelocationAssessment, E> {
        assert!(
            std::ptr::eq(&self.assignment, projection.assignment()),
            "candidate placement projection must belong to this immutable assignment"
        );
        let effect = effect_translation_assess(
            EffectTranslationValidationInputs {
                graph: self.inputs.dependency_graph(),
                assignment: &self.assignment,
                emitted_order,
                facts,
                legacy,
                module_path: &|id| self.module_path(id),
            },
            visitor,
        )?;
        Ok(CandidateRelocationAssessment {
            placement: structural_placement_assessment(
                self.inputs.dependency_graph(),
                validation,
                projection,
            ),
            effect,
        })
    }

    /// High-fidelity node-link view of the fine dependency graph plus
    /// its current module quotient. Written as
    /// `<chunk_id>/dependency_graph.json.gz` for downstream peel tooling;
    /// `program_graph` is the run's program graph relative to that file.
    pub fn dependency_graph_report(
        &self,
        program_graph: std::path::PathBuf,
        author_inputs: &analysis::reports::author_inputs::AuthorInputLocations<'_>,
    ) -> DependencyGraphReport {
        build_dependency_graph_report(self, program_graph, author_inputs)
    }
}

/// The modules the emitted entry imports, plus the entry itself.
fn emitted_modules(planned_module_count: usize, entry: ModuleId) -> BTreeSet<ModuleId> {
    let mut modules: BTreeSet<ModuleId> = (0..planned_module_count).map(ModuleId).collect();
    modules.insert(entry);
    modules
}

fn tv_oracle_enabled() -> bool {
    static ENABLED: OnceLock<bool> = OnceLock::new();
    *ENABLED.get_or_init(|| std::env::var_os("DEBUNDLE_TV_ORACLE").is_some())
}

#[cfg(test)]
mod placement_classification_tests {
    use super::*;

    fn conflict(causes: impl IntoIterator<Item = DepKind>) -> AtomicGroupConflict {
        AtomicGroupConflict {
            members: vec![SourceUnitId(3), SourceUnitId(7)],
            claims: Vec::new(),
            causes: causes.into_iter().collect(),
        }
    }

    #[test]
    fn classifies_semantic_order_and_structural_constraints_separately() {
        assert_eq!(
            classify_atomic_group_conflict(&conflict([DepKind::EagerUse, DepKind::Sequenced])),
            AtomicGroupPlacementClass::SemanticOrderScc,
        );
        assert_eq!(
            classify_atomic_group_conflict(&conflict([DepKind::CoDeclaration])),
            AtomicGroupPlacementClass::StructuralConstraint,
        );
        assert_eq!(
            classify_atomic_group_conflict(&conflict([DepKind::LazyRebind])),
            AtomicGroupPlacementClass::StructuralConstraint,
        );
        assert_eq!(
            classify_atomic_group_conflict(&conflict([DepKind::EagerUse, DepKind::CoDeclaration])),
            AtomicGroupPlacementClass::MixedOrUnclassified,
        );
        assert_eq!(
            classify_atomic_group_conflict(&conflict([])),
            AtomicGroupPlacementClass::MixedOrUnclassified,
        );
    }

    #[test]
    fn co_declaration_obligation_keeps_the_full_source_unit_group() {
        let conflict = conflict([DepKind::CoDeclaration]);
        let obligation = AtomicGroupPlacementObligation {
            classification: classify_atomic_group_conflict(&conflict),
            conflict,
        };
        assert_eq!(
            obligation.classification,
            AtomicGroupPlacementClass::StructuralConstraint
        );
        assert_eq!(
            obligation.conflict.members,
            [SourceUnitId(3), SourceUnitId(7)]
        );
        assert!(obligation.conflict.causes.contains(&DepKind::CoDeclaration));
    }

    #[test]
    fn semantic_order_veto_is_not_a_structural_candidate_blocker() {
        let conflict = conflict([DepKind::EagerUse, DepKind::Sequenced]);
        let placement = StructuralPlacementAssessment {
            status: StructuralPlacementStatus::NoKnownBlocker,
            emission_mappable: false,
            coverage: StructuralPlacementCoverage {
                unresolved_checks: [StructuralPlacementCheck::SourceUnitToBodyMapping]
                    .into_iter()
                    .collect(),
            },
            projection_violations: Vec::new(),
            cross_destination_rebinds: Vec::new(),
            atomic_group_obligations: vec![AtomicGroupPlacementObligation {
                classification: classify_atomic_group_conflict(&conflict),
                conflict,
            }],
            semantic_order_sccs: Vec::new(),
        };

        assert!(has_legacy_gate_ordering_veto(&placement));
        assert_eq!(
            candidate_relocation_status(false, false, &placement),
            CandidateRelocationStatus::Incomplete,
            "a semantic-order-only legacy veto is not an effect or structural block",
        );
    }

    #[test]
    fn structural_coverage_completeness_is_derived_from_unresolved_checks() {
        let incomplete = StructuralPlacementCoverage {
            unresolved_checks: [StructuralPlacementCheck::DestructuringDeclaratorGrouping]
                .into_iter()
                .collect(),
        };
        let complete = StructuralPlacementCoverage {
            unresolved_checks: BTreeSet::new(),
        };

        assert!(!incomplete.is_complete());
        assert!(complete.is_complete());
    }

    #[test]
    fn complete_scoped_candidate_can_pass_while_legacy_ordering_veto_remains() {
        let conflict = conflict([DepKind::EagerUse, DepKind::Sequenced]);
        let placement = StructuralPlacementAssessment {
            status: StructuralPlacementStatus::NoKnownBlocker,
            emission_mappable: true,
            coverage: StructuralPlacementCoverage {
                unresolved_checks: BTreeSet::new(),
            },
            projection_violations: Vec::new(),
            cross_destination_rebinds: Vec::new(),
            atomic_group_obligations: vec![AtomicGroupPlacementObligation {
                classification: classify_atomic_group_conflict(&conflict),
                conflict,
            }],
            semantic_order_sccs: Vec::new(),
        };

        assert!(has_legacy_gate_ordering_veto(&placement));
        assert_eq!(
            candidate_relocation_status(false, true, &placement),
            CandidateRelocationStatus::AcceptedWithinCheckedScope,
        );
    }
}
