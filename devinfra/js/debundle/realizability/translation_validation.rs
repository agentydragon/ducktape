//! Translation validation of the emitted evaluation order: accept iff every
//! statement pair the emit runs out of source order commutes. Conflicts are
//! today's ordering edges, and the realizability gate stays authoritative;
//! this verdict is reported beside it.
//!
//! The emitted order is the exact ESM loader model over the emitter's own
//! import lists and `EsmImportOrder`, cyclic or not: module bodies in DFS
//! post-order from the entry, each body in source order. A sweep in that
//! order finds every swapped statement pair that conflicts without
//! enumerating pairs.
//!
//! An ordering edge `u -> v` asks for `v` to run before `u`. When `v`
//! precedes `u` in the source, violating it swaps the pair, which the sweep
//! finds. A forward edge (`v` follows `u` in the source, as for an at-init
//! read of a later declaration) is violated when the emit keeps source order,
//! and satisfying it swaps the pair. The sweep sees neither: both are reported
//! beside it, and only the violated forward edges count toward the verdict,
//! as they do for the gate.

use std::collections::{BTreeMap, BTreeSet};

use rustc_hash::FxHashMap;
use serde::Serialize;
use source_identity::SourceUnitOrdinal;

use analysis::graph::{DependencyEdgeId, chunk_constraining_module_edges};
use analysis::ids::ModuleId;
use analysis::module_assignment::ModuleAssignment;
use analysis::{DepKind, DependencyGraph, SourceUnitId};
use spec::ModulePath;

use super::esm_simulator::simulate_esm_post_order;
use crate::esm_import_order::EsmImportOrder;

/// One swapped pair the sweep found conflicting: `runs_first` follows
/// `runs_later` in the source, and the emit runs it first.
#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub struct SwappedConflict {
    pub runs_first: SourceUnitId,
    pub runs_later: SourceUnitId,
    pub cell: DependencyEdgeId,
}

const EXAMPLE_LIMIT: usize = 20;

/// Exact category totals, independent of diagnostic retention.
#[derive(Debug, Clone, Default, Eq, PartialEq, Serialize)]
pub struct TranslationValidationCounts {
    pub conflicts: usize,
    pub forward_edges_kept: usize,
    pub forward_edges_swapped: usize,
    pub placement_splits: usize,
    pub conflicts_by_kind: BTreeMap<DepKind, usize>,
}

/// One result from the existing edge classification or emitted-order sweep.
#[derive(Debug, Clone, Copy)]
pub enum TranslationValidationEvidence {
    Conflict(SwappedConflict),
    ForwardEdgeKept(DependencyEdgeId),
    ForwardEdgeSwapped(DependencyEdgeId),
    PlacementSplit(DependencyEdgeId),
}

/// Complete semantic result plus bounded raw diagnostic examples.
#[derive(Debug, Clone, Default, Eq, PartialEq)]
pub struct TranslationValidation {
    pub swapped_pairs: u64,
    pub counts: TranslationValidationCounts,
    /// Complete pair set for the effect validator's legacy comparison. Keys
    /// retain the existing wire-report ordinal convention.
    pub conflict_pairs: BTreeSet<(SourceUnitId, SourceUnitId)>,
    conflicts: Vec<SwappedConflict>,
    forward_edges_kept: Vec<DependencyEdgeId>,
    forward_edges_swapped: Vec<DependencyEdgeId>,
    placement_splits: Vec<DependencyEdgeId>,
}

impl TranslationValidation {
    pub fn accepts(&self) -> bool {
        self.counts.conflicts == 0
            && self.counts.forward_edges_kept == 0
            && self.counts.placement_splits == 0
    }

    fn record(&mut self, evidence: TranslationValidationEvidence, graph: &DependencyGraph) {
        fn retain<T>(examples: &mut Vec<T>, value: T) {
            if examples.len() < EXAMPLE_LIMIT {
                examples.push(value);
            }
        }
        match evidence {
            TranslationValidationEvidence::Conflict(conflict) => {
                self.counts.conflicts += 1;
                *self
                    .counts
                    .conflicts_by_kind
                    .entry(graph.edge(conflict.cell).reason.kind)
                    .or_default() += 1;
                let key = |unit| {
                    SourceUnitId(
                        graph
                            .node(unit)
                            .expect("conflicts name graph nodes")
                            .statement_ordinal
                            .0,
                    )
                };
                self.conflict_pairs
                    .insert((key(conflict.runs_first), key(conflict.runs_later)));
                retain(&mut self.conflicts, conflict);
            }
            TranslationValidationEvidence::ForwardEdgeKept(edge) => {
                self.counts.forward_edges_kept += 1;
                retain(&mut self.forward_edges_kept, edge);
            }
            TranslationValidationEvidence::ForwardEdgeSwapped(edge) => {
                self.counts.forward_edges_swapped += 1;
                retain(&mut self.forward_edges_swapped, edge);
            }
            TranslationValidationEvidence::PlacementSplit(edge) => {
                self.counts.placement_splits += 1;
                retain(&mut self.placement_splits, edge);
            }
        }
    }
}

/// Judge `assignment`'s emitted order. `emitted_modules` is every module the
/// entry imports plus the entry; `import_order` is the one the emitter
/// renders import lists in.
pub fn translation_validate<E>(
    dependency_graph: &DependencyGraph,
    assignment: &ModuleAssignment,
    emitted_modules: &BTreeSet<ModuleId>,
    import_order: &EsmImportOrder,
    mut visitor: Option<&mut impl FnMut(TranslationValidationEvidence) -> Result<(), E>>,
) -> Result<TranslationValidation, E> {
    let emitted =
        emitted_statement_order(dependency_graph, assignment, emitted_modules, import_order);
    let positions: BTreeMap<_, _> = emitted
        .iter()
        .map(|&(position, _, unit)| (unit, position))
        .collect();
    let module_position = |unit| positions[&unit];
    let ordinal = |unit| {
        dependency_graph
            .node(unit)
            .expect("edges name graph nodes")
            .statement_ordinal
    };

    let mut verdict = TranslationValidation {
        swapped_pairs: count_inversions(emitted.iter().map(|(_, ordinal, _)| *ordinal)),
        ..TranslationValidation::default()
    };
    let mut emit = |evidence| {
        verdict.record(evidence, dependency_graph);
        if let Some(visitor) = visitor.as_mut() {
            visitor(evidence)?;
        }
        Ok(())
    };
    let mut accesses: BTreeMap<SourceUnitId, Vec<(DependencyEdgeId, Access)>> = BTreeMap::new();
    for edge in dependency_graph.iter_edges() {
        if dependency_graph.node(edge.from).is_none() || dependency_graph.node(edge.to).is_none() {
            continue;
        }
        // Statements of one module never swap. Unlike the gate's module view,
        // no cross-module edge is dropped: a swap is judged per statement.
        if assignment.of(edge.from) == assignment.of(edge.to) {
            continue;
        }
        match edge.reason.kind {
            DepKind::EagerRebind
            | DepKind::LazyRebind
            | DepKind::DeferredRebind
            | DepKind::CoDeclaration
            | DepKind::CoLocation
            | DepKind::LocalEffect => emit(TranslationValidationEvidence::PlacementSplit(edge.id))?,
            DepKind::LazyUse | DepKind::HoistedUse => {}
            DepKind::EagerUse | DepKind::Sequenced if !edge.reason.constrains_init_order() => {}
            DepKind::EagerUse | DepKind::Sequenced if ordinal(edge.to) > ordinal(edge.from) => {
                if module_position(edge.to) > module_position(edge.from) {
                    emit(TranslationValidationEvidence::ForwardEdgeKept(edge.id))?;
                } else {
                    emit(TranslationValidationEvidence::ForwardEdgeSwapped(edge.id))?;
                }
            }
            DepKind::EagerUse | DepKind::Sequenced => {
                // The dependency writes the edge's cell and the dependent
                // reads it, so the sweep flags the pair exactly when the emit
                // swaps it.
                accesses
                    .entry(edge.to)
                    .or_default()
                    .push((edge.id, Access::Write));
                accesses
                    .entry(edge.from)
                    .or_default()
                    .push((edge.id, Access::Read));
            }
        }
    }

    sweep(
        emitted.iter().map(|&(_, ordinal, unit)| {
            (
                ordinal,
                unit,
                accesses.get(&unit).map_or(&[][..], Vec::as_slice),
            )
        }),
        |conflict| emit(TranslationValidationEvidence::Conflict(conflict)),
    )?;
    Ok(verdict)
}

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
enum Access {
    Read,
    Write,
}

type Ran = (SourceUnitOrdinal, SourceUnitId);

/// Per cell, the already-run writer and reader with the largest source
/// ordinal.
#[derive(Default)]
struct CellFrontier {
    writer: Option<Ran>,
    reader: Option<Ran>,
}

/// Sweep statements in emitted order. A write conflicts with an already-run
/// reader or writer later in the source, a read with an already-run writer
/// later in the source; the partner reported is the latest such statement.
fn sweep<'a, E>(
    emitted: impl Iterator<
        Item = (
            SourceUnitOrdinal,
            SourceUnitId,
            &'a [(DependencyEdgeId, Access)],
        ),
    >,
    mut visitor: impl FnMut(SwappedConflict) -> Result<(), E>,
) -> Result<(), E> {
    let mut frontier: FxHashMap<DependencyEdgeId, CellFrontier> = FxHashMap::default();
    for (ordinal, unit, accesses) in emitted {
        for &(cell, access) in accesses {
            let state = frontier.entry(cell).or_default();
            let partner = match access {
                Access::Read => state.writer,
                Access::Write => state.writer.max(state.reader),
            };
            if let Some((partner_ordinal, partner)) = partner
                && partner_ordinal > ordinal
            {
                visitor(SwappedConflict {
                    runs_first: partner,
                    runs_later: unit,
                    cell,
                })?;
            }
            let slot = match access {
                Access::Read => &mut state.reader,
                Access::Write => &mut state.writer,
            };
            *slot = (*slot).max(Some((ordinal, unit)));
        }
    }
    Ok(())
}

/// Exact emitter order shared by both report-only translation validators.
pub(super) fn emitted_statement_order(
    dependency_graph: &DependencyGraph,
    assignment: &ModuleAssignment,
    emitted_modules: &BTreeSet<ModuleId>,
    import_order: &EsmImportOrder,
) -> Vec<(usize, SourceUnitOrdinal, SourceUnitId)> {
    let canonical = chunk_constraining_module_edges(dependency_graph, assignment);
    let post_order = simulate_esm_post_order(
        assignment.entry_destination().module_id(),
        &canonical.i_successors,
        emitted_modules,
        import_order,
    );
    let module_position = |unit: SourceUnitId| {
        let module = assignment.of(unit);
        *post_order
            .get(&module)
            .unwrap_or_else(|| panic!("the entry imports every emitted module, {module:?} too"))
    };

    let mut emitted: Vec<(usize, SourceUnitOrdinal, SourceUnitId)> = dependency_graph
        .iter_nodes()
        .map(|node| (module_position(node.id), node.statement_ordinal, node.id))
        .collect();
    emitted.sort_unstable();
    emitted
}

/// Pairs `i < j` with `ordinals[i] > ordinals[j]`, by a Fenwick tree over
/// ordinal ranks.
fn count_inversions(ordinals: impl Iterator<Item = SourceUnitOrdinal>) -> u64 {
    let ordinals: Vec<SourceUnitOrdinal> = ordinals.collect();
    let mut ranked = ordinals.clone();
    ranked.sort_unstable();
    ranked.dedup();
    let mut tree = vec![0u64; ranked.len() + 1];
    let mut inversions = 0u64;
    for (seen, ordinal) in ordinals.iter().enumerate() {
        let rank = ranked.partition_point(|candidate| candidate <= ordinal);
        let mut not_greater = 0u64;
        let mut index = rank;
        while index > 0 {
            not_greater += tree[index];
            index &= index - 1;
        }
        inversions += seen as u64 - not_greater;
        let mut index = rank;
        while index < tree.len() {
            tree[index] += 1;
            index += index.isolate_lowest_one();
        }
    }
    inversions
}

/// A diagnostic list with exact cardinality and a bounded deterministic prefix.
#[derive(Debug, Clone, Serialize)]
pub struct TranslationExamples<T> {
    pub total: usize,
    pub examples: Vec<T>,
    pub truncated: bool,
}

impl<T> TranslationExamples<T> {
    fn new(total: usize, examples: Vec<T>) -> Self {
        Self {
            total,
            truncated: total > examples.len(),
            examples,
        }
    }
}

/// Bounded diagnostic output. Never used to decide acceptance or pair coverage.
#[derive(Debug, Clone, Serialize)]
pub struct TranslationValidationReport {
    pub gate_accepts: bool,
    pub accepts: bool,
    pub swapped_pairs: u64,
    pub counts: TranslationValidationCounts,
    pub conflicts: TranslationExamples<SwappedConflictReport>,
    pub forward_edges_kept: TranslationExamples<String>,
    pub forward_edges_swapped: TranslationExamples<String>,
    pub placement_splits: TranslationExamples<String>,
    pub example_limit: usize,
    pub full_detail_export_available: bool,
    pub full_detail_export_command: &'static str,
}

/// Full-check result consumed by the effect validator, with its diagnostic view.
#[derive(Debug)]
pub struct TranslationValidationResult {
    pub report: TranslationValidationReport,
    pub(crate) conflict_pairs: BTreeSet<(SourceUnitId, SourceUnitId)>,
}

/// One complete evidence row, projected only for an active streaming sink.
#[derive(Debug, Serialize)]
#[serde(tag = "category", rename_all = "snake_case")]
pub enum TranslationValidationEvidenceReport {
    Conflict {
        #[serde(flatten)]
        conflict: SwappedConflictReport,
    },
    ForwardEdgeKept {
        dependency_edge: String,
    },
    ForwardEdgeSwapped {
        dependency_edge: String,
    },
    PlacementSplit {
        dependency_edge: String,
    },
}

#[derive(Debug, Clone, Serialize)]
pub struct SwappedConflictReport {
    pub runs_first: StatementReport,
    pub runs_later: StatementReport,
    pub dependency_edge: String,
    pub kind: DepKind,
}

#[derive(Debug, Clone, Serialize)]
pub struct StatementReport {
    pub unit: SourceUnitOrdinal,
    pub module: ModulePath,
}

impl SwappedConflictReport {
    fn new(
        conflict: SwappedConflict,
        graph: &DependencyGraph,
        assignment: &ModuleAssignment,
        module_path: &dyn Fn(ModuleId) -> ModulePath,
    ) -> Self {
        let statement = |unit| StatementReport {
            unit: graph
                .node(unit)
                .expect("conflicts name graph nodes")
                .statement_ordinal,
            module: module_path(assignment.of(unit)),
        };
        Self {
            runs_first: statement(conflict.runs_first),
            runs_later: statement(conflict.runs_later),
            dependency_edge: conflict.cell.report_key(),
            kind: graph.edge(conflict.cell).reason.kind,
        }
    }
}

impl TranslationValidationEvidenceReport {
    pub(crate) fn new(
        evidence: TranslationValidationEvidence,
        graph: &DependencyGraph,
        assignment: &ModuleAssignment,
        module_path: &dyn Fn(ModuleId) -> ModulePath,
    ) -> Self {
        match evidence {
            TranslationValidationEvidence::Conflict(conflict) => Self::Conflict {
                conflict: SwappedConflictReport::new(conflict, graph, assignment, module_path),
            },
            TranslationValidationEvidence::ForwardEdgeKept(edge) => Self::ForwardEdgeKept {
                dependency_edge: edge.report_key(),
            },
            TranslationValidationEvidence::ForwardEdgeSwapped(edge) => Self::ForwardEdgeSwapped {
                dependency_edge: edge.report_key(),
            },
            TranslationValidationEvidence::PlacementSplit(edge) => Self::PlacementSplit {
                dependency_edge: edge.report_key(),
            },
        }
    }
}

impl TranslationValidationReport {
    pub fn new(
        verdict: &TranslationValidation,
        gate_accepts: bool,
        dependency_graph: &DependencyGraph,
        assignment: &ModuleAssignment,
        module_path: &dyn Fn(ModuleId) -> ModulePath,
    ) -> Self {
        let keys = |total, edges: &[DependencyEdgeId]| {
            TranslationExamples::new(total, edges.iter().map(|edge| edge.report_key()).collect())
        };
        Self {
            gate_accepts,
            accepts: verdict.accepts(),
            swapped_pairs: verdict.swapped_pairs,
            counts: verdict.counts.clone(),
            conflicts: TranslationExamples::new(
                verdict.counts.conflicts,
                verdict
                    .conflicts
                    .iter()
                    .map(|conflict| {
                        SwappedConflictReport::new(
                            *conflict,
                            dependency_graph,
                            assignment,
                            module_path,
                        )
                    })
                    .collect(),
            ),
            forward_edges_kept: keys(
                verdict.counts.forward_edges_kept,
                &verdict.forward_edges_kept,
            ),
            forward_edges_swapped: keys(
                verdict.counts.forward_edges_swapped,
                &verdict.forward_edges_swapped,
            ),
            placement_splits: keys(verdict.counts.placement_splits, &verdict.placement_splits),
            example_limit: EXAMPLE_LIMIT,
            full_detail_export_available: true,
            full_detail_export_command: "debundle run --full-report translation-validation",
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn count_inversions_matches_pairwise_count() {
        let cases: &[&[usize]] = &[
            &[],
            &[0],
            &[0, 1, 2],
            &[2, 1, 0],
            &[1, 3, 0, 2],
            &[3, 0, 3, 1],
        ];
        for case in cases {
            let pairwise = (0..case.len())
                .flat_map(|i| (i + 1..case.len()).map(move |j| (i, j)))
                .filter(|&(i, j)| case[i] > case[j])
                .count() as u64;
            assert_eq!(
                count_inversions(case.iter().copied().map(SourceUnitOrdinal)),
                pairwise,
                "{case:?}"
            );
        }
    }
}
