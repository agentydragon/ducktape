//! Conservative effect-set assessment beside the authoritative legacy gate.
//!
//! Enumerate every inverted conflicting pair, but retain only the current
//! pair's cells and a bounded deterministic sample. Pair premises
//! never alter cell frontiers or suppress other partners.

use std::cmp::Reverse;
use std::collections::{BTreeMap, BTreeSet, BinaryHeap, btree_map};
use std::ops::Bound::{Excluded, Unbounded};

use analysis::DependencyGraph;
use analysis::analysis_inputs::{CommutingStatementsRef, ProofDependencies};
use analysis::facts::{
    CellOverlap, CellOverlapIndex, InitializationEffectEvidence, InitializationEffects,
    OwnerInitializationCoverageRefusal, OwnerInitializationCoverageRefusalReason, StatementFacts,
    UnknownEffectCoverage,
};
use analysis::graph::{AppliedCommutingPremise, CellOverlapReport, EffectCellReport};
use analysis::ids::ModuleId;
use analysis::module_assignment::ModuleAssignment;
use serde::{Serialize, Serializer, ser::SerializeSeq};
use source_identity::SourceUnitId;
use source_identity::SourceUnitOrdinal;
use spec::ModulePath;

use super::translation_validation::{
    CandidateEmittedOrder, StatementReport, TranslationValidationResult,
};

const EXAMPLE_LIMIT: usize = 20;

#[derive(Debug, Clone, Default, Serialize)]
pub struct EffectConflictCounts {
    pub pairs: u64,
    pub cell_incidences: u64,
    pub havoc_pairs: u64,
    pub uncovered_pairs: u64,
    pub premise_covered_pairs: u64,
    pub premise_contradictions: u64,
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct LegacyEdgeComparisonCounts {
    pub legacy_edge_only_pairs: u64,
    pub effect_only_pairs: u64,
    pub both_pairs: u64,
}

#[derive(Debug, Clone, Serialize)]
pub struct EffectCoverageReport {
    pub statement_count: usize,
    pub covered_owner_dependencies: analysis::analysis_inputs::ProofDependencies,
    pub unknown_coverage: BTreeMap<UnknownEffectCoverage, usize>,
    pub owner_coverage_refusals: BTreeMap<OwnerInitializationCoverageRefusalReason, usize>,
    pub legacy_dataflow_opaque_statements: usize,
    pub incomplete_execution_statements: usize,
    pub precision_loss_statements: usize,
    pub opaque_heap_statements: usize,
    pub havoc_statements: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct EffectTranslationValidationReport {
    pub report_only: bool,
    pub full_semantic_precision: bool,
    pub decision: EffectRelocationDecision,
    pub legacy_gate_accepts: bool,
    pub legacy_tv_accepts: bool,
    pub swapped_pairs: u64,
    /// Exact statement-unit sequence assessed using the candidate's emitter
    /// import order. This is diagnostic provenance, never trusted input.
    pub emitted_order: Vec<SourceUnitId>,
    pub counts: EffectConflictCounts,
    pub legacy_edge_comparison: LegacyEdgeComparisonCounts,
    /// Typed coverage conditions used for the exact reordered participants.
    pub relocation_dependencies: ProofDependencies,
    /// Authored commuting premises that conditionally covered swept pairs.
    pub premise_dependencies: BTreeSet<CommutingStatementsRef>,
    pub coverage: EffectCoverageReport,
    pub examples: Vec<EffectConflictExample>,
    pub example_limit: usize,
    pub cell_example_limit: usize,
    pub global_access_example_limit: usize,
    pub examples_truncated: bool,
    pub full_detail_export_available: bool,
    pub limitations: Vec<&'static str>,
}

/// Effect-set verdict for the exact emitted statement order passed to the
/// assessment. `Conflict` identifies an uncovered modeled may-conflict or a
/// contradiction of an authored premise. `Incomplete` identifies uncovered
/// Havoc or a pair that needs an authored commuting premise. Neither status
/// changes the legacy gate.
#[derive(Debug, Clone, Copy, Serialize)]
#[serde(tag = "status", rename_all = "snake_case")]
pub enum EffectRelocationDecision {
    Accepted {
        scope: EffectRelocationAcceptanceScope,
    },
    Conflict {
        modeled_may_conflict: bool,
        uncovered_modeled_may_conflict: bool,
        havoc: bool,
        uncovered_havoc: bool,
        premise_contradiction: bool,
        premise_covered: bool,
    },
    Incomplete {
        havoc: bool,
        uncovered_havoc: bool,
        premise_covered: bool,
    },
}

/// What the effect assessment establishes about statement-order changes.
#[derive(Debug, Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum EffectRelocationAcceptanceScope {
    /// The candidate's exact emitted statement order has no inversion.
    NoReordering,
    /// Every inverted conflicting pair commutes under complete effect-domain
    /// coverage, or crosses a statement proved to have no evaluation effects.
    StatementOrderCommutation,
}

#[derive(Debug, Clone, Serialize)]
pub struct EffectConflictExample {
    pub runs_first: StatementReport,
    pub runs_later: StatementReport,
    pub cells: Vec<EffectConflictCell>,
    pub cell_count: usize,
    pub cells_truncated: bool,
    pub havoc: bool,
    pub disposition: EffectConflictDisposition,
    pub premise_locations: Vec<String>,
    pub premise_references: Vec<CommutingStatementsRef>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub first_owner_coverage_refusal: Option<OwnerInitializationCoverageRefusal>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub later_owner_coverage_refusal: Option<OwnerInitializationCoverageRefusal>,
}

#[derive(Debug, Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum EffectConflictDisposition {
    Uncovered,
    CoveredByExistingPremise,
    ContradictsExistingPremise,
}

#[derive(Debug, Clone, Serialize)]
pub struct EffectConflictCell<Evidence = InitializationEffectEvidence> {
    /// The later statement's footprint, kept as the stable primary cell.
    pub cell: EffectCellReport,
    /// The earlier and later footprints and the law that joined them.
    pub overlap: CellOverlapReport,
    pub first_read: Option<Evidence>,
    pub first_write: Option<Evidence>,
    pub later_read: Option<Evidence>,
    pub later_write: Option<Evidence>,
    pub allocation_involved: bool,
    pub observed_conflict: bool,
}

impl<Evidence> EffectConflictCell<Evidence> {
    fn map_evidence<Next>(
        self,
        mut project: impl FnMut(Evidence) -> Next,
    ) -> EffectConflictCell<Next> {
        EffectConflictCell {
            cell: self.cell,
            overlap: self.overlap,
            first_read: self.first_read.map(&mut project),
            first_write: self.first_write.map(&mut project),
            later_read: self.later_read.map(&mut project),
            later_write: self.later_write.map(&mut project),
            allocation_involved: self.allocation_involved,
            observed_conflict: self.observed_conflict,
        }
    }
}

/// Borrowed full-detail view of one conflict. Cells serialize one at a time;
/// the sweep never constructs a full-detail row or global pair vector.
#[derive(Serialize)]
pub struct EffectConflictRow<'a> {
    pub runs_first: StatementReport,
    pub runs_later: StatementReport,
    cells: EffectConflictCells<'a>,
    pub cell_count: usize,
    pub havoc: bool,
    pub disposition: EffectConflictDisposition,
    pub premise_locations: Vec<String>,
    pub premise_references: Vec<CommutingStatementsRef>,
    pub first_opacity: EffectOpacity<'a>,
    pub later_opacity: EffectOpacity<'a>,
}

/// Typed pair evidence emitted by the semantic sweep. It keeps source-unit
/// identity, candidate module placement, full cell evidence and premise
/// dependencies without constructing a presentation row for every pair.
pub struct EffectRelocationConflict<'a> {
    pub first_unit: SourceUnitId,
    pub later_unit: SourceUnitId,
    pub first_ordinal: SourceUnitOrdinal,
    pub later_ordinal: SourceUnitOrdinal,
    pub first_module: ModuleId,
    pub later_module: ModuleId,
    cells: EffectConflictCells<'a>,
    pub cell_count: usize,
    pub havoc: bool,
    pub disposition: EffectConflictDisposition,
    premises: Option<&'a Vec<&'a AppliedCommutingPremise>>,
    pub first_opacity: EffectOpacity<'a>,
    pub later_opacity: EffectOpacity<'a>,
}

impl EffectRelocationConflict<'_> {
    pub fn cells(
        &self,
    ) -> impl Iterator<Item = EffectConflictCell<&InitializationEffectEvidence>> + '_ {
        self.cells.iter()
    }

    pub fn premise_references(&self) -> impl Iterator<Item = &CommutingStatementsRef> {
        self.premises
            .into_iter()
            .flatten()
            .map(|premise| &premise.reference)
    }

    fn report_row(&self, module_path: &dyn Fn(ModuleId) -> ModulePath) -> EffectConflictRow<'_> {
        EffectConflictRow {
            runs_first: StatementReport {
                unit: self.first_ordinal,
                module: module_path(self.first_module),
            },
            runs_later: StatementReport {
                unit: self.later_ordinal,
                module: module_path(self.later_module),
            },
            cells: EffectConflictCells {
                cells: self.cells.cells,
                first: self.cells.first,
                later: self.cells.later,
            },
            cell_count: self.cell_count,
            havoc: self.havoc,
            disposition: self.disposition,
            premise_locations: self
                .premises
                .into_iter()
                .flatten()
                .map(|premise| premise.spec_location.clone())
                .collect(),
            premise_references: self
                .premises
                .into_iter()
                .flatten()
                .map(|premise| premise.reference.clone())
                .collect(),
            first_opacity: EffectOpacity {
                unknown_coverage: self.first_opacity.unknown_coverage,
                dataflow_opacity: self.first_opacity.dataflow_opacity,
                incomplete_execution: self.first_opacity.incomplete_execution,
                execution_losses: self.first_opacity.execution_losses,
                opaque_heap: self.first_opacity.opaque_heap,
                owner_coverage_refusal: self.first_opacity.owner_coverage_refusal,
            },
            later_opacity: EffectOpacity {
                unknown_coverage: self.later_opacity.unknown_coverage,
                dataflow_opacity: self.later_opacity.dataflow_opacity,
                incomplete_execution: self.later_opacity.incomplete_execution,
                execution_losses: self.later_opacity.execution_losses,
                opaque_heap: self.later_opacity.opaque_heap,
                owner_coverage_refusal: self.later_opacity.owner_coverage_refusal,
            },
        }
    }
}

/// Diagnostic JSONL projection that joins the semantic row with the separate
/// edge-TV comparison. The assessment itself never receives legacy facts.
#[derive(Serialize)]
pub struct EffectConflictDiagnosticRow<'a> {
    #[serde(flatten)]
    pub conflict: &'a EffectConflictRow<'a>,
    pub legacy_edge_conflict: bool,
}

#[derive(Serialize)]
pub struct EffectOpacity<'a> {
    pub unknown_coverage: &'a BTreeSet<UnknownEffectCoverage>,
    pub dataflow_opacity: &'a BTreeSet<analysis::DataflowOpacityCause>,
    pub incomplete_execution: bool,
    pub execution_losses: &'a [analysis::execution_evidence::ExecutionPrecisionLoss],
    pub opaque_heap: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub owner_coverage_refusal: Option<&'a OwnerInitializationCoverageRefusal>,
}

impl<'a> From<&'a InitializationEffects<'_>> for EffectOpacity<'a> {
    fn from(effects: &'a InitializationEffects<'_>) -> Self {
        Self {
            unknown_coverage: &effects.unknown_coverage,
            dataflow_opacity: &effects.dataflow_opacity,
            incomplete_execution: effects.incomplete_execution,
            execution_losses: effects.execution_losses,
            opaque_heap: effects.opaque_heap,
            owner_coverage_refusal: effects.owner_coverage_refusal.as_ref(),
        }
    }
}

struct EffectConflictCells<'a> {
    cells: &'a BTreeMap<CellOverlap, bool>,
    first: &'a InitializationEffects<'a>,
    later: &'a InitializationEffects<'a>,
}

impl EffectConflictCells<'_> {
    fn iter(&self) -> impl Iterator<Item = EffectConflictCell<&InitializationEffectEvidence>> + '_ {
        self.cells
            .iter()
            .map(|(overlap, observed)| EffectConflictCell {
                cell: EffectCellReport::from(overlap.right()),
                overlap: CellOverlapReport::from(overlap),
                first_read: self.first.reads.get(overlap.left()),
                first_write: self.first.writes.get(overlap.left()),
                later_read: self.later.reads.get(overlap.right()),
                later_write: self.later.writes.get(overlap.right()),
                allocation_involved: self.first.allocates.contains(overlap.left())
                    || self.later.allocates.contains(overlap.right()),
                observed_conflict: *observed,
            })
    }
}

impl EffectConflictRow<'_> {
    pub fn cells(
        &self,
    ) -> impl Iterator<Item = EffectConflictCell<&InitializationEffectEvidence>> + '_ {
        self.cells.iter()
    }

    fn bounded_example(&self) -> EffectConflictExample {
        EffectConflictExample {
            runs_first: self.runs_first.clone(),
            runs_later: self.runs_later.clone(),
            cells: self
                .cells()
                .take(EXAMPLE_LIMIT)
                .map(|cell| cell.map_evidence(|evidence| evidence.sampled(EXAMPLE_LIMIT)))
                .collect(),
            cell_count: self.cell_count,
            cells_truncated: self.cell_count > EXAMPLE_LIMIT,
            havoc: self.havoc,
            disposition: self.disposition,
            premise_locations: self.premise_locations.clone(),
            premise_references: self.premise_references.clone(),
            first_owner_coverage_refusal: self.first_opacity.owner_coverage_refusal.cloned(),
            later_owner_coverage_refusal: self.later_opacity.owner_coverage_refusal.cloned(),
        }
    }
}

fn should_capture_example(examples: &[EffectConflictExample], has_modeled_cells: bool) -> bool {
    examples.len() < EXAMPLE_LIMIT
        || (has_modeled_cells && examples.iter().any(|example| example.cell_count == 0))
}

fn retain_example(examples: &mut Vec<EffectConflictExample>, example: EffectConflictExample) {
    if examples.len() < EXAMPLE_LIMIT {
        examples.push(example);
    } else if example.cell_count > 0
        && let Some(last_havoc_only) = examples.iter().rposition(|sample| sample.cell_count == 0)
    {
        examples.remove(last_havoc_only);
        examples.push(example);
    }
}

impl Serialize for EffectConflictCells<'_> {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut sequence = serializer.serialize_seq(Some(self.cells.len()))?;
        for cell in self.iter() {
            sequence.serialize_element(&cell)?;
        }
        sequence.end()
    }
}

type CellParticipants = BTreeMap<SourceUnitOrdinal, SourceUnitId>;

#[derive(Default)]
struct CellParticipantIndexes {
    readers: CellOverlapIndex<CellParticipants>,
    writers: CellOverlapIndex<CellParticipants>,
}

#[derive(Default)]
struct PairConflict {
    cells: BTreeMap<CellOverlap, bool>,
    havoc: bool,
}

/// One settled assignment and the runtime facts used to diagnose its effects.
pub(crate) struct EffectTranslationValidationInputs<'a> {
    pub graph: &'a DependencyGraph,
    pub assignment: &'a ModuleAssignment,
    pub emitted_order: &'a CandidateEmittedOrder,
    pub facts: &'a [StatementFacts],
    pub legacy: &'a TranslationValidationResult,
    pub module_path: &'a dyn Fn(ModuleId) -> ModulePath,
}

/// Trusted in-memory inputs for one candidate-order effect assessment.
/// Reports decoded from disk are deliberately not accepted here.
struct EffectRelocationAssessmentInputs<'a> {
    pub graph: &'a DependencyGraph,
    pub assignment: &'a ModuleAssignment,
    pub emitted_order: &'a CandidateEmittedOrder,
    pub facts: &'a [StatementFacts],
}

/// One sweep result. `emitted_order` binds the verdict to the candidate
/// statement order; this ephemeral value is not a persisted proof artifact.
#[derive(Debug, Clone, Serialize)]
pub struct EffectRelocationAssessment {
    pub emitted_order: Vec<SourceUnitId>,
    pub decision: EffectRelocationDecision,
    pub swapped_pairs: u64,
    /// Typed conditions used for the exact inverted statement participants.
    pub relocation_dependencies: ProofDependencies,
    /// Authored premises actually used to cover conflicting inverted pairs.
    pub premise_dependencies: BTreeSet<CommutingStatementsRef>,
    pub counts: EffectConflictCounts,
    pub coverage: EffectCoverageReport,
}

/// One effect sweep's typed assessment and rendered report. The caller can
/// pair the typed assessment with structural placement obligations without
/// repeating the sweep.
#[derive(Debug, Clone)]
pub struct EffectTranslationValidationResult {
    pub assessment: EffectRelocationAssessment,
    pub report: EffectTranslationValidationReport,
}

/// Run the candidate-order effect sweep once and return both its typed result
/// and the existing report projection. This does not alter the legacy gate or
/// its oracle. An active visitor still receives every conflict in sweep order;
/// only the bounded `examples` selection changes.
pub(crate) fn effect_translation_assess<E>(
    inputs: EffectTranslationValidationInputs<'_>,
    mut visitor: Option<&mut impl FnMut(&EffectConflictDiagnosticRow<'_>) -> Result<(), E>>,
) -> Result<EffectTranslationValidationResult, E> {
    let EffectTranslationValidationInputs {
        graph,
        assignment,
        emitted_order,
        facts,
        legacy,
        module_path,
    } = inputs;
    let mut legacy_pairs = legacy.conflict_pairs.clone();
    let mut legacy_comparison = LegacyEdgeComparisonCounts::default();
    let mut examples = Vec::new();
    let mut diagnostic_visitor = |conflict: &EffectRelocationConflict<'_>| -> Result<(), E> {
        let legacy_edge_conflict = legacy_pairs.remove(&(conflict.first_unit, conflict.later_unit));
        if legacy_edge_conflict {
            legacy_comparison.both_pairs += 1;
        } else {
            legacy_comparison.effect_only_pairs += 1;
        }
        let capture_example = should_capture_example(&examples, conflict.cell_count > 0);
        if capture_example || visitor.is_some() {
            let row = conflict.report_row(module_path);
            if capture_example {
                retain_example(&mut examples, row.bounded_example());
            }
            if let Some(visitor) = &mut visitor {
                visitor(&EffectConflictDiagnosticRow {
                    conflict: &row,
                    legacy_edge_conflict,
                })?;
            }
        }
        Ok(())
    };
    let assessment = assess_effect_relocation(
        EffectRelocationAssessmentInputs {
            graph,
            assignment,
            emitted_order,
            facts,
        },
        Some(&mut diagnostic_visitor),
    )?;
    legacy_comparison.legacy_edge_only_pairs = legacy_pairs.len() as u64;
    let examples_truncated = assessment.counts.pairs > examples.len() as u64;
    let report = EffectTranslationValidationReport {
        report_only: true,
        // Closed domains still contain may-effects and do not establish every
        // runtime property such as heap identity or conditional execution.
        full_semantic_precision: false,
        decision: assessment.decision,
        legacy_gate_accepts: legacy.report.gate_accepts,
        legacy_tv_accepts: legacy.report.accepts,
        swapped_pairs: assessment.swapped_pairs,
        emitted_order: assessment.emitted_order.clone(),
        counts: assessment.counts.clone(),
        legacy_edge_comparison: legacy_comparison,
        relocation_dependencies: assessment.relocation_dependencies.clone(),
        premise_dependencies: assessment.premise_dependencies.clone(),
        coverage: assessment.coverage.clone(),
        examples,
        example_limit: EXAMPLE_LIMIT,
        cell_example_limit: EXAMPLE_LIMIT,
        global_access_example_limit: EXAMPLE_LIMIT,
        examples_truncated,
        full_detail_export_available: true,
        limitations: vec![
            "Unknown global/property/host coverage remains global Havoc, independently of purity or execution completeness.",
            "Composed heap accesses lack per-access provenance; allocation sites do not prove heap locality.",
            "Counts cover every conflicting swapped pair; bounded examples prefer modeled-cell rows, then retain the earliest Havoc-only rows. Request --effect-conflicts-jsonl for full rows.",
            "The edge comparison excludes forward-edge and placement diagnostics; this assessment does not change acceptance or prove emission placement/rebinding obligations.",
        ],
    };
    Ok(EffectTranslationValidationResult { assessment, report })
}

/// Sweep every conflicting inversion in the exact emitter order. The decision
/// is computed while those full rows are streamed, never from report samples or
/// aggregate counts.
fn assess_effect_relocation<E>(
    inputs: EffectRelocationAssessmentInputs<'_>,
    mut visitor: Option<&mut impl FnMut(&EffectRelocationConflict<'_>) -> Result<(), E>>,
) -> Result<EffectRelocationAssessment, E> {
    let EffectRelocationAssessmentInputs {
        graph,
        assignment,
        emitted_order,
        facts,
    } = inputs;
    let effects: BTreeMap<_, _> = facts
        .iter()
        .map(|fact| {
            let effects = fact.initialization_effects();
            (SourceUnitId(effects.ordinal.0), effects)
        })
        .collect();
    // Missing facts are an integration bug, never an empty effect summary.
    assert_eq!(effects.len(), facts.len(), "duplicate effect-fact ordinals");
    assert_eq!(
        effects.keys().copied().collect::<BTreeSet<_>>(),
        graph
            .iter_nodes()
            .map(|node| node.id)
            .collect::<BTreeSet<_>>(),
        "effect facts and assignment graph must name exactly the same source units",
    );
    let mut coverage = EffectCoverageReport {
        statement_count: effects.len(),
        covered_owner_dependencies: Default::default(),
        unknown_coverage: BTreeMap::new(),
        owner_coverage_refusals: BTreeMap::new(),
        legacy_dataflow_opaque_statements: 0,
        incomplete_execution_statements: 0,
        precision_loss_statements: 0,
        opaque_heap_statements: 0,
        havoc_statements: 0,
    };
    for effect in effects.values() {
        if let Some(dependencies) = effect.coverage_dependencies {
            coverage.covered_owner_dependencies.extend(dependencies);
        }
        for domain in &effect.unknown_coverage {
            *coverage.unknown_coverage.entry(*domain).or_default() += 1;
        }
        if let Some(refusal) = &effect.owner_coverage_refusal {
            *coverage
                .owner_coverage_refusals
                .entry(refusal.reason)
                .or_default() += 1;
        }
        coverage.legacy_dataflow_opaque_statements +=
            usize::from(!effect.dataflow_opacity.is_empty());
        coverage.incomplete_execution_statements += usize::from(effect.incomplete_execution);
        coverage.precision_loss_statements += usize::from(!effect.execution_losses.is_empty());
        coverage.opaque_heap_statements += usize::from(effect.opaque_heap);
        coverage.havoc_statements += usize::from(effect.has_havoc());
    }
    let mut counts = EffectConflictCounts::default();
    let emitted = emitted_order.rows();
    let has_reordering = emitted.windows(2).any(|pair| pair[0].1 > pair[1].1);
    let swapped_pairs = super::translation_validation::count_inversions(
        emitted.iter().map(|&(_, ordinal, _)| ordinal),
    );
    let swapped_units = units_in_inversions(emitted);
    let mut relocation_dependencies = ProofDependencies::default();
    for unit in &swapped_units {
        let effect = &effects[unit];
        if effect_domains_closed(effect)
            && let Some(dependencies) = effect.coverage_dependencies
        {
            relocation_dependencies.extend(dependencies);
        }
    }
    let mut has_modeled_may_conflict = false;
    let mut has_uncovered_modeled_may_conflict = false;
    let mut has_premise_contradiction = false;
    let mut has_havoc_conflict = false;
    let mut has_uncovered_havoc = false;
    let mut has_premise_covered = false;
    let mut premise_dependencies = BTreeSet::new();
    let mut premises: BTreeMap<_, Vec<_>> = BTreeMap::new();
    for premise in graph.commuting_premises() {
        premises
            .entry((premise.a.min(premise.b), premise.a.max(premise.b)))
            .or_default()
            .push(premise);
    }
    let mut cells = CellParticipantIndexes::default();
    let mut nonempty: BTreeMap<SourceUnitOrdinal, SourceUnitId> = BTreeMap::new();
    let mut havoc: BTreeMap<SourceUnitOrdinal, SourceUnitId> = BTreeMap::new();
    for (_, ordinal, unit) in emitted.iter().copied() {
        let current = &effects[&unit];
        // Merge ordered suffix iterators by partner. This retains only one
        // pair's cells, even if one statement conflicts with many partners on
        // many cells. The merge is output-sensitive, including Havoc pairs.
        let mut ranges = Vec::new();
        let mut pending = BinaryHeap::new();
        for cell in current
            .reads
            .keys()
            .chain(current.writes.keys())
            .chain(&current.allocates)
            .collect::<BTreeSet<_>>()
        {
            for (overlap, participants) in cells.writers.overlapping(cell) {
                push_range(
                    &mut ranges,
                    &mut pending,
                    Some(overlap),
                    participants.range((Excluded(ordinal), Unbounded)),
                );
            }
            if current.writes.contains_key(cell) || current.allocates.contains(cell) {
                for (overlap, participants) in cells.readers.overlapping(cell) {
                    push_range(
                        &mut ranges,
                        &mut pending,
                        Some(overlap),
                        participants.range((Excluded(ordinal), Unbounded)),
                    );
                }
            }
        }
        if !current.is_empty() {
            let wildcard_partners = if current.has_havoc() {
                &nonempty
            } else {
                &havoc
            };
            push_range(
                &mut ranges,
                &mut pending,
                None,
                wildcard_partners.range((Excluded(ordinal), Unbounded)),
            );
        }
        let mut emit_pair = |partner: SourceUnitId, conflict: PairConflict| -> Result<(), E> {
            let pair_premises = premises.get(&(partner.min(unit), partner.max(unit)));
            let known = conflict.cells.values().any(|known| *known);
            has_modeled_may_conflict |= !conflict.cells.is_empty();
            has_havoc_conflict |= conflict.havoc;
            if let Some(pair_premises) = pair_premises {
                premise_dependencies.extend(
                    pair_premises
                        .iter()
                        .map(|premise| premise.reference.clone()),
                );
            }
            let disposition = if pair_premises.is_some() && known {
                counts.premise_contradictions += 1;
                has_premise_contradiction = true;
                EffectConflictDisposition::ContradictsExistingPremise
            } else if pair_premises.is_some() {
                counts.premise_covered_pairs += 1;
                has_premise_covered = true;
                EffectConflictDisposition::CoveredByExistingPremise
            } else {
                counts.uncovered_pairs += 1;
                if conflict.havoc {
                    has_uncovered_havoc = true;
                }
                if !conflict.cells.is_empty() {
                    has_modeled_may_conflict = true;
                    has_uncovered_modeled_may_conflict = true;
                }
                EffectConflictDisposition::Uncovered
            };
            counts.pairs += 1;
            counts.cell_incidences += conflict.cells.len() as u64;
            counts.havoc_pairs += u64::from(conflict.havoc);
            // The semantic event retains source identities and evidence.
            // Presentation paths and sampled evidence are added by the report
            // adapter only for bounded examples or an active full sink.
            if visitor.is_some() {
                let first = &effects[&partner];
                let first_node = graph
                    .node(partner)
                    .expect("projected facts name graph nodes");
                let later_node = graph.node(unit).expect("projected facts name graph nodes");
                let conflict_row = EffectRelocationConflict {
                    first_unit: partner,
                    later_unit: unit,
                    first_ordinal: first_node.statement_ordinal,
                    later_ordinal: later_node.statement_ordinal,
                    first_module: assignment.of(partner),
                    later_module: assignment.of(unit),
                    cells: EffectConflictCells {
                        cells: &conflict.cells,
                        first,
                        later: current,
                    },
                    cell_count: conflict.cells.len(),
                    havoc: conflict.havoc,
                    disposition,
                    premises: pair_premises,
                    first_opacity: first.into(),
                    later_opacity: current.into(),
                };
                if let Some(visitor) = &mut visitor {
                    visitor(&conflict_row)?;
                }
            }
            Ok(())
        };
        let mut active: Option<(SourceUnitId, PairConflict)> = None;
        while let Some(Reverse((_, partner, cursor))) = pending.pop() {
            if active
                .as_ref()
                .is_some_and(|(previous, _)| *previous != partner)
            {
                let (previous, conflict) = active.take().expect("an active pair");
                emit_pair(previous, conflict)?;
            }
            let (_, conflict) = active.get_or_insert_with(|| (partner, PairConflict::default()));
            if let Some(overlap) = &ranges[cursor].0 {
                let observed = observed_conflict(&effects[&partner], current, overlap);
                conflict
                    .cells
                    .entry(overlap.clone())
                    .and_modify(|known| *known |= observed)
                    .or_insert(observed);
            } else {
                conflict.havoc = true;
            }
            if let Some((&next_ordinal, &next_partner)) = ranges[cursor].1.next() {
                pending.push(Reverse((next_ordinal, next_partner, cursor)));
            }
        }
        if let Some((partner, conflict)) = active {
            emit_pair(partner, conflict)?;
        }
        drop(ranges);
        // All queries precede insertion, so a statement never conflicts with
        // itself. No pair-premise decision mutates these participants.
        for cell in current.reads.keys() {
            cells
                .readers
                .get_or_default(cell.clone())
                .insert(ordinal, unit);
        }
        for cell in current.writes.keys().chain(&current.allocates) {
            cells
                .writers
                .get_or_default(cell.clone())
                .insert(ordinal, unit);
        }
        if !current.is_empty() {
            nonempty.insert(ordinal, unit);
        }
        if current.has_havoc() {
            havoc.insert(ordinal, unit);
        }
    }
    let decision = if !has_reordering {
        debug_assert_eq!(counts.pairs, 0, "a non-inverted order has no conflicts");
        EffectRelocationDecision::Accepted {
            scope: EffectRelocationAcceptanceScope::NoReordering,
        }
    } else if has_uncovered_modeled_may_conflict || has_premise_contradiction {
        EffectRelocationDecision::Conflict {
            modeled_may_conflict: has_modeled_may_conflict,
            uncovered_modeled_may_conflict: has_uncovered_modeled_may_conflict,
            havoc: has_havoc_conflict,
            uncovered_havoc: has_uncovered_havoc,
            premise_contradiction: has_premise_contradiction,
            premise_covered: has_premise_covered,
        }
    } else if has_uncovered_havoc || has_premise_covered {
        EffectRelocationDecision::Incomplete {
            havoc: has_havoc_conflict,
            uncovered_havoc: has_uncovered_havoc,
            premise_covered: has_premise_covered,
        }
    } else {
        // Every open effect set participates as Havoc with every nonempty
        // crossing partner. If the complete sweep found no such pair, an
        // open statement crossed only statements whose complete effects are
        // empty; no assumption about the open side is needed for that pair.
        EffectRelocationDecision::Accepted {
            scope: EffectRelocationAcceptanceScope::StatementOrderCommutation,
        }
    };
    Ok(EffectRelocationAssessment {
        emitted_order: emitted.iter().map(|&(_, _, unit)| unit).collect(),
        decision,
        swapped_pairs,
        relocation_dependencies,
        premise_dependencies,
        counts,
        coverage,
    })
}

fn effect_domains_closed(effects: &InitializationEffects<'_>) -> bool {
    !effects.has_havoc() && effects.owner_coverage_refusal.is_none()
}

fn units_in_inversions(
    emitted: &[(usize, SourceUnitOrdinal, SourceUnitId)],
) -> BTreeSet<SourceUnitId> {
    let mut involved = BTreeSet::new();
    let mut prefix_max: Option<SourceUnitOrdinal> = None;
    for &(_, ordinal, unit) in emitted {
        if prefix_max.is_some_and(|max| max > ordinal) {
            involved.insert(unit);
        }
        prefix_max = Some(prefix_max.map_or(ordinal, |max| max.max(ordinal)));
    }
    let mut suffix_min: Option<SourceUnitOrdinal> = None;
    for &(_, ordinal, unit) in emitted.iter().rev() {
        if suffix_min.is_some_and(|min| min < ordinal) {
            involved.insert(unit);
        }
        suffix_min = Some(suffix_min.map_or(ordinal, |min| min.min(ordinal)));
    }
    involved
}

fn observed_conflict(
    a: &InitializationEffects<'_>,
    b: &InitializationEffects<'_>,
    overlap: &CellOverlap,
) -> bool {
    let observed = |value: Option<&InitializationEffectEvidence>| {
        value.is_some_and(InitializationEffectEvidence::is_observed)
    };
    (observed(a.writes.get(overlap.left()))
        && (observed(b.reads.get(overlap.right())) || observed(b.writes.get(overlap.right()))))
        || (observed(b.writes.get(overlap.right())) && observed(a.reads.get(overlap.left())))
}

type ParticipantRange<'a> = btree_map::Range<'a, SourceUnitOrdinal, SourceUnitId>;
type PendingParticipant = Reverse<(SourceUnitOrdinal, SourceUnitId, usize)>;

fn push_range<'participants>(
    ranges: &mut Vec<(Option<CellOverlap>, ParticipantRange<'participants>)>,
    pending: &mut BinaryHeap<PendingParticipant>,
    overlap: Option<CellOverlap>,
    mut range: ParticipantRange<'participants>,
) {
    if let Some((&ordinal, &partner)) = range.next() {
        let cursor = ranges.len();
        ranges.push((overlap, range));
        pending.push(Reverse((ordinal, partner, cursor)));
    }
}
