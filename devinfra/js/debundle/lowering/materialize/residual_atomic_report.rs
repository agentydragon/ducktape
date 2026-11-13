//! Residual atomic groups in the actual candidate assignment, without full graph export.

use std::collections::{BTreeMap, BTreeSet};

use analysis::reports::{atomic_group_key, source_unit_key};
use analysis::{BindingId, ModuleId, ModuleRole, SourceLocation, StatementKind};
use analysis_model::DepKind;
use gate::AssignmentAnalysis;
use serde::Serialize;

use crate::ownership_subset::SelectedModule;

const EDGE_EXAMPLES_PER_KIND: usize = 4;

#[derive(Serialize)]
pub(super) struct Report<'a> {
    chunk_id: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    ownership_subset: Option<&'a BTreeSet<SelectedModule>>,
    gate_accepts: bool,
    groups: Vec<Group>,
}

#[derive(Serialize)]
struct Group {
    id: String,
    fully_residual: bool,
    causes: Vec<DepKind>,
    source_units: Vec<Unit>,
    incoming: BTreeMap<DepKind, EdgeSummary>,
    outgoing: BTreeMap<DepKind, EdgeSummary>,
}

#[derive(Serialize)]
struct Unit {
    id: String,
    statement_ordinal: usize,
    kind: StatementKind,
    source_location: Option<SourceLocation>,
    bindings: Vec<Binding>,
    destination: Destination,
}

#[derive(Serialize)]
struct Binding {
    binding: String,
    export_name: String,
}

#[derive(Serialize)]
struct Destination {
    module: String,
    residual: bool,
}

#[derive(Default, Serialize)]
struct EdgeSummary {
    total: usize,
    truncated: bool,
    examples: Vec<EdgeExample>,
}

#[derive(Serialize)]
struct EdgeExample {
    source_unit: String,
    target_unit: String,
    other_atomic_group: String,
    other_destination: Destination,
    binding: Option<String>,
    constrains_init_order: bool,
}

fn destination(assignment: &AssignmentAnalysis, id: ModuleId) -> Destination {
    let is_entry = id == assignment.assignment().entry_destination().module_id();
    let residual = is_entry
        || assignment
            .inputs()
            .planned_module(id)
            .is_some_and(|module| module.role == ModuleRole::Unassigned);
    Destination {
        module: assignment.module_path(id).to_string(),
        residual,
    }
}

/// Summaries count graph edges, not distinct dependencies, and bound examples.
fn add_edge(
    summaries: &mut BTreeMap<DepKind, EdgeSummary>,
    assignment: &AssignmentAnalysis,
    edge: &analysis::DependencyEdge,
    other_group: usize,
    other_unit: source_identity::SourceUnitId,
) {
    let summary = summaries.entry(edge.reason.kind()).or_default();
    summary.total += 1;
    if summary.examples.len() < EDGE_EXAMPLES_PER_KIND {
        summary.examples.push(EdgeExample {
            source_unit: source_unit_key(edge.from),
            target_unit: source_unit_key(edge.to),
            other_atomic_group: atomic_group_key(other_group),
            other_destination: destination(assignment, assignment.assignment().of(other_unit)),
            binding: edge.reason.binding.as_ref().map(|id| id.0.to_string()),
            constrains_init_order: edge.reason.constrains_init_order(),
        });
    }
    summary.truncated = summary.total > summary.examples.len();
}

pub(super) fn build<'a>(
    assignment: &'a AssignmentAnalysis,
    ownership_subset: Option<&'a BTreeSet<SelectedModule>>,
    gate_accepts: bool,
) -> Report<'a> {
    let graph = assignment.inputs().dependency_graph();
    let mut atoms: Vec<_> = assignment.atomic_groups().iter().collect();
    atoms.sort_by_key(|atom| atom.members.iter().next().copied());
    let mut unit_atom = vec![0; graph.num_nodes()];
    let mut report_index = vec![None; atoms.len()];
    let mut groups = Vec::new();
    for (index, atom) in atoms.iter().enumerate() {
        for unit in &atom.members {
            unit_atom[unit.0] = index;
        }
        let units: Vec<_> = atom
            .members
            .iter()
            .map(|id| {
                let node = graph.node(*id).expect("atomic group source unit exists");
                let bindings = node
                    .declared
                    .iter()
                    .map(|binding| Binding {
                        binding: binding.0.to_string(),
                        export_name: assignment
                            .inputs()
                            .export_name_for(&BindingId::new(
                                assignment.inputs().chunk_identity(),
                                binding.clone(),
                            ))
                            .expect("source binding belongs to chunk")
                            .to_string(),
                    })
                    .collect();
                Unit {
                    id: source_unit_key(*id),
                    statement_ordinal: node.statement_ordinal.0,
                    kind: node.kind,
                    source_location: node.source_location.clone(),
                    bindings,
                    destination: destination(assignment, assignment.assignment().of(*id)),
                }
            })
            .collect();
        if !units.iter().any(|unit| unit.destination.residual) {
            continue;
        }
        report_index[index] = Some(groups.len());
        groups.push(Group {
            id: atomic_group_key(index),
            fully_residual: units.iter().all(|unit| unit.destination.residual),
            causes: atom.causes.iter().copied().collect(),
            source_units: units,
            incoming: BTreeMap::new(),
            outgoing: BTreeMap::new(),
        });
    }
    for edge in graph.iter_edges() {
        let from = unit_atom[edge.from.0];
        let to = unit_atom[edge.to.0];
        if from == to {
            continue;
        }
        if let Some(index) = report_index[from] {
            add_edge(&mut groups[index].outgoing, assignment, edge, to, edge.to);
        }
        if let Some(index) = report_index[to] {
            add_edge(
                &mut groups[index].incoming,
                assignment,
                edge,
                from,
                edge.from,
            );
        }
    }
    Report {
        chunk_id: assignment.inputs().chunk_id(),
        ownership_subset,
        gate_accepts,
        groups,
    }
}
