//! Independent hypothetical extractions from the current residual assignment.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use analysis::analysis_inputs::CommutingStatementsRef;
use analysis::assignment_assembly::AtomicGroupConflict;
use analysis::reports::{atomic_group_key, source_unit_key};
use analysis::{ModuleId, ModuleRole, SourceLocation, StatementKind};
use anyhow::{Result, ensure};
use gate::{AssignmentAnalysis, BlockingSccEntry, CycleEdge, CycleReport, RealizabilityVerdict};
use serde::{Serialize, Serializer};
use source_identity::SourceUnitId;

use crate::ownership_subset::SelectedModule;

#[derive(Serialize)]
pub(super) struct Report<'a> {
    chunk_id: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    ownership_subset: Option<&'a BTreeSet<SelectedModule>>,
    baseline: Baseline<'a>,
    groups: Vec<Group>,
}

#[derive(Serialize)]
struct Baseline<'a> {
    gate_accepts: bool,
    #[serde(serialize_with = "serialize_cycles")]
    cycles: &'a [CycleReport],
    atomic_group_conflicts: &'a [AtomicGroupConflict],
    cross_rebinds: &'a [String],
}

#[derive(Serialize)]
struct Group {
    id: String,
    source_units: Vec<Unit>,
    #[serde(skip_serializing_if = "Option::is_none")]
    candidate: Option<Candidate>,
    #[serde(skip_serializing_if = "Option::is_none")]
    unprobed_reason: Option<UnprobedReason>,
}

#[derive(Serialize)]
struct Unit {
    id: String,
    statement_ordinal: usize,
    kind: StatementKind,
    source_location: Option<SourceLocation>,
    bindings: Vec<String>,
    current_destination: String,
}

#[derive(Serialize)]
struct Candidate {
    proposed_destination: String,
    gate_accepts: bool,
    #[serde(serialize_with = "serialize_cycles")]
    cycles: Vec<CycleReport>,
    atomic_group_conflicts: Vec<AtomicGroupConflict>,
    cross_rebinds: Vec<String>,
    /// Source locations for both endpoints of every blocking cycle edge.
    blocker_source_units: Vec<BlockerUnit>,
}

#[derive(Serialize)]
struct BlockerUnit {
    id: String,
    source_location: Option<SourceLocation>,
}

/// The ordinary `cycles.json` wire entry plus report-only Pass-2 and premise
/// provenance. `CycleReport` itself is an in-memory diagnostic: its premise
/// location map has structured keys, which JSON objects cannot encode.
#[derive(Serialize)]
struct CycleEvidence<'a> {
    #[serde(flatten)]
    blocking: BlockingSccEntry,
    lazy_closure: &'a [CycleEdge],
    commuting_premise_locations: Vec<PremiseLocation<'a>>,
}

#[derive(Serialize)]
struct PremiseLocation<'a> {
    premise: &'a CommutingStatementsRef,
    spec_location: &'a str,
}

fn serialize_cycles<S>(
    cycles: &[CycleReport],
    serializer: S,
) -> std::result::Result<S::Ok, S::Error>
where
    S: Serializer,
{
    cycles
        .iter()
        .enumerate()
        .map(|(id, cycle)| CycleEvidence {
            blocking: BlockingSccEntry::from_cycle_report(id, cycle),
            lazy_closure: &cycle.lazy_closure,
            commuting_premise_locations: cycle
                .commuting_premise_locations
                .iter()
                .map(|(premise, spec_location)| PremiseLocation {
                    premise,
                    spec_location,
                })
                .collect(),
        })
        .collect::<Vec<_>>()
        .serialize(serializer)
}

#[derive(Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
enum UnprobedReason {
    SplitAtomicGroup,
    EntryScaffolding,
    NotSelected,
}

fn is_residual(assignment: &AssignmentAnalysis, module: ModuleId) -> bool {
    module == assignment.assignment().entry_destination().module_id()
        || assignment
            .inputs()
            .planned_module(module)
            .is_some_and(|plan| plan.role == ModuleRole::Unassigned)
}

fn blocker_source_units(
    graph: &analysis::DependencyGraph,
    verdict: &RealizabilityVerdict,
    cycles: &[CycleReport],
    unit_by_key: &HashMap<String, SourceUnitId>,
) -> Vec<BlockerUnit> {
    let mut locations = BTreeMap::new();
    let blocking_edges = verdict
        .unrealizable_sccs
        .iter()
        .flat_map(|scc| scc.core.constraining_dependency_edges.iter().copied())
        .chain(
            verdict
                .cross_rebinds
                .iter()
                .map(|edge| edge.dependency_edge),
        );
    for edge_id in blocking_edges {
        let edge = graph.edge(edge_id);
        for unit in [edge.from, edge.to] {
            locations.entry(unit.0).or_insert_with(|| BlockerUnit {
                id: source_unit_key(unit),
                source_location: graph
                    .node(unit)
                    .expect("blocking source unit exists")
                    .source_location
                    .clone(),
            });
        }
    }
    // The rendered Pass-2 lazy closure is diagnostic evidence outside the
    // constraining-edge core. Resolve its canonical IDs back to the same
    // graph's typed units for locations; this does not decide the gate.
    for cycle in cycles {
        for edge in cycle.cut.iter().chain(cycle.lazy_closure.iter()).chain(
            cycle
                .pass_one_witness
                .iter()
                .flat_map(|walk| walk.steps.iter()),
        ) {
            for key in [&edge.from_source_unit_id, &edge.to_source_unit_id] {
                let unit = *unit_by_key
                    .get(key)
                    .expect("cycle endpoint belongs to this dependency graph");
                locations.entry(unit.0).or_insert_with(|| BlockerUnit {
                    id: source_unit_key(unit),
                    source_location: graph
                        .node(unit)
                        .expect("blocking source unit exists")
                        .source_location
                        .clone(),
                });
            }
        }
    }
    locations.into_values().collect()
}

pub(super) fn build<'a>(
    assignment: &'a AssignmentAnalysis,
    ownership_subset: Option<&'a BTreeSet<SelectedModule>>,
    baseline: &'a gate::AssignmentReport,
    baseline_accepts: bool,
    selected_atoms: &BTreeSet<String>,
) -> Result<Report<'a>> {
    let graph = assignment.inputs().dependency_graph();
    let unit_by_key: HashMap<_, _> = graph
        .iter_nodes()
        .map(|node| (source_unit_key(node.id), node.id))
        .collect();
    let mut atoms: Vec<_> = assignment.atomic_groups().iter().enumerate().collect();
    atoms.sort_by_key(|(_, atom)| atom.members.iter().next().copied());
    let available: BTreeSet<_> = atoms
        .iter()
        .enumerate()
        .filter(|(_, (_, atom))| {
            atom.members
                .iter()
                .any(|&unit| is_residual(assignment, assignment.assignment().of(unit)))
        })
        .map(|(index, _)| atomic_group_key(index))
        .collect();
    for atom in selected_atoms {
        ensure!(
            available.contains(atom),
            "--residual-extraction-atom {atom} is not a residual atomic group in chunk {}",
            assignment.inputs().chunk_id()
        );
    }
    // Entry is the last real module. One unused ID identifies each independent
    // fresh destination; the same ID is safe because probes never commit.
    let fresh_id = ModuleId(assignment.assignment().entry_destination().module_id().0 + 1);
    let existing_paths: BTreeSet<_> = (0..fresh_id.0)
        .map(|module| assignment.module_path(ModuleId(module)))
        .collect();
    let mut probe = None;
    let mut groups = Vec::new();
    for (atom_index, (native_index, atom)) in atoms.iter().enumerate() {
        let members: Vec<_> = atom.members.iter().copied().collect();
        if !members
            .iter()
            .any(|&unit| is_residual(assignment, assignment.assignment().of(unit)))
        {
            continue;
        }
        let id = atomic_group_key(atom_index);
        let source_units: Vec<_> = members
            .iter()
            .map(|&unit| {
                let node = graph.node(unit).expect("atomic-group source unit exists");
                Unit {
                    id: source_unit_key(unit),
                    statement_ordinal: node.statement_ordinal.0,
                    kind: node.kind,
                    source_location: node.source_location.clone(),
                    bindings: node
                        .declared
                        .iter()
                        .map(|name| name.0.to_string())
                        .collect(),
                    current_destination: assignment
                        .module_path(assignment.assignment().of(unit))
                        .to_string(),
                }
            })
            .collect();
        let reason = if members.iter().any(|&unit| {
            !is_residual(assignment, assignment.assignment().of(unit))
                || assignment.assignment().of(unit) != assignment.assignment().of(members[0])
        }) {
            Some(UnprobedReason::SplitAtomicGroup)
        } else if source_units
            .iter()
            .any(|unit| matches!(unit.kind, StatementKind::Import | StatementKind::Export))
        {
            Some(UnprobedReason::EntryScaffolding)
        } else if !selected_atoms.is_empty() && !selected_atoms.contains(&id) {
            Some(UnprobedReason::NotSelected)
        } else {
            None
        };
        let candidate = if reason.is_none() {
            let probe = probe.get_or_insert_with(|| assignment.atomic_move_probe());
            let base = format!("__candidate/residual_extraction_{atom_index:04}");
            let mut proposed_destination = base.clone();
            let mut path = spec::ModulePath::parse(&proposed_destination, "")
                .expect("fresh candidate path is canonical");
            let mut suffix = 1;
            while existing_paths.contains(&path) {
                proposed_destination = format!("{base}_{suffix}");
                path = spec::ModulePath::parse(&proposed_destination, "")
                    .expect("fresh candidate path is canonical");
                suffix += 1;
            }
            let validation = probe.validate_atom(*native_index, fresh_id, &path);
            let gate_accepts = validation.verdict.is_realizable()
                && validation.report.atomic_group_conflicts.is_empty()
                && validation.report.cycles.is_empty();
            Some(Candidate {
                proposed_destination,
                gate_accepts,
                blocker_source_units: blocker_source_units(
                    graph,
                    &validation.verdict,
                    &validation.report.cycles,
                    &unit_by_key,
                ),
                cycles: validation.report.cycles,
                atomic_group_conflicts: validation.report.atomic_group_conflicts,
                cross_rebinds: validation.report.cross_rebinds,
            })
        } else {
            None
        };
        groups.push(Group {
            id,
            source_units,
            candidate,
            unprobed_reason: reason,
        });
    }
    Ok(Report {
        chunk_id: assignment.inputs().chunk_id(),
        ownership_subset,
        baseline: Baseline {
            gate_accepts: baseline_accepts,
            cycles: &baseline.cycles,
            atomic_group_conflicts: &baseline.atomic_group_conflicts,
            cross_rebinds: &baseline.cross_rebinds,
        },
        groups,
    })
}
