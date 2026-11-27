use std::cmp::Reverse;
use std::collections::{BTreeMap, BTreeSet};

use petgraph::graphmap::DiGraphMap;

use crate::module_assignment::ModuleAssignment;
use analysis::ModuleId;

use super::dependency_graph::DependencyGraph;
use super::edge::DependencyEdgeId;
use super::quotient::{EndpointView, assignment_endpoints};

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
///   * Every remaining cross-module edge, `LazyUse` and `HoistedUse` included, joins
///     `i_successors`: the simulator's Pass-2 DFS needs lazy
///     back-edges to find asymmetric (constraining forward, lazy back)
///     I-cycles that Lemma 2's source-import reversal must rescue.
///   * Of those, the edges whose reason `constrains_init_order()` —
///     `EagerUse`, `Sequenced`, `LocalEffect` — also join `edges`:
///     the ones the emitter turns into a binding-level ESM import or
///     a phantom side-effect import. Pure `LazyUse` edges are
///     function-body reads resolved at call time after every module
///     has loaded, so they never constrain init order.
///     `HoistedUse` reads target function declarations initialized during
///     instantiation; they retain import topology without an init constraint.
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
    dependency_graph: &DependencyGraph,
    assignment: &ModuleAssignment,
) -> ChunkConstrainingEdgeSet {
    let mut edges: BTreeMap<(ModuleId, ModuleId), Vec<DependencyEdgeId>> = BTreeMap::new();
    let mut i_successors: BTreeMap<ModuleId, BTreeSet<ModuleId>> = BTreeMap::new();
    let mut seen_sequenced_pairs: BTreeSet<(ModuleId, ModuleId)> = BTreeSet::new();
    for edge in dependency_graph.iter_edges() {
        // Gate-side view: keep cross-module at-init promoted edges.
        // The matching `EndpointView::Lenient` view would drop them;
        // the canonical edge set is the strict view (see
        // [`assignment_endpoints`]).
        let Some((from, to)) = assignment_endpoints(edge, assignment, EndpointView::Gate) else {
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
    /// `(from_module, to_module) -> all dependency edge ids` projecting
    /// onto this module pair. Stable ordering by `(ModuleId,
    /// ModuleId)`.
    pub edges: BTreeMap<(ModuleId, ModuleId), Vec<DependencyEdgeId>>,
    /// `from_module -> set of import targets` over every non-rebind
    /// cross-module edge, `LazyUse` included — a superset of the
    /// `edges` keys. Precomputed because every simulator and emitter
    /// consumer walks adjacency, not the raw `(from, to)` list.
    pub i_successors: BTreeMap<ModuleId, BTreeSet<ModuleId>>,
}

impl ChunkConstrainingEdgeSet {
    /// `(from, to) -> &[DependencyEdgeId]` lookup.
    pub fn edges_for(&self, from: ModuleId, to: ModuleId) -> &[DependencyEdgeId] {
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
/// set's [`pairs`](ChunkConstrainingEdgeSet::pairs)), deepest
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
/// Use [`position_lookup`] to index the result by module.
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

/// Index a module order by module ID.
pub fn position_lookup(order: &[ModuleId]) -> BTreeMap<ModuleId, usize> {
    order
        .iter()
        .copied()
        .enumerate()
        .map(|(idx, id)| (id, idx))
        .collect()
}

/// Order modules by their import SCC's earliest linker position, then by
/// descending linker position within each SCC (Lemma 2). Lazy edges belong
/// to the import graph, so an SCC can contain modules with no linker
/// position. Within that SCC they sort after positioned members, in
/// module ID order. SCCs with no positioned members, including isolated
/// `extra_nodes`, sort last.
pub fn chunk_source_import_order_from_adjacency(
    linker_position: &BTreeMap<ModuleId, usize>,
    i_successors: &BTreeMap<ModuleId, BTreeSet<ModuleId>>,
    extra_nodes: &BTreeSet<ModuleId>,
) -> Vec<ModuleId> {
    use petgraph::algo::tarjan_scc;

    let mut graph = DiGraphMap::<ModuleId, ()>::from_edges(
        i_successors
            .iter()
            .flat_map(|(&from, targets)| targets.iter().map(move |&to| (from, to))),
    );
    for &module in extra_nodes {
        graph.add_node(module);
    }

    let mut ranked = Vec::with_capacity(graph.node_count());
    for component in tarjan_scc(&graph) {
        let rank = component
            .iter()
            .filter_map(|module| linker_position.get(module).copied())
            .min()
            .unwrap_or(usize::MAX);
        ranked.extend(component.into_iter().map(|module| {
            let position = linker_position.get(&module).copied();
            (rank, Reverse(position), module)
        }));
    }
    ranked.sort_unstable();
    ranked.into_iter().map(|(_, _, module)| module).collect()
}
