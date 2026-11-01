//! Conservative effect-set diagnostics beside the authoritative legacy gate.
//!
//! Enumerate every inverted conflicting pair, but retain only the current
//! pair's cells and a bounded deterministic sample. Pair premises
//! never alter cell frontiers or suppress other partners.

use std::cmp::Reverse;
use std::collections::{BTreeMap, BTreeSet, BinaryHeap, btree_map};
use std::ops::Bound::{Excluded, Unbounded};

use analysis::facts::{
    InitializationEffectEvidence, InitializationEffects, StatementFacts, UnknownEffectCoverage,
};
use analysis::graph::EffectCellReport;
use analysis::ids::ModuleId;
use analysis::module_assignment::ModuleAssignment;
use analysis::{DependencyGraph, EffectCell, SourceUnitId};
use serde::Serialize;
use source_identity::SourceUnitOrdinal;
use spec::ModulePath;

use super::translation_validation::{
    StatementReport, TranslationValidationReport, emitted_statement_order,
};
use crate::esm_import_order::EsmImportOrder;

const EXAMPLE_LIMIT: usize = 20;

#[derive(Debug, Clone, Default, Serialize)]
pub struct EffectConflictCounts {
    pub pairs: u64,
    pub cell_incidences: u64,
    pub havoc_pairs: u64,
    pub uncovered_pairs: u64,
    pub premise_covered_pairs: u64,
    pub premise_contradictions: u64,
    pub legacy_edge_only_pairs: u64,
    pub effect_only_pairs: u64,
    pub both_pairs: u64,
}

#[derive(Debug, Clone, Serialize)]
pub struct EffectCoverageReport {
    pub statement_count: usize,
    pub unknown_coverage: BTreeMap<UnknownEffectCoverage, usize>,
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
    pub legacy_gate_accepts: bool,
    pub legacy_tv_accepts: bool,
    pub swapped_pairs: u64,
    pub counts: EffectConflictCounts,
    pub coverage: EffectCoverageReport,
    pub examples: Vec<EffectConflictExample>,
    pub example_limit: usize,
    pub cell_example_limit: usize,
    pub examples_truncated: bool,
    pub full_detail_export_available: bool,
    pub limitations: Vec<&'static str>,
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
}

#[derive(Debug, Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum EffectConflictDisposition {
    Uncovered,
    CoveredByExistingPremise,
    ContradictsExistingPremise,
}

#[derive(Debug, Clone, Serialize)]
pub struct EffectConflictCell {
    pub cell: EffectCellReport,
    pub first_read: Option<InitializationEffectEvidence>,
    pub first_write: Option<InitializationEffectEvidence>,
    pub later_read: Option<InitializationEffectEvidence>,
    pub later_write: Option<InitializationEffectEvidence>,
    pub allocation_involved: bool,
    pub observed_conflict: bool,
}

#[derive(Default)]
struct CellParticipants {
    readers: BTreeMap<SourceUnitOrdinal, SourceUnitId>,
    writers: BTreeMap<SourceUnitOrdinal, SourceUnitId>,
}

#[derive(Default)]
struct PairConflict {
    cells: BTreeMap<EffectCell, bool>,
    havoc: bool,
}

/// Only reports; neither the legacy gate nor its oracle consumes this result.
pub fn effect_translation_validate(
    graph: &DependencyGraph,
    assignment: &ModuleAssignment,
    emitted_modules: &BTreeSet<ModuleId>,
    import_order: &EsmImportOrder,
    facts: &[StatementFacts],
    legacy: &TranslationValidationReport,
    module_path: &dyn Fn(ModuleId) -> ModulePath,
) -> EffectTranslationValidationReport {
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
        unknown_coverage: BTreeMap::new(),
        legacy_dataflow_opaque_statements: 0,
        incomplete_execution_statements: 0,
        precision_loss_statements: 0,
        opaque_heap_statements: 0,
        havoc_statements: 0,
    };
    for effect in effects.values() {
        for domain in &effect.unknown_coverage {
            *coverage.unknown_coverage.entry(*domain).or_default() += 1;
        }
        coverage.legacy_dataflow_opaque_statements +=
            usize::from(!effect.dataflow_opacity.is_empty());
        coverage.incomplete_execution_statements += usize::from(effect.incomplete_execution);
        coverage.precision_loss_statements += usize::from(!effect.execution_losses.is_empty());
        coverage.opaque_heap_statements += usize::from(effect.opaque_heap);
        coverage.havoc_statements += usize::from(effect.has_havoc());
    }
    let mut report = EffectTranslationValidationReport {
        report_only: true,
        full_semantic_precision: false,
        legacy_gate_accepts: legacy.gate_accepts,
        legacy_tv_accepts: legacy.accepts,
        swapped_pairs: legacy.swapped_pairs,
        counts: EffectConflictCounts::default(),
        coverage,
        examples: Vec::new(),
        example_limit: EXAMPLE_LIMIT,
        cell_example_limit: EXAMPLE_LIMIT,
        examples_truncated: false,
        full_detail_export_available: false,
        limitations: vec![
            "Unknown global/property/host coverage remains global Havoc, independently of purity or execution completeness.",
            "Composed heap accesses lack per-access provenance; allocation sites do not prove heap locality.",
            "Counts cover every conflicting swapped pair; examples are bounded and deterministic. Full-detail CLI export is not implemented.",
            "Legacy edge comparison excludes its separate forward-edge and placement diagnostics; this report never changes acceptance.",
        ],
    };
    let mut legacy_pairs: BTreeSet<_> = legacy
        .conflicts
        .iter()
        .map(|conflict| {
            (
                SourceUnitId(conflict.runs_first.unit.0),
                SourceUnitId(conflict.runs_later.unit.0),
            )
        })
        .collect();
    let mut premises: BTreeMap<_, Vec<_>> = BTreeMap::new();
    for premise in graph.commuting_premises() {
        premises
            .entry((premise.a.min(premise.b), premise.a.max(premise.b)))
            .or_default()
            .push(premise);
    }
    let mut cells: BTreeMap<EffectCell, CellParticipants> = BTreeMap::new();
    let mut nonempty: BTreeMap<SourceUnitOrdinal, SourceUnitId> = BTreeMap::new();
    let mut havoc: BTreeMap<SourceUnitOrdinal, SourceUnitId> = BTreeMap::new();
    for (_, ordinal, unit) in
        emitted_statement_order(graph, assignment, emitted_modules, import_order)
    {
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
            let Some(participants) = cells.get(cell) else {
                continue;
            };
            push_range(
                &mut ranges,
                &mut pending,
                Some(cell),
                participants.writers.range((Excluded(ordinal), Unbounded)),
            );
            if current.writes.contains_key(cell) || current.allocates.contains(cell) {
                push_range(
                    &mut ranges,
                    &mut pending,
                    Some(cell),
                    participants.readers.range((Excluded(ordinal), Unbounded)),
                );
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
        let mut emit_pair = |partner: SourceUnitId, conflict: PairConflict| {
            let pair_premises = premises.get(&(partner.min(unit), partner.max(unit)));
            let known = conflict.cells.values().any(|known| *known);
            let disposition = if pair_premises.is_some() && known {
                report.counts.premise_contradictions += 1;
                EffectConflictDisposition::ContradictsExistingPremise
            } else if pair_premises.is_some() {
                report.counts.premise_covered_pairs += 1;
                EffectConflictDisposition::CoveredByExistingPremise
            } else {
                report.counts.uncovered_pairs += 1;
                EffectConflictDisposition::Uncovered
            };
            report.counts.pairs += 1;
            report.counts.cell_incidences += conflict.cells.len() as u64;
            report.counts.havoc_pairs += u64::from(conflict.havoc);
            if legacy_pairs.remove(&(partner, unit)) {
                report.counts.both_pairs += 1;
            } else {
                report.counts.effect_only_pairs += 1;
            }
            if report.examples.len() < EXAMPLE_LIMIT {
                let statement = |id| StatementReport {
                    unit: graph
                        .node(id)
                        .expect("projected facts name graph nodes")
                        .statement_ordinal,
                    module: module_path(assignment.of(id)),
                };
                let first = &effects[&partner];
                report.examples.push(EffectConflictExample {
                    runs_first: statement(partner),
                    runs_later: statement(unit),
                    havoc: conflict.havoc,
                    disposition,
                    premise_locations: pair_premises
                        .map(|items| {
                            items
                                .iter()
                                .map(|item| item.spec_location.clone())
                                .collect()
                        })
                        .unwrap_or_default(),
                    cell_count: conflict.cells.len(),
                    cells_truncated: conflict.cells.len() > EXAMPLE_LIMIT,
                    cells: conflict
                        .cells
                        .iter()
                        .take(EXAMPLE_LIMIT)
                        .map(|(cell, observed)| EffectConflictCell {
                            cell: EffectCellReport::from(cell),
                            first_read: first.reads.get(cell).cloned(),
                            first_write: first.writes.get(cell).cloned(),
                            later_read: current.reads.get(cell).cloned(),
                            later_write: current.writes.get(cell).cloned(),
                            allocation_involved: first.allocates.contains(cell)
                                || current.allocates.contains(cell),
                            observed_conflict: *observed,
                        })
                        .collect(),
                });
            }
        };
        let mut active: Option<(SourceUnitId, PairConflict)> = None;
        while let Some(Reverse((_, partner, cursor))) = pending.pop() {
            if active
                .as_ref()
                .is_some_and(|(previous, _)| *previous != partner)
            {
                let (previous, conflict) = active.take().expect("an active pair");
                emit_pair(previous, conflict);
            }
            let (_, conflict) = active.get_or_insert_with(|| (partner, PairConflict::default()));
            if let Some(cell) = ranges[cursor].0 {
                let observed = observed_conflict(current, &effects[&partner], cell);
                conflict
                    .cells
                    .entry(cell.clone())
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
            emit_pair(partner, conflict);
        }
        drop(ranges);
        // All queries precede insertion, so a statement never conflicts with
        // itself. No pair-premise decision mutates these participants.
        for cell in current.reads.keys() {
            cells
                .entry(cell.clone())
                .or_default()
                .readers
                .insert(ordinal, unit);
        }
        for cell in current.writes.keys().chain(&current.allocates) {
            cells
                .entry(cell.clone())
                .or_default()
                .writers
                .insert(ordinal, unit);
        }
        if !current.is_empty() {
            nonempty.insert(ordinal, unit);
        }
        if current.has_havoc() {
            havoc.insert(ordinal, unit);
        }
    }
    report.counts.legacy_edge_only_pairs = legacy_pairs.len() as u64;
    report.examples_truncated = report.counts.pairs > report.examples.len() as u64;
    report
}

fn observed_conflict(
    a: &InitializationEffects,
    b: &InitializationEffects,
    cell: &EffectCell,
) -> bool {
    let observed = |value: Option<&InitializationEffectEvidence>| {
        value.is_some_and(InitializationEffectEvidence::is_observed)
    };
    (observed(a.writes.get(cell)) && (observed(b.reads.get(cell)) || observed(b.writes.get(cell))))
        || (observed(b.writes.get(cell)) && observed(a.reads.get(cell)))
}

type ParticipantRange<'a> = btree_map::Range<'a, SourceUnitOrdinal, SourceUnitId>;
type PendingParticipant = Reverse<(SourceUnitOrdinal, SourceUnitId, usize)>;

fn push_range<'cell, 'participants>(
    ranges: &mut Vec<(Option<&'cell EffectCell>, ParticipantRange<'participants>)>,
    pending: &mut BinaryHeap<PendingParticipant>,
    cell: Option<&'cell EffectCell>,
    mut range: ParticipantRange<'participants>,
) {
    if let Some((&ordinal, &partner)) = range.next() {
        let cursor = ranges.len();
        ranges.push((cell, range));
        pending.push(Reverse((ordinal, partner, cursor)));
    }
}
