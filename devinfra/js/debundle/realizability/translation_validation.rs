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

/// Translation-validation verdict on one assignment's emitted order.
#[derive(Debug, Clone, Default, Eq, PartialEq)]
pub struct TranslationValidation {
    /// Statement pairs whose emitted order differs from source order.
    pub swapped_pairs: u64,
    pub conflicts: Vec<SwappedConflict>,
    /// Forward ordering edges the emit leaves in source order.
    pub forward_edges_kept: Vec<DependencyEdgeId>,
    /// Forward ordering edges whose endpoints the emit swaps. Not part of the
    /// verdict, as the gate accepts them, though a read of a binding before
    /// its declaration conflicts with that declaration once swapped.
    pub forward_edges_swapped: Vec<DependencyEdgeId>,
    /// Placement-equality edges (rebinds, shared `var` declarations,
    /// co-location, local effects) whose endpoints the assignment splits.
    pub placement_splits: Vec<DependencyEdgeId>,
}

impl TranslationValidation {
    pub fn accepts(&self) -> bool {
        self.conflicts.is_empty()
            && self.forward_edges_kept.is_empty()
            && self.placement_splits.is_empty()
    }
}

/// Judge `assignment`'s emitted order. `emitted_modules` is every module the
/// entry imports plus the entry; `import_order` is the one the emitter
/// renders import lists in.
pub fn translation_validate(
    dependency_graph: &DependencyGraph,
    assignment: &ModuleAssignment,
    emitted_modules: &BTreeSet<ModuleId>,
    import_order: &EsmImportOrder,
) -> TranslationValidation {
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
    let ordinal = |unit: SourceUnitId| {
        dependency_graph
            .node(unit)
            .expect("edges name graph nodes")
            .statement_ordinal
    };

    let mut verdict = TranslationValidation::default();
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
            | DepKind::LocalEffect => verdict.placement_splits.push(edge.id),
            DepKind::LazyUse | DepKind::HoistedUse => {}
            DepKind::EagerUse | DepKind::Sequenced if !edge.reason.constrains_init_order() => {}
            DepKind::EagerUse | DepKind::Sequenced if ordinal(edge.to) > ordinal(edge.from) => {
                if module_position(edge.to) > module_position(edge.from) {
                    verdict.forward_edges_kept.push(edge.id);
                } else {
                    verdict.forward_edges_swapped.push(edge.id);
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

    let mut emitted: Vec<(usize, SourceUnitOrdinal, SourceUnitId)> = dependency_graph
        .iter_nodes()
        .map(|node| (module_position(node.id), node.statement_ordinal, node.id))
        .collect();
    emitted.sort_unstable();
    verdict.swapped_pairs = count_inversions(emitted.iter().map(|(_, ordinal, _)| *ordinal));
    verdict.conflicts = sweep(emitted.iter().map(|&(_, ordinal, unit)| {
        (
            ordinal,
            unit,
            accesses.get(&unit).map_or(&[][..], Vec::as_slice),
        )
    }));
    verdict
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
fn sweep<'a>(
    emitted: impl Iterator<
        Item = (
            SourceUnitOrdinal,
            SourceUnitId,
            &'a [(DependencyEdgeId, Access)],
        ),
    >,
) -> Vec<SwappedConflict> {
    let mut frontier: FxHashMap<DependencyEdgeId, CellFrontier> = FxHashMap::default();
    let mut conflicts = Vec::new();
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
                conflicts.push(SwappedConflict {
                    runs_first: partner,
                    runs_later: unit,
                    cell,
                });
            }
            let slot = match access {
                Access::Read => &mut state.reader,
                Access::Write => &mut state.writer,
            };
            *slot = (*slot).max(Some((ordinal, unit)));
        }
    }
    conflicts
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

/// Wire shape of [`TranslationValidation`] beside the gate's verdict,
/// written as `translation_validation.json`.
#[derive(Debug, Clone, Serialize)]
pub struct TranslationValidationReport {
    /// Today's gate verdict: no cycle, atomic-group conflict or cross-module
    /// rebind.
    pub gate_accepts: bool,
    pub accepts: bool,
    pub swapped_pairs: u64,
    pub conflicts: Vec<SwappedConflictReport>,
    pub forward_edges_kept: Vec<String>,
    pub forward_edges_swapped: Vec<String>,
    pub placement_splits: Vec<String>,
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

impl TranslationValidationReport {
    pub fn new(
        verdict: &TranslationValidation,
        gate_accepts: bool,
        dependency_graph: &DependencyGraph,
        assignment: &ModuleAssignment,
        module_path: &dyn Fn(ModuleId) -> ModulePath,
    ) -> Self {
        let statement = |unit: SourceUnitId| StatementReport {
            unit: dependency_graph
                .node(unit)
                .expect("conflicts name graph nodes")
                .statement_ordinal,
            module: module_path(assignment.of(unit)),
        };
        let keys =
            |edges: &[DependencyEdgeId]| edges.iter().map(|edge| edge.report_key()).collect();
        Self {
            gate_accepts,
            accepts: verdict.accepts(),
            swapped_pairs: verdict.swapped_pairs,
            conflicts: verdict
                .conflicts
                .iter()
                .map(|conflict| SwappedConflictReport {
                    runs_first: statement(conflict.runs_first),
                    runs_later: statement(conflict.runs_later),
                    dependency_edge: conflict.cell.report_key(),
                    kind: dependency_graph.edge(conflict.cell).reason.kind,
                })
                .collect(),
            forward_edges_kept: keys(&verdict.forward_edges_kept),
            forward_edges_swapped: keys(&verdict.forward_edges_swapped),
            placement_splits: keys(&verdict.placement_splits),
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
