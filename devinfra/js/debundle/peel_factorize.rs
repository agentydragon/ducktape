//! Algorithmic peel proposer.
//!
//! Reads a debundle's `owner_graph.json` + the spec's
//! `modules/` tree, identifies the residual owners (those whose
//! members haven't been claimed by any *active* spec YAML), and
//! proposes coarse-grained module partitions over them using a
//! single principled objective: **agglomerate maximally subject
//! to a per-cell line-count ceiling**.
//!
//! # Scoping: what counts as residual?
//!
//! * **Active claims** (`*.yaml` outside `residual/`) are locked.
//!   Their bindings don't appear in any proposal.
//! * **Deferred modules** (`*.yaml.deferred` outside `residual/`)
//!   are **seed cells**: their bindings get pre-grouped into a
//!   single cell each. Proposals can *grow* a seed cell by
//!   absorbing residual bindings, or *merge* two seed cells when
//!   constraining edges link them — but the algorithm never
//!   splits a seed cell apart. This matches the spec author's
//!   intent that deferred groupings are "things that should
//!   travel together someday."
//! * **True residuals** (bindings in no YAML at all, plus
//!   anything under `residual/`) start as singleton cells.
//!
//! # Algorithm
//!
//! 1. **Residual scoping** (above).
//! 2. **Constraining-edge subgraph.** Edges where
//!    `constrains_init_order == true` between two residual
//!    vertices form the SCC condensation input. Edges from a
//!    residual vertex to an *active-claimed* vertex don't affect
//!    the partition but ARE counted per-cell for
//!    `edges_to_active_modules` (they're safe — active modules
//!    materialize before residual_entry, so reads to them don't
//!    cycle).
//! 3. **SCC condensation.** Tarjan on the residual-only
//!    constraining subgraph. Each non-singleton SCC becomes one
//!    mandatory cell (splitting a cycle would violate
//!    realizability). SCCs already over the line cap start
//!    `oversize: true`.
//! 4. **Greedy agglomeration.** While some pair of cells
//!    `(A, B)` share a constraining edge AND
//!    `lines(A) + lines(B) ≤ cap`, merge the pair with the most
//!    shared edges; tie-break by smaller minimum statement
//!    ordinal.
//! 5. **Emit.** Each cell becomes a proposal. Cells exceeding
//!    the cap are flagged `oversize: true` and emitted whole;
//!    the algorithm never manufactures structural splits.
//! 6. **Topological sort.** Cells with no outgoing
//!    inter-residual edges (= `landable_today`) come first;
//!    cells depending on them follow in DAG order. Within an
//!    equivalence class, sort by first source line.
//!
//! Output minimizes cell count subject to the constraining edges
//! and the line ceiling. One tuning knob (`size_cap_lines`); no
//! weighted score terms.

use std::cmp::Reverse;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fs;
use std::path::PathBuf;

use anyhow::{Context, Result};
use serde::Serialize;

use analysis::OwnerGraphReport;
use spec_modules::{load_active_claims, load_deferred_groups};

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
    pub active_claimed_binding_count: usize,
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
    /// Edges from this cell to OTHER residual cells. These are
    /// cycle-risk edges: promoting this cell to active while the
    /// pointed-at residual cells stay residual would create
    /// `<this>` → `residual_entry` reads, which the cycle gate
    /// will reject. Drives `landable_today`.
    pub edges_to_other_residual_cells: usize,
    /// Other residual cells (by proposed_module_id) this cell's
    /// outgoing constraining edges target. Empty iff
    /// `edges_to_other_residual_cells == 0`.
    pub other_residual_cells_referenced: Vec<String>,
    /// Edges from this cell to active-claimed bindings. Safe:
    /// active modules materialize before residual_entry, so reads
    /// to them don't cycle. Informational — useful for the
    /// reviewer to understand what subsystems the cell depends on.
    pub edges_to_active_modules: usize,
    /// Active module paths this cell's outgoing constraining edges
    /// target (deduplicated). Empty iff `edges_to_active_modules
    /// == 0`.
    pub active_modules_referenced: Vec<String>,
    /// `true` iff `edges_to_other_residual_cells == 0`. The cell
    /// can be promoted to an active `.yaml` right now without
    /// creating a cycle through `residual_entry`. `false` cells
    /// need their referenced residual cells promoted first (or
    /// the spec author makes them deferred for now).
    pub landable_today: bool,
    /// `true` when the cell's `size_lines_estimate` exceeds
    /// `size_cap_lines`. Caused by either a single owner whose
    /// body is itself >cap lines, or a constraining SCC whose
    /// collective body is >cap lines. The reviewer treats these
    /// as "structurally indivisible at this snapshot" rather than
    /// as actionable proposals.
    pub oversize: bool,
    /// Deferred module paths whose bindings ended up in this
    /// cell. Interpretation:
    ///
    /// * Empty → a brand-new cell composed purely of residual
    ///   singleton bindings.
    /// * One path → "grow this existing deferred module by
    ///   absorbing the residual bindings listed in
    ///   `binding_ids \ <deferred module's current members>`".
    ///   Or, if `binding_ids` matches the deferred module's
    ///   members exactly: just a candidate for promotion.
    /// * Two or more paths → "merge these deferred modules
    ///   together (and optionally add residual bindings)".
    pub seeded_from_deferred: Vec<String>,
}

pub fn analyze_peel_factorize(options: &PeelFactorizeOptions) -> Result<PeelFactorizeReport> {
    let graph: OwnerGraphReport = serde_json::from_str(
        &fs::read_to_string(&options.owner_graph_path)
            .with_context(|| format!("reading {}", options.owner_graph_path.display()))?,
    )
    .with_context(|| format!("parsing {}", options.owner_graph_path.display()))?;
    let claims = load_active_claims(&options.modules_root)?;
    let deferred = load_deferred_groups(&options.modules_root)?;
    Ok(factorize(
        &graph,
        &claims,
        &deferred,
        options.size_cap_lines,
    ))
}

pub fn factorize(
    graph: &OwnerGraphReport,
    active_claims: &BTreeMap<String, String>,
    deferred_groups: &BTreeMap<String, String>,
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
                .any(|b| !active_claims.contains_key(b.binding.as_str()))
        })
        .map(|(i, _)| i)
        .collect();

    // Owner -> active module path (for any owner whose binding lives
    // in an active YAML). Multi-binding owners take the first match's
    // module — in practice owners after Bucket-F split have one
    // binding each, so the choice is unambiguous.
    let owner_to_active_module: HashMap<usize, String> = graph
        .nodes
        .iter()
        .enumerate()
        .filter_map(|(i, node)| {
            node.declared_bindings
                .iter()
                .find_map(|b| active_claims.get(b.binding.as_str()))
                .map(|path| (i, path.clone()))
        })
        .collect();

    // SCC-building input: constraining edges that both endpoints are
    // in residual. Edges leaving the residual to active claims get
    // tracked separately below for `edges_to_active_modules`.
    let mut residual_constraining_edges: Vec<(usize, usize)> = Vec::new();
    let mut edges_to_active: Vec<(usize, String)> = Vec::new();
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
        if !residual.contains(&source) {
            continue;
        }
        if source == target {
            continue;
        }
        if residual.contains(&target) {
            residual_constraining_edges.push((source, target));
        } else if let Some(module_path) = owner_to_active_module.get(&target) {
            edges_to_active.push((source, module_path.clone()));
        }
    }

    let sccs = strongly_connected_components(&residual, &residual_constraining_edges);

    // Per-owner deferred-module attribution. Each residual owner
    // that has a binding in some deferred YAML is tagged with
    // that module path; the cell-formation step uses this to
    // force same-deferred owners into the same cell ("don't break
    // existing factors apart").
    let owner_to_deferred_module: HashMap<usize, String> = graph
        .nodes
        .iter()
        .enumerate()
        .filter_map(|(i, node)| {
            if !residual.contains(&i) {
                return None;
            }
            node.declared_bindings
                .iter()
                .find_map(|b| deferred_groups.get(b.binding.as_str()))
                .map(|path| (i, path.clone()))
        })
        .collect();

    let cells = form_cells_with_deferred_seeds(&sccs, &owner_to_deferred_module, graph);

    let mut cells = cells;
    agglomerate(&mut cells, &residual_constraining_edges, size_cap_lines);

    let proposals = emit_proposals(
        &cells,
        &residual_constraining_edges,
        &edges_to_active,
        graph,
        active_claims,
        deferred_groups,
        size_cap_lines,
    );
    PeelFactorizeReport {
        proposals,
        size_cap_lines,
        residual_owner_count: residual.len(),
        active_claimed_binding_count: active_claims.len(),
    }
}

/// Build the initial cell set. Starts from SCC condensation of the
/// residual constraining-edge subgraph; then merges any SCCs that
/// share owners with the same deferred-module attribution. Result:
/// every deferred module's members end up in one cell, with any
/// constraining-edge-tied residual owners pulled in alongside.
///
/// Cycle safety: merging SCCs that share a deferred grouping can't
/// introduce a cycle in the inter-cell DAG. If SCC X and SCC Y are
/// distinct (i.e. there's no constraining-edge cycle between them),
/// merging them into one cell is equivalent to adding an undirected
/// equivalence (deferred grouping), not a directed edge — the
/// constraining-edge DAG between cells is unaffected.
fn form_cells_with_deferred_seeds(
    sccs: &[Vec<usize>],
    owner_to_deferred_module: &HashMap<usize, String>,
    graph: &OwnerGraphReport,
) -> Vec<Cell> {
    let mut parent: Vec<usize> = (0..sccs.len()).collect();
    fn find(parent: &mut [usize], i: usize) -> usize {
        if parent[i] != i {
            let root = find(parent, parent[i]);
            parent[i] = root;
        }
        parent[i]
    }
    let mut owner_to_scc: HashMap<usize, usize> = HashMap::new();
    for (idx, scc) in sccs.iter().enumerate() {
        for &owner in scc {
            owner_to_scc.insert(owner, idx);
        }
    }
    let mut sccs_by_module: BTreeMap<&String, Vec<usize>> = BTreeMap::new();
    for (&owner, module) in owner_to_deferred_module {
        if let Some(&scc_idx) = owner_to_scc.get(&owner) {
            sccs_by_module.entry(module).or_default().push(scc_idx);
        }
    }
    for (_, scc_indices) in sccs_by_module {
        let mut iter = scc_indices.into_iter();
        let Some(first) = iter.next() else { continue };
        let root = find(&mut parent, first);
        for other in iter {
            let other_root = find(&mut parent, other);
            if other_root != root {
                parent[other_root] = root;
            }
        }
    }
    let mut grouped: BTreeMap<usize, Vec<usize>> = BTreeMap::new();
    for (scc_idx, scc) in sccs.iter().enumerate() {
        let root = find(&mut parent, scc_idx);
        grouped.entry(root).or_default().extend(scc);
    }
    grouped
        .into_values()
        .map(|owners| Cell::from_owners(owners, graph))
        .collect()
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

struct ProposalContext<'a> {
    graph: &'a OwnerGraphReport,
    residual_edges: &'a [(usize, usize)],
    active_edges: &'a [(usize, String)],
    active_claims: &'a BTreeMap<String, String>,
    deferred_groups: &'a BTreeMap<String, String>,
    owner_to_cell: HashMap<usize, usize>,
    size_cap_lines: usize,
}

fn emit_proposals(
    cells: &[Cell],
    residual_edges: &[(usize, usize)],
    active_edges: &[(usize, String)],
    graph: &OwnerGraphReport,
    active_claims: &BTreeMap<String, String>,
    deferred_groups: &BTreeMap<String, String>,
    size_cap_lines: usize,
) -> Vec<FactorizeProposal> {
    let mut owner_to_cell: HashMap<usize, usize> = HashMap::new();
    for (cell_idx, cell) in cells.iter().enumerate() {
        for &owner in &cell.owners {
            owner_to_cell.insert(owner, cell_idx);
        }
    }
    let ctx = ProposalContext {
        graph,
        residual_edges,
        active_edges,
        active_claims,
        deferred_groups,
        owner_to_cell,
        size_cap_lines,
    };

    let mut proposals: Vec<FactorizeProposal> = cells
        .iter()
        .enumerate()
        .map(|(cell_idx, cell)| build_proposal(cell_idx, cell, &ctx))
        .collect();

    // Topological-by-residual-dependency sort with source-line
    // tie-break. Each cell's depth = 1 + max(depth(c) for c in cells
    // it references via inter-residual edges). Cells with
    // `landable_today` (no inter-residual outgoing edges) get
    // depth 0 and emit first. Cycles between cells are impossible
    // at this stage — the SCC condensation pass collapsed every
    // residual-edge cycle into a single cell — so the recursion
    // bottoms out.
    let depths = compute_topo_depths(cells, residual_edges, &ctx.owner_to_cell);
    let mut indexed: Vec<(usize, FactorizeProposal)> = proposals.drain(..).enumerate().collect();
    indexed.sort_by(|(li, left), (ri, right)| {
        depths[*li].cmp(&depths[*ri]).then_with(|| {
            let lk = left
                .source_line_range
                .map(|range| range[0])
                .unwrap_or(usize::MAX);
            let rk = right
                .source_line_range
                .map(|range| range[0])
                .unwrap_or(usize::MAX);
            lk.cmp(&rk)
        })
    });

    // After topo-sort the cells are renumbered; rebuild the original
    // cell_idx → new_idx map so cross-references inside the
    // `other_residual_cells_referenced` lists point at the right
    // post-sort module IDs.
    let new_id_for: HashMap<usize, usize> = indexed
        .iter()
        .enumerate()
        .map(|(new_idx, (orig_idx, _))| (*orig_idx, new_idx))
        .collect();
    let mut out: Vec<FactorizeProposal> = indexed.into_iter().map(|(_, p)| p).collect();
    for (new_idx, proposal) in out.iter_mut().enumerate() {
        proposal.proposed_module_id = format!("auto_partition_{new_idx:04}");
        proposal.other_residual_cells_referenced = proposal
            .other_residual_cells_referenced
            .iter()
            .filter_map(|old_id| {
                let old_idx: usize = old_id.strip_prefix("auto_partition_")?.parse().ok()?;
                new_id_for
                    .get(&old_idx)
                    .map(|i| format!("auto_partition_{i:04}"))
            })
            .collect();
    }
    out
}

fn compute_topo_depths(
    cells: &[Cell],
    residual_edges: &[(usize, usize)],
    owner_to_cell: &HashMap<usize, usize>,
) -> Vec<usize> {
    let mut adj: Vec<BTreeSet<usize>> = vec![BTreeSet::new(); cells.len()];
    for &(s, t) in residual_edges {
        let (Some(&cs), Some(&ct)) = (owner_to_cell.get(&s), owner_to_cell.get(&t)) else {
            continue;
        };
        if cs != ct {
            adj[cs].insert(ct);
        }
    }
    let mut depths = vec![None; cells.len()];
    fn dfs(node: usize, adj: &[BTreeSet<usize>], depths: &mut [Option<usize>]) -> usize {
        if let Some(d) = depths[node] {
            return d;
        }
        // Mark as in-progress with depth 0; SCC condensation guarantees
        // the inter-cell graph is acyclic, so we never re-enter the
        // same node mid-DFS in a meaningful cycle.
        depths[node] = Some(0);
        let max_child = adj[node]
            .iter()
            .map(|&child| dfs(child, adj, depths))
            .max()
            .map(|d| d + 1)
            .unwrap_or(0);
        depths[node] = Some(max_child);
        max_child
    }
    for i in 0..cells.len() {
        dfs(i, &adj, &mut depths);
    }
    depths.into_iter().map(|d| d.unwrap_or(0)).collect()
}

fn build_proposal(cell_idx: usize, cell: &Cell, ctx: &ProposalContext) -> FactorizeProposal {
    let mut owner_ids: Vec<String> = Vec::with_capacity(cell.owners.len());
    let mut binding_ids: BTreeSet<String> = BTreeSet::new();
    let mut seeded: BTreeSet<String> = BTreeSet::new();
    let mut start_line = usize::MAX;
    let mut end_line = 0usize;
    let mut have_loc = false;
    let mut max_ordinal = 0usize;
    let mut min_ordinal = usize::MAX;
    for &owner_idx in &cell.owners {
        let node = &ctx.graph.nodes[owner_idx];
        owner_ids.push(node.id.clone());
        for binding in &node.declared_bindings {
            if !ctx.active_claims.contains_key(binding.binding.as_str()) {
                binding_ids.insert(binding.binding.clone());
            }
            if let Some(module_path) = ctx.deferred_groups.get(binding.binding.as_str()) {
                seeded.insert(module_path.clone());
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
    let mut to_residual = 0usize;
    let mut residual_targets: BTreeSet<usize> = BTreeSet::new();
    for &(s, t) in ctx.residual_edges {
        let (Some(&cs), Some(&ct)) = (ctx.owner_to_cell.get(&s), ctx.owner_to_cell.get(&t)) else {
            continue;
        };
        if cs == cell_idx && ct == cell_idx {
            internal += 1;
        } else if cs == cell_idx {
            to_residual += 1;
            residual_targets.insert(ct);
        }
    }
    let other_residual_cells_referenced: Vec<String> = residual_targets
        .into_iter()
        .map(|idx| format!("auto_partition_{idx:04}"))
        .collect();

    let mut to_active = 0usize;
    let mut active_targets: BTreeSet<String> = BTreeSet::new();
    for (source_owner, module_path) in ctx.active_edges {
        if ctx.owner_to_cell.get(source_owner) == Some(&cell_idx) {
            to_active += 1;
            active_targets.insert(module_path.clone());
        }
    }
    let active_modules_referenced: Vec<String> = active_targets.into_iter().collect();

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
        edges_to_other_residual_cells: to_residual,
        other_residual_cells_referenced,
        edges_to_active_modules: to_active,
        active_modules_referenced,
        landable_today: to_residual == 0,
        oversize: cell.lines > ctx.size_cap_lines,
        seeded_from_deferred: seeded.into_iter().collect(),
    }
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

    fn no_claims() -> BTreeMap<String, String> {
        BTreeMap::new()
    }

    #[test]
    fn factorize_emits_each_residual_owner_as_singleton_when_no_edges() {
        let graph = empty_graph(
            vec![owner("a", 1, &["a"], 10), owner("b", 2, &["b"], 10)],
            vec![],
        );
        let report = factorize(&graph, &no_claims(), &no_claims(), 2000);
        assert_eq!(report.residual_owner_count, 2);
        assert_eq!(report.proposals.len(), 2);
        assert!(report.proposals.iter().all(|p| p.size_members == 1));
        assert!(report.proposals.iter().all(|p| p.landable_today));
    }

    #[test]
    fn factorize_skips_active_claimed_owners() {
        let graph = empty_graph(
            vec![owner("a", 1, &["a"], 10), owner("b", 2, &["b"], 10)],
            vec![],
        );
        let claims = BTreeMap::from([("a".to_string(), "ui/x".to_string())]);
        let report = factorize(&graph, &claims, &no_claims(), 2000);
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
        let report = factorize(&graph, &no_claims(), &no_claims(), 2000);
        assert_eq!(report.proposals.len(), 1);
        assert_eq!(report.proposals[0].size_members, 3);
        assert_eq!(report.proposals[0].internal_edges, 2);
        assert_eq!(report.proposals[0].edges_to_other_residual_cells, 0);
        assert!(report.proposals[0].landable_today);
    }

    #[test]
    fn factorize_ignores_non_constraining_edges_for_merging() {
        let graph = empty_graph(
            vec![owner("a", 1, &["a"], 10), owner("b", 2, &["b"], 10)],
            vec![edge("e1", "a", "b", DepKind::LazyUse, false)],
        );
        let report = factorize(&graph, &no_claims(), &no_claims(), 2000);
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
        let report = factorize(&graph, &no_claims(), &no_claims(), 5);
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
        let report = factorize(&graph, &no_claims(), &no_claims(), 15);
        assert_eq!(report.proposals.len(), 3);
    }

    #[test]
    fn factorize_records_residual_edges_when_cells_dont_merge() {
        // Same chain but cap=20 — a+b can merge (lines 20), but
        // adding c would push to 30 > 20, so cell {a,b} keeps the
        // edge to c as an inter-residual edge.
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
        let report = factorize(&graph, &no_claims(), &no_claims(), 20);
        assert_eq!(report.proposals.len(), 2);
        let bigger = report
            .proposals
            .iter()
            .find(|p| p.size_members == 2)
            .expect("merged cell");
        assert_eq!(bigger.edges_to_other_residual_cells, 1);
        assert_eq!(bigger.other_residual_cells_referenced.len(), 1);
        assert!(!bigger.landable_today);
    }

    #[test]
    fn factorize_counts_edges_to_active_modules_separately_from_residual_edges() {
        // 2 residual owners (a, b) + 1 active-claimed binding (c).
        // a → b is a constraining edge that the agglomerator merges.
        // b → c is a constraining edge to an active module — safe;
        // counted under `edges_to_active_modules`, not blocking
        // promotion.
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
        let claims = BTreeMap::from([("c".to_string(), "ui/x".to_string())]);
        let report = factorize(&graph, &claims, &no_claims(), 2000);
        assert_eq!(report.proposals.len(), 1);
        let cell = &report.proposals[0];
        assert_eq!(cell.size_members, 2);
        assert_eq!(cell.edges_to_other_residual_cells, 0);
        assert_eq!(cell.edges_to_active_modules, 1);
        assert_eq!(cell.active_modules_referenced, vec!["ui/x".to_string()]);
        // Active edges are safe — promoting this cell today wouldn't
        // create a cycle.
        assert!(cell.landable_today);
    }

    #[test]
    fn factorize_treats_deferred_module_as_one_seed_cell() {
        // Owners a and c are in the same deferred module; owner b
        // sits between them with no constraining edges either way.
        // Without the deferred-seed rule, a and c would emerge as
        // separate singleton cells. The seed rule forces them into
        // one cell because they share `mod/x.yaml.deferred`.
        let graph = empty_graph(
            vec![
                owner("a", 1, &["a"], 10),
                owner("b", 2, &["b"], 10),
                owner("c", 3, &["c"], 10),
            ],
            vec![],
        );
        let deferred = BTreeMap::from([
            ("a".to_string(), "mod/x".to_string()),
            ("c".to_string(), "mod/x".to_string()),
        ]);
        let report = factorize(&graph, &no_claims(), &deferred, 2000);
        assert_eq!(report.proposals.len(), 2);
        let seeded = report
            .proposals
            .iter()
            .find(|p| !p.seeded_from_deferred.is_empty())
            .expect("one cell should be seeded");
        assert_eq!(seeded.binding_ids, vec!["a".to_string(), "c".to_string()],);
        assert_eq!(seeded.seeded_from_deferred, vec!["mod/x".to_string()]);
        let lone = report
            .proposals
            .iter()
            .find(|p| p.seeded_from_deferred.is_empty())
            .expect("one cell should not be seeded");
        assert_eq!(lone.binding_ids, vec!["b".to_string()]);
    }

    #[test]
    fn factorize_grows_seed_cell_with_residual_bindings_via_constraining_edges() {
        // Deferred module `mod/x` holds binding `a`. Owner `b` is a
        // pure residual binding that has a constraining edge to `a`.
        // Expected: one cell containing both, tagged as seeded
        // from `mod/x` — i.e., "grow mod/x by absorbing b".
        let graph = empty_graph(
            vec![owner("a", 1, &["a"], 10), owner("b", 2, &["b"], 10)],
            vec![edge("e1", "b", "a", DepKind::EagerUse, true)],
        );
        let deferred = BTreeMap::from([("a".to_string(), "mod/x".to_string())]);
        let report = factorize(&graph, &no_claims(), &deferred, 2000);
        assert_eq!(report.proposals.len(), 1);
        let cell = &report.proposals[0];
        assert_eq!(cell.size_members, 2);
        assert_eq!(cell.binding_ids, vec!["a".to_string(), "b".to_string()]);
        assert_eq!(cell.seeded_from_deferred, vec!["mod/x".to_string()]);
    }

    #[test]
    fn factorize_merges_two_seed_cells_when_constraining_edges_link_them() {
        // Two deferred modules X and Y, each with one binding. A
        // constraining edge between their owners. Expected: one
        // merged cell, seeded from both X and Y — i.e., "merge X
        // and Y".
        let graph = empty_graph(
            vec![owner("a", 1, &["a"], 10), owner("b", 2, &["b"], 10)],
            vec![edge("e1", "a", "b", DepKind::EagerUse, true)],
        );
        let deferred = BTreeMap::from([
            ("a".to_string(), "mod/x".to_string()),
            ("b".to_string(), "mod/y".to_string()),
        ]);
        let report = factorize(&graph, &no_claims(), &deferred, 2000);
        assert_eq!(report.proposals.len(), 1);
        let cell = &report.proposals[0];
        assert_eq!(cell.size_members, 2);
        assert_eq!(
            cell.seeded_from_deferred,
            vec!["mod/x".to_string(), "mod/y".to_string()],
        );
    }

    #[test]
    fn factorize_topo_orders_landable_cells_before_dependents() {
        // Three cells in a chain: a → b → c. With cap=15 they
        // can't merge. After SCC + topo order:
        //   * cell{c} has depth 0 (no outgoing residual edges).
        //   * cell{b} has depth 1 (points at c).
        //   * cell{a} has depth 2 (points at b).
        // Emitted order: c, b, a (assigned auto_partition_0000..0002).
        let graph = empty_graph(
            vec![
                owner("a", 10, &["a"], 10),
                owner("b", 20, &["b"], 10),
                owner("c", 30, &["c"], 10),
            ],
            vec![
                edge("e1", "a", "b", DepKind::EagerUse, true),
                edge("e2", "b", "c", DepKind::EagerUse, true),
            ],
        );
        let report = factorize(&graph, &no_claims(), &no_claims(), 15);
        assert_eq!(report.proposals.len(), 3);
        // First (depth 0): c — landable today.
        assert_eq!(report.proposals[0].binding_ids, vec!["c".to_string()]);
        assert!(report.proposals[0].landable_today);
        // Middle (depth 1): b — references the now-renamed cell c.
        assert_eq!(report.proposals[1].binding_ids, vec!["b".to_string()]);
        assert!(!report.proposals[1].landable_today);
        assert_eq!(
            report.proposals[1].other_residual_cells_referenced,
            vec!["auto_partition_0000".to_string()],
        );
        // Last (depth 2): a.
        assert_eq!(report.proposals[2].binding_ids, vec!["a".to_string()]);
        assert!(!report.proposals[2].landable_today);
        assert_eq!(
            report.proposals[2].other_residual_cells_referenced,
            vec!["auto_partition_0001".to_string()],
        );
    }
}
