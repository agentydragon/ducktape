use std::collections::{BTreeMap, BTreeSet};

use petgraph::graphmap::DiGraphMap;

use crate::ModuleId;
use crate::partition::Partition;

use super::edge::OwnerEdgeId;
use super::owner_graph::OwnerGraph;
use super::quotient::{EndpointView, partition_endpoints};

/// The canonical chunk-wide ESM I-graph. The realizability gate
/// (Pass-2 simulator's `i_successors`, linker / source-import
/// positions) and the emitter
/// (`lowering::imports::plan_references::collect_phantom_side_effect_providers`,
/// `esm_import_order::EsmImportOrder::build`)
/// MUST drive their topology decisions through this single set so
/// they cannot drift apart.
///
/// Filter rule:
///   * Drop same-module edges (no ESM `import`).
///   * Drop `EagerRebind` / `LazyRebind` / `DeferredRebind`
///     cross-module edges. They surface as `cross_rebinds` in the
///     realizability verdict, not as I-graph nodes; the emitter never
///     emits them as imports.
///   * Keep cross-module at-init promoted edges (see
///     [`EndpointView::Gate`]) — the emitter's phantom side-effect
///     importer also keeps them, so the gate must too.
///   * Every remaining cross-module edge, `LazyUse` included, joins
///     `i_successors`: the simulator's Pass-2 DFS needs lazy
///     back-edges to find asymmetric (constraining forward, lazy back)
///     I-cycles that Lemma 2's source-import reversal must rescue.
///   * Of those, the edges whose reason `constrains_init_order()` —
///     `EagerUse`, `Sequenced`, `LocalEffect` — also join `edges`:
///     the ones the emitter turns into a binding-level ESM import or
///     a phantom side-effect import. Pure `LazyUse` edges are
///     function-body reads resolved at call time after every module
///     has loaded, so they never constrain init order.
///
/// Sequenced edges are deduped per `(from, to)` pair in `edges`:
/// multiple sequenced reasons between the same module pair represent
/// the same ordering constraint and should not over-weight the
/// I-graph.
///
/// Returns the constraining edge set plus the precomputed
/// `from -> {to}` adjacency map (`i_successors`) ready to feed into
/// the simulator.
pub fn chunk_constraining_module_edges(
    owner_graph: &OwnerGraph,
    partition: &Partition,
) -> ChunkConstrainingEdgeSet {
    let mut edges: BTreeMap<(ModuleId, ModuleId), Vec<OwnerEdgeId>> = BTreeMap::new();
    let mut i_successors: BTreeMap<ModuleId, BTreeSet<ModuleId>> = BTreeMap::new();
    let mut seen_sequenced_pairs: BTreeSet<(ModuleId, ModuleId)> = BTreeSet::new();
    for edge in owner_graph.iter_edges() {
        // Gate-side view: keep cross-module at-init promoted edges.
        // The matching `EndpointView::Lenient` view would drop them;
        // the canonical edge set is the strict view (see
        // [`partition_endpoints`]).
        let Some((from, to)) = partition_endpoints(edge, partition, EndpointView::Gate) else {
            continue;
        };
        if edge.reason.is_rebind() {
            // Rebinds are not I-graph members; they surface via the
            // `cross_rebinds` verdict and are never emitted as ESM
            // imports.
            continue;
        }
        // Every non-rebind cross-module edge — including LazyUse —
        // joins `i_successors`. The simulator's Pass-2 DFS needs
        // lazy back-edges to identify asymmetric (constraining
        // forward / lazy back) I-cycles that Lemma 2's source-import
        // reversal must rescue. The diagnostic `edges` field below
        // is constraining-only — that's the surface Pass-1's strict
        // SCC search and the cycle-report carry.
        i_successors.entry(from).or_default().insert(to);
        if !edge.reason.constrains_init_order() {
            continue;
        }
        if edge.reason.is_sequenced() && !seen_sequenced_pairs.insert((from, to)) {
            continue;
        }
        edges.entry((from, to)).or_default().push(edge.id);
    }
    ChunkConstrainingEdgeSet {
        edges,
        i_successors,
    }
}

/// Output of [`chunk_constraining_module_edges`]: the canonical
/// chunk-wide ESM I-graph plus its precomputed adjacency map.
///
/// Consumers MUST treat this as the single source of truth for the
/// "edges the emitter emits as ESM imports" question. See the
/// function-level doc for the filter rule.
#[derive(Debug, Clone, Default, Eq, PartialEq)]
pub struct ChunkConstrainingEdgeSet {
    /// `(from_module, to_module) -> all owner-edge ids` projecting
    /// onto this module pair. Stable ordering by `(ModuleId,
    /// ModuleId)`.
    pub edges: BTreeMap<(ModuleId, ModuleId), Vec<OwnerEdgeId>>,
    /// `from_module -> set of import targets` over every non-rebind
    /// cross-module edge, `LazyUse` included — a superset of the
    /// `edges` keys. Precomputed because every simulator and emitter
    /// consumer walks adjacency, not the raw `(from, to)` list.
    pub i_successors: BTreeMap<ModuleId, BTreeSet<ModuleId>>,
}

impl ChunkConstrainingEdgeSet {
    /// `(from, to) -> &[OwnerEdgeId]` lookup.
    pub fn edges_for(&self, from: ModuleId, to: ModuleId) -> &[OwnerEdgeId] {
        self.edges
            .get(&(from, to))
            .map(Vec::as_slice)
            .unwrap_or(&[])
    }

    /// `(from, to)` pairs in the canonical edge set (constraining
    /// only). Stable iteration order.
    pub fn pairs(&self) -> impl Iterator<Item = (ModuleId, ModuleId)> + '_ {
        self.edges.keys().copied()
    }
}

/// Toposort of the constraining `(from, to)` pairs (the canonical edge
/// set's [`pairs`](ChunkConstrainingEdgeSet::pairs), or the overlay
/// realizability path's `IncrementalQuotient` pairs), deepest
/// dependency first. The returned `Vec<ModuleId>` is the canonical
/// "linker order": element 0 is the deepest dependency (must evaluate
/// before everything else); the last element is the most-dependent
/// module.
/// Position in this vector is the module's "linker_position" — the
/// relative order ECMA-262's depth-first link traversal needs to
/// evaluate this chunk so that every constraining edge `M → M'` has
/// `M'` evaluating before `M`.
///
/// Modules that don't participate in any canonical edge are omitted
/// from the result (they're absent from the constraining DAG, hence
/// unconstrained relative to it). Callers fall back to `usize::MAX`
/// when sorting by linker_position so unconstrained modules sort
/// LAST.
///
/// Callers that need O(1) position lookup should pipe the result
/// through [`position_lookup`] once.
///
/// Every pair already satisfies `constrains_init_order()`, so the
/// toposort runs on the full set — no extra filter needed. If the
/// pairs have a constraining-only cycle (Pass 1 reports it as
/// unrealizable), `toposort` returns `Err`; this function returns the
/// empty vector.
pub fn chunk_linker_order_from_pairs(
    pairs: impl IntoIterator<Item = (ModuleId, ModuleId)>,
) -> Vec<ModuleId> {
    use petgraph::algo::toposort;
    let mut graph: DiGraphMap<ModuleId, ()> = DiGraphMap::new();
    for (from, to) in pairs {
        graph.add_node(from);
        graph.add_node(to);
        graph.add_edge(from, to, ());
    }
    match toposort(&graph, None) {
        // `toposort` yields dependents first (root → leaves); the
        // canonical "linker order" is dependency-first, so reverse.
        Ok(order) => order.into_iter().rev().collect(),
        Err(_) => Vec::new(),
    }
}

/// Build an O(1) position lookup from a canonical linker-order slice.
/// `result[id] = i` iff `id` is at index `i` in `order`. Modules
/// absent from `order` are absent from the returned map; callers
/// fall back to `usize::MAX` when sorting.
///
/// This is the one place that materializes the
/// `BTreeMap<ModuleId, usize>` view of the linker order. Callers
/// that only need to iterate in order should consume the
/// `Vec<ModuleId>` directly instead of going through this helper.
pub fn position_lookup(order: &[ModuleId]) -> BTreeMap<ModuleId, usize> {
    order
        .iter()
        .copied()
        .enumerate()
        .map(|(idx, id)| (id, idx))
        .collect()
}

/// Lemma 2 ordering: sort by `(SCC dep rank ASC, intra-SCC
/// linker_position DESC)`. SCCs are over the full I-graph
/// (`i_successors`, lazy edges included); `linker_position` is the
/// [`chunk_linker_order_from_pairs`] position of `constraining_pairs`.
/// SCC dep rank = min linker_position of SCC members.
///
/// The returned vector is the order in which entry's source-level
/// `import` directives must appear so the runtime ECMA-262 linker
/// DFS lands on the desired evaluation order (post-DFS = ESM Phase-2
/// evaluation). Within each SCC, members with no linker_position
/// (modules absent from the canonical set — they can only be SCC
/// members via lazy back-edges; canonical edges are all
/// init-constraining, so this case is empty by construction — but
/// the `None`-after-Some clause is kept for robustness against
/// future filter changes that might admit non-constraining members)
/// sort AFTER constraining members.
///
/// `extra_nodes` — modules that should appear in the result even if
/// they have no canonical edges (e.g. spec-known logical modules
/// the emitter wants a deterministic source-order slot for). These
/// land at the end with `linker_position = None`.
pub fn chunk_source_import_order_from_adjacency(
    constraining_pairs: impl IntoIterator<Item = (ModuleId, ModuleId)>,
    i_successors: &BTreeMap<ModuleId, BTreeSet<ModuleId>>,
    extra_nodes: &BTreeSet<ModuleId>,
) -> Vec<ModuleId> {
    use petgraph::algo::tarjan_scc;
    // We need O(1) position lookups inside the sort comparator below,
    // so materialize the linker order into the position-lookup map
    // once. The canonical linker-order Vec is the toposort output;
    // `position_lookup` is the small enumerate-collect adapter.
    let linker_position = position_lookup(&chunk_linker_order_from_pairs(constraining_pairs));
    // SCCs are computed over the FULL I-graph (constraining + lazy
    // back-edges) so Lemma 2's intra-SCC `linker_position`-DESC
    // reversal catches asymmetric cycles. The constraining-only
    // view would collapse `(constraining-forward, lazy-back)`
    // shapes into singleton SCCs and miss the rescue.
    let mut graph: DiGraphMap<ModuleId, ()> = DiGraphMap::new();
    let mut nodes: BTreeSet<ModuleId> = extra_nodes.iter().copied().collect();
    for (from, succs) in i_successors {
        for to in succs {
            graph.add_edge(*from, *to, ());
            nodes.insert(*from);
            nodes.insert(*to);
        }
    }
    for &n in &nodes {
        graph.add_node(n);
    }
    let sccs = tarjan_scc(&graph);
    let mut scc_of: BTreeMap<ModuleId, usize> = BTreeMap::new();
    let mut scc_rank: Vec<usize> = Vec::with_capacity(sccs.len());
    for (idx, scc) in sccs.iter().enumerate() {
        let min_pos = scc
            .iter()
            .filter_map(|m| linker_position.get(m).copied())
            .min()
            .unwrap_or(usize::MAX);
        scc_rank.push(min_pos);
        for m in scc {
            scc_of.insert(*m, idx);
        }
    }
    let mut sorted: Vec<ModuleId> = nodes.into_iter().collect();
    sorted.sort_by(|a, b| {
        let a_rank = scc_of
            .get(a)
            .and_then(|i| scc_rank.get(*i).copied())
            .unwrap_or(usize::MAX);
        let b_rank = scc_of
            .get(b)
            .and_then(|i| scc_rank.get(*i).copied())
            .unwrap_or(usize::MAX);
        let a_pos = linker_position.get(a).copied();
        let b_pos = linker_position.get(b).copied();
        a_rank.cmp(&b_rank).then_with(|| match (a_pos, b_pos) {
            (Some(a), Some(b)) => b.cmp(&a),
            (Some(_), None) => std::cmp::Ordering::Less,
            (None, Some(_)) => std::cmp::Ordering::Greater,
            (None, None) => std::cmp::Ordering::Equal,
        })
    });
    sorted
}
