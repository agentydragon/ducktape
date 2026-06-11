//! ECMA-262 Phase-2 evaluation-order simulator for the gate's
//! Lemma-2 rescue check. Split from `realizability/mod.rs`; the
//! shared import ordering itself lives in `esm_import_order`.

use std::collections::{BTreeMap, BTreeSet};

use petgraph::visit::{DfsPostOrder, GraphBase, GraphRef, IntoNeighbors, Visitable};
use rustc_hash::FxHashSet;

use analysis::ids::ModuleId;

use crate::esm_import_order::EsmImportOrder;

/// Simulator for ECMA-262 Phase-2 module evaluation order, used to
/// decide whether Lemma 2's source-import reversal actually rescues
/// a candidate asymmetric I-SCC at runtime.
///
/// The simulator models the **same** import-ordering decisions the
/// materializer makes, via the shared `EsmImportOrder` (the single
/// source of truth both sides consume — see `esm_import_order`):
///   - residual (= the chunk's emitted entry file) imports **every**
///     emitted logical module — binding imports for binding-owning
///     plans, side-effect-only imports for binding-less plans — in
///     `EsmImportOrder::sort_entry_imports` order (Lemma 2's
///     source-import order). See `lowering::lower_chunk`.
///   - every other module's intra-chunk imports (cross-module
///     binding imports, phantom side-effect imports, residual-entry
///     import — one merged list) follow
///     `EsmImportOrder::sort_module_imports` (dependency-first
///     linker order). See `lowering::lower_chunk::lower_single_plan`.
///
/// It then walks DFS from residual, records the post-order
/// evaluation index per module, and verifies every cross-module
/// constraining edge `(M, X)` evaluates the target `X` before the
/// source `M`. Equivalent to asking whether the emitted ESM bundle
/// would actually execute without TDZ on the constraining edges in
/// the candidate SCC.
#[derive(Debug, Clone, Eq, PartialEq)]
pub(super) struct EsmEvaluationSimulator {
    /// Post-order index per module after DFS from residual. Lower
    /// index = earlier post-order = body evaluates earlier. Modules
    /// unreachable from residual are absent — ESM doesn't load them,
    /// so the simulator skips constraining-edge checks involving
    /// them.
    pub(super) post_order: BTreeMap<ModuleId, usize>,
}

impl EsmEvaluationSimulator {
    /// Build from precomputed adjacency. Used by both the gate's
    /// canonical-edge-set path (`check_realizability` extracts the
    /// pairs from a [`ChunkConstrainingEdgeSet`]) and the overlay
    /// path (`IncrementalQuotient::build_simulator_from_scratch`
    /// materialises pairs from its own `(i_graph,
    /// constraining_buckets)` shape). The runtime DFS only needs
    /// the adjacency map; both callers thread their pairs in
    /// directly.
    pub(super) fn build(
        i_successors: &BTreeMap<ModuleId, BTreeSet<ModuleId>>,
        constraining_pairs: &BTreeSet<(ModuleId, ModuleId)>,
        residual: ModuleId,
    ) -> Self {
        // The simulator's module universe: every module that appears
        // in the I-graph (plus residual itself). The emitted entry
        // imports every logical module; modules with no I-edges are
        // DFS dead-ends that cannot affect any other module's
        // post-order, so restricting the universe to I-graph
        // participants is exact.
        let mut nodes: BTreeSet<ModuleId> = BTreeSet::new();
        nodes.insert(residual);
        for (from, succs) in i_successors {
            nodes.insert(*from);
            nodes.extend(succs.iter().copied());
        }
        let import_order = EsmImportOrder::build(constraining_pairs, i_successors, &nodes);
        let post_order = simulate_esm_post_order(residual, i_successors, &nodes, &import_order);
        Self { post_order }
    }

    /// Yields the `(from, to)` constraining pairs inside `modules`
    /// whose simulator-derived post-order has `to` evaluating at or
    /// after `from` — i.e. the at-init read of `to`'s binding from
    /// `from`'s body would TDZ. Returns the surgical TDZ subset
    /// callers use for diagnostics; an empty iterator means Lemma 2
    /// rescues the SCC.
    ///
    /// Endpoints unreachable from residual are skipped — ESM never
    /// loads them, so they can't fire a TDZ at runtime.
    pub(super) fn tdz_pairs<'a>(
        &'a self,
        modules: &'a BTreeSet<ModuleId>,
        constraining_pairs: &'a BTreeSet<(ModuleId, ModuleId)>,
    ) -> impl Iterator<Item = (ModuleId, ModuleId)> + 'a {
        constraining_pairs
            .iter()
            .copied()
            .filter(move |&(from, to)| {
                if !modules.contains(&from) || !modules.contains(&to) {
                    return false;
                }
                let (Some(from_idx), Some(to_idx)) =
                    (self.post_order.get(&from), self.post_order.get(&to))
                else {
                    return false;
                };
                to_idx >= from_idx
            })
    }
}

/// Simulate ECMA-262 Phase-2 DFS from `residual`. Returns a
/// `post_order` map: lower index = earlier post-order = body
/// evaluates earlier.
///
/// Import ordering per visitor (the shared `EsmImportOrder` rules):
///   - At `residual`: fan out to **every** module in `nodes`
///     (the emitted entry imports every logical module), in
///     `sort_entry_imports` order (Lemma 2-aware).
///   - Elsewhere: that module's I-successors in
///     `sort_module_imports` order (dependency-first; mirrors the
///     merged cross/phantom/residual-entry import list the emitter
///     renders in `lowering::lower_chunk::lower_single_plan`).
///
/// Cycle no-op: a back-edge to a module already on the link-DFS
/// stack is skipped. petgraph's `DfsPostOrder` filters neighbors
/// already in its `discovered` set, which covers both back-edges
/// (discovered-but-not-finished) and cross-edges (already finished),
/// matching the hand-rolled walker's `on_stack` + `visited` checks.
pub(super) fn simulate_esm_post_order(
    residual: ModuleId,
    i_successors: &BTreeMap<ModuleId, BTreeSet<ModuleId>>,
    nodes: &BTreeSet<ModuleId>,
    import_order: &EsmImportOrder,
) -> BTreeMap<ModuleId, usize> {
    let graph = EsmIGraph {
        i_successors,
        residual,
        nodes,
        import_order,
    };
    let mut dfs = DfsPostOrder::new(&graph, residual);
    let mut post_order: BTreeMap<ModuleId, usize> = BTreeMap::new();
    while let Some(node) = dfs.next(&graph) {
        let idx = post_order.len();
        post_order.insert(node, idx);
    }
    post_order
}

/// Petgraph view over `i_successors` that bakes in the ECMA-262
/// import-order sort from the shared `EsmImportOrder`: residual fans
/// out to every module in `nodes` in entry-import order, every other
/// module to its I-successors in module-import order. Neighbors are
/// yielded in REVERSE sort-key order so that `DfsPostOrder`'s
/// push-all-then-pop-top semantics visits the smallest-key successor
/// first (matching the hand-rolled walker's reverse-push of
/// `sorted_successors`).
pub(super) struct EsmIGraph<'a> {
    pub(super) i_successors: &'a BTreeMap<ModuleId, BTreeSet<ModuleId>>,
    pub(super) residual: ModuleId,
    /// The simulator's module universe (see
    /// `EsmEvaluationSimulator::build`). Residual's neighbor set —
    /// the emitted entry imports every logical module, not only the
    /// ones residual's own statements reference.
    pub(super) nodes: &'a BTreeSet<ModuleId>,
    pub(super) import_order: &'a EsmImportOrder,
}

impl GraphBase for &EsmIGraph<'_> {
    type NodeId = ModuleId;
    type EdgeId = (ModuleId, ModuleId);
}

impl GraphRef for &EsmIGraph<'_> {}

impl Visitable for &EsmIGraph<'_> {
    // FxHash instead of std SipHash: the visit map is the per-node
    // dedup set for the simulator's DFS, hit once per `simulate_esm_post_order`
    // call (= once per `build_simulator` rebuild = once per gate
    // `would_be_cycles_after_contract`). SipHash's DoS resistance is
    // irrelevant on internal ModuleId data; FxHash is ~5× cheaper per
    // probe.
    type Map = FxHashSet<ModuleId>;

    fn visit_map(&self) -> Self::Map {
        FxHashSet::default()
    }

    fn reset_map(&self, map: &mut Self::Map) {
        map.clear();
    }
}

impl IntoNeighbors for &EsmIGraph<'_> {
    type Neighbors = std::vec::IntoIter<ModuleId>;

    fn neighbors(self, node: ModuleId) -> Self::Neighbors {
        let mut succs: Vec<ModuleId> = if node == self.residual {
            // The emitted entry imports EVERY logical module
            // (binding-owning plans via named imports, binding-less
            // plans via side-effect-only imports), not just the
            // modules residual's own statements reference.
            self.nodes
                .iter()
                .copied()
                .filter(|m| *m != self.residual)
                .collect()
        } else {
            self.i_successors
                .get(&node)
                .map(|succs| succs.iter().copied().collect())
                .unwrap_or_default()
        };
        // Reverse sort: largest key first, smallest key last. petgraph's
        // DfsPostOrder pushes neighbors in iteration order and visits
        // the top of the stack next, so the LAST-yielded neighbor is
        // descended into first.
        if node == self.residual {
            succs.sort_by_key(|m| std::cmp::Reverse(self.import_order.entry_import_sort_key(*m)));
        } else {
            succs.sort_by_key(|m| std::cmp::Reverse(self.import_order.module_import_sort_key(*m)));
        }
        succs.into_iter()
    }
}
