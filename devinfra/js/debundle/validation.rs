use std::collections::{HashMap, HashSet};

use petgraph::algo::{greedy_feedback_arc_set, tarjan_scc};
use petgraph::graph::DiGraph;
use petgraph::graphmap::DiGraphMap;
use petgraph::visit::EdgeRef;
use serde::Serialize;

use crate::partition::Partition;
use crate::reports::owner_key;
use crate::{
    BindingName, DepKind, EdgeMetadata, ModuleId, ModuleQuotient, OwnerGraph, SourceLocation,
    StatementOrdinal,
};

/// Result of validating a module dep graph.
#[derive(Debug, Clone, Serialize)]
pub struct ScheduleReport {
    pub cycles: Vec<CycleReport>,
    /// Rebinding writes whose assigning owner and binding owner are
    /// destined for different output modules. These specs are always
    /// invalid because emitted ESM imports are read-only in the
    /// importing module.
    pub cross_destination_assignments: Vec<CrossDestinationAssignmentReport>,
    /// Topological linearization of `I ∪ S` rooted at the entry,
    /// dependency-first. Empty when the dep graph has cycles
    /// (validation rejects). Captured here so debug tooling can
    /// see the linker's evaluation order without re-running
    /// materialization. See DESIGN.md "Lemma 2".
    pub linker_order: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct CrossDestinationAssignmentReport {
    pub binding: BindingName,
    pub assigner_owner: String,
    pub binding_owner: String,
    pub assigner_statement_ordinal: StatementOrdinal,
    pub binding_statement_ordinal: StatementOrdinal,
    pub assigner_module: String,
    pub binding_module: String,
    pub kind: DepKind,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub assigner_source_location: Option<SourceLocation>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub binding_source_location: Option<SourceLocation>,
}

#[derive(Debug, Clone, Serialize)]
pub struct CycleReport {
    pub modules: Vec<String>,
    pub evidence: Vec<CycleEdge>,
    /// Spec-author-actionable cut: a near-minimum set of
    /// realizability-constraining (`at-init` or `side-effect`)
    /// reasons whose removal would lift the cycle's realizability
    /// violation. Computed by [`compute_realizability_cut`].
    ///
    /// The cut never includes `lazy` reasons — lazy edges don't
    /// constrain ESM evaluation order, so removing one cannot help
    /// fix a cycle. Each entry corresponds to (and shares its
    /// shape with) a row in `evidence`.
    ///
    /// The algorithm is iterative: while the working subgraph
    /// still has an SCC carrying a cross-module
    /// realizability-constraining edge, run petgraph's
    /// `greedy_feedback_arc_set` (Eades-Lin-Smyth, 1993,
    /// `O(V + E)`) on the offending sub-SCC, pick the first FAS
    /// edge with an `R` or `S` reason, append its constraining
    /// reasons to the cut, remove it from the working graph, and
    /// repeat. Sound (every iteration removes one constraining
    /// edge from a problematic SCC) and heuristic-minimum
    /// (petgraph's FAS approximates within a constant factor on
    /// dense instances).
    pub cut: Vec<CycleEdge>,
}

#[derive(Debug, Clone, Serialize)]
pub struct CycleEdge {
    pub from: String,
    pub to: String,
    pub statement_ordinal: StatementOrdinal,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub binding: Option<BindingName>,
    /// Edge kind. Lets
    /// downstream consumers (cycle-evidence visualizers, spec
    /// authors triaging which edges to break) tell at a glance
    /// which reasons are actually realizability-constraining
    /// (`eager_use` and `sequenced`) vs.
    /// inert-but-graph-present (`lazy_use`).
    pub kind: DepKind,
}

/// Render a compact human-readable summary of cycle reports for the
/// bail message. The full per-cycle evidence + cut goes to a side-
/// output file (`<chunk_id>/cycles.json`); the summary stays under
/// the typical CI log-tail threshold so the bail-message version
/// fits in stderr without truncation.
///
/// Per cycle, the summary lists:
/// - SCC size (modules) and total evidence-edge count.
/// - Top-5 modules by in-degree within the SCC — these are the
///   hubs whose incoming edges drive most of the cycle weight.
/// - Top-5 cut edges by reason count — the highest-leverage
///   `(from, to)` pairs to break.
/// - Cut total size (number of constraining reasons selected by
///   the FAS heuristic).
pub fn render_cycle_summary(cycles: &[CycleReport]) -> String {
    let mut out = String::new();
    for (i, cycle) in cycles.iter().enumerate() {
        let mut in_degree: HashMap<&str, usize> = HashMap::new();
        for edge in &cycle.evidence {
            *in_degree.entry(edge.to.as_str()).or_insert(0) += 1;
        }
        let mut top_in: Vec<(&str, usize)> = in_degree.into_iter().collect();
        top_in.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(b.0)));
        top_in.truncate(5);

        let mut cut_pairs: HashMap<(&str, &str), usize> = HashMap::new();
        for edge in &cycle.cut {
            *cut_pairs
                .entry((edge.from.as_str(), edge.to.as_str()))
                .or_insert(0) += 1;
        }
        let mut top_cut: Vec<((&str, &str), usize)> = cut_pairs.into_iter().collect();
        top_cut.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(&b.0)));
        top_cut.truncate(5);

        out.push_str(&format!(
            "Cycle #{i}: {} modules, {} evidence edges, cut {} reasons across {} (from, to) pairs.\n",
            cycle.modules.len(),
            cycle.evidence.len(),
            cycle.cut.len(),
            cut_pairs_count(&cycle.cut),
        ));
        out.push_str("  Top in-degree hubs (incoming evidence edges):\n");
        for (m, n) in &top_in {
            out.push_str(&format!("    {n:>6}  {m}\n"));
        }
        out.push_str("  Top cut edges (R/S reasons to break):\n");
        for ((f, t), n) in &top_cut {
            out.push_str(&format!("    {n:>6}  {f}  ->  {t}\n"));
        }
    }
    out
}

/// Render a compact human-readable summary of cross-destination
/// rebinding writes for the materializer bail message.
pub fn render_cross_destination_assignment_summary(
    assignments: &[CrossDestinationAssignmentReport],
) -> String {
    let mut out = String::new();
    for assignment in assignments.iter().take(10) {
        out.push_str(&format!(
            "  assigner {} in {} writes mutable binding `{}` owned by {} in {} ({:?}, assigner statement #{}, binding statement #{}).\n",
            assignment.assigner_owner,
            assignment.assigner_module,
            assignment.binding,
            assignment.binding_owner,
            assignment.binding_module,
            assignment.kind,
            assignment.assigner_statement_ordinal.0,
            assignment.binding_statement_ordinal.0,
        ));
    }
    if assignments.len() > 10 {
        out.push_str(&format!(
            "  ... and {} more cross-destination assignment(s).\n",
            assignments.len() - 10
        ));
    }
    out
}

fn cut_pairs_count(cut: &[CycleEdge]) -> usize {
    let mut seen: HashSet<(&str, &str)> = HashSet::new();
    for edge in cut {
        seen.insert((edge.from.as_str(), edge.to.as_str()));
    }
    seen.len()
}

/// Find SCCs in the dep graph and produce a report listing every
/// non-trivial cycle (size > 1 OR a self-loop). Trivial single-node
/// non-self-loop SCCs are dropped.
pub fn validate_schedule(
    graph: &ModuleQuotient,
    module_name: &dyn Fn(ModuleId) -> String,
) -> ScheduleReport {
    let sccs = tarjan_scc(&graph.graph);
    let mut cycles = Vec::new();
    for scc in sccs {
        let in_scc: HashSet<ModuleId> = scc.iter().copied().collect();
        let is_cycle =
            scc.len() > 1 || (scc.len() == 1 && graph.graph.contains_edge(scc[0], scc[0]));
        if !is_cycle {
            continue;
        }
        // Realizability filter (per DESIGN.md "The realizability
        // theorem"): an `I ∪ S` SCC is unrealizable iff at least
        // one cross-module edge between its members carries a
        // realizability-constraining reason — an at-init read
        // (`R`) or a side-effect ordering edge (`S`). Lazy reads
        // alone don't constrain it: the ESM linker evaluates the
        // SCC in *some* order, and the lazy reads only fire
        // afterwards (no TDZ, no missed side-effect ordering).
        let scc_constrains_evaluation_order = scc.iter().any(|&from| {
            scc.iter()
                .any(|&to| from != to && graph.has_init_order_constraining_edge(from, to))
        });
        if !scc_constrains_evaluation_order {
            continue;
        }
        let mut evidence = Vec::new();
        for (from, to, weight) in graph.iter_edges() {
            if !in_scc.contains(&from) || !in_scc.contains(&to) {
                continue;
            }
            for reason in &weight.reasons {
                evidence.push(CycleEdge {
                    from: module_name(from),
                    to: module_name(to),
                    statement_ordinal: reason.statement_ordinal,
                    binding: reason
                        .binding
                        .map(|binding| graph.binding_table.required_name(binding).clone()),
                    kind: reason.kind,
                });
            }
        }
        let cut = compute_realizability_cut(graph, &scc, module_name);
        cycles.push(CycleReport {
            modules: scc.iter().copied().map(module_name).collect(),
            evidence,
            cut,
        });
    }
    ScheduleReport {
        cycles,
        cross_destination_assignments: Vec::new(),
        linker_order: Vec::new(),
    }
}

pub(crate) fn validate_cross_destination_assignments(
    owner_graph: &OwnerGraph,
    partition: &Partition,
    module_name: &dyn Fn(ModuleId) -> String,
) -> Vec<CrossDestinationAssignmentReport> {
    let mut violations = Vec::new();
    for edge in owner_graph.iter_edges() {
        let Some(from_node) = owner_graph.node(edge.from) else {
            continue;
        };
        let Some(to_node) = owner_graph.node(edge.to) else {
            continue;
        };
        let from_module = partition.of(edge.from);
        let to_module = partition.of(edge.to);
        if from_module == to_module {
            continue;
        }
        if !edge.reason.is_rebind() {
            continue;
        }
        let Some(binding_id) = edge.reason.binding else {
            continue;
        };
        let binding = owner_graph.binding_table.required_name(binding_id).clone();
        violations.push(CrossDestinationAssignmentReport {
            binding,
            assigner_owner: owner_key(edge.from),
            binding_owner: owner_key(edge.to),
            assigner_statement_ordinal: from_node.statement_ordinal,
            binding_statement_ordinal: to_node.statement_ordinal,
            assigner_module: module_name(from_module),
            binding_module: module_name(to_module),
            kind: edge.reason.kind,
            assigner_source_location: from_node.source_location.clone(),
            binding_source_location: to_node.source_location.clone(),
        });
    }
    violations.sort_by(|a, b| {
        (
            a.assigner_statement_ordinal,
            a.binding_statement_ordinal,
            a.binding.as_str(),
            a.kind,
        )
            .cmp(&(
                b.assigner_statement_ordinal,
                b.binding_statement_ordinal,
                b.binding.as_str(),
                b.kind,
            ))
    });
    violations
}

/// Compute a near-minimum cut of realizability-constraining edges
/// inside `scc` whose removal makes the SCC realizable.
///
/// Each iteration:
/// 1. Tarjan-SCC the working graph (initially the induced subgraph
///    on `scc` from `graph`).
/// 2. If no SCC of size ≥ 2 carries a cross-module
///    realizability-constraining edge, return the accumulated cut.
/// 3. Otherwise, pick the first such SCC, run
///    `petgraph::algo::greedy_feedback_arc_set` (Eades-Lin-Smyth)
///    on its induced subgraph, and pick the first FAS edge whose
///    metadata has an `EagerUse` or `Sequenced` reason.
/// 4. Fall back to scanning the SCC's edges if the FAS only
///    yielded lazy edges (rare; happens when tie-breaking biases
///    the order toward picking lazy edges as back-edges).
/// 5. Append the picked edge's R/S reasons to the cut and remove
///    it from the working graph.
///
/// Termination: each iteration removes at least one R/S edge from
/// the working graph, and the count of R/S edges is finite.
/// Soundness: when the loop exits, every remaining SCC has only
/// lazy cross-module edges between members — realizable per the
/// DESIGN.md realizability theorem. Cuts are sorted
/// deterministically `(from, to, statement_ordinal, binding, kind)`
/// so test snapshots compare cleanly.
fn compute_realizability_cut(
    graph: &ModuleQuotient,
    scc: &[ModuleId],
    module_name: &dyn Fn(ModuleId) -> String,
) -> Vec<CycleEdge> {
    if scc.len() < 2 {
        return Vec::new();
    }
    // Working copy: induced subgraph on `scc`. Edge weight is the
    // full `EdgeMetadata` so we can read reasons when adding to
    // the cut. Cloning is cheap — petgraph's `DiGraphMap` clone
    // is per-edge, and an SCC is at most a few thousand edges in
    // practice.
    let in_scc: HashSet<ModuleId> = scc.iter().copied().collect();
    let mut working = DiGraphMap::<ModuleId, EdgeMetadata>::new();
    for &m in scc {
        working.add_node(m);
    }
    for (from, to, weight) in graph.iter_edges() {
        if !in_scc.contains(&from) || !in_scc.contains(&to) || from == to {
            continue;
        }
        working.add_edge(from, to, weight.clone());
    }

    let mut cut: Vec<CycleEdge> = Vec::new();
    loop {
        let sub_sccs = tarjan_scc(&working);
        let problematic = sub_sccs.into_iter().find(|s| {
            if s.len() < 2 {
                return false;
            }
            let in_s: HashSet<ModuleId> = s.iter().copied().collect();
            s.iter().any(|&from| {
                working
                    .edges(from)
                    .any(|(_, to, w)| from != to && in_s.contains(&to) && w.constrains_init_order())
            })
        });
        let Some(s) = problematic else { break };
        let in_s: HashSet<ModuleId> = s.iter().copied().collect();

        // Induce a sub-SCC subgraph as an index-based `DiGraph`.
        // petgraph's `greedy_feedback_arc_set` requires
        // `NodeId: GraphIndex`, which `DiGraphMap`'s arbitrary key
        // type doesn't satisfy — `DiGraph` indexes nodes by
        // contiguous `NodeIndex`. Carry `ModuleId` as the node
        // weight so we can map FAS endpoints back.
        let mut induced: DiGraph<ModuleId, ()> = DiGraph::new();
        let mut idx_of: HashMap<ModuleId, _> = HashMap::new();
        for &m in &s {
            let ix = induced.add_node(m);
            idx_of.insert(m, ix);
        }
        for &from in &s {
            for (_, to, _) in working.edges(from) {
                if from != to && in_s.contains(&to) {
                    induced.add_edge(idx_of[&from], idx_of[&to], ());
                }
            }
        }
        let fas: Vec<(ModuleId, ModuleId)> = greedy_feedback_arc_set(&induced)
            .map(|e| (induced[e.source()], induced[e.target()]))
            .collect();

        // Prefer R/S FAS edges; fall back to scanning the sub-SCC
        // for any R/S edge if FAS only flagged lazy edges (rare).
        let pick_in_fas = fas.iter().copied().find(|&(u, v)| {
            working
                .edge_weight(u, v)
                .is_some_and(EdgeMetadata::constrains_init_order)
        });
        let pick = pick_in_fas.or_else(|| {
            for &from in &s {
                for (_, to, w) in working.edges(from) {
                    if from != to && in_s.contains(&to) && w.constrains_init_order() {
                        return Some((from, to));
                    }
                }
            }
            None
        });
        let Some((u, v)) = pick else {
            // Should be unreachable — `problematic` confirmed at
            // least one constraining cross-module edge in `s`.
            break;
        };

        let weight = working
            .remove_edge(u, v)
            .expect("edge picked from working graph just above");
        for reason in &weight.reasons {
            if !reason.constrains_init_order() {
                continue;
            }
            cut.push(CycleEdge {
                from: module_name(u),
                to: module_name(v),
                statement_ordinal: reason.statement_ordinal,
                binding: reason
                    .binding
                    .map(|binding| graph.binding_table.required_name(binding).clone()),
                kind: reason.kind,
            });
        }
    }

    cut.sort_by(|a, b| {
        (
            a.from.as_str(),
            a.to.as_str(),
            a.statement_ordinal,
            &a.binding,
            a.kind,
        )
            .cmp(&(
                b.from.as_str(),
                b.to.as_str(),
                b.statement_ordinal,
                &b.binding,
                b.kind,
            ))
    });
    cut
}
