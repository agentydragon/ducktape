use analysis_model::DepKind;
use source_identity::ChunkId;
use std::collections::{BTreeMap, BTreeSet, HashMap};
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
    /// Planning and final validation share this verdict for the immutable
    /// graph/assignment pair. A rebuilt assignment starts with an empty cache.
    realizability: OnceLock<RealizabilityVerdict>,
}

/// An immutable assignment whose atomic-group, schedule, and rebinding checks
/// passed. The borrow prevents changing the assignment while lowering uses it.
#[derive(Debug)]
pub struct ValidatedAssignment<'a> {
    assignment_analysis: &'a AssignmentAnalysis,
    validation: AssignmentValidation,
}

impl ValidatedAssignment<'_> {
    pub fn assignment_analysis(&self) -> &AssignmentAnalysis {
        self.assignment_analysis
    }

    pub fn report(&self) -> &AssignmentReport {
        &self.validation.report
    }

    pub fn cross_destination_rebinds(&self) -> &[CrossRebindEdge] {
        &self.validation.verdict.cross_rebinds
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

impl RejectedAssignment<'_> {
    pub fn assignment_analysis(&self) -> &AssignmentAnalysis {
        self.assignment_analysis
    }

    pub fn report(&self) -> &AssignmentReport {
        &self.validation.report
    }

    pub fn cross_destination_rebinds(&self) -> &[CrossRebindEdge] {
        &self.validation.verdict.cross_rebinds
    }
}

/// The authoritative assignment gate and both diagnostic projections of the
/// same concrete candidate. Assessment always runs; report selection controls
/// only serialization. Effect coverage remains diagnostic until all required
/// owner domains can discharge the relocation obligations.
#[derive(Debug)]
pub struct CandidateEvaluation<'a> {
    validation: Result<ValidatedAssignment<'a>, RejectedAssignment<'a>>,
    translation_validation: TranslationValidationResult,
    candidate_assessment: CandidateRelocationAssessment,
}

impl<'a> CandidateEvaluation<'a> {
    pub fn assignment_analysis(&self) -> &AssignmentAnalysis {
        match &self.validation {
            Ok(validated) => validated.assignment_analysis(),
            Err(rejected) => rejected.assignment_analysis(),
        }
    }

    pub fn validation(&self) -> &Result<ValidatedAssignment<'a>, RejectedAssignment<'a>> {
        &self.validation
    }

    pub fn is_rejected(&self) -> bool {
        self.validation.is_err()
    }

    pub fn report(&self) -> &AssignmentReport {
        match &self.validation {
            Ok(validated) => validated.report(),
            Err(rejected) => rejected.report(),
        }
    }

    pub fn translation_validation(&self) -> &TranslationValidationResult {
        &self.translation_validation
    }

    pub fn candidate_assessment(&self) -> &CandidateRelocationAssessment {
        &self.candidate_assessment
    }

    pub fn into_validation(self) -> Result<ValidatedAssignment<'a>, RejectedAssignment<'a>> {
        self.validation
    }

    /// Evaluate legacy translation validation and candidate effects/placement
    /// against the same accepted or rejected assignment. The visitors stream
    /// complete evidence without retaining a second copy of its rows.
    pub fn evaluate<E>(
        validation: Result<ValidatedAssignment<'a>, RejectedAssignment<'a>>,
        facts: &[analysis::facts::StatementFacts],
        projection: &analysis::PlacementProjection<'_>,
        translation_validation_visitor: Option<
            &mut impl FnMut(&TranslationValidationEvidenceReport) -> Result<(), E>,
        >,
        effect_visitor: Option<&mut impl FnMut(&EffectConflictDiagnosticRow<'_>) -> Result<(), E>>,
    ) -> Result<Self, E> {
        let assignment_analysis = match &validation {
            Ok(assignment) => assignment.assignment_analysis,
            Err(rejected) => rejected.assignment_analysis,
        };
        let assignment_validation = match &validation {
            Ok(assignment) => &assignment.validation,
            Err(rejected) => rejected.validation.as_ref(),
        };
        let emitted_order = assignment_analysis.candidate_emitted_order();
        let translation_validation = assignment_analysis.translation_validation(
            assignment_validation,
            &emitted_order,
            translation_validation_visitor,
        )?;
        let candidate_assessment = assignment_analysis.candidate_relocation_assessment(
            assignment_validation,
            facts,
            &translation_validation,
            &emitted_order,
            projection,
            effect_visitor,
        )?;
        Ok(Self {
            validation,
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
    /// Reads the same typed outcome that `EmissionPlacement::from_projection`
    /// uses; diagnostic counts do not reconstruct the emitter's decision.
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
    /// The current legacy gate still rejects semantic-order-only SCCs. This
    /// field distinguishes that remaining veto from effect/placement blocks.
    pub legacy_gate_ordering_veto: bool,
    pub placement: StructuralPlacementAssessment,
}

/// This assessment never claims acceptance while the structural coverage
/// declaration is incomplete. The current legacy gate remains authoritative.
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
    pub fn report(&self) -> CandidateRelocationAssessmentReport {
        let effect_blocked = matches!(
            self.effect.assessment.decision,
            crate::realizability::EffectRelocationDecision::Conflict { .. }
        );
        let effect_accepted = matches!(
            self.effect.assessment.decision,
            crate::realizability::EffectRelocationDecision::Accepted { .. }
        );
        CandidateRelocationAssessmentReport {
            status: candidate_relocation_status(effect_blocked, effect_accepted, &self.placement),
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
        (false, false) if !effect_accepted || !placement.coverage.is_complete() => {
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
        // Every output module and the entry destination need a deterministic
        // source-order slot for emit stability, even singletons that don't
        // participate in any canonical edge.
        let constraining_pairs: BTreeSet<(ModuleId, ModuleId)> = canonical_edges.pairs().collect();
        let mut import_order = EsmImportOrder::build(
            &constraining_pairs,
            &canonical_edges.i_successors,
            &emitted_modules(planned_modules.len(), entry_module_id),
        );
        let mut first_units = BTreeMap::new();
        for node in dependency_graph.iter_nodes() {
            first_units
                .entry(assignment.of(node.id))
                .and_modify(|first: &mut source_identity::SourceUnitOrdinal| {
                    *first = (*first).min(node.statement_ordinal);
                })
                .or_insert(node.statement_ordinal);
        }
        import_order.preserve_source_order(
            &canonical_edges.i_successors,
            &first_units,
            entry_module_id,
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

    /// Translation validation of the emitted order beside the gate's verdict
    /// in `report`. The gate stays authoritative. With conflicts read off the
    /// gate's ordering edges the two must agree, so with `DEBUNDLE_TV_ORACLE`
    /// set a disagreement panics: it is a bug in the loader model, the sweep
    /// or the gate.
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

    pub fn validate_for_emission(&self) -> Result<ValidatedAssignment<'_>, RejectedAssignment<'_>> {
        let validation = self.validate_with_verdict();
        if !validation.verdict.is_realizable()
            || !validation.report.atomic_group_conflicts.is_empty()
            || !validation.report.cycles.is_empty()
        {
            return Err(RejectedAssignment {
                assignment_analysis: self,
                validation: Box::new(validation),
            });
        }
        Ok(ValidatedAssignment {
            assignment_analysis: self,
            validation,
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
