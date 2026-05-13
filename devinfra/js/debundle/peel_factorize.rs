//! Algorithmic peel proposer.
//!
//! Reads a debundle's `owner_graph.json` + the spec's
//! `modules/` tree, identifies the residual owners (those whose
//! members haven't been claimed by any active or deferred spec
//! YAML, excluding `residual/*` catch-all files), and proposes
//! coarse-grained module partitions over them using a single
//! principled objective: **agglomerate maximally subject to a
//! per-cell line-count ceiling**.
//!
//! # Algorithm
//!
//! 1. **Residual scoping.** Each owner whose `declared_bindings`
//!    contains at least one binding not in
//!    `spec_modules::load_claimed_bindings(...)` is a residual
//!    vertex. Claimed bindings (active or non-residual deferred)
//!    are out of scope.
//! 2. **Constraining-edge subgraph.** Edges where
//!    `constrains_init_order == true` between two residual
//!    vertices are the only edges considered. Non-constraining
//!    edges (lazy reads, etc.) don't affect realizability so they
//!    don't influence the partition.
//! 3. **SCC condensation.** Tarjan on the constraining subgraph.
//!    Each non-singleton SCC becomes one mandatory cell — splitting
//!    a cycle would violate realizability so the algorithm can't
//!    propose breaking it apart. (SCCs that already exceed the
//!    line cap start `oversize: true`.)
//! 4. **Greedy agglomeration.** While some pair of cells
//!    `(A, B)` share at least one constraining edge AND
//!    `lines(A) + lines(B) ≤ cap`, merge the pair with the most
//!    shared edges; tie-break by smaller minimum statement
//!    ordinal. Loop terminates when no merge fits under the cap.
//! 5. **Emit.** Every cell becomes a proposal regardless of size.
//!    Cells exceeding the cap are flagged `oversize: true` and
//!    still emitted (the algorithm doesn't manufacture splits the
//!    structural graph doesn't suggest).
//!
//! The output minimizes cell count subject to the constraining
//! edges and the line ceiling. There's exactly one tuning knob
//! (`size_cap_lines`); no weighted score terms.

use std::cmp::Reverse;
use std::collections::{BTreeSet, HashMap};
use std::fs;
use std::path::PathBuf;

use anyhow::{Context, Result};
use serde::Serialize;

use analysis::OwnerGraphReport;
use spec_modules::load_claimed_bindings;

#[derive(Debug, Clone)]
pub struct PeelFactorizeOptions {
    pub owner_graph_path: PathBuf,
    pub modules_root: PathBuf,
    /// Hard ceiling (in summed source-line counts) per emitted cell.
    /// Cells that exceed the cap because their underlying SCC or
    /// single owner is itself larger get flagged `oversize: true`
    /// and emitted whole — the algorithm doesn't manufacture
    /// splits.
    pub size_cap_lines: usize,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct PeelFactorizeReport {
    pub proposals: Vec<FactorizeProposal>,
    pub size_cap_lines: usize,
    pub residual_owner_count: usize,
    pub claimed_binding_count: usize,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct FactorizeProposal {
    pub proposed_module_id: String,
    pub owner_ids: Vec<String>,
    pub binding_ids: Vec<String>,
    pub size_lines_estimate: usize,
    pub size_members: usize,
    /// `[start_line, end_line]` of the lowest-line and highest-line
    /// owner bodies. `None` when none of the cell's owners have
    /// a `source_location`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_line_range: Option<[usize; 2]>,
    pub ordinal_span: usize,
    pub internal_edges: usize,
    pub external_edges: usize,
    /// Other cell IDs (proposed or already-claimed) that the
    /// cell's outgoing constraining edges point at. Useful for
    /// the reviewer to spot grab-bag cells (`external > internal`).
    pub external_edge_targets: Vec<String>,
    /// `true` when the cell's `size_lines_estimate` exceeds
    /// `size_cap_lines`. Caused by either a single owner whose
    /// body is itself >cap lines, or a constraining SCC whose
    /// collective body is >cap lines. The reviewer treats these
    /// as "structurally indivisible at this snapshot" rather than
    /// as actionable proposals.
    pub oversize: bool,
}

pub fn analyze_peel_factorize(options: &PeelFactorizeOptions) -> Result<PeelFactorizeReport> {
    let graph: OwnerGraphReport = serde_json::from_str(
        &fs::read_to_string(&options.owner_graph_path)
            .with_context(|| format!("reading {}", options.owner_graph_path.display()))?,
    )
    .with_context(|| format!("parsing {}", options.owner_graph_path.display()))?;
    let claimed = load_claimed_bindings(&options.modules_root)?;
    Ok(factorize(&graph, &claimed, options.size_cap_lines))
}

pub fn factorize(
    graph: &OwnerGraphReport,
    claimed: &BTreeSet<String>,
    size_cap_lines: usize,
) -> PeelFactorizeReport {
    let owner_index: HashMap<&str, usize> = graph
        .nodes
        .iter()
        .enumerate()
        .map(|(i, node)| (node.id.as_str(), i))
        .collect();

    let residual: BTreeSet<usize> = graph
        .nodes
        .iter()
        .enumerate()
        .filter(|(_, node)| {
            node.declared_bindings
                .iter()
                .any(|b| !claimed.contains(b.binding.as_str()))
        })
        .map(|(i, _)| i)
        .collect();

    let mut constraining_edges: Vec<(usize, usize)> = Vec::new();
    for edge in &graph.edges {
        if !edge.constrains_init_order {
            continue;
        }
        let (Some(&source), Some(&target)) = (
            owner_index.get(edge.source.as_str()),
            owner_index.get(edge.target.as_str()),
        ) else {
            continue;
        };
        if !residual.contains(&source) || !residual.contains(&target) {
            continue;
        }
        if source == target {
            continue;
        }
        constraining_edges.push((source, target));
    }

    let sccs = strongly_connected_components(&residual, &constraining_edges);
    let mut cells: Vec<Cell> = sccs
        .into_iter()
        .map(|members| Cell::from_owners(members, graph))
        .collect();

    agglomerate(&mut cells, &constraining_edges, size_cap_lines);

    let proposals = emit_proposals(&cells, &constraining_edges, graph, claimed, size_cap_lines);
    PeelFactorizeReport {
        proposals,
        size_cap_lines,
        residual_owner_count: residual.len(),
        claimed_binding_count: claimed.len(),
    }
}

#[derive(Debug, Clone)]
struct Cell {
    owners: BTreeSet<usize>,
    lines: usize,
    min_ordinal: usize,
}

impl Cell {
    fn from_owners(members: Vec<usize>, graph: &OwnerGraphReport) -> Self {
        let mut owners = BTreeSet::new();
        let mut lines = 0;
        let mut min_ordinal = usize::MAX;
        for owner_idx in members {
            owners.insert(owner_idx);
            let node = &graph.nodes[owner_idx];
            lines += owner_line_count(node);
            min_ordinal = min_ordinal.min(node.statement_ordinal.0);
        }
        Self {
            owners,
            lines,
            min_ordinal,
        }
    }
}

fn owner_line_count(node: &analysis::OwnerGraphNodeReport) -> usize {
    node.source_location
        .as_ref()
        .map(|loc| {
            loc.end_line
                .saturating_sub(loc.start_line)
                .saturating_add(1)
        })
        .unwrap_or(0)
}

/// Tarjan's strongly connected components. Returns each SCC as a
/// vector of owner indices, sorted descending by SCC size so the
/// caller emits proposals in roughly large-first order. Owners
/// that don't appear in any edge each become their own singleton
/// SCC (so every residual owner shows up in exactly one cell).
fn strongly_connected_components(
    vertices: &BTreeSet<usize>,
    edges: &[(usize, usize)],
) -> Vec<Vec<usize>> {
    let mut adj: HashMap<usize, Vec<usize>> = HashMap::new();
    for &v in vertices {
        adj.entry(v).or_default();
    }
    for &(s, t) in edges {
        adj.entry(s).or_default().push(t);
    }

    let mut tarjan = Tarjan {
        adj,
        index: HashMap::new(),
        lowlink: HashMap::new(),
        on_stack: HashMap::new(),
        stack: Vec::new(),
        next_index: 0,
        sccs: Vec::new(),
    };
    for &v in vertices {
        if !tarjan.index.contains_key(&v) {
            tarjan.visit(v);
        }
    }
    let mut sccs = tarjan.sccs;
    sccs.sort_by_key(|scc| Reverse(scc.len()));
    sccs
}

struct Tarjan {
    adj: HashMap<usize, Vec<usize>>,
    index: HashMap<usize, usize>,
    lowlink: HashMap<usize, usize>,
    on_stack: HashMap<usize, bool>,
    stack: Vec<usize>,
    next_index: usize,
    sccs: Vec<Vec<usize>>,
}

impl Tarjan {
    fn visit(&mut self, v: usize) {
        self.index.insert(v, self.next_index);
        self.lowlink.insert(v, self.next_index);
        self.next_index += 1;
        self.stack.push(v);
        self.on_stack.insert(v, true);

        let neighbors: Vec<usize> = self.adj.get(&v).cloned().unwrap_or_default();
        for w in neighbors {
            if !self.index.contains_key(&w) {
                self.visit(w);
                let wl = *self.lowlink.get(&w).expect("dfs-visited");
                let vl = *self.lowlink.get(&v).expect("self");
                self.lowlink.insert(v, vl.min(wl));
            } else if *self.on_stack.get(&w).unwrap_or(&false) {
                let wi = *self.index.get(&w).expect("on-stack-visited");
                let vl = *self.lowlink.get(&v).expect("self");
                self.lowlink.insert(v, vl.min(wi));
            }
        }

        if self.lowlink.get(&v) == self.index.get(&v) {
            let mut scc = Vec::new();
            while let Some(w) = self.stack.pop() {
                self.on_stack.insert(w, false);
                scc.push(w);
                if w == v {
                    break;
                }
            }
            self.sccs.push(scc);
        }
    }
}

fn agglomerate(cells: &mut Vec<Cell>, edges: &[(usize, usize)], size_cap_lines: usize) {
    // Build owner -> cell index reverse map.
    let mut owner_to_cell: HashMap<usize, usize> = HashMap::new();
    for (cell_idx, cell) in cells.iter().enumerate() {
        for &owner in &cell.owners {
            owner_to_cell.insert(owner, cell_idx);
        }
    }

    loop {
        let mut shared: HashMap<(usize, usize), usize> = HashMap::new();
        for &(s, t) in edges {
            let (Some(&cs), Some(&ct)) = (owner_to_cell.get(&s), owner_to_cell.get(&t)) else {
                continue;
            };
            if cs == ct {
                continue;
            }
            let key = if cs < ct { (cs, ct) } else { (ct, cs) };
            *shared.entry(key).or_default() += 1;
        }

        let mut best: Option<((usize, usize), usize, usize)> = None;
        for (&(a, b), &count) in &shared {
            if cells[a].lines + cells[b].lines > size_cap_lines {
                continue;
            }
            let tie = cells[a].min_ordinal.min(cells[b].min_ordinal);
            let candidate = ((a, b), count, tie);
            best = match best {
                None => Some(candidate),
                Some((_, c, t)) if count > c || (count == c && tie < t) => Some(candidate),
                Some(_) => best,
            };
        }
        let Some(((a, b), _, _)) = best else {
            break;
        };

        let (keep, drop) = (a.min(b), a.max(b));
        let donor = cells.remove(drop);
        let target = &mut cells[keep];
        for owner in &donor.owners {
            owner_to_cell.insert(*owner, keep);
        }
        target.owners.extend(donor.owners);
        target.lines += donor.lines;
        target.min_ordinal = target.min_ordinal.min(donor.min_ordinal);
        // All owner_to_cell entries for cells > drop now point one
        // index too high; fix them.
        for (_, cell_idx) in owner_to_cell.iter_mut() {
            if *cell_idx > drop {
                *cell_idx -= 1;
            }
        }
    }
}

fn emit_proposals(
    cells: &[Cell],
    edges: &[(usize, usize)],
    graph: &OwnerGraphReport,
    claimed: &BTreeSet<String>,
    size_cap_lines: usize,
) -> Vec<FactorizeProposal> {
    let mut owner_to_cell: HashMap<usize, usize> = HashMap::new();
    for (cell_idx, cell) in cells.iter().enumerate() {
        for &owner in &cell.owners {
            owner_to_cell.insert(owner, cell_idx);
        }
    }

    let mut proposals: Vec<FactorizeProposal> = cells
        .iter()
        .enumerate()
        .map(|(cell_idx, cell)| {
            build_proposal(
                cell_idx,
                cell,
                edges,
                graph,
                claimed,
                &owner_to_cell,
                size_cap_lines,
            )
        })
        .collect();

    // Stable sort by the cell's first source line, then by min ordinal,
    // so deterministic output across runs.
    proposals.sort_by(|left, right| {
        let lk = left
            .source_line_range
            .map(|range| range[0])
            .unwrap_or(usize::MAX);
        let rk = right
            .source_line_range
            .map(|range| range[0])
            .unwrap_or(usize::MAX);
        lk.cmp(&rk)
    });
    for (idx, proposal) in proposals.iter_mut().enumerate() {
        proposal.proposed_module_id = format!("auto_partition_{idx:04}");
    }
    proposals
}

fn build_proposal(
    cell_idx: usize,
    cell: &Cell,
    edges: &[(usize, usize)],
    graph: &OwnerGraphReport,
    claimed: &BTreeSet<String>,
    owner_to_cell: &HashMap<usize, usize>,
    size_cap_lines: usize,
) -> FactorizeProposal {
    let mut owner_ids: Vec<String> = Vec::with_capacity(cell.owners.len());
    let mut binding_ids: BTreeSet<String> = BTreeSet::new();
    let mut start_line = usize::MAX;
    let mut end_line = 0usize;
    let mut have_loc = false;
    let mut max_ordinal = 0usize;
    let mut min_ordinal = usize::MAX;
    for &owner_idx in &cell.owners {
        let node = &graph.nodes[owner_idx];
        owner_ids.push(node.id.clone());
        for binding in &node.declared_bindings {
            if !claimed.contains(binding.binding.as_str()) {
                binding_ids.insert(binding.binding.clone());
            }
        }
        if let Some(loc) = &node.source_location {
            have_loc = true;
            start_line = start_line.min(loc.start_line);
            end_line = end_line.max(loc.end_line);
        }
        min_ordinal = min_ordinal.min(node.statement_ordinal.0);
        max_ordinal = max_ordinal.max(node.statement_ordinal.0);
    }
    owner_ids.sort();

    let mut internal = 0usize;
    let mut external = 0usize;
    let mut external_targets: BTreeSet<usize> = BTreeSet::new();
    for &(s, t) in edges {
        let (Some(&cs), Some(&ct)) = (owner_to_cell.get(&s), owner_to_cell.get(&t)) else {
            continue;
        };
        if cs == cell_idx && ct == cell_idx {
            internal += 1;
        } else if cs == cell_idx {
            external += 1;
            external_targets.insert(ct);
        }
    }
    let external_edge_targets: Vec<String> = external_targets
        .into_iter()
        .map(|idx| format!("auto_partition_{idx:04}"))
        .collect();

    FactorizeProposal {
        proposed_module_id: format!("auto_partition_{cell_idx:04}"),
        owner_ids,
        binding_ids: binding_ids.into_iter().collect(),
        size_lines_estimate: cell.lines,
        size_members: cell.owners.len(),
        source_line_range: if have_loc {
            Some([start_line, end_line])
        } else {
            None
        },
        ordinal_span: max_ordinal.saturating_sub(min_ordinal),
        internal_edges: internal,
        external_edges: external,
        external_edge_targets,
        oversize: cell.lines > size_cap_lines,
    }
}

pub fn render_factorize_report(report: &PeelFactorizeReport) -> String {
    let mut out = String::new();
    out.push_str(&format!(
        "Residual owners: {}    Claimed bindings: {}    Size cap: {} lines\n",
        report.residual_owner_count, report.claimed_binding_count, report.size_cap_lines,
    ));
    out.push_str(&format!(
        "Emitted {} cell{}; {} oversize.\n\n",
        report.proposals.len(),
        if report.proposals.len() == 1 { "" } else { "s" },
        report.proposals.iter().filter(|p| p.oversize).count(),
    ));
    out.push_str(&format!(
        "{:<22}  {:>7}  {:>7}  {:>5}  {:>5}  {}\n",
        "module_id", "members", "lines", "in", "out", "source_line_range",
    ));
    for proposal in &report.proposals {
        let range = match proposal.source_line_range {
            Some([start, end]) => format!("{start}-{end}"),
            None => "-".to_string(),
        };
        let flag = if proposal.oversize { " [oversize]" } else { "" };
        out.push_str(&format!(
            "{:<22}  {:>7}  {:>7}  {:>5}  {:>5}  {}{flag}\n",
            proposal.proposed_module_id,
            proposal.size_members,
            proposal.size_lines_estimate,
            proposal.internal_edges,
            proposal.external_edges,
            range,
        ));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use analysis::{
        BindingReport, DepKind, ModuleReportRef, OwnerGraphEdgeReport, OwnerGraphNodeReport,
        OwnerGraphPeelabilityReport, OwnerGraphQuotientReport, OwnerGraphReport, Purity,
        SourceLocation, StatementKind, StatementOrdinal,
    };

    fn binding(name: &str) -> BindingReport {
        BindingReport {
            binding: name.into(),
            export_name: name.into(),
        }
    }

    fn module_ref(label: &str) -> ModuleReportRef {
        ModuleReportRef {
            id: label.to_string(),
            label: label.to_string(),
            residual: label == "residual/unhandled",
            index: None,
            target_file: None,
        }
    }

    fn owner(
        id: &str,
        ordinal_value: usize,
        bindings: &[&str],
        lines: usize,
    ) -> OwnerGraphNodeReport {
        OwnerGraphNodeReport {
            id: id.to_string(),
            statement_ordinal: StatementOrdinal(ordinal_value),
            source_location: Some(SourceLocation {
                source_path: "x.js".to_string(),
                start_line: ordinal_value * 100,
                end_line: ordinal_value * 100 + lines.saturating_sub(1),
            }),
            declared_bindings: bindings.iter().map(|b| binding(b)).collect(),
            statement_kind: StatementKind::VarDecl,
            purity: Purity::Pure,
            destination: module_ref("residual/unhandled"),
        }
    }

    fn edge(
        id: &str,
        source: &str,
        target: &str,
        kind: DepKind,
        constrains: bool,
    ) -> OwnerGraphEdgeReport {
        OwnerGraphEdgeReport {
            id: id.to_string(),
            source: source.to_string(),
            target: target.to_string(),
            edge_kind: kind,
            binding: None,
            statement_ordinal: StatementOrdinal(0),
            constrains_init_order: constrains,
        }
    }

    fn empty_graph(
        nodes: Vec<OwnerGraphNodeReport>,
        edges: Vec<OwnerGraphEdgeReport>,
    ) -> OwnerGraphReport {
        OwnerGraphReport {
            chunk_id: "x".to_string(),
            nodes,
            edges,
            quotient: OwnerGraphQuotientReport {
                nodes: vec![],
                edges: vec![],
                sccs: vec![],
            },
            peelability: OwnerGraphPeelabilityReport {
                residual_destinations: vec![],
                minimal_peel_sets: vec![],
                residual_owner_horizon: vec![],
                evaluated_owner_sets: vec![],
            },
        }
    }

    #[test]
    fn factorize_emits_each_residual_owner_as_singleton_when_no_edges() {
        let graph = empty_graph(
            vec![owner("a", 1, &["a"], 10), owner("b", 2, &["b"], 10)],
            vec![],
        );
        let report = factorize(&graph, &BTreeSet::new(), 2000);
        assert_eq!(report.residual_owner_count, 2);
        assert_eq!(report.proposals.len(), 2);
        assert!(report.proposals.iter().all(|p| p.size_members == 1));
    }

    #[test]
    fn factorize_skips_claimed_owners() {
        let graph = empty_graph(
            vec![owner("a", 1, &["a"], 10), owner("b", 2, &["b"], 10)],
            vec![],
        );
        let report = factorize(&graph, &BTreeSet::from(["a".to_string()]), 2000);
        assert_eq!(report.residual_owner_count, 1);
        assert_eq!(report.proposals.len(), 1);
        assert_eq!(report.proposals[0].binding_ids, vec!["b".to_string()]);
    }

    #[test]
    fn factorize_merges_residual_owners_connected_by_constraining_edges() {
        let graph = empty_graph(
            vec![
                owner("a", 1, &["a"], 10),
                owner("b", 2, &["b"], 10),
                owner("c", 3, &["c"], 10),
            ],
            vec![
                edge("e1", "a", "b", DepKind::EagerUse, true),
                edge("e2", "b", "c", DepKind::EagerUse, true),
            ],
        );
        let report = factorize(&graph, &BTreeSet::new(), 2000);
        assert_eq!(report.proposals.len(), 1);
        assert_eq!(report.proposals[0].size_members, 3);
        assert_eq!(report.proposals[0].internal_edges, 2);
        assert_eq!(report.proposals[0].external_edges, 0);
    }

    #[test]
    fn factorize_ignores_non_constraining_edges_for_merging() {
        let graph = empty_graph(
            vec![owner("a", 1, &["a"], 10), owner("b", 2, &["b"], 10)],
            vec![edge("e1", "a", "b", DepKind::LazyUse, false)],
        );
        let report = factorize(&graph, &BTreeSet::new(), 2000);
        // Without a constraining edge to bind them, the two owners
        // stay separate cells.
        assert_eq!(report.proposals.len(), 2);
    }

    #[test]
    fn factorize_keeps_scc_as_one_mandatory_cell_even_if_over_cap() {
        // SCC: a → b → a forces both into one cell. With cap=5
        // and each owner taking 10 lines, the cell is oversize.
        let graph = empty_graph(
            vec![owner("a", 1, &["a"], 10), owner("b", 2, &["b"], 10)],
            vec![
                edge("e1", "a", "b", DepKind::EagerUse, true),
                edge("e2", "b", "a", DepKind::EagerUse, true),
            ],
        );
        let report = factorize(&graph, &BTreeSet::new(), 5);
        assert_eq!(report.proposals.len(), 1);
        assert!(report.proposals[0].oversize);
        assert_eq!(report.proposals[0].size_members, 2);
    }

    #[test]
    fn factorize_respects_size_cap_when_merging_singletons() {
        // 3 owners each 10 lines, chain a→b→c, cap=15.
        // Merging a+b yields 20 > 15, so no merge fits. All stay
        // separate.
        let graph = empty_graph(
            vec![
                owner("a", 1, &["a"], 10),
                owner("b", 2, &["b"], 10),
                owner("c", 3, &["c"], 10),
            ],
            vec![
                edge("e1", "a", "b", DepKind::EagerUse, true),
                edge("e2", "b", "c", DepKind::EagerUse, true),
            ],
        );
        let report = factorize(&graph, &BTreeSet::new(), 15);
        assert_eq!(report.proposals.len(), 3);
    }

    #[test]
    fn factorize_records_external_edges_when_cells_dont_merge() {
        // Same chain but cap=20 — a+b can merge (lines 20), but
        // adding c would push to 30 > 20, so cell {a,b} keeps the
        // edge to c as external.
        let graph = empty_graph(
            vec![
                owner("a", 1, &["a"], 10),
                owner("b", 2, &["b"], 10),
                owner("c", 3, &["c"], 10),
            ],
            vec![
                edge("e1", "a", "b", DepKind::EagerUse, true),
                edge("e2", "b", "c", DepKind::EagerUse, true),
            ],
        );
        let report = factorize(&graph, &BTreeSet::new(), 20);
        assert_eq!(report.proposals.len(), 2);
        let bigger = report
            .proposals
            .iter()
            .find(|p| p.size_members == 2)
            .expect("merged cell");
        assert_eq!(bigger.external_edges, 1);
        assert_eq!(bigger.external_edge_targets.len(), 1);
    }
}
