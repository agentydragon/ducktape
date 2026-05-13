//! CLI-facing peel proposer that **annotates** the analyzer's
//! SSOT factorize cells with spec-tree context (active claims,
//! deferred-module attribution) and cell-graph metrics.
//!
//! The cell algorithm itself (SCC condensation, rebind union,
//! agglomeration, auto-grow, SSOT predicate verdicts) lives in
//! `analysis::factorize` and runs at owner-graph build time. The
//! resulting `FactorizeReport` rides inside `OwnerGraphReport.factorize`.
//! This crate reads those precomputed cells and adds:
//!
//! - `seeded_from_deferred`: which `*.yaml.deferred` module paths
//!   contributed members to each cell. Empty means a brand-new
//!   cell purely from un-claimed residual bindings; one path means
//!   "grow this existing deferred module"; multiple means "merge
//!   these deferred modules together".
//! - `edges_to_active_modules` / `active_modules_referenced`:
//!   outgoing constraining edges from each cell to active-claimed
//!   binding modules (safe references — active modules materialize
//!   before residual_entry).
//! - `internal_edges`, `edges_to_other_residual_cells`,
//!   `other_residual_cells_referenced`: cell-graph relationship
//!   counts derived from the partition the analyzer chose.
//!
//! The `landable_today`, `emit_blocked_residual_bindings`, and
//! `oversize` verdicts come straight from the analyzer's SSOT cell
//! (matching the materializer's gate predicates exactly); we don't
//! recompute them.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fs;
use std::path::PathBuf;

use anyhow::{Context, Result};
use serde::Serialize;

use analysis::{FactorizeCell, OwnerGraphReport, RESIDUAL_ENTRY_MODULE_ID};
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
    /// Bindings declared by the cell's owners. Excludes
    /// anonymous side-effect statements, which appear under
    /// `anonymous_statement_owner_ids` instead.
    pub binding_ids: Vec<String>,
    /// Anonymous side-effect statements (owners with empty
    /// `declared_bindings`) in this cell. Lane workers materialize
    /// these via `anonymous_statements:` entries quoting the
    /// statement's source verbatim — the materializer's cycle
    /// gate counts these statements when determining whether the
    /// cell's promotion would cycle through `residual_entry`.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub anonymous_statement_owner_ids: Vec<String>,
    pub size_lines_estimate: usize,
    pub size_members: usize,
    /// `[start_line, end_line]` of the lowest-line and highest-line
    /// owner bodies. `None` when none of the cell's owners have
    /// a `source_location`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_line_range: Option<[usize; 2]>,
    pub ordinal_span: usize,
    pub internal_edges: usize,
    /// Edges from this cell to OTHER residual cells. Cycle-risk
    /// edges: promoting this cell to active while the pointed-at
    /// residual cells stay residual would create `<this>` →
    /// `residual_entry` reads, which the cycle gate will reject.
    pub edges_to_other_residual_cells: usize,
    /// Other residual cells (by proposed_module_id) this cell's
    /// outgoing constraining edges target.
    pub other_residual_cells_referenced: Vec<String>,
    /// Edges from this cell to active-claimed bindings. Safe:
    /// active modules materialize before residual_entry, so reads
    /// to them don't cycle. Informational.
    pub edges_to_active_modules: usize,
    /// Active module paths this cell's outgoing constraining edges
    /// target (deduplicated).
    pub active_modules_referenced: Vec<String>,
    /// Residual binding names this cell's bodies reference that
    /// **aren't** on entry's export list — neither in
    /// `pre_existing_entry_exports` from the upstream source nor
    /// auto-added because some currently-active module owns them.
    /// Promoting the cell to active without first arranging for
    /// these bindings to be exported by entry (e.g. by moving
    /// each binding's owner into this cell, or by separately
    /// promoting its current home) will be rejected by
    /// `materialize_logical_modules`'s emit-resolvability gate.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub emit_blocked_residual_bindings: Vec<String>,
    /// `true` iff both gates would pass:
    /// * `edges_to_other_residual_cells == 0` (cycle gate).
    /// * `emit_blocked_residual_bindings.is_empty()`
    ///   (emit-resolvability gate).
    ///
    /// Mirrors the predicates `materialize_logical_modules`
    /// applies; a `true` cell can be promoted to an active YAML
    /// right now without spec-level surgery. `false` cells need
    /// either their referenced residual cells / bindings landed
    /// first, or remain `.yaml.deferred` until the prerequisites
    /// move.
    pub landable_today: bool,
    /// `true` when the cell's `size_lines_estimate` exceeds
    /// `size_cap_lines`. Caused by either a single owner whose
    /// body is itself >cap lines, or a constraining SCC whose
    /// collective body is >cap lines. Treated as "structurally
    /// indivisible at this snapshot."
    pub oversize: bool,
    /// Deferred module paths whose bindings ended up in this
    /// cell:
    /// * Empty → a brand-new cell composed purely of residual
    ///   singleton bindings.
    /// * One path → "grow this existing deferred module by
    ///   absorbing the residual bindings listed in
    ///   `binding_ids \ <deferred module's current members>`".
    /// * Two or more paths → "merge these deferred modules
    ///   together (and optionally add residual bindings)".
    pub seeded_from_deferred: Vec<String>,
    /// When this proposal is **extending an existing active
    /// module** (i.e. the analyzer's supernode-aware factorize
    /// emitted an `extends_module_id` on the underlying cell),
    /// this carries the active module's id — passed through from
    /// `FactorizeCell::extends_module_id`. `None` for fresh-module
    /// proposals.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub extends_module_id: Option<String>,
    /// Loose owner ids (residual today) the analyzer's supernode-
    /// aware factorize identified as the extension's additions to
    /// the existing module. Empty for fresh-module proposals. Pass-
    /// through from `FactorizeCell::extension_owner_ids`.
    pub extension_owner_ids: Vec<String>,
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

    // Residual scope mirrors the analyzer's SSOT residual definition:
    // owners whose post-spec destination is the implicit
    // `<residual_entry>` (ModuleId::ResidualEntry), including anonymous
    // side-effect statements.
    let residual: BTreeSet<usize> = graph
        .nodes
        .iter()
        .enumerate()
        .filter(|(_, node)| node.destination.id == RESIDUAL_ENTRY_MODULE_ID)
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

    // Cell partition is the analyzer's SSOT verdict: each
    // `FactorizeCell` carries its owner_ids (graph node ids) along
    // with the materializer's gate verdict (landable, oversize,
    // emit-blocked bindings). Translate to internal `Cell` form
    // (owner indices into `graph.nodes`) and pair each with a small
    // `Verdict` capturing the analyzer's gate result. After
    // agglomeration this verdict gets replaced with a synthesized
    // one on merged cells.
    let mut cells: Vec<(Cell, Verdict)> = graph
        .factorize
        .cells
        .iter()
        .map(|cell| {
            (
                cell_from_factorize_cell(cell, graph, &owner_index),
                Verdict {
                    landable_today: cell.landable_today,
                    emit_blocked_residual_bindings: cell.emit_blocked_residual_bindings.clone(),
                },
            )
        })
        .collect();

    // Per-cell edge accounting. We walk every constraining edge
    // once, classifying the source-cell / target-cell pair into:
    // - internal (same cell, residual)            → cell.internal_edges
    // - inter-residual (different residual cells) → cell.edges_to_other_residual_cells
    // - cell → active claim                       → cell.edges_to_active_modules
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
        if !residual.contains(&source) || source == target {
            continue;
        }
        if residual.contains(&target) {
            residual_constraining_edges.push((source, target));
        } else if let Some(module_path) = owner_to_active_module.get(&target) {
            edges_to_active.push((source, module_path.clone()));
        }
    }

    // Agglomeration pass: greedy merging of landable cells along
    // inter-cell constraining edges, up to `size_cap_lines`. The
    // analyzer's closure cells are minimal (one SCC per cell);
    // landable singletons that share constraining edges get
    // combined into useful module-sized factors here. Validity is
    // preserved by only merging cells whose union remains landable
    // (no cycle gate trigger) — see `agglomerate_landable_cells`.
    agglomerate_landable_cells(&mut cells, &residual_constraining_edges, size_cap_lines);

    let proposals = emit_proposals(
        &cells,
        &residual_constraining_edges,
        &edges_to_active,
        graph,
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

/// Translate the analyzer-side `FactorizeCell` shape (string owner
/// ids + SSOT verdict) into the CLI's internal `Cell` shape (owner
/// indices into `graph.nodes` + cached line count). `FactorizeCell`
/// entries whose `owner_ids` don't resolve via `owner_index` get
/// silently dropped — that shouldn't happen for any well-formed
/// report but stays defensive against report-shape drift.
fn cell_from_factorize_cell(
    cell: &FactorizeCell,
    graph: &OwnerGraphReport,
    owner_index: &HashMap<&str, usize>,
) -> Cell {
    let owners: BTreeSet<usize> = cell
        .owner_ids
        .iter()
        .filter_map(|id| owner_index.get(id.as_str()).copied())
        .collect();
    let lines = owners
        .iter()
        .map(|&i| owner_line_count(&graph.nodes[i]))
        .sum();
    let extension_owner_idxs: BTreeSet<usize> = cell
        .extension_owner_ids
        .iter()
        .filter_map(|id| owner_index.get(id.as_str()).copied())
        .collect();
    Cell {
        owners,
        lines,
        extends_module_id: cell.extends_module_id.clone(),
        extension_owner_idxs,
    }
}

#[derive(Debug, Clone)]
struct Cell {
    owners: BTreeSet<usize>,
    lines: usize,
    /// Pass-through from `FactorizeCell::extends_module_id` — the
    /// analyzer-side cell's supernode target, if any. Preserved
    /// across agglomeration only when every merged cell points at
    /// the same module.
    extends_module_id: Option<String>,
    /// Pass-through from `FactorizeCell::extension_owner_ids`,
    /// translated to owner indices.
    extension_owner_idxs: BTreeSet<usize>,
}

/// Per-cell gate result. For original closure cells, this mirrors
/// the analyzer's `FactorizeCell`. For agglomerated cells produced
/// by `agglomerate_landable_cells`, this is synthesized: only
/// landable cells get merged, and merging two landable cells whose
/// union is still landable produces another landable cell with
/// empty `emit_blocked_residual_bindings`.
#[derive(Debug, Clone)]
struct Verdict {
    landable_today: bool,
    emit_blocked_residual_bindings: Vec<String>,
}

/// Greedy agglomeration of landable closure cells along inter-cell
/// constraining edges. Modifies `cells` in place: merged cells are
/// rolled into earlier indices, dropped cells are removed.
///
/// A merge of two landable cells A, B is safe iff `A ∪ B` is itself
/// landable. Since A and B are individually landable (cycle gate
/// passes), each has external residual edges flowing in one
/// direction only. The merge stays landable iff the combined cell
/// still has external edges in only one direction. Equivalently:
/// `(out[A] ∪ out[B]) \ {A, B}` is empty OR
/// `(in[A] ∪ in[B]) \ {A, B}` is empty.
///
/// Bounded by `size_cap_lines`: the merged cell's line count must
/// not exceed the cap. Non-landable cells are never merged.
fn agglomerate_landable_cells(
    cells: &mut Vec<(Cell, Verdict)>,
    residual_constraining_edges: &[(usize, usize)],
    size_cap_lines: usize,
) {
    if cells.is_empty() {
        return;
    }
    let n = cells.len();

    // Owner-idx → closure cell idx.
    let mut owner_to_cell: HashMap<usize, usize> = HashMap::new();
    for (idx, (cell, _)) in cells.iter().enumerate() {
        for &o in &cell.owners {
            owner_to_cell.insert(o, idx);
        }
    }

    // Cell-level edges (closure cell idx). Maintained per root via
    // the union-find below.
    let mut out_neighbors: Vec<BTreeSet<usize>> = vec![BTreeSet::new(); n];
    let mut in_neighbors: Vec<BTreeSet<usize>> = vec![BTreeSet::new(); n];
    for &(s, t) in residual_constraining_edges {
        let (Some(&cs), Some(&ct)) = (owner_to_cell.get(&s), owner_to_cell.get(&t)) else {
            continue;
        };
        if cs != ct {
            out_neighbors[cs].insert(ct);
            in_neighbors[ct].insert(cs);
        }
    }

    let mut parent: Vec<usize> = (0..n).collect();

    fn find(parent: &mut [usize], i: usize) -> usize {
        let mut r = i;
        while parent[r] != r {
            r = parent[r];
        }
        let mut x = i;
        while parent[x] != r {
            let next = parent[x];
            parent[x] = r;
            x = next;
        }
        r
    }

    // Greedy merge. Iterate edges; for each pair of cells joined by
    // an edge, attempt a merge. Repeat until a pass yields no
    // merges. Each merge reduces the cell count by 1, so the loop
    // is bounded by `n - 1` iterations across all passes.
    loop {
        let mut merged_any = false;
        for &(s, t) in residual_constraining_edges {
            let (Some(&cs), Some(&ct)) = (owner_to_cell.get(&s), owner_to_cell.get(&t)) else {
                continue;
            };
            let ra = find(&mut parent, cs);
            let rb = find(&mut parent, ct);
            if ra == rb {
                continue;
            }
            if !cells[ra].1.landable_today || !cells[rb].1.landable_today {
                continue;
            }
            if cells[ra].0.lines + cells[rb].0.lines > size_cap_lines {
                continue;
            }
            // Check that the merged cell stays landable. Resolve
            // each neighbor through find() so stale roots from
            // earlier merges don't show up as phantom externals.
            let mut merged_out: BTreeSet<usize> = BTreeSet::new();
            for &n in out_neighbors[ra].iter().chain(out_neighbors[rb].iter()) {
                let rn = find(&mut parent, n);
                if rn != ra && rn != rb {
                    merged_out.insert(rn);
                }
            }
            let mut merged_in: BTreeSet<usize> = BTreeSet::new();
            for &n in in_neighbors[ra].iter().chain(in_neighbors[rb].iter()) {
                let rn = find(&mut parent, n);
                if rn != ra && rn != rb {
                    merged_in.insert(rn);
                }
            }
            if !merged_out.is_empty() && !merged_in.is_empty() {
                continue;
            }
            // Merge: smaller index becomes the root.
            let (keep, drop) = if ra < rb { (ra, rb) } else { (rb, ra) };
            parent[drop] = keep;
            let drop_owners = std::mem::take(&mut cells[drop].0.owners);
            cells[keep].0.owners.extend(drop_owners);
            let drop_lines = cells[drop].0.lines;
            cells[keep].0.lines += drop_lines;
            cells[drop].0.lines = 0;
            // Extension info survives only if both sides point at
            // the same supernode; otherwise the merged cell is no
            // longer a clean "extend module X" proposal.
            let drop_extends = cells[drop].0.extends_module_id.take();
            let drop_extension_owners = std::mem::take(&mut cells[drop].0.extension_owner_idxs);
            if cells[keep].0.extends_module_id == drop_extends {
                cells[keep]
                    .0
                    .extension_owner_idxs
                    .extend(drop_extension_owners);
            } else {
                cells[keep].0.extends_module_id = None;
                cells[keep].0.extension_owner_idxs.clear();
            }
            // Drop's emit-blocked is empty (landable) so no merge needed there.
            out_neighbors[keep] = merged_out;
            in_neighbors[keep] = merged_in;
            out_neighbors[drop].clear();
            in_neighbors[drop].clear();
            merged_any = true;
        }
        if !merged_any {
            break;
        }
    }

    // Collapse: keep only root cells.
    let roots: Vec<usize> = (0..n).map(|i| find(&mut parent, i)).collect();
    let mut kept = Vec::with_capacity(n);
    for (i, entry) in cells.drain(..).enumerate() {
        if roots[i] == i {
            kept.push(entry);
        }
    }
    *cells = kept;
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

struct ProposalContext<'a> {
    graph: &'a OwnerGraphReport,
    residual_edges: &'a [(usize, usize)],
    active_edges: &'a [(usize, String)],
    deferred_groups: &'a BTreeMap<String, String>,
    owner_to_cell: HashMap<usize, usize>,
    size_cap_lines: usize,
}

fn emit_proposals(
    cells: &[(Cell, Verdict)],
    residual_edges: &[(usize, usize)],
    active_edges: &[(usize, String)],
    graph: &OwnerGraphReport,
    deferred_groups: &BTreeMap<String, String>,
    size_cap_lines: usize,
) -> Vec<FactorizeProposal> {
    let mut owner_to_cell: HashMap<usize, usize> = HashMap::new();
    for (cell_idx, (cell, _)) in cells.iter().enumerate() {
        for &owner in &cell.owners {
            owner_to_cell.insert(owner, cell_idx);
        }
    }

    let ctx = ProposalContext {
        graph,
        residual_edges,
        active_edges,
        deferred_groups,
        owner_to_cell,
        size_cap_lines,
    };

    let mut proposals: Vec<FactorizeProposal> = cells
        .iter()
        .enumerate()
        .map(|(cell_idx, (cell, verdict))| build_proposal(cell_idx, cell, verdict, &ctx))
        .collect();

    // Topological-by-residual-dependency sort with source-line
    // tie-break. Each cell's depth = 1 + max(depth(c) for c in cells
    // it references via inter-residual edges). Cells with
    // `landable_today` (no inter-residual outgoing edges) get
    // depth 0 and emit first. Cycles between cells are impossible
    // at this stage — the SCC condensation pass collapsed every
    // residual-edge cycle into a single cell — so the recursion
    // bottoms out.
    let depths = compute_topo_depths(cells.len(), residual_edges, &ctx.owner_to_cell);
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
    let mut fresh_counter = 0usize;
    for proposal in out.iter_mut() {
        if proposal.extends_module_id.is_none() {
            proposal.proposed_module_id = format!("auto_partition_{fresh_counter:04}");
            fresh_counter += 1;
        }
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
    cell_count: usize,
    residual_edges: &[(usize, usize)],
    owner_to_cell: &HashMap<usize, usize>,
) -> Vec<usize> {
    let mut adj: Vec<BTreeSet<usize>> = vec![BTreeSet::new(); cell_count];
    for &(s, t) in residual_edges {
        let (Some(&cs), Some(&ct)) = (owner_to_cell.get(&s), owner_to_cell.get(&t)) else {
            continue;
        };
        if cs != ct {
            adj[cs].insert(ct);
        }
    }
    let mut depths = vec![None; cell_count];
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
    for i in 0..cell_count {
        dfs(i, &adj, &mut depths);
    }
    depths.into_iter().map(|d| d.unwrap_or(0)).collect()
}

fn build_proposal(
    cell_idx: usize,
    cell: &Cell,
    verdict: &Verdict,
    ctx: &ProposalContext,
) -> FactorizeProposal {
    let mut owner_ids: Vec<String> = Vec::with_capacity(cell.owners.len());
    let mut anonymous_owner_ids: Vec<String> = Vec::new();
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
        if node.declared_bindings.is_empty() {
            anonymous_owner_ids.push(node.id.clone());
        }
        for binding in &node.declared_bindings {
            binding_ids.insert(binding.binding.clone());
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
    anonymous_owner_ids.sort();

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

    // `landable_today`, `emit_blocked_residual_bindings`, and
    // `oversize` come straight from the analyzer's SSOT verdict on
    // this cell (computed once via
    // `peelability::evaluate_peel_candidate` at owner-graph
    // build time). The CLI used to recompute them from the JSON
    // shape, which drifted from the predicate on edges through
    // pre-existing entry exports (the recompute treated those as
    // residual_entry cycles even though entry mediates them).
    let emit_blocked_residual_bindings: Vec<String> =
        verdict.emit_blocked_residual_bindings.clone();
    let extension_owner_ids: Vec<String> = {
        let mut ids: Vec<String> = cell
            .extension_owner_idxs
            .iter()
            .map(|&idx| ctx.graph.nodes[idx].id.clone())
            .collect();
        ids.sort();
        ids
    };
    let proposed_module_id = match &cell.extends_module_id {
        Some(target) => format!("extend:{target}"),
        None => format!("auto_partition_{cell_idx:04}"),
    };
    FactorizeProposal {
        proposed_module_id,
        owner_ids,
        binding_ids: binding_ids.into_iter().collect(),
        anonymous_statement_owner_ids: anonymous_owner_ids,
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
        emit_blocked_residual_bindings,
        landable_today: verdict.landable_today,
        oversize: cell.lines > ctx.size_cap_lines,
        seeded_from_deferred: seeded.into_iter().collect(),
        extends_module_id: cell.extends_module_id.clone(),
        extension_owner_ids,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use analysis::{
        BindingReport, DepKind, FactorizeCell, FactorizeReport, ModuleReportRef,
        OwnerGraphEdgeReport, OwnerGraphNodeReport, OwnerGraphPeelabilityReport,
        OwnerGraphQuotientReport, OwnerGraphReport, PeelCandidateStatus, Purity,
        RESIDUAL_ENTRY_LABEL, SourceLocation, StatementKind, StatementOrdinal,
    };
    use spec::DEFAULT_RESIDUAL_MODULE_PATH;

    fn binding(name: &str) -> BindingReport {
        BindingReport {
            binding: name.into(),
            export_name: name.into(),
        }
    }

    fn module_ref(label: &str) -> ModuleReportRef {
        // Match the production `module_key` shape: the implicit
        // residual entry's id is `RESIDUAL_ENTRY_MODULE_ID`, not
        // the path-style `DEFAULT_RESIDUAL_MODULE_PATH` label some
        // test fixtures use as a catch-all sentinel.
        let is_residual = label == DEFAULT_RESIDUAL_MODULE_PATH || label == RESIDUAL_ENTRY_LABEL;
        ModuleReportRef {
            id: if is_residual {
                RESIDUAL_ENTRY_MODULE_ID.to_string()
            } else {
                label.to_string()
            },
            label: label.to_string(),
            residual: is_residual,
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
        owner_at(
            id,
            ordinal_value,
            bindings,
            lines,
            DEFAULT_RESIDUAL_MODULE_PATH,
        )
    }

    fn owner_in_active_module(
        id: &str,
        ordinal_value: usize,
        bindings: &[&str],
        lines: usize,
        module_path: &str,
    ) -> OwnerGraphNodeReport {
        owner_at(id, ordinal_value, bindings, lines, module_path)
    }

    fn owner_at(
        id: &str,
        ordinal_value: usize,
        bindings: &[&str],
        lines: usize,
        destination_label: &str,
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
            destination: module_ref(destination_label),
        }
    }

    fn edge(
        id: &str,
        source: &str,
        target: &str,
        kind: DepKind,
        constrains: bool,
    ) -> OwnerGraphEdgeReport {
        edge_for_binding(id, source, target, kind, constrains, None)
    }

    fn edge_for_binding(
        id: &str,
        source: &str,
        target: &str,
        kind: DepKind,
        constrains: bool,
        binding: Option<&str>,
    ) -> OwnerGraphEdgeReport {
        OwnerGraphEdgeReport {
            id: id.to_string(),
            source: source.to_string(),
            target: target.to_string(),
            edge_kind: kind,
            binding: binding.map(str::to_string),
            statement_ordinal: StatementOrdinal(0),
            constrains_init_order: constrains,
        }
    }

    fn graph_with_cells(
        nodes: Vec<OwnerGraphNodeReport>,
        edges: Vec<OwnerGraphEdgeReport>,
        cells: Vec<FactorizeCell>,
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
            pre_existing_entry_exports: vec![],
            factorize: FactorizeReport {
                size_cap_lines: 2000,
                residual_owner_count: nodes_residual_count(&[]),
                cells,
            },
        }
    }

    fn nodes_residual_count(_nodes: &[OwnerGraphNodeReport]) -> usize {
        // Stub: the field is informational on the analyzer side;
        // tests don't assert on it.
        0
    }

    /// Build a `FactorizeCell` mirroring the shape the analyzer
    /// would emit for a given owner set. `proposed_module_id`,
    /// `binding_ids`, `anonymous_statement_owner_ids`, and
    /// `source_line_range` are derived from the matching nodes
    /// so callers only need to spell out the owner partition and
    /// the status verdict.
    fn cell(
        id: &str,
        owner_ids: &[&str],
        nodes: &[OwnerGraphNodeReport],
        status: PeelCandidateStatus,
        emit_blocked: &[&str],
    ) -> FactorizeCell {
        let owners: Vec<String> = owner_ids.iter().map(|s| s.to_string()).collect();
        let mut bindings: Vec<String> = Vec::new();
        let mut anonymous: Vec<String> = Vec::new();
        let mut size_lines = 0usize;
        let mut start = usize::MAX;
        let mut end = 0usize;
        let mut have_loc = false;
        let mut min_ord = usize::MAX;
        let mut max_ord = 0usize;
        for owner_id in &owners {
            let node = nodes
                .iter()
                .find(|n| &n.id == owner_id)
                .unwrap_or_else(|| panic!("cell owner {owner_id} not in nodes"));
            if node.declared_bindings.is_empty() {
                anonymous.push(node.id.clone());
            }
            for b in &node.declared_bindings {
                bindings.push(b.binding.clone());
            }
            if let Some(loc) = &node.source_location {
                have_loc = true;
                start = start.min(loc.start_line);
                end = end.max(loc.end_line);
                size_lines += loc.end_line + 1 - loc.start_line;
            }
            min_ord = min_ord.min(node.statement_ordinal.0);
            max_ord = max_ord.max(node.statement_ordinal.0);
        }
        bindings.sort();
        bindings.dedup();
        anonymous.sort();
        let landable = matches!(status, PeelCandidateStatus::PeelableNow);
        FactorizeCell {
            proposed_module_id: id.to_string(),
            owner_ids: owners,
            binding_ids: bindings,
            anonymous_statement_owner_ids: anonymous,
            size_lines_estimate: size_lines,
            size_members: owner_ids.len(),
            source_line_range: have_loc.then_some([start, end]),
            ordinal_span: max_ord.saturating_sub(min_ord),
            status,
            landable_today: landable,
            oversize: false,
            emit_blocked_residual_bindings: emit_blocked.iter().map(|s| s.to_string()).collect(),
            cycle_blocker_owner_ids: Vec::new(),
            active_modules_referenced: Vec::new(),
            extends_module_id: None,
            extension_owner_ids: Vec::new(),
        }
    }

    fn no_claims() -> BTreeMap<String, String> {
        BTreeMap::new()
    }

    #[test]
    fn empty_factorize_cells_produce_no_proposals() {
        let graph = graph_with_cells(
            vec![owner("a", 1, &["a"], 10), owner("b", 2, &["b"], 10)],
            vec![],
            vec![],
        );
        let report = factorize(&graph, &no_claims(), &no_claims(), 2000);
        assert_eq!(report.residual_owner_count, 2);
        assert!(report.proposals.is_empty());
    }

    #[test]
    fn singleton_cells_pass_through_with_landable_verdict() {
        let nodes = vec![owner("a", 1, &["a"], 10), owner("b", 2, &["b"], 10)];
        let cells = vec![
            cell(
                "auto_partition_0000",
                &["a"],
                &nodes,
                PeelCandidateStatus::PeelableNow,
                &[],
            ),
            cell(
                "auto_partition_0001",
                &["b"],
                &nodes,
                PeelCandidateStatus::PeelableNow,
                &[],
            ),
        ];
        let graph = graph_with_cells(nodes, vec![], cells);
        let report = factorize(&graph, &no_claims(), &no_claims(), 2000);
        assert_eq!(report.proposals.len(), 2);
        assert!(report.proposals.iter().all(|p| p.size_members == 1));
        assert!(report.proposals.iter().all(|p| p.landable_today));
    }

    #[test]
    fn deferred_group_attribution_is_annotated_per_cell() {
        // Two cells. Cell 0 contains binding `a` which is in
        // `mod/x.yaml.deferred`. Cell 1 contains binding `b` which
        // isn't in any deferred group. Expected: cell 0 reports
        // `seeded_from_deferred = ["mod/x"]`; cell 1 reports empty.
        let nodes = vec![owner("a", 1, &["a"], 10), owner("b", 2, &["b"], 10)];
        let cells = vec![
            cell(
                "auto_partition_0000",
                &["a"],
                &nodes,
                PeelCandidateStatus::PeelableNow,
                &[],
            ),
            cell(
                "auto_partition_0001",
                &["b"],
                &nodes,
                PeelCandidateStatus::PeelableNow,
                &[],
            ),
        ];
        let graph = graph_with_cells(nodes, vec![], cells);
        let deferred = BTreeMap::from([("a".to_string(), "mod/x".to_string())]);
        let report = factorize(&graph, &no_claims(), &deferred, 2000);
        let cell_a = report
            .proposals
            .iter()
            .find(|p| p.binding_ids.contains(&"a".to_string()))
            .expect("cell containing a");
        assert_eq!(cell_a.seeded_from_deferred, vec!["mod/x".to_string()]);
        let cell_b = report
            .proposals
            .iter()
            .find(|p| p.binding_ids.contains(&"b".to_string()))
            .expect("cell containing b");
        assert!(cell_b.seeded_from_deferred.is_empty());
    }

    #[test]
    fn multi_deferred_module_merge_is_annotated_with_all_paths() {
        // Single cell containing bindings `a` (deferred `mod/x`)
        // and `b` (deferred `mod/y`). The cell is the result of the
        // analyzer merging the two deferred members via a
        // constraining edge. The CLI surfaces both deferred paths
        // — signal to the spec author that promoting this cell
        // means "merge mod/x and mod/y together".
        let nodes = vec![owner("a", 1, &["a"], 10), owner("b", 2, &["b"], 10)];
        let cells = vec![cell(
            "auto_partition_0000",
            &["a", "b"],
            &nodes,
            PeelCandidateStatus::PeelableNow,
            &[],
        )];
        let graph = graph_with_cells(nodes, vec![], cells);
        let deferred = BTreeMap::from([
            ("a".to_string(), "mod/x".to_string()),
            ("b".to_string(), "mod/y".to_string()),
        ]);
        let report = factorize(&graph, &no_claims(), &deferred, 2000);
        assert_eq!(report.proposals.len(), 1);
        assert_eq!(
            report.proposals[0].seeded_from_deferred,
            vec!["mod/x".to_string(), "mod/y".to_string()],
        );
    }

    #[test]
    fn inter_cell_constraining_edges_are_counted_per_proposal() {
        // Two cells (a, b) with a single constraining edge a → b.
        // Cell 0 (a) reports edges_to_other_residual_cells=1
        // pointing at cell 1 (b). Cell 1 reports 0.
        let nodes = vec![owner("a", 1, &["a"], 10), owner("b", 2, &["b"], 10)];
        let edges = vec![edge("e1", "a", "b", DepKind::EagerUse, true)];
        let cells = vec![
            cell(
                "auto_partition_0000",
                &["a"],
                &nodes,
                PeelCandidateStatus::BlockedResidualDependency,
                &[],
            ),
            cell(
                "auto_partition_0001",
                &["b"],
                &nodes,
                PeelCandidateStatus::PeelableNow,
                &[],
            ),
        ];
        let graph = graph_with_cells(nodes, edges, cells);
        let report = factorize(&graph, &no_claims(), &no_claims(), 2000);
        let by_binding = |b: &str| -> &FactorizeProposal {
            report
                .proposals
                .iter()
                .find(|p| p.binding_ids.contains(&b.to_string()))
                .expect("cell")
        };
        assert_eq!(by_binding("a").edges_to_other_residual_cells, 1);
        assert_eq!(
            by_binding("a").other_residual_cells_referenced,
            vec![by_binding("b").proposed_module_id.clone()],
        );
        assert_eq!(by_binding("b").edges_to_other_residual_cells, 0);
    }

    #[test]
    fn edges_to_active_modules_count_outgoing_to_active_claims() {
        // Residual cell {b} has a constraining edge to `a`, an
        // owner whose destination is the active module `ui/x`.
        // Expected: edges_to_active_modules=1,
        // active_modules_referenced=["ui/x"].
        let nodes = vec![
            owner_in_active_module("a", 1, &["a"], 10, "ui/x"),
            owner("b", 2, &["b"], 10),
        ];
        let edges = vec![edge("e1", "b", "a", DepKind::EagerUse, true)];
        let cells = vec![cell(
            "auto_partition_0000",
            &["b"],
            &nodes,
            PeelCandidateStatus::PeelableNow,
            &[],
        )];
        let graph = graph_with_cells(nodes, edges, cells);
        let claims = BTreeMap::from([("a".to_string(), "ui/x".to_string())]);
        let report = factorize(&graph, &claims, &no_claims(), 2000);
        assert_eq!(report.proposals.len(), 1);
        assert_eq!(report.proposals[0].edges_to_active_modules, 1);
        assert_eq!(
            report.proposals[0].active_modules_referenced,
            vec!["ui/x".to_string()],
        );
    }

    #[test]
    fn analyzer_emit_blocked_verdict_passes_through_to_proposal() {
        // Analyzer-side cell carries emit_blocked_residual_bindings.
        // The CLI proposal should surface the same list and report
        // landable_today=false. The CLI computes its own emit-block
        // check (post-promotion exports), but for a cell with no
        // active claims and only residual owners, both checks land
        // on the same set.
        let nodes = vec![
            owner("consumer", 1, &["consumer"], 10),
            owner("dep", 2, &["dep"], 5),
        ];
        let edges = vec![edge_for_binding(
            "e1",
            "consumer",
            "dep",
            DepKind::LazyUse,
            false,
            Some("dep"),
        )];
        let cells = vec![
            cell(
                "auto_partition_0000",
                &["consumer"],
                &nodes,
                PeelCandidateStatus::BlockedEmitResolvability,
                &["dep"],
            ),
            cell(
                "auto_partition_0001",
                &["dep"],
                &nodes,
                PeelCandidateStatus::PeelableNow,
                &[],
            ),
        ];
        let graph = graph_with_cells(nodes, edges, cells);
        let report = factorize(&graph, &no_claims(), &no_claims(), 2000);
        let consumer = report
            .proposals
            .iter()
            .find(|p| p.binding_ids.contains(&"consumer".to_string()))
            .expect("consumer cell");
        assert_eq!(
            consumer.emit_blocked_residual_bindings,
            vec!["dep".to_string()],
        );
        assert!(!consumer.landable_today);
    }
}
