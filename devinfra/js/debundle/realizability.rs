//! Single source of truth for the three-clause validity predicate
//! (docs/design.md "Valid peels and atomic modules"). The validator and any
//! hypothetical-move planner checks reach the verdict through this module —
//! see "Realizability primitive" in `docs/design.md`.
//!
//! Scope: clauses 2 (no cross-destination rebinding writes) and 3
//! (no multi-module SCC in the constraining-edge subgraph of the
//! quotient). Clause 1 (importability) is policy that lives in
//! `materialize_logical_modules` per "Emit-side responsibilities":
//! residual-entry bindings are importable by construction via the
//! auto-grown export pass. Callers that need a private-read blocker
//! (the proposer's `BlockedResidualDependency`) layer it on top of the
//! verdict; it is not part of the realizability primitive.
//!
//! Two access shapes:
//!
//! - `check_realizability(owner_graph, partition) -> Verdict`: pure
//!   function, from-scratch. The correctness reference and the cold-
//!   start path. `O(N + M)` per call.
//! - `RealizabilityIndex`: a stateful index that owns a working
//!   `Partition` and supports `push`/`undo` of `PartitionDelta`s.
//!   `verdict()` reads the current state. A non-mutating overlay query is
//!   kept as a tested future optimization path for planner checks that need
//!   hypothetical owner moves.
//!
//! The transactional API is backed by a rollbackable quotient index:
//! owner-graph edges are fixed, so `push`/`undo` only updates quotient
//! edge buckets incident to moved owners. Full verdicts run SCC over
//! the maintained quotient; candidate verdicts use localized
//! reachability around the hypothetical destination.

use std::cell::{Cell, RefCell};
use std::collections::{BTreeMap, BTreeSet};
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

use petgraph::algo::tarjan_scc;
use petgraph::graphmap::DiGraphMap;
use petgraph::visit::{DfsPostOrder, GraphBase, GraphRef, IntoNeighbors, Visitable};
use rustc_hash::FxHashSet;

use crate::esm_import_order::EsmImportOrder;
use crate::rollback_graph::{GraphMark, RollbackDiGraph};
use analysis::OwnerId;
use analysis::graph::{OwnerEdge, OwnerEdgeId, OwnerGraph, chunk_constraining_module_edges};
use analysis::ids::ModuleId;
use analysis::partition::Partition;

/// Canonical in-memory diagnosis of one offending module-quotient
/// SCC. The presence of any such diagnosis on a
/// [`RealizabilityVerdict`] violates clause 3 (multi-module SCC in
/// the constraining-edge subgraph of the quotient).
///
/// This is the **primitive** shape: typed `ModuleId`s and typed
/// `OwnerEdgeId` evidence, no rendering. Downstream projection types
/// derive their fields from this:
///
/// - [`crate::validation::CycleReport`] — validator's rendered
///   projection: stringified module names + `evidence` and FAS `cut`
///   decorations.
/// - [`analysis::reports::schema::QuotientSccReport`] — wire-format
///   projection: stringified module ids + edge ids. Covers every
///   SCC of the dep graph (including realizable single-module ones),
///   not only the offending diagnoses listed here.
#[derive(Debug, Clone, Eq, PartialEq)]
pub struct SccDiagnosis {
    /// Modules participating in the cycle.
    pub modules: BTreeSet<ModuleId>,
    /// Constraining owner-edge evidence the rejection is composed of,
    /// in stable `OwnerEdgeId` order. The exact edge set depends on
    /// `rejection`:
    ///
    /// - [`SccRejection::MutualConstrainingCycle`]: every constraining
    ///   cross-module owner edge whose endpoints both fall inside
    ///   `modules`.
    /// - [`SccRejection::EsmEvaluationTdz`]: only the owner edges
    ///   backing the constraining `(from, to)` pairs whose simulated
    ///   post-order check failed — the surgical set whose removal
    ///   (by co-locating the binding pair) lifts the violation.
    pub constraining_owner_edges: Vec<OwnerEdgeId>,
    /// Which gate pass rejected this SCC.
    pub rejection: SccRejection,
}

/// How the realizability gate decided an SCC is unrealizable. See
/// docs/design.md "Lemma 2: entry-side import ordering" for the
/// two-pass gating rule.
#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub enum SccRejection {
    /// Pass 1: the SCC is cyclic in the constraining-edge subgraph
    /// alone — a mutual-eager cycle no source import order can
    /// satisfy.
    MutualConstrainingCycle,
    /// Pass 2: the constraining subgraph of the SCC is acyclic, but
    /// the full I-graph (constraining ∪ lazy back-edges) is cyclic
    /// and the ESM evaluation simulator proved a TDZ: some
    /// constraining edge's target evaluates at or after its source
    /// under the materializer's actual import-order choices.
    EsmEvaluationTdz,
}

/// Cross-destination rebinding write. ESM imports are read-only in the
/// importing module, so any such edge violates clause 2. One entry per
/// owner-edge.
#[derive(Debug, Clone, Eq, PartialEq)]
pub struct CrossRebindEdge {
    pub from: ModuleId,
    pub to: ModuleId,
    pub owner_edge: OwnerEdgeId,
}

/// Verdict on a (current or hypothetical) destination assignment.
/// Empty `unrealizable_sccs` + `cross_rebinds` ↔ realizable per
/// clauses 2 and 3.
#[derive(Debug, Clone, Default, Eq, PartialEq)]
pub struct RealizabilityVerdict {
    pub unrealizable_sccs: Vec<SccDiagnosis>,
    pub cross_rebinds: Vec<CrossRebindEdge>,
}

impl RealizabilityVerdict {
    pub fn is_realizable(&self) -> bool {
        self.unrealizable_sccs.is_empty() && self.cross_rebinds.is_empty()
    }

    /// Modules participating in any unrealizable SCC. Convenience for
    /// the proposer, which decodes the verdict against the candidate's
    /// hypothetical destination.
    pub fn modules_in_unrealizable_sccs(&self) -> BTreeSet<ModuleId> {
        let mut out = BTreeSet::new();
        for scc in &self.unrealizable_sccs {
            for &m in &scc.modules {
                out.insert(m);
            }
        }
        out
    }
}

/// Pure-function form. Builds the canonical constraining edge set,
/// runs Tarjan, surfaces multi-module SCCs and cross-rebinds. The
/// correctness reference for the `RealizabilityIndex`'s incremental
/// backing (verified by differential test in the
/// `RealizabilityIndex` step 1b follow-up).
pub fn check_realizability(
    owner_graph: &OwnerGraph,
    partition: &Partition,
) -> RealizabilityVerdict {
    let mut verdict = RealizabilityVerdict::default();

    // Cross-destination rebinds are a separate clause-2 violation and
    // not part of the I-graph. Collect them in a single pass over
    // owner edges; the canonical edge set handles everything else.
    for edge in owner_graph.iter_edges() {
        if owner_graph.node(edge.from).is_none() || owner_graph.node(edge.to).is_none() {
            continue;
        }
        if !edge.reason.is_rebind() {
            continue;
        }
        let Some((from, to)) = analysis::graph::partition_endpoints(
            edge,
            partition,
            analysis::graph::EndpointView::Gate,
        ) else {
            continue;
        };
        verdict.cross_rebinds.push(CrossRebindEdge {
            from,
            to,
            owner_edge: edge.id,
        });
    }

    // Canonical I-graph: cross-module edges the emitter actually
    // emits as ESM imports. By construction every entry of this set
    // also satisfies `constrains_init_order()` (lazy_use edges are
    // dropped at the helper); the gate's Pass-1 constraining SCC
    // search and Pass-2 simulator therefore run over the SAME
    // adjacency, eliminating the historical drift between the two
    // views.
    let canonical = chunk_constraining_module_edges(owner_graph, partition);
    if canonical.edges.is_empty() {
        return verdict;
    }

    // Pass 1: Tarjan over the constraining-edge subgraph — the
    // historical relaxed clause-3 rule. Catches **mutual**
    // constraining cycles (both sides eager-read each other; no
    // source order can satisfy both).
    //
    // Under the unification, the canonical edge set IS the
    // constraining set, so Pass 1's SCC search is also the
    // I-graph SCC search. Pass 2 below applies the simulator only
    // when an SCC hasn't already been flagged here.
    let mut con_graph: DiGraphMap<ModuleId, ()> = DiGraphMap::new();
    for (from, to) in canonical.pairs() {
        con_graph.add_edge(from, to, ());
    }
    let mut reported: BTreeSet<BTreeSet<ModuleId>> = BTreeSet::new();
    let sccs = tarjan_scc(&con_graph);
    for scc in &sccs {
        if scc.len() < 2 {
            continue;
        }
        let modules: BTreeSet<ModuleId> = scc.iter().copied().collect();
        let mut owner_edges: Vec<OwnerEdgeId> = Vec::new();
        for ((from, to), edges) in &canonical.edges {
            if modules.contains(from) && modules.contains(to) {
                owner_edges.extend_from_slice(edges);
            }
        }
        owner_edges.sort();
        reported.insert(modules.clone());
        verdict.unrealizable_sccs.push(SccDiagnosis {
            modules,
            constraining_owner_edges: owner_edges,
            rejection: SccRejection::MutualConstrainingCycle,
        });
    }

    // Pass 2: Tarjan over the full I-graph (canonical
    // `i_successors`, which includes lazy back-edges). Multi-module
    // I-SCCs not already in `reported` are the asymmetric
    // `(at-init forward, lazy back)` candidates: the constraining
    // subgraph alone is acyclic, but the lazy back-edge closes a
    // cycle in the runtime DFS topology. Lemma 2
    // (`chunk_source_import_order`) reverses entry's import order
    // within each I-SCC so DFS lands on the dependent first and
    // unwinds through the dependency; the simulator below checks
    // whether that reversal actually rescues evaluation given the
    // spec's full import topology.
    let mut i_graph: DiGraphMap<ModuleId, ()> = DiGraphMap::new();
    for (from, succs) in &canonical.i_successors {
        for to in succs {
            i_graph.add_edge(*from, *to, ());
        }
    }
    let i_sccs = tarjan_scc(&i_graph);
    let candidate_sccs: Vec<BTreeSet<ModuleId>> = i_sccs
        .into_iter()
        .filter_map(|scc| {
            if scc.len() < 2 {
                return None;
            }
            let modules: BTreeSet<ModuleId> = scc.into_iter().collect();
            if reported.contains(&modules) {
                return None;
            }
            // Skip SCCs that carry no constraining edge between
            // members — pure-lazy I-cycles never TDZ regardless of
            // entry's import order.
            let has_constraining = canonical
                .edges
                .keys()
                .any(|(from, to)| modules.contains(from) && modules.contains(to));
            if !has_constraining {
                return None;
            }
            Some(modules)
        })
        .collect();

    if !candidate_sccs.is_empty() {
        let constraining_pairs: BTreeSet<(ModuleId, ModuleId)> = canonical.pairs().collect();
        let simulation = EsmEvaluationSimulator::build(
            &canonical.i_successors,
            &constraining_pairs,
            partition.residual(),
        );
        for modules in candidate_sccs {
            let tdz_pairs: Vec<(ModuleId, ModuleId)> = simulation
                .tdz_pairs(&modules, &constraining_pairs)
                .collect();
            if tdz_pairs.is_empty() {
                continue;
            }
            let mut owner_edges: Vec<OwnerEdgeId> = Vec::new();
            for (from, to) in &tdz_pairs {
                owner_edges.extend_from_slice(canonical.edges_for(*from, *to));
            }
            owner_edges.sort();
            verdict.unrealizable_sccs.push(SccDiagnosis {
                modules,
                constraining_owner_edges: owner_edges,
                rejection: SccRejection::EsmEvaluationTdz,
            });
        }
    }

    verdict
}

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
struct EsmEvaluationSimulator {
    /// Post-order index per module after DFS from residual. Lower
    /// index = earlier post-order = body evaluates earlier. Modules
    /// unreachable from residual are absent — ESM doesn't load them,
    /// so the simulator skips constraining-edge checks involving
    /// them.
    post_order: BTreeMap<ModuleId, usize>,
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
    fn build(
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
    fn tdz_pairs<'a>(
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
fn simulate_esm_post_order(
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
struct EsmIGraph<'a> {
    i_successors: &'a BTreeMap<ModuleId, BTreeSet<ModuleId>>,
    residual: ModuleId,
    /// The simulator's module universe (see
    /// `EsmEvaluationSimulator::build`). Residual's neighbor set —
    /// the emitted entry imports every logical module, not only the
    /// ones residual's own statements reference.
    nodes: &'a BTreeSet<ModuleId>,
    import_order: &'a EsmImportOrder,
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

/// A reversible mutation of a `Partition`. Planner checks can construct
/// deltas to describe hypothetical or actual destination assignments; the
/// index applies and reverts them.
#[derive(Debug, Clone)]
pub enum PartitionDelta {
    /// Reassign every owner in `owners` to `to`. Owners not in the
    /// list keep their current assignment. Owners already at `to` are
    /// no-ops but recorded for journal symmetry.
    MoveOwners { owners: Vec<OwnerId>, to: ModuleId },
}

/// Opaque handle returned by `push`. Passing it to `undo` rolls back
/// to the state before the corresponding push. Handles must be undone
/// in LIFO order — the journal is a stack — and `undo` panics in
/// debug builds on misuse so caller bugs surface early instead of
/// silently corrupting the index.
#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub struct DeltaHandle(usize);

/// Inverse of a `MoveOwners` delta: the prior `(owner, module)` pairs
/// so `undo` can restore them.
#[derive(Debug, Clone)]
struct JournalEntry {
    prior_assignments: Vec<(OwnerId, ModuleId)>,
    impacted_edges: Vec<OwnerEdgeId>,
    i_graph_mark: GraphMark,
    constraining_graph_mark: GraphMark,
}

#[derive(Debug, Clone, Default)]
struct ConstrainingBucket {
    non_sequenced: BTreeSet<OwnerEdgeId>,
    sequenced: BTreeSet<OwnerEdgeId>,
}

impl ConstrainingBucket {
    fn is_empty(&self) -> bool {
        self.non_sequenced.is_empty() && self.sequenced.is_empty()
    }

    fn insert_edge(&mut self, edge_id: OwnerEdgeId, sequenced: bool) {
        if sequenced {
            self.sequenced.insert(edge_id);
        } else {
            self.non_sequenced.insert(edge_id);
        }
    }

    fn remove_edge(&mut self, edge_id: OwnerEdgeId, sequenced: bool) {
        if sequenced {
            self.sequenced.remove(&edge_id);
        } else {
            self.non_sequenced.remove(&edge_id);
        }
    }

    fn extend_from(&mut self, other: &Self) {
        self.non_sequenced
            .extend(other.non_sequenced.iter().copied());
        self.sequenced.extend(other.sequenced.iter().copied());
    }

    fn remove_from(&mut self, other: &Self) {
        for edge_id in &other.non_sequenced {
            self.non_sequenced.remove(edge_id);
        }
        for edge_id in &other.sequenced {
            self.sequenced.remove(edge_id);
        }
    }

    fn evidence_edges(&self) -> Vec<OwnerEdgeId> {
        let mut edges: Vec<OwnerEdgeId> = self.non_sequenced.iter().copied().collect();
        if let Some(first_sequenced) = self.sequenced.first() {
            edges.push(*first_sequenced);
        }
        edges.sort();
        edges
    }
}

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
struct EdgeContribution {
    from: ModuleId,
    to: ModuleId,
    owner_edge: OwnerEdgeId,
    kind: EdgeContributionKind,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
enum EdgeContributionKind {
    Rebind,
    Import { constraining: bool, sequenced: bool },
}

#[derive(Debug, Clone, Default)]
struct QuotientOverlay {
    i_delta: BTreeMap<(ModuleId, ModuleId), isize>,
    constraining_delta: BTreeMap<(ModuleId, ModuleId), isize>,
    constraining_added: BTreeMap<(ModuleId, ModuleId), ConstrainingBucket>,
    constraining_removed: BTreeMap<(ModuleId, ModuleId), ConstrainingBucket>,
    cross_rebind_added: BTreeMap<OwnerEdgeId, CrossRebindEdge>,
    cross_rebind_removed: BTreeSet<OwnerEdgeId>,
}

impl QuotientOverlay {
    fn add_contribution(&mut self, contribution: EdgeContribution) {
        match contribution.kind {
            EdgeContributionKind::Rebind => {
                self.cross_rebind_added.insert(
                    contribution.owner_edge,
                    CrossRebindEdge {
                        from: contribution.from,
                        to: contribution.to,
                        owner_edge: contribution.owner_edge,
                    },
                );
            }
            EdgeContributionKind::Import {
                constraining,
                sequenced,
            } => {
                increment_delta(&mut self.i_delta, contribution.from, contribution.to, 1);
                if constraining {
                    increment_delta(
                        &mut self.constraining_delta,
                        contribution.from,
                        contribution.to,
                        1,
                    );
                    self.constraining_added
                        .entry((contribution.from, contribution.to))
                        .or_default()
                        .insert_edge(contribution.owner_edge, sequenced);
                }
            }
        }
    }

    fn remove_contribution(&mut self, contribution: EdgeContribution) {
        match contribution.kind {
            EdgeContributionKind::Rebind => {
                self.cross_rebind_removed.insert(contribution.owner_edge);
            }
            EdgeContributionKind::Import {
                constraining,
                sequenced,
            } => {
                increment_delta(&mut self.i_delta, contribution.from, contribution.to, -1);
                if constraining {
                    increment_delta(
                        &mut self.constraining_delta,
                        contribution.from,
                        contribution.to,
                        -1,
                    );
                    self.constraining_removed
                        .entry((contribution.from, contribution.to))
                        .or_default()
                        .insert_edge(contribution.owner_edge, sequenced);
                }
            }
        }
    }
}

fn increment_delta(
    deltas: &mut BTreeMap<(ModuleId, ModuleId), isize>,
    from: ModuleId,
    to: ModuleId,
    delta: isize,
) {
    let key = (from, to);
    let next = deltas.get(&key).copied().unwrap_or(0) + delta;
    if next == 0 {
        deltas.remove(&key);
    } else {
        deltas.insert(key, next);
    }
}

fn edge_contribution(edge: &OwnerEdge, from: ModuleId, to: ModuleId) -> Option<EdgeContribution> {
    if from == to {
        return None;
    }
    // NOTE: cross-module at-init promoted edges are intentionally NOT
    // filtered here — the matching gate-side view in
    // `partition_endpoints(.., EndpointView::Gate)` keeps them for
    // soundness (see
    // `tests::promoted_edge_in_aggregator_cycle_is_unrealizable`).

    let kind = if edge.reason.is_rebind() {
        EdgeContributionKind::Rebind
    } else {
        EdgeContributionKind::Import {
            constraining: edge.reason.constrains_init_order(),
            sequenced: edge.reason.is_sequenced(),
        }
    };

    Some(EdgeContribution {
        from,
        to,
        owner_edge: edge.id,
        kind,
    })
}

struct OverlayGraphView<'a> {
    base: &'a RollbackDiGraph<ModuleId>,
    delta: &'a BTreeMap<(ModuleId, ModuleId), isize>,
    added_out: BTreeMap<ModuleId, BTreeSet<ModuleId>>,
    added_in: BTreeMap<ModuleId, BTreeSet<ModuleId>>,
}

impl<'a> OverlayGraphView<'a> {
    fn new(
        base: &'a RollbackDiGraph<ModuleId>,
        delta: &'a BTreeMap<(ModuleId, ModuleId), isize>,
    ) -> Self {
        let mut added_out = BTreeMap::<ModuleId, BTreeSet<ModuleId>>::new();
        let mut added_in = BTreeMap::<ModuleId, BTreeSet<ModuleId>>::new();
        for (&(from, to), &count) in delta {
            if count <= 0 {
                continue;
            }
            added_out.entry(from).or_default().insert(to);
            added_in.entry(to).or_default().insert(from);
        }
        Self {
            base,
            delta,
            added_out,
            added_in,
        }
    }

    fn scc_containing(&self, node: ModuleId) -> BTreeSet<ModuleId> {
        // Cheap counts and bounded histograms are always recorded.
        // Wall-clock timing is only useful when reporting is enabled,
        // so keep `Instant` calls behind `DEBUNDLE_TIMING`.
        let start = if gate_perf_counters::enabled() {
            Some(Instant::now())
        } else {
            None
        };

        let result = self.scc_containing_inner(node);

        let nanos = start.map(|start| gate_perf_counters::elapsed_to_u64(start.elapsed()));
        let overlay_empty = self.delta.is_empty();
        // Classify each overlay entry as addition vs removal in
        // the effective graph. Cheap: linear in `delta.len()`
        // which is small by design (<50 in practice; see
        // tana measurements in `perf/proposer.md`).
        let mut additions = 0usize;
        let mut removals = 0usize;
        for &(from, to) in self.delta.keys() {
            if self.effective_count(from, to) > 0 {
                additions += 1;
            } else {
                removals += 1;
            }
        }
        gate_perf_counters::record_call(
            nanos,
            overlay_empty,
            self.delta.len(),
            additions,
            removals,
        );

        result
    }

    fn scc_containing_inner(&self, node: ModuleId) -> BTreeSet<ModuleId> {
        if !self.has_neighbor(node, WalkDirection::Forward)
            || !self.has_neighbor(node, WalkDirection::Reverse)
        {
            return BTreeSet::from([node]);
        }
        let forward = self.reachable_from(node, WalkDirection::Forward);
        let reverse = self.reachable_from(node, WalkDirection::Reverse);
        forward.intersection(&reverse).copied().collect()
    }

    fn reachable_from(&self, start: ModuleId, direction: WalkDirection) -> BTreeSet<ModuleId> {
        let mut seen = BTreeSet::new();
        let mut stack = vec![start];
        // Hoist these match arms out of the inner loop to avoid the
        // O(|stack|) dispatch and the per-call BTreeSet allocation in
        // the previous `neighbors()` helper. The `seen` set provides
        // dedup between base/overlay edges, so we can iterate both
        // streams directly without a scratch set.
        while let Some(node) = stack.pop() {
            if !seen.insert(node) {
                continue;
            }
            match direction {
                WalkDirection::Forward => {
                    for to in self.base.successors(node) {
                        if !seen.contains(&to) && self.effective_count(node, to) > 0 {
                            stack.push(to);
                        }
                    }
                    if let Some(overlay_neighbors) = self.added_out.get(&node) {
                        for &to in overlay_neighbors {
                            if !seen.contains(&to) && self.effective_count(node, to) > 0 {
                                stack.push(to);
                            }
                        }
                    }
                }
                WalkDirection::Reverse => {
                    for from in self.base.predecessors(node) {
                        if !seen.contains(&from) && self.effective_count(from, node) > 0 {
                            stack.push(from);
                        }
                    }
                    if let Some(overlay_neighbors) = self.added_in.get(&node) {
                        for &from in overlay_neighbors {
                            if !seen.contains(&from) && self.effective_count(from, node) > 0 {
                                stack.push(from);
                            }
                        }
                    }
                }
            }
        }
        seen
    }

    fn has_neighbor(&self, node: ModuleId, direction: WalkDirection) -> bool {
        let check = |neighbor| {
            let (from, to) = match direction {
                WalkDirection::Forward => (node, neighbor),
                WalkDirection::Reverse => (neighbor, node),
            };
            self.effective_count(from, to) > 0
        };
        match direction {
            WalkDirection::Forward => {
                if self.base.successors(node).any(check) {
                    return true;
                }
                if let Some(overlay) = self.added_out.get(&node) {
                    if overlay.iter().any(|&n| check(n)) {
                        return true;
                    }
                }
            }
            WalkDirection::Reverse => {
                if self.base.predecessors(node).any(check) {
                    return true;
                }
                if let Some(overlay) = self.added_in.get(&node) {
                    if overlay.iter().any(|&n| check(n)) {
                        return true;
                    }
                }
            }
        }
        false
    }

    fn effective_count(&self, from: ModuleId, to: ModuleId) -> isize {
        self.base.edge_count(from, to) as isize + self.delta.get(&(from, to)).copied().unwrap_or(0)
    }
}

#[derive(Debug, Clone, Copy)]
enum WalkDirection {
    Forward,
    Reverse,
}

/// Module-keyed adjacency: `from → {to, ...}`. Cached on the
/// `IncrementalQuotient` so the overlay-aware simulator build can patch
/// it instead of rewalking every edge in `i_graph.edge_pairs()`.
type ISuccessorsMap = BTreeMap<ModuleId, BTreeSet<ModuleId>>;

/// Set of constraining-edge endpoints, as
/// `(from_module, to_module)` pairs. Cached snapshot of
/// `constraining_buckets.keys()` for the overlay path.
type ConstrainingPairs = BTreeSet<(ModuleId, ModuleId)>;

#[derive(Debug, Clone)]
struct IncrementalQuotient {
    i_graph: RollbackDiGraph<ModuleId>,
    constraining_graph: RollbackDiGraph<ModuleId>,
    constraining_buckets: BTreeMap<(ModuleId, ModuleId), ConstrainingBucket>,
    cross_rebinds: BTreeMap<OwnerEdgeId, CrossRebindEdge>,
    /// Chunk's residual module — the ESM DFS root. The Lemma 2
    /// simulator that decides candidate asymmetric I-SCCs needs to
    /// know which module gets the source_import_position reversal
    /// (residual) vs which use plain linker_position
    /// (every other module).
    residual: ModuleId,
    /// Lazily-computed base `EsmEvaluationSimulator` for the current
    /// committed I-graph / constraining-buckets state. Invalidated on
    /// every `add_current_edge` / `remove_current_edge` that mutates
    /// the underlying graphs. Used by `verdict()` and
    /// `verdict_touching()` directly, and by `build_simulator(Some(_))`
    /// when the overlay introduces no I-graph or constraining-edge
    /// changes (the no-op overlay short-circuit).
    cached_base_simulator: RefCell<Option<EsmEvaluationSimulator>>,
    /// Lazily-computed materialization of the base I-graph as an
    /// adjacency map keyed by source module. See `ISuccessorsMap`.
    /// Invalidated alongside the simulator cache.
    cached_base_i_successors: RefCell<Option<ISuccessorsMap>>,
    /// Lazily-computed snapshot of the constraining pairs set
    /// (`constraining_buckets.keys()`). See `ConstrainingPairs`.
    cached_base_constraining_pairs: RefCell<Option<ConstrainingPairs>>,
    /// `DEBUNDLE_TIMING=1` shadow-state: did the committed graphs
    /// change since the last time the gate path queried an SCC? Set
    /// in every `invalidate_cached_simulator` (push/undo/commit
    /// funnel) and cleared by `gate_perf_counters::shadow_snapshot_if_stale`
    /// after emulating one base-tarjan-scc rebuild. Stays at `false`
    /// when timing is disabled — no real cost in the normal path.
    /// `Cell` (not `RefCell`) because the value is `Copy` and we only
    /// read/write a single bool.
    base_snapshot_stale: Cell<bool>,
}

impl IncrementalQuotient {
    fn new(owner_graph: &OwnerGraph, partition: &Partition) -> Self {
        let mut quotient = Self {
            i_graph: RollbackDiGraph::new(),
            constraining_graph: RollbackDiGraph::new(),
            constraining_buckets: BTreeMap::new(),
            cross_rebinds: BTreeMap::new(),
            residual: partition.residual(),
            cached_base_simulator: RefCell::new(None),
            cached_base_i_successors: RefCell::new(None),
            cached_base_constraining_pairs: RefCell::new(None),
            // Start dirty so the first gate query emulates a fresh
            // base-snapshot rebuild — matches what a real
            // snapshot-per-push design would do on startup.
            base_snapshot_stale: Cell::new(true),
        };
        for edge in owner_graph.iter_edges() {
            quotient.add_current_edge(edge, partition, true);
        }
        quotient
    }

    /// Invalidate the cached base simulator and its precomputed input
    /// snapshots. Called from every mutating path that changes the
    /// I-graph adjacency or the constraining-edge buckets. Each is
    /// rebuilt lazily on the next read.
    fn invalidate_cached_simulator(&mut self) {
        *self.cached_base_simulator.borrow_mut() = None;
        *self.cached_base_i_successors.borrow_mut() = None;
        *self.cached_base_constraining_pairs.borrow_mut() = None;
        // DEBUNDLE_TIMING=1 only: the next gate query will emulate a
        // fresh base-SCC snapshot rebuild. The flag costs one
        // unconditional store per invalidation (a few thousand per
        // proposer run); cheap.
        self.base_snapshot_stale.set(true);
    }

    /// `DEBUNDLE_TIMING=1` only: if the committed graphs changed since
    /// the last gate query, run `tarjan_scc` on each base graph once
    /// (constraining and I) and record shape + time. This emulates
    /// the per-push cost a snapshot+clone design would pay. Cleared
    /// after the emulated rebuild so subsequent queries within the
    /// same delta window don't double-count.
    ///
    /// Disabled path: a single atomic-bool load followed by a
    /// `Cell::get` branch. Negligible overhead.
    fn maybe_record_base_snapshot(&self) {
        if !gate_perf_counters::enabled() {
            return;
        }
        if !self.base_snapshot_stale.get() {
            return;
        }
        gate_perf_counters::record_base_snapshot(&self.constraining_graph);
        gate_perf_counters::record_base_snapshot(&self.i_graph);
        self.base_snapshot_stale.set(false);
    }

    /// Borrow the base simulator, building it on demand if the cache
    /// is empty. The simulator is a function of `(i_graph,
    /// constraining_buckets, residual)` — all of which are stable
    /// between mutating calls — so the cached instance is reused
    /// across queries that don't apply an overlay.
    fn base_simulator(&self) -> std::cell::Ref<'_, EsmEvaluationSimulator> {
        {
            let mut slot = self.cached_base_simulator.borrow_mut();
            if slot.is_none() {
                let start = if gate_perf_counters::enabled() {
                    Some(Instant::now())
                } else {
                    None
                };
                let (i_successors, constraining_pairs) = self.effective_simulator_inputs(None);
                *slot = Some(EsmEvaluationSimulator::build(
                    &i_successors,
                    &constraining_pairs,
                    self.residual,
                ));
                gate_perf_counters::record_simulator_base_rebuild(
                    start.map(|start| gate_perf_counters::elapsed_to_u64(start.elapsed())),
                );
            }
        }
        std::cell::Ref::map(self.cached_base_simulator.borrow(), |opt| {
            opt.as_ref()
                .expect("cached_base_simulator was just populated")
        })
    }

    /// Borrow the base I-successor adjacency, recomputing it from the
    /// I-graph on first access after invalidation. Walks every base
    /// edge once per refresh — overlay queries clone-and-patch this
    /// instead of rewalking every edge per call.
    fn base_i_successors(&self) -> std::cell::Ref<'_, BTreeMap<ModuleId, BTreeSet<ModuleId>>> {
        {
            let mut slot = self.cached_base_i_successors.borrow_mut();
            if slot.is_none() {
                let mut succs: BTreeMap<ModuleId, BTreeSet<ModuleId>> = BTreeMap::new();
                for (from, to) in self.i_graph.edge_pairs() {
                    succs.entry(from).or_default().insert(to);
                }
                *slot = Some(succs);
            }
        }
        std::cell::Ref::map(self.cached_base_i_successors.borrow(), |opt| {
            opt.as_ref()
                .expect("cached_base_i_successors was just populated")
        })
    }

    /// Borrow the base constraining-pair set, recomputing it on first
    /// access after invalidation. Equivalent to
    /// `constraining_buckets.keys().copied().collect()` but cached
    /// across overlay queries.
    fn base_constraining_pairs(&self) -> std::cell::Ref<'_, BTreeSet<(ModuleId, ModuleId)>> {
        {
            let mut slot = self.cached_base_constraining_pairs.borrow_mut();
            if slot.is_none() {
                *slot = Some(self.constraining_buckets.keys().copied().collect());
            }
        }
        std::cell::Ref::map(self.cached_base_constraining_pairs.borrow(), |opt| {
            opt.as_ref()
                .expect("cached_base_constraining_pairs was just populated")
        })
    }

    fn marks(&self) -> (GraphMark, GraphMark) {
        (self.i_graph.mark(), self.constraining_graph.mark())
    }

    fn rollback_graphs(&mut self, i_mark: GraphMark, constraining_mark: GraphMark) {
        self.i_graph.rollback_to(i_mark);
        self.constraining_graph.rollback_to(constraining_mark);
        // Graph topology just changed; drop the cached simulator.
        self.invalidate_cached_simulator();
    }

    fn add_current_edge(
        &mut self,
        edge: &analysis::graph::OwnerEdge,
        partition: &Partition,
        update_graphs: bool,
    ) {
        // Gate-side view: keep cross-module at-init promoted edges.
        // See [`analysis::graph::partition_endpoints`] for why and
        // `tests::promoted_edge_in_aggregator_cycle_is_unrealizable`
        // for the regression fixture.
        let Some((from, to)) = analysis::graph::partition_endpoints(
            edge,
            partition,
            analysis::graph::EndpointView::Gate,
        ) else {
            return;
        };
        if edge.reason.is_rebind() {
            self.cross_rebinds.insert(
                edge.id,
                CrossRebindEdge {
                    from,
                    to,
                    owner_edge: edge.id,
                },
            );
            return;
        }

        // I-graph or constraining-bucket mutation invalidates the
        // cached base simulator.
        self.invalidate_cached_simulator();
        if update_graphs {
            self.i_graph.increment_edge(from, to);
        }
        if !edge.reason.constrains_init_order() {
            return;
        }
        if update_graphs {
            self.constraining_graph.increment_edge(from, to);
        }
        let bucket = self.constraining_buckets.entry((from, to)).or_default();
        bucket.insert_edge(edge.id, edge.reason.is_sequenced());
    }

    fn remove_current_edge(
        &mut self,
        edge: &analysis::graph::OwnerEdge,
        partition: &Partition,
        update_graphs: bool,
    ) {
        // Gate-side view: keep cross-module at-init promoted edges.
        // Must mirror `add_current_edge` (see
        // [`analysis::graph::partition_endpoints`]).
        let Some((from, to)) = analysis::graph::partition_endpoints(
            edge,
            partition,
            analysis::graph::EndpointView::Gate,
        ) else {
            return;
        };
        if edge.reason.is_rebind() {
            self.cross_rebinds.remove(&edge.id);
            return;
        }

        // I-graph or constraining-bucket mutation invalidates the
        // cached base simulator.
        self.invalidate_cached_simulator();
        if update_graphs {
            self.i_graph.decrement_edge(from, to);
        }
        if !edge.reason.constrains_init_order() {
            return;
        }
        if update_graphs {
            self.constraining_graph.decrement_edge(from, to);
        }
        let pair = (from, to);
        let mut remove_bucket = false;
        if let Some(bucket) = self.constraining_buckets.get_mut(&pair) {
            bucket.remove_edge(edge.id, edge.reason.is_sequenced());
            remove_bucket = bucket.is_empty();
        }
        if remove_bucket {
            self.constraining_buckets.remove(&pair);
        }
    }

    fn verdict(&self) -> RealizabilityVerdict {
        let mut verdict = RealizabilityVerdict {
            unrealizable_sccs: Vec::new(),
            cross_rebinds: self.cross_rebinds.values().cloned().collect(),
        };
        let mut reported = BTreeSet::<BTreeSet<ModuleId>>::new();

        for modules in self.constraining_graph.all_sccs() {
            if modules.len() < 2 {
                continue;
            }
            let constraining_owner_edges = self.constraining_edges_inside(&modules);
            reported.insert(modules.clone());
            verdict.unrealizable_sccs.push(SccDiagnosis {
                modules,
                constraining_owner_edges,
                rejection: SccRejection::MutualConstrainingCycle,
            });
        }

        let mut candidates: Vec<BTreeSet<ModuleId>> = Vec::new();
        for modules in self.i_graph.all_sccs() {
            if modules.len() < 2 || reported.contains(&modules) {
                continue;
            }
            let constraining_owner_edges = self.constraining_edges_inside(&modules);
            if constraining_owner_edges.is_empty() {
                continue;
            }
            candidates.push(modules);
        }
        if !candidates.is_empty() {
            let simulation = self.build_simulator(None);
            let constraining_pairs: BTreeSet<(ModuleId, ModuleId)> =
                self.constraining_buckets.keys().copied().collect();
            for modules in candidates {
                let tdz_pairs: Vec<(ModuleId, ModuleId)> = simulation
                    .tdz_pairs(&modules, &constraining_pairs)
                    .collect();
                if tdz_pairs.is_empty() {
                    continue;
                }
                let constraining_owner_edges = self.tdz_constraining_edges(&tdz_pairs, None);
                verdict.unrealizable_sccs.push(SccDiagnosis {
                    modules,
                    constraining_owner_edges,
                    rejection: SccRejection::EsmEvaluationTdz,
                });
            }
        }

        verdict
    }

    fn verdict_touching(&self, module: ModuleId) -> RealizabilityVerdict {
        let mut verdict = RealizabilityVerdict {
            unrealizable_sccs: Vec::new(),
            cross_rebinds: self.cross_rebinds_touching(module),
        };
        let mut reported = BTreeSet::<BTreeSet<ModuleId>>::new();
        let mut i_scc_had_constraining_pair = false;

        // `DEBUNDLE_TIMING=1` shadow path: see
        // `verdict_with_overlay_touching` for the rationale.
        self.maybe_record_base_snapshot();

        let constraining_modules = self.constraining_graph.scc_containing(module);
        let constraining_scc_size = constraining_modules.len();
        if constraining_modules.len() >= 2 {
            let constraining_owner_edges = self.constraining_edges_inside(&constraining_modules);
            reported.insert(constraining_modules.clone());
            verdict.unrealizable_sccs.push(SccDiagnosis {
                modules: constraining_modules,
                constraining_owner_edges,
                rejection: SccRejection::MutualConstrainingCycle,
            });
        }

        let i_modules = self.i_graph.scc_containing(module);
        let i_scc_size = i_modules.len();
        if i_modules.len() >= 2 && !reported.contains(&i_modules) {
            let any_constraining = self
                .constraining_buckets
                .keys()
                .any(|(from, to)| i_modules.contains(from) && i_modules.contains(to));
            i_scc_had_constraining_pair = any_constraining;
            if any_constraining {
                let simulation = self.build_simulator(None);
                let constraining_pairs: BTreeSet<(ModuleId, ModuleId)> =
                    self.constraining_buckets.keys().copied().collect();
                let tdz_pairs: Vec<(ModuleId, ModuleId)> = simulation
                    .tdz_pairs(&i_modules, &constraining_pairs)
                    .collect();
                if !tdz_pairs.is_empty() {
                    let constraining_owner_edges = self.tdz_constraining_edges(&tdz_pairs, None);
                    verdict.unrealizable_sccs.push(SccDiagnosis {
                        modules: i_modules,
                        constraining_owner_edges,
                        rejection: SccRejection::EsmEvaluationTdz,
                    });
                }
            }
        }

        gate_perf_counters::record_verdict_touching(
            false,
            constraining_scc_size,
            i_scc_size,
            i_scc_had_constraining_pair,
            !verdict.is_realizable(),
        );
        verdict
    }

    fn verdict_with_overlay_touching(
        &self,
        module: ModuleId,
        overlay: &QuotientOverlay,
    ) -> RealizabilityVerdict {
        let mut verdict = RealizabilityVerdict {
            unrealizable_sccs: Vec::new(),
            cross_rebinds: self.cross_rebinds_touching_with_overlay(module, overlay),
        };
        let mut reported = BTreeSet::<BTreeSet<ModuleId>>::new();
        let mut i_scc_had_constraining_pair = false;

        // `DEBUNDLE_TIMING=1` shadow path: if the base graphs changed
        // since the last gate query, emulate the snapshot-per-push
        // design by running `tarjan_scc` on each base graph once and
        // recording shape + time. Cleared after the emulated rebuild.
        self.maybe_record_base_snapshot();

        let constraining_graph =
            OverlayGraphView::new(&self.constraining_graph, &overlay.constraining_delta);
        let constraining_modules = constraining_graph.scc_containing(module);
        let constraining_scc_size = constraining_modules.len();
        if constraining_modules.len() >= 2 {
            let constraining_owner_edges =
                self.constraining_edges_inside_with_overlay(&constraining_modules, overlay);
            reported.insert(constraining_modules.clone());
            verdict.unrealizable_sccs.push(SccDiagnosis {
                modules: constraining_modules,
                constraining_owner_edges,
                rejection: SccRejection::MutualConstrainingCycle,
            });
        }

        let i_graph_view = OverlayGraphView::new(&self.i_graph, &overlay.i_delta);
        let i_modules = i_graph_view.scc_containing(module);
        let i_scc_size = i_modules.len();
        if i_modules.len() >= 2 && !reported.contains(&i_modules) {
            let constraining_pairs = self.constraining_pairs_with_overlay(overlay);
            let any_inside_scc = constraining_pairs.iter().any(|(from, to)| {
                i_modules.contains(from)
                    && i_modules.contains(to)
                    && !self
                        .constraining_bucket_with_overlay((*from, *to), overlay)
                        .is_empty()
            });
            i_scc_had_constraining_pair = any_inside_scc;
            if any_inside_scc {
                let simulation = self.build_simulator(Some(overlay));
                let effective_pairs: BTreeSet<(ModuleId, ModuleId)> = constraining_pairs
                    .into_iter()
                    .filter(|pair| {
                        !self
                            .constraining_bucket_with_overlay(*pair, overlay)
                            .is_empty()
                    })
                    .collect();
                let tdz_pairs: Vec<(ModuleId, ModuleId)> =
                    simulation.tdz_pairs(&i_modules, &effective_pairs).collect();
                if !tdz_pairs.is_empty() {
                    let constraining_owner_edges =
                        self.tdz_constraining_edges(&tdz_pairs, Some(overlay));
                    verdict.unrealizable_sccs.push(SccDiagnosis {
                        modules: i_modules,
                        constraining_owner_edges,
                        rejection: SccRejection::EsmEvaluationTdz,
                    });
                }
            }
        }

        gate_perf_counters::record_verdict_touching(
            true,
            constraining_scc_size,
            i_scc_size,
            i_scc_had_constraining_pair,
            !verdict.is_realizable(),
        );
        verdict
    }

    /// Resolve a list of TDZ-violating `(from, to)` pairs to their
    /// owner-edge ids, optionally applying `overlay`'s edits. Used
    /// by `verdict*` to surface only the surgical set of
    /// constraining edges the simulator flagged.
    fn tdz_constraining_edges(
        &self,
        tdz_pairs: &[(ModuleId, ModuleId)],
        overlay: Option<&QuotientOverlay>,
    ) -> Vec<OwnerEdgeId> {
        let mut edges: Vec<OwnerEdgeId> = Vec::new();
        for &pair in tdz_pairs {
            let bucket = match overlay {
                Some(overlay) => self.constraining_bucket_with_overlay(pair, overlay),
                None => self
                    .constraining_buckets
                    .get(&pair)
                    .cloned()
                    .unwrap_or_default(),
            };
            edges.extend(bucket.evidence_edges());
        }
        edges.sort();
        edges
    }

    /// Build an ESM evaluation simulator from the current quotient
    /// state, optionally applying `overlay`'s I-graph and
    /// constraining-pair edits. Used by every `verdict*` to decide
    /// whether Lemma 2 rescues a candidate asymmetric I-SCC.
    ///
    /// Fast path: when `overlay` is `None` *or* the overlay introduces
    /// no I-graph and no constraining-bucket changes (a no-op merge
    /// from the simulator's perspective), return the cached base
    /// simulator without recomputing. Otherwise, fall through to
    /// `build_simulator_from_scratch`.
    fn build_simulator(&self, overlay: Option<&QuotientOverlay>) -> EsmEvaluationSimulator {
        let structural_noop = overlay_is_simulator_noop(overlay);
        gate_perf_counters::record_simulator_request(structural_noop);
        if structural_noop {
            return self.base_simulator().clone();
        }
        self.build_simulator_from_scratch(overlay)
    }

    /// Cold-path simulator construction. Materializes the effective
    /// `i_successors` / `constraining_pairs` via
    /// `effective_simulator_inputs` (cached base + cheap overlay
    /// patch) and calls `EsmEvaluationSimulator::build`. Used by:
    ///   - the lazy build of `cached_base_simulator` (overlay = None,
    ///     via `base_simulator`),
    ///   - overlay queries whose overlay actually changes the
    ///     I-graph or constraining-bucket structure (slow path of
    ///     `build_simulator`).
    fn build_simulator_from_scratch(
        &self,
        overlay: Option<&QuotientOverlay>,
    ) -> EsmEvaluationSimulator {
        let start = if gate_perf_counters::enabled() {
            Some(Instant::now())
        } else {
            None
        };
        let (i_successors, constraining_pairs) = self.effective_simulator_inputs(overlay);
        let simulator =
            EsmEvaluationSimulator::build(&i_successors, &constraining_pairs, self.residual);
        gate_perf_counters::record_simulator_overlay_rebuild(
            start.map(|start| gate_perf_counters::elapsed_to_u64(start.elapsed())),
        );
        simulator
    }

    /// Materialize `(i_successors, constraining_pairs)` — the inputs
    /// `EsmEvaluationSimulator::build` consumes — for the current
    /// committed state with `overlay`'s edits applied. Factored out of
    /// `build_simulator_from_scratch` because the overlay-pair
    /// assembly is the per-call cost we measure separately from the
    /// simulator's own toposort + DFS.
    ///
    /// The base case (`overlay = None`) returns clones of
    /// `base_i_successors()` / `base_constraining_pairs()` — the
    /// shared caches refreshed on the next mutation. The overlay
    /// case applies the overlay's small `i_delta` / constraining
    /// edits to a cloned base, which is `O(|overlay|)` instead of the
    /// previous `O(|base_edges|)` per call.
    fn effective_simulator_inputs(
        &self,
        overlay: Option<&QuotientOverlay>,
    ) -> (ISuccessorsMap, ConstrainingPairs) {
        let Some(overlay) = overlay else {
            return (
                self.base_i_successors().clone(),
                self.base_constraining_pairs().clone(),
            );
        };
        // Overlay path: clone base + apply deltas. Each i_delta entry
        // is either an addition (count > 0) or a removal (count < 0,
        // offsetting an existing base edge). The effective edge set
        // is the symmetric difference described by `effective_count`.
        let mut i_successors = self.base_i_successors().clone();
        for (&(from, to), &count) in &overlay.i_delta {
            let base = self.i_graph.edge_count(from, to) as isize;
            let effective = base + count;
            if effective > 0 {
                i_successors.entry(from).or_default().insert(to);
            } else {
                // Effective edge dropped from the base set.
                if let Some(succs) = i_successors.get_mut(&from) {
                    succs.remove(&to);
                    if succs.is_empty() {
                        i_successors.remove(&from);
                    }
                }
            }
        }
        let mut constraining_pairs = self.base_constraining_pairs().clone();
        // Only pairs the overlay actually touched need re-evaluation
        // against the effective bucket: untouched pairs keep the base
        // bucket (which is non-empty by construction of
        // `constraining_buckets`).
        let touched_pairs: BTreeSet<(ModuleId, ModuleId)> = overlay
            .constraining_added
            .keys()
            .chain(overlay.constraining_removed.keys())
            .copied()
            .collect();
        for pair in &touched_pairs {
            if self
                .constraining_bucket_with_overlay(*pair, overlay)
                .is_empty()
            {
                constraining_pairs.remove(pair);
            } else {
                constraining_pairs.insert(*pair);
            }
        }
        (i_successors, constraining_pairs)
    }

    fn overlay_for_move(
        &self,
        owner_graph: &OwnerGraph,
        partition: &Partition,
        owners: &[OwnerId],
        to: ModuleId,
    ) -> QuotientOverlay {
        let impacted_edges = impacted_owner_edges(owner_graph, owners);
        let owners: BTreeSet<OwnerId> = owners.iter().copied().collect();
        let mut overlay = QuotientOverlay::default();
        for edge_id in impacted_edges {
            let edge = owner_graph.edge(edge_id);
            let current = edge_contribution(edge, partition.of(edge.from), partition.of(edge.to));
            let next_from = if owners.contains(&edge.from) {
                to
            } else {
                partition.of(edge.from)
            };
            let next_to = if owners.contains(&edge.to) {
                to
            } else {
                partition.of(edge.to)
            };
            let next = edge_contribution(edge, next_from, next_to);
            if current == next {
                continue;
            }
            if let Some(contribution) = current {
                overlay.remove_contribution(contribution);
            }
            if let Some(contribution) = next {
                overlay.add_contribution(contribution);
            }
        }
        overlay
    }

    fn cross_rebinds_touching_with_overlay(
        &self,
        module: ModuleId,
        overlay: &QuotientOverlay,
    ) -> Vec<CrossRebindEdge> {
        let mut rebinds: Vec<CrossRebindEdge> = self
            .cross_rebinds
            .iter()
            .filter(|(edge_id, rebind)| {
                !overlay.cross_rebind_removed.contains(edge_id)
                    && (rebind.from == module || rebind.to == module)
            })
            .map(|(_, rebind)| rebind.clone())
            .collect();
        rebinds.extend(
            overlay
                .cross_rebind_added
                .values()
                .filter(|rebind| rebind.from == module || rebind.to == module)
                .cloned(),
        );
        rebinds.sort_by_key(|rebind| rebind.owner_edge);
        rebinds
    }

    fn cross_rebinds_touching(&self, module: ModuleId) -> Vec<CrossRebindEdge> {
        let mut rebinds: Vec<CrossRebindEdge> = self
            .cross_rebinds
            .values()
            .filter(|rebind| rebind.from == module || rebind.to == module)
            .cloned()
            .collect();
        rebinds.sort_by_key(|rebind| rebind.owner_edge);
        rebinds
    }

    fn constraining_edges_inside(&self, modules: &BTreeSet<ModuleId>) -> Vec<OwnerEdgeId> {
        let mut edges = Vec::new();
        for ((from, to), bucket) in &self.constraining_buckets {
            if modules.contains(from) && modules.contains(to) {
                edges.extend(bucket.evidence_edges());
            }
        }
        edges.sort();
        edges
    }

    fn constraining_edges_inside_with_overlay(
        &self,
        modules: &BTreeSet<ModuleId>,
        overlay: &QuotientOverlay,
    ) -> Vec<OwnerEdgeId> {
        let mut edges = Vec::new();
        for pair in self.constraining_pairs_with_overlay(overlay) {
            if modules.contains(&pair.0) && modules.contains(&pair.1) {
                edges.extend(
                    self.constraining_bucket_with_overlay(pair, overlay)
                        .evidence_edges(),
                );
            }
        }
        edges.sort();
        edges
    }

    fn constraining_pairs_with_overlay(
        &self,
        overlay: &QuotientOverlay,
    ) -> BTreeSet<(ModuleId, ModuleId)> {
        let mut pairs: BTreeSet<(ModuleId, ModuleId)> =
            self.constraining_buckets.keys().copied().collect();
        pairs.extend(overlay.constraining_added.keys().copied());
        pairs.extend(overlay.constraining_removed.keys().copied());
        pairs
    }

    fn constraining_bucket_with_overlay(
        &self,
        pair: (ModuleId, ModuleId),
        overlay: &QuotientOverlay,
    ) -> ConstrainingBucket {
        let mut bucket = self
            .constraining_buckets
            .get(&pair)
            .cloned()
            .unwrap_or_default();
        if let Some(removed) = overlay.constraining_removed.get(&pair) {
            bucket.remove_from(removed);
        }
        if let Some(added) = overlay.constraining_added.get(&pair) {
            bucket.extend_from(added);
        }
        bucket
    }
}

/// Mutable index over a working partition. The single shared
/// implementation of the three-clause predicate, exposed in the
/// transactional shape docs/design.md "Realizability primitive" prescribes.
///
/// Each `push` snapshots the prior assignments of the touched owners,
/// updates only quotient edge buckets incident to those owners, and
/// records enough graph state for LIFO undo. `verdict()` reads the
/// maintained quotient graph instead of rebuilding it from owner edges.
///
/// The index does NOT hold a borrow of `OwnerGraph`. Every mutating
/// method (`push`, `undo`, `scoped`) and every `*_after_moving_owners*`
/// verdict query takes the graph as a parameter. Storing the borrow
/// would force callers that also own the graph (e.g., the peel kernel's
/// `QuotientGraph`) into a self-referential struct; passing the graph
/// per call keeps that ownership flat.
#[derive(Debug, Clone)]
pub struct RealizabilityIndex {
    partition: Partition,
    quotient: IncrementalQuotient,
    journal: Vec<JournalEntry>,
}

impl RealizabilityIndex {
    pub fn from_partition(owner_graph: &OwnerGraph, partition: Partition) -> Self {
        let quotient = IncrementalQuotient::new(owner_graph, &partition);
        Self {
            partition,
            quotient,
            journal: Vec::new(),
        }
    }

    /// Borrow the current working partition. Callers should treat this
    /// as read-only — mutation should go through `push`/`undo` so the
    /// journal stays consistent.
    pub fn partition(&self) -> &Partition {
        &self.partition
    }

    /// Apply `delta` and record its inverse on the journal. Returns a
    /// handle that the matching `undo` consumes.
    ///
    /// Prefer [`Self::scoped`] when the delta lifetime is lexical —
    /// the `push`/`undo` pair is then guaranteed to be balanced and
    /// LIFO-ordered without manual bookkeeping. The raw `push`/`undo`
    /// surface exists only for the `peel/quotient.rs` cases that
    /// `scoped` cannot express:
    /// * `commit_merge`: a batch of deltas lands permanently with no
    ///   matching undo.
    /// * `verdict_after_chained_deltas`: push a batch, read the post-
    ///   push verdict, then undo every handle in reverse order.
    ///
    /// All other callers must use [`Self::scoped`]; the
    /// `unbalanced_journal_push_undo_should_not_compile` doctest below
    /// is the contract.
    pub fn push(&mut self, owner_graph: &OwnerGraph, delta: PartitionDelta) -> DeltaHandle {
        let entry = match delta {
            PartitionDelta::MoveOwners { owners, to } => {
                let owners: Vec<OwnerId> = owners
                    .into_iter()
                    .collect::<BTreeSet<_>>()
                    .into_iter()
                    .collect();
                let impacted_edges = impacted_owner_edges(owner_graph, &owners);
                let (i_graph_mark, constraining_graph_mark) = self.quotient.marks();
                for edge_id in &impacted_edges {
                    let edge = owner_graph.edge(*edge_id);
                    self.quotient
                        .remove_current_edge(edge, &self.partition, true);
                }

                let mut prior = Vec::with_capacity(owners.len());
                for owner in owners {
                    let was = self.partition.of(owner);
                    if was != to {
                        self.partition.set(owner, to);
                    }
                    prior.push((owner, was));
                }
                for edge_id in &impacted_edges {
                    let edge = owner_graph.edge(*edge_id);
                    self.quotient.add_current_edge(edge, &self.partition, true);
                }
                JournalEntry {
                    prior_assignments: prior,
                    impacted_edges,
                    i_graph_mark,
                    constraining_graph_mark,
                }
            }
        };
        let handle = DeltaHandle(self.journal.len());
        self.journal.push(entry);
        handle
    }

    /// Roll back the delta identified by `handle`. Must be the top of
    /// the journal; panics otherwise — also in release builds, since
    /// the index backs committed planner state and an out-of-LIFO
    /// undo silently corrupts the maintained quotient. `pub` for the
    /// same peel-internal reasons as [`Self::push`] — prefer
    /// [`Self::scoped`].
    pub fn undo(&mut self, owner_graph: &OwnerGraph, handle: DeltaHandle) {
        assert_eq!(
            handle.0 + 1,
            self.journal.len(),
            "RealizabilityIndex::undo called out of LIFO order \
             (handle {:?}, journal depth {})",
            handle,
            self.journal.len(),
        );
        let entry = self
            .journal
            .pop()
            .expect("journal must be non-empty for undo");
        for edge_id in &entry.impacted_edges {
            let edge = owner_graph.edge(*edge_id);
            self.quotient
                .remove_current_edge(edge, &self.partition, false);
        }
        for (owner, prior) in entry.prior_assignments {
            self.partition.set(owner, prior);
        }
        for edge_id in &entry.impacted_edges {
            let edge = owner_graph.edge(*edge_id);
            self.quotient.add_current_edge(edge, &self.partition, false);
        }
        self.quotient
            .rollback_graphs(entry.i_graph_mark, entry.constraining_graph_mark);
    }

    /// Discard rollback state for every delta pushed so far. Call
    /// after a batch of **permanent** pushes (the `commit_merge` case
    /// above) — committed deltas are never undone, and without this
    /// truncation their journal entries (inverse assignments,
    /// impacted-edge lists, and the two graphs' edge journals)
    /// accumulate for the lifetime of the index.
    ///
    /// Caller contract: no outstanding [`DeltaHandle`] may be undone
    /// after `commit` (the journal is cleared, so any such `undo`
    /// panics on the LIFO check). The peel kernel satisfies this by
    /// construction — speculative push/undo pairs are scoped and
    /// balanced before any commit.
    pub fn commit(&mut self) {
        self.journal.clear();
        self.quotient.i_graph.commit();
        self.quotient.constraining_graph.commit();
    }

    /// Apply `delta`, run `f` against the index in its post-push
    /// state, then undo. The scoped form guarantees the per-call
    /// push/undo pair regardless of `f`'s control flow.
    pub fn scoped<F, R>(&mut self, owner_graph: &OwnerGraph, delta: PartitionDelta, f: F) -> R
    where
        F: FnOnce(&mut Self) -> R,
    {
        let handle = self.push(owner_graph, delta);
        let result = f(self);
        self.undo(owner_graph, handle);
        result
    }

    /// Verdict against the current working partition. Reads the
    /// incrementally maintained quotient graph and evidence buckets.
    pub fn verdict(&self) -> RealizabilityVerdict {
        self.quotient.verdict()
    }

    /// Verdict filtered to SCCs and cross-rebinds touching `module`.
    /// Candidate evaluation uses this for the fresh hypothetical
    /// destination: unrelated pre-existing bad SCCs are intentionally
    /// ignored, matching the previous full-verdict-then-filter logic.
    pub fn verdict_touching(&self, module: ModuleId) -> RealizabilityVerdict {
        self.quotient.verdict_touching(module)
    }

    /// Verdict for a hypothetical owner move, filtered to the target
    /// module, without mutating the working partition. This is the
    /// candidate-evaluation fast path: it builds a small quotient
    /// overlay for the moved owners' incident edges and runs directed
    /// reachability against the effective graph.
    pub fn verdict_after_moving_owners_touching(
        &self,
        owner_graph: &OwnerGraph,
        owners: &[OwnerId],
        to: ModuleId,
    ) -> RealizabilityVerdict {
        let overlay = self
            .quotient
            .overlay_for_move(owner_graph, &self.partition, owners, to);
        self.quotient.verdict_with_overlay_touching(to, &overlay)
    }
}

/// True when `overlay` introduces no changes the ESM evaluation
/// simulator can observe — i.e. `i_delta` is empty AND no constraining
/// bucket is added/removed. Cross-rebind edits do not affect the
/// simulator (rebinds participate in the rebind verdict, not the
/// I-graph DFS or constraining-edge SCC). Used by
/// `IncrementalQuotient::build_simulator`'s fast path to reuse the
/// cached base simulator.
fn overlay_is_simulator_noop(overlay: Option<&QuotientOverlay>) -> bool {
    let Some(overlay) = overlay else {
        return true;
    };
    overlay.i_delta.is_empty()
        && overlay.constraining_added.is_empty()
        && overlay.constraining_removed.is_empty()
}

fn impacted_owner_edges(owner_graph: &OwnerGraph, owners: &[OwnerId]) -> Vec<OwnerEdgeId> {
    let mut impacted = BTreeSet::<OwnerEdgeId>::new();
    for owner in owners {
        impacted.extend(owner_graph.out_edges_of(*owner).iter().copied());
        impacted.extend(owner_graph.in_edges_of(*owner).iter().copied());
        // Edges whose [`EdgeRole::PromotedAtInit`] callee_owner is in
        // the move set — `EdgeRole::is_cross_module_promotion`'s
        // verdict depends on `partition.of(callee_owner)`, so moving
        // a callee owner can flip an edge's contribution between
        // "skipped (intra-callee-module)" and "counted
        // (cross-callee-module)" without the callee owner appearing
        // on `from`/`to`.
        // Resolved via the precomputed `callee_edges` CSR instead of
        // a per-call full-edge-list scan.
        impacted.extend(owner_graph.callee_edges_of(*owner).iter().copied());
    }
    impacted.into_iter().collect()
}

// ---------------------------------------------------------------------------
// Gate-path performance counters.
//
// Permanent diagnostics for `OverlayGraphView::scc_containing` and the
// adjacent realizability-gate costs. Cheap counters and bounded
// histograms are recorded on every run; `DEBUNDLE_TIMING` gates
// wall-clock timing, stderr reporting, and the expensive shadow
// base-Tarjan measurement that emulates snapshot rebuild cost.
//
// Records:
//   * `scc_containing` call count, split overlay-empty vs overlay-non-empty.
//   * Cumulative `scc_containing` wall time.
//   * Overlay shape histograms per call: `delta.len()`, additions
//     (edges whose effective count crosses 0 → positive),
//     removals (edges whose effective count crosses positive → 0 or
//     below).
//   * Base graph shape recorded on each emulated "base SCC snapshot
//     rebuild": `nodes_count`, `distinct_edges_count`, `sccs_count`,
//     `condensation_edges_count`. One snapshot is built per
//     stale→fresh transition (mirrors a snapshot-per-push design).
//   * Base-graph `tarjan_scc` call count + cumulative wall time.
//
// Output: an RAII reporter (`SccTimingReporter`) installed early in
// `main` when `DEBUNDLE_TIMING=1` prints the tally to stderr on drop.
// ---------------------------------------------------------------------------
pub mod gate_perf_counters {
    use super::*;

    /// One-time cached answer for "is `DEBUNDLE_TIMING` set in the
    /// process env?" Resolved at first query. The first call is the
    /// only one that touches `std::env::var_os`; every later call is
    /// an atomic load.
    static TIMING_ENABLED: OnceLock<bool> = OnceLock::new();

    #[inline]
    pub(super) fn enabled() -> bool {
        *TIMING_ENABLED.get_or_init(|| std::env::var_os("DEBUNDLE_TIMING").is_some())
    }

    // -- scc_containing per-call counters ----------------------------------

    pub(super) static SCC_CALLS_TOTAL: AtomicUsize = AtomicUsize::new(0);
    pub(super) static SCC_CALLS_OVERLAY_EMPTY: AtomicUsize = AtomicUsize::new(0);
    pub(super) static SCC_CALLS_OVERLAY_NONEMPTY: AtomicUsize = AtomicUsize::new(0);
    /// Cumulative wall time of `OverlayGraphView::scc_containing` in
    /// nanoseconds.
    pub(super) static SCC_NANOS: AtomicU64 = AtomicU64::new(0);

    // -- Base-graph tarjan_scc counters ------------------------------------

    pub(super) static BASE_TARJAN_CALLS: AtomicUsize = AtomicUsize::new(0);
    pub(super) static BASE_TARJAN_NANOS: AtomicU64 = AtomicU64::new(0);

    // -- Gate verdict / simulator counters ---------------------------------

    pub(super) static VERDICT_TOUCHING_CALLS: AtomicUsize = AtomicUsize::new(0);
    pub(super) static VERDICT_WITH_OVERLAY_CALLS: AtomicUsize = AtomicUsize::new(0);
    pub(super) static VERDICT_REALIZABLE: AtomicUsize = AtomicUsize::new(0);
    pub(super) static VERDICT_REJECTED: AtomicUsize = AtomicUsize::new(0);
    pub(super) static I_SCC_WITH_CONSTRAINING: AtomicUsize = AtomicUsize::new(0);

    pub(super) static SIMULATOR_REQUESTS: AtomicUsize = AtomicUsize::new(0);
    pub(super) static SIMULATOR_STRUCTURAL_NOOP: AtomicUsize = AtomicUsize::new(0);
    pub(super) static SIMULATOR_STRUCTURAL_CHANGED: AtomicUsize = AtomicUsize::new(0);
    pub(super) static SIMULATOR_BASE_REBUILDS: AtomicUsize = AtomicUsize::new(0);
    pub(super) static SIMULATOR_BASE_REBUILD_NANOS: AtomicU64 = AtomicU64::new(0);
    pub(super) static SIMULATOR_OVERLAY_REBUILDS: AtomicUsize = AtomicUsize::new(0);
    pub(super) static SIMULATOR_OVERLAY_REBUILD_NANOS: AtomicU64 = AtomicU64::new(0);

    pub(super) static DIAGNOSTIC_TRANSLATION_CALLS: AtomicUsize = AtomicUsize::new(0);
    pub(super) static DIAGNOSTIC_TRANSLATION_ACTIVE: AtomicUsize = AtomicUsize::new(0);

    // -- Reservoir-sampled histograms --------------------------------------
    //
    // Each histogram keeps at most `RESERVOIR_CAP` samples via
    // Algorithm R (uniform reservoir sampling). That's enough resolution
    // for stable median / p95 on the tana workload (~4400 calls; the
    // reservoir holds 4096 entries and never trims). Storing the
    // reservoir in a `Mutex<Vec<u32>>` is cheap under the proposer's
    // single-threaded driver.
    pub(super) const RESERVOIR_CAP: usize = 4096;

    pub(super) struct Histogram {
        samples: Mutex<Vec<u32>>,
        count: AtomicU64,
        sum: AtomicU64,
        min: AtomicU64,
        max: AtomicU64,
    }

    impl Histogram {
        const fn new() -> Self {
            Self {
                samples: Mutex::new(Vec::new()),
                count: AtomicU64::new(0),
                sum: AtomicU64::new(0),
                min: AtomicU64::new(u64::MAX),
                max: AtomicU64::new(0),
            }
        }

        pub(super) fn record(&self, value: u32) {
            let v = value as u64;
            let n = self.count.fetch_add(1, Ordering::Relaxed);
            self.sum.fetch_add(v, Ordering::Relaxed);
            // Lock-free min/max update via CAS.
            let mut cur_min = self.min.load(Ordering::Relaxed);
            while v < cur_min {
                match self.min.compare_exchange_weak(
                    cur_min,
                    v,
                    Ordering::Relaxed,
                    Ordering::Relaxed,
                ) {
                    Ok(_) => break,
                    Err(x) => cur_min = x,
                }
            }
            let mut cur_max = self.max.load(Ordering::Relaxed);
            while v > cur_max {
                match self.max.compare_exchange_weak(
                    cur_max,
                    v,
                    Ordering::Relaxed,
                    Ordering::Relaxed,
                ) {
                    Ok(_) => break,
                    Err(x) => cur_max = x,
                }
            }
            // Reservoir sample. Single-threaded under the proposer,
            // so contention here is zero; we keep the lock anyway so
            // the reporter on Drop sees a consistent vec.
            let mut samples = self.samples.lock().expect("Histogram mutex poisoned");
            if samples.len() < RESERVOIR_CAP {
                samples.push(value);
            } else {
                // Deterministic-ish: use n as the random index source.
                // Not statistically perfect but adequate for proposer
                // diagnostic purposes; we'll see the full call stream
                // for tana (~4400 calls; cap is 4096, so we keep ~93%).
                let idx = (n as usize).wrapping_mul(2654435761) % (n as usize + 1);
                if idx < RESERVOIR_CAP {
                    samples[idx] = value;
                }
            }
        }

        fn snapshot(&self) -> HistogramSnapshot {
            let count = self.count.load(Ordering::Relaxed);
            let sum = self.sum.load(Ordering::Relaxed);
            let min = self.min.load(Ordering::Relaxed);
            let max = self.max.load(Ordering::Relaxed);
            let samples = self
                .samples
                .lock()
                .expect("Histogram mutex poisoned")
                .clone();
            HistogramSnapshot {
                count,
                sum,
                min: if count == 0 { 0 } else { min },
                max,
                samples,
            }
        }
    }

    pub(super) struct HistogramSnapshot {
        pub count: u64,
        pub sum: u64,
        pub min: u64,
        pub max: u64,
        pub samples: Vec<u32>,
    }

    impl HistogramSnapshot {
        pub(super) fn percentile(&self, p: f64) -> u32 {
            if self.samples.is_empty() {
                return 0;
            }
            let mut sorted = self.samples.clone();
            sorted.sort_unstable();
            let idx = ((sorted.len() as f64 - 1.0) * p).round() as usize;
            sorted[idx.min(sorted.len() - 1)]
        }

        pub(super) fn mean(&self) -> f64 {
            if self.count == 0 {
                0.0
            } else {
                self.sum as f64 / self.count as f64
            }
        }
    }

    pub(super) static OVERLAY_DELTA_LEN: Histogram = Histogram::new();
    pub(super) static OVERLAY_ADDITIONS: Histogram = Histogram::new();
    pub(super) static OVERLAY_REMOVALS: Histogram = Histogram::new();
    pub(super) static BASE_NODES: Histogram = Histogram::new();
    pub(super) static BASE_EDGES: Histogram = Histogram::new();
    pub(super) static BASE_SCCS: Histogram = Histogram::new();
    pub(super) static BASE_COND_EDGES: Histogram = Histogram::new();
    pub(super) static CONSTRAINING_SCC_SIZE: Histogram = Histogram::new();
    pub(super) static I_SCC_SIZE: Histogram = Histogram::new();
    pub(super) static DIAGNOSTIC_OWNER_COUNT: Histogram = Histogram::new();
    pub(super) static DIAGNOSTIC_SCC_COUNT: Histogram = Histogram::new();

    /// Record a single `scc_containing` call's per-call shape.
    /// `delta_len` is `overlay.delta.len()`; `additions` is the number
    /// of overlay edges whose effective count > 0 (i.e. they manifest
    /// as a new edge in the effective graph relative to base);
    /// `removals` is the number whose effective count is ≤ 0 (the
    /// overlay zeroes out a base edge).
    pub(super) fn record_call(
        nanos: Option<u64>,
        overlay_empty: bool,
        delta_len: usize,
        additions: usize,
        removals: usize,
    ) {
        SCC_CALLS_TOTAL.fetch_add(1, Ordering::Relaxed);
        if overlay_empty {
            SCC_CALLS_OVERLAY_EMPTY.fetch_add(1, Ordering::Relaxed);
        } else {
            SCC_CALLS_OVERLAY_NONEMPTY.fetch_add(1, Ordering::Relaxed);
        }
        if let Some(nanos) = nanos {
            SCC_NANOS.fetch_add(nanos, Ordering::Relaxed);
        }
        OVERLAY_DELTA_LEN.record(delta_len.min(u32::MAX as usize) as u32);
        OVERLAY_ADDITIONS.record(additions.min(u32::MAX as usize) as u32);
        OVERLAY_REMOVALS.record(removals.min(u32::MAX as usize) as u32);
    }

    pub(super) fn record_verdict_touching(
        overlay: bool,
        constraining_scc_size: usize,
        i_scc_size: usize,
        i_scc_had_constraining_pair: bool,
        rejected: bool,
    ) {
        VERDICT_TOUCHING_CALLS.fetch_add(1, Ordering::Relaxed);
        if overlay {
            VERDICT_WITH_OVERLAY_CALLS.fetch_add(1, Ordering::Relaxed);
        }
        if rejected {
            VERDICT_REJECTED.fetch_add(1, Ordering::Relaxed);
        } else {
            VERDICT_REALIZABLE.fetch_add(1, Ordering::Relaxed);
        }
        if i_scc_had_constraining_pair {
            I_SCC_WITH_CONSTRAINING.fetch_add(1, Ordering::Relaxed);
        }
        CONSTRAINING_SCC_SIZE.record(constraining_scc_size.min(u32::MAX as usize) as u32);
        I_SCC_SIZE.record(i_scc_size.min(u32::MAX as usize) as u32);
    }

    pub(super) fn record_simulator_request(structural_noop: bool) {
        SIMULATOR_REQUESTS.fetch_add(1, Ordering::Relaxed);
        if structural_noop {
            SIMULATOR_STRUCTURAL_NOOP.fetch_add(1, Ordering::Relaxed);
        } else {
            SIMULATOR_STRUCTURAL_CHANGED.fetch_add(1, Ordering::Relaxed);
        }
    }

    pub(super) fn record_simulator_base_rebuild(nanos: Option<u64>) {
        SIMULATOR_BASE_REBUILDS.fetch_add(1, Ordering::Relaxed);
        if let Some(nanos) = nanos {
            SIMULATOR_BASE_REBUILD_NANOS.fetch_add(nanos, Ordering::Relaxed);
        }
    }

    pub(super) fn record_simulator_overlay_rebuild(nanos: Option<u64>) {
        SIMULATOR_OVERLAY_REBUILDS.fetch_add(1, Ordering::Relaxed);
        if let Some(nanos) = nanos {
            SIMULATOR_OVERLAY_REBUILD_NANOS.fetch_add(nanos, Ordering::Relaxed);
        }
    }

    pub(super) fn record_diagnostic_translation(
        active: bool,
        owner_count: usize,
        scc_count: usize,
    ) {
        DIAGNOSTIC_TRANSLATION_CALLS.fetch_add(1, Ordering::Relaxed);
        if active {
            DIAGNOSTIC_TRANSLATION_ACTIVE.fetch_add(1, Ordering::Relaxed);
        }
        DIAGNOSTIC_OWNER_COUNT.record(owner_count.min(u32::MAX as usize) as u32);
        DIAGNOSTIC_SCC_COUNT.record(scc_count.min(u32::MAX as usize) as u32);
    }

    /// Record one emulated base-SCC snapshot rebuild. Runs `tarjan_scc`
    /// on the base graph, records the call count + time + shape. Only
    /// invoked when `DEBUNDLE_TIMING=1`.
    pub(super) fn record_base_snapshot(graph: &RollbackDiGraph<ModuleId>) {
        let nodes = graph.node_count();
        let edges = graph.distinct_edge_count();
        let start = Instant::now();
        let sccs = graph.all_sccs();
        let elapsed_nanos = elapsed_to_u64(start.elapsed());
        BASE_TARJAN_CALLS.fetch_add(1, Ordering::Relaxed);
        BASE_TARJAN_NANOS.fetch_add(elapsed_nanos, Ordering::Relaxed);
        BASE_NODES.record(nodes.min(u32::MAX as usize) as u32);
        BASE_EDGES.record(edges.min(u32::MAX as usize) as u32);
        BASE_SCCS.record(sccs.len().min(u32::MAX as usize) as u32);
        // Condensation edges: count distinct cross-SCC `(from, to)`
        // pairs at the SCC level. Cheap pass over the base edge set
        // using the SCC partition we just computed.
        let mut scc_of: BTreeMap<ModuleId, usize> = BTreeMap::new();
        for (i, scc) in sccs.iter().enumerate() {
            for &m in scc {
                scc_of.insert(m, i);
            }
        }
        let mut cond_edges: BTreeSet<(usize, usize)> = BTreeSet::new();
        for (from, to) in graph.edge_pairs() {
            let a = scc_of.get(&from).copied().unwrap_or(usize::MAX);
            let b = scc_of.get(&to).copied().unwrap_or(usize::MAX);
            if a != b {
                cond_edges.insert((a, b));
            }
        }
        BASE_COND_EDGES.record(cond_edges.len().min(u32::MAX as usize) as u32);
    }

    pub(super) fn elapsed_to_u64(elapsed: Duration) -> u64 {
        let nanos = elapsed.as_nanos();
        if nanos > u64::MAX as u128 {
            u64::MAX
        } else {
            nanos as u64
        }
    }

    /// Print the gathered counter report to stderr.
    pub(super) fn report_to_stderr() {
        use std::io::Write;
        let stderr = std::io::stderr();
        let mut out = stderr.lock();
        let _ = writeln!(
            out,
            "=== debundle gate perf counters (DEBUNDLE_TIMING=1) ==="
        );

        let calls = SCC_CALLS_TOTAL.load(Ordering::Relaxed);
        let empty = SCC_CALLS_OVERLAY_EMPTY.load(Ordering::Relaxed);
        let nonempty = SCC_CALLS_OVERLAY_NONEMPTY.load(Ordering::Relaxed);
        let nanos = SCC_NANOS.load(Ordering::Relaxed);
        let total_secs = nanos as f64 / 1e9;
        let per_call_us = if calls > 0 {
            (nanos as f64 / calls as f64) / 1e3
        } else {
            0.0
        };
        let _ = writeln!(
            out,
            "scc_containing: {calls} calls ({empty} overlay-empty, {nonempty} overlay-nonempty)"
        );
        let _ = writeln!(
            out,
            "  cumulative: {total_secs:.3}s, per-call avg: {per_call_us:.3} µs"
        );

        let base_calls = BASE_TARJAN_CALLS.load(Ordering::Relaxed);
        let base_nanos = BASE_TARJAN_NANOS.load(Ordering::Relaxed);
        let base_secs = base_nanos as f64 / 1e9;
        let base_per_call_us = if base_calls > 0 {
            (base_nanos as f64 / base_calls as f64) / 1e3
        } else {
            0.0
        };
        let _ = writeln!(
            out,
            "base tarjan_scc: {base_calls} calls, cumulative {base_secs:.3}s, per-call avg {base_per_call_us:.3} µs"
        );

        let verdict_calls = VERDICT_TOUCHING_CALLS.load(Ordering::Relaxed);
        let overlay_verdict_calls = VERDICT_WITH_OVERLAY_CALLS.load(Ordering::Relaxed);
        let verdict_realizable = VERDICT_REALIZABLE.load(Ordering::Relaxed);
        let verdict_rejected = VERDICT_REJECTED.load(Ordering::Relaxed);
        let i_scc_with_constraining = I_SCC_WITH_CONSTRAINING.load(Ordering::Relaxed);
        let _ = writeln!(
            out,
            "verdict_touching: {verdict_calls} calls ({overlay_verdict_calls} overlay), {verdict_realizable} realizable, {verdict_rejected} rejected"
        );
        let _ = writeln!(
            out,
            "  I-SCC with constraining pair: {i_scc_with_constraining}"
        );

        let simulator_requests = SIMULATOR_REQUESTS.load(Ordering::Relaxed);
        let simulator_noop = SIMULATOR_STRUCTURAL_NOOP.load(Ordering::Relaxed);
        let simulator_changed = SIMULATOR_STRUCTURAL_CHANGED.load(Ordering::Relaxed);
        let _ = writeln!(
            out,
            "simulator requests: {simulator_requests} ({simulator_noop} structural-noop, {simulator_changed} structural-changed)"
        );
        let base_rebuilds = SIMULATOR_BASE_REBUILDS.load(Ordering::Relaxed);
        let base_rebuild_nanos = SIMULATOR_BASE_REBUILD_NANOS.load(Ordering::Relaxed);
        let base_rebuild_secs = base_rebuild_nanos as f64 / 1e9;
        let base_rebuild_per_call_us = if base_rebuilds > 0 {
            (base_rebuild_nanos as f64 / base_rebuilds as f64) / 1e3
        } else {
            0.0
        };
        let _ = writeln!(
            out,
            "  base simulator rebuilds: {base_rebuilds}, cumulative {base_rebuild_secs:.3}s, per-call avg {base_rebuild_per_call_us:.3} µs"
        );
        let overlay_rebuilds = SIMULATOR_OVERLAY_REBUILDS.load(Ordering::Relaxed);
        let overlay_rebuild_nanos = SIMULATOR_OVERLAY_REBUILD_NANOS.load(Ordering::Relaxed);
        let overlay_rebuild_secs = overlay_rebuild_nanos as f64 / 1e9;
        let overlay_rebuild_per_call_us = if overlay_rebuilds > 0 {
            (overlay_rebuild_nanos as f64 / overlay_rebuilds as f64) / 1e3
        } else {
            0.0
        };
        let _ = writeln!(
            out,
            "  overlay simulator rebuilds: {overlay_rebuilds}, cumulative {overlay_rebuild_secs:.3}s, per-call avg {overlay_rebuild_per_call_us:.3} µs"
        );

        let diagnostic_calls = DIAGNOSTIC_TRANSLATION_CALLS.load(Ordering::Relaxed);
        let diagnostic_active = DIAGNOSTIC_TRANSLATION_ACTIVE.load(Ordering::Relaxed);
        let diagnostic_bypassed = diagnostic_calls.saturating_sub(diagnostic_active);
        let _ = writeln!(
            out,
            "diagnostic translation: {diagnostic_calls} calls ({diagnostic_active} active, {diagnostic_bypassed} bypassed)"
        );

        report_histogram(&mut out, "  overlay delta.len()", &OVERLAY_DELTA_LEN);
        report_histogram(&mut out, "  overlay additions", &OVERLAY_ADDITIONS);
        report_histogram(&mut out, "  overlay removals", &OVERLAY_REMOVALS);
        report_histogram(&mut out, "  constraining SCC size", &CONSTRAINING_SCC_SIZE);
        report_histogram(&mut out, "  I-SCC size", &I_SCC_SIZE);
        report_histogram(
            &mut out,
            "  diagnostic owner_modules count",
            &DIAGNOSTIC_OWNER_COUNT,
        );
        report_histogram(
            &mut out,
            "  diagnostic unrealizable SCC count",
            &DIAGNOSTIC_SCC_COUNT,
        );
        report_histogram(&mut out, "  base nodes", &BASE_NODES);
        report_histogram(&mut out, "  base edges (distinct)", &BASE_EDGES);
        report_histogram(&mut out, "  base SCCs", &BASE_SCCS);
        report_histogram(&mut out, "  base condensation edges", &BASE_COND_EDGES);
    }

    fn report_histogram<W: std::io::Write>(out: &mut W, label: &str, hist: &Histogram) {
        let snap = hist.snapshot();
        if snap.count == 0 {
            let _ = writeln!(out, "{label}: (no samples)");
            return;
        }
        let median = snap.percentile(0.50);
        let p95 = snap.percentile(0.95);
        let _ = writeln!(
            out,
            "{label}: count={count} min={min} median={median} p95={p95} max={max} mean={mean:.2}",
            count = snap.count,
            min = snap.min,
            max = snap.max,
            mean = snap.mean(),
        );
    }

    /// RAII guard installed by `SccTimingReporter::install_if_enabled`.
    /// Prints the counter summary on drop when timing is enabled.
    pub struct InstalledGuard;

    impl Drop for InstalledGuard {
        fn drop(&mut self) {
            report_to_stderr();
        }
    }
}

/// RAII guard that prints the gate-path perf counter summary on drop
/// when `DEBUNDLE_TIMING=1` is set in the process environment.
/// Construct one early in `main` (`SccTimingReporter::install_if_enabled`)
/// to get a tally at program exit. Cheap counters are still recorded
/// when reporting is disabled; only wall-clock timing, stderr output,
/// and shadow base-Tarjan measurement are gated.
///
/// The counters themselves live in the private
/// `realizability::gate_perf_counters` module; the guard is the only
/// public surface, deliberately small.
pub struct SccTimingReporter(gate_perf_counters::InstalledGuard);

impl SccTimingReporter {
    pub fn install_if_enabled() -> Option<Self> {
        if gate_perf_counters::enabled() {
            Some(Self(gate_perf_counters::InstalledGuard))
        } else {
            None
        }
    }
}

/// Record the cost shape of proposer-side verdict-to-diagnostic
/// translation. This is intentionally always-on and cheap; reporting is
/// still controlled by [`SccTimingReporter`].
pub fn record_gate_diagnostic_translation(active: bool, owner_count: usize, scc_count: usize) {
    gate_perf_counters::record_diagnostic_translation(active, owner_count, scc_count);
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeSet;

    use super::*;
    use analysis::OwnerId;
    use analysis::facts::analyze_chunk;
    use analysis::graph::build_owner_graph;
    use analysis::ids::{LogicalModuleIndex, ModuleId};
    use analysis::partition::Partition;
    use analysis::{AnalysisHints, OwnerGraph};
    use swc_common::{FileName, SourceMap, sync::Lrc};
    use swc_ecma_parser::{Parser, StringInput, Syntax, lexer::Lexer};

    fn module_id(index: usize) -> ModuleId {
        ModuleId(LogicalModuleIndex(index))
    }

    fn parse_and_build(source: &str) -> OwnerGraph {
        let cm: Lrc<SourceMap> = Default::default();
        let fm = cm.new_source_file(
            FileName::Custom("test.js".into()).into(),
            source.to_string(),
        );
        let lexer = Lexer::new(
            Syntax::Es(Default::default()),
            Default::default(),
            StringInput::from(&*fm),
            None,
        );
        let module = Parser::new_from(lexer)
            .parse_module()
            .expect("parse module");
        let facts = analyze_chunk(&module, &AnalysisHints::default(), None, |_| None).facts;
        build_owner_graph(&facts)
    }

    /// Two top-level constants in different modules, with one reading
    /// the other at-init across the module boundary acyclically. No
    /// cycle, no rebind — verdict is empty.
    #[test]
    fn acyclic_cross_module_at_init_read_is_realizable() {
        let source = "const a = 1; const b = a + 1;";
        let owner_graph = parse_and_build(source);
        // Owner 0: const a = 1 → module 0.
        // Owner 1: const b = a + 1 → module 1.
        // Edge owner_1 → owner_0 (eager_use of `a`).
        let mut partition = Partition::new(&owner_graph, module_id(0));
        partition.set(OwnerId(1), module_id(1));
        let verdict = check_realizability(&owner_graph, &partition);
        assert!(
            verdict.is_realizable(),
            "verdict should be empty: {verdict:#?}"
        );
    }

    /// Same setup but flipped to create a constraining cycle: both
    /// statements live in different modules and mutually at-init read
    /// the other. Quotient has a 2-cycle of constraining edges →
    /// unrealizable.
    #[test]
    fn constraining_cycle_across_two_modules_is_unrealizable() {
        // Two top-level constants whose initializers eager-read each
        // other. Real JS would TDZ at runtime, but the analyzer just
        // records the structural graph: two `eager_use` edges in
        // opposite directions. Placing them in different modules
        // forms a constraining-edge SCC of the quotient — exactly
        // what clause 3 rejects.
        let source = "const a = b + 1; const b = a + 1;";
        let owner_graph = parse_and_build(source);
        let mut partition = Partition::new(&owner_graph, module_id(0));
        partition.set(OwnerId(1), module_id(1));
        let verdict = check_realizability(&owner_graph, &partition);
        assert!(
            !verdict.is_realizable(),
            "verdict should report an SCC: {verdict:#?}"
        );
        let modules: BTreeSet<ModuleId> = verdict.modules_in_unrealizable_sccs();
        assert!(modules.contains(&module_id(0)));
        assert!(modules.contains(&module_id(1)));
        assert!(
            verdict
                .unrealizable_sccs
                .iter()
                .all(|scc| !scc.constraining_owner_edges.is_empty()),
            "every SCC must carry owner-edge evidence"
        );
    }

    /// A pure lazy-read cycle (mutual references inside function
    /// bodies) is realizable: ESM evaluates the lazy side first, no
    /// TDZ. Verdict must be empty even when the modules form a cycle
    /// in the *full* quotient.
    #[test]
    fn pure_lazy_cycle_is_realizable() {
        let source = "function a() { return b(); } function b() { return a(); }";
        let owner_graph = parse_and_build(source);
        let mut partition = Partition::new(&owner_graph, module_id(0));
        partition.set(OwnerId(1), module_id(1));
        let verdict = check_realizability(&owner_graph, &partition);
        assert!(
            verdict.is_realizable(),
            "lazy-only cycle should be realizable: {verdict:#?}"
        );
    }

    /// Asymmetric I-cycle `{mod_dep, mod_dependent}` with eager
    /// `mod_dependent → mod_dep` and lazy `mod_dep → mod_dependent`.
    /// Residual (`module_id(0)`) at-init-reads both, so residual has
    /// I-edges into the SCC and Lemma 2 rescues — the simulator's
    /// post-order puts mod_dep's body before mod_dependent's body.
    /// Verdict must be empty.
    #[test]
    fn lemma_two_rescues_asymmetric_cycle_when_residual_imports_scc() {
        // owner_0 (residual): const a = 1; (also reads b, lazy_reader at-init via console.log)
        // owner_1 (mod_dep): const dep_value = "alpha"
        // owner_2 (mod_dep): function lazy_reader() { return cross_value; }
        // owner_3 (mod_dependent): const cross_value = dep_value + "-beta"
        // owner_4 (residual): console.log reads dep_value, cross_value, lazy_reader at-init
        let source = "const dep_value = \"alpha\"; const cross_value = dep_value + \"-beta\"; function lazy_reader() { return cross_value; } console.log(dep_value, cross_value, lazy_reader());";
        let owner_graph = parse_and_build(source);
        let mut partition = Partition::new(&owner_graph, module_id(0));
        // dep_value (owner 0) → mod_dep, cross_value (owner 1) →
        // mod_dependent, lazy_reader (owner 2) → mod_dep,
        // console.log (owner 3) stays in residual (= module_id(0)).
        partition.set(OwnerId(0), module_id(1));
        partition.set(OwnerId(1), module_id(2));
        partition.set(OwnerId(2), module_id(1));
        let verdict = check_realizability(&owner_graph, &partition);
        assert!(
            verdict.is_realizable(),
            "Lemma 2 should rescue this shape; verdict: {verdict:#?}",
        );
    }

    /// Same SCC shape but residual's own statements have NO direct
    /// I-edge into the SCC — they reach it only through
    /// `mod_mediator`. Still realizable: the emitted entry imports
    /// EVERY logical module (not just the ones residual's statements
    /// reference), in Lemma 2's source-import order, so ESM DFS
    /// enters the SCC at `mod_dependent` (the dependent) before the
    /// mediator's dependency-first imports could reach it at
    /// `mod_dep`. The simulator's universal residual fan-out models
    /// this; the matching Node-anchored pin is
    /// `e2e/mediator_reaches_asymmetric_cycle_test` (the emitted
    /// output runs cleanly and prints the mediator-derived value).
    ///
    /// Simulated post-order: `mod_dep` → `mod_dependent` →
    /// `mod_mediator` → residual; the constraining pair
    /// `(mod_dependent → mod_dep)` is satisfied.
    #[test]
    fn mediator_only_entrant_into_asymmetric_cycle_is_rescued_by_entry_imports() {
        // owner_0: const dep_value = "alpha"
        // owner_1: const cross_value = dep_value + "-beta"
        // owner_2: function lazy_reader() { return cross_value; }
        // owner_3: function mediator_helper() { return dep_value + lazy_reader(); }
        // owner_4: const mediator_init = mediator_helper(); (at-init promotes
        //          to a constraining edge into the dep_value owner —
        //          mediator → mod_dep eager)
        // owner_5: console.log(mediator_init); (residual at-init)
        let source = "const dep_value = \"alpha\"; const cross_value = dep_value + \"-beta\"; function lazy_reader() { return cross_value; } function mediator_helper() { return dep_value + lazy_reader(); } const mediator_init = mediator_helper(); console.log(mediator_init);";
        let owner_graph = parse_and_build(source);
        let mut partition = Partition::new(&owner_graph, module_id(0));
        partition.set(OwnerId(0), module_id(1)); // dep_value → mod_dep
        partition.set(OwnerId(1), module_id(2)); // cross_value → mod_dependent
        partition.set(OwnerId(2), module_id(1)); // lazy_reader → mod_dep
        partition.set(OwnerId(3), module_id(3)); // mediator_helper → mod_mediator
        partition.set(OwnerId(4), module_id(3)); // mediator_init → mod_mediator
        // owner_5 (console.log) stays in residual.
        let verdict = check_realizability(&owner_graph, &partition);
        assert!(
            verdict.is_realizable(),
            "entry's universal per-plan imports DFS into the SCC at the \
             dependent first (Lemma 2); the mediator path never wins. \
             verdict: {verdict:#?}",
        );
    }

    /// Regression test for the gaffer over-rejection. Asymmetric
    /// I-cycle where residual's own statements reach the SCC only
    /// via the constraining edge's **target** (the dependency), not
    /// the source (the dependent).
    ///
    /// Shape (gaffer's `domains/system/ids` ↔ `domains/system/schemas`
    /// minimal repro):
    ///   - `mod_schemas` owns `schemas_target` (the eager-read target)
    ///     and `lazy_back` (whose body lazily references `ids_val`).
    ///   - `mod_ids` owns `ids_val`, whose initializer eager-reads
    ///     `schemas_target` from `mod_schemas`.
    ///   - residual reads ONLY `schemas_target` — no direct
    ///     reference to `ids_val`.
    ///
    /// I-graph cross-module edges:
    ///   - `mod_ids → mod_schemas` `EagerUse(schemas_target)` (forward, constraining)
    ///   - `mod_schemas → mod_ids` `LazyUse(ids_val)` (back, non-constraining)
    ///   - `residual → mod_schemas` `EagerUse(schemas_target)` (constraining)
    ///
    /// I-graph SCC: `{mod_ids, mod_schemas}`. Residual is NOT in the
    /// SCC; residual's statements only reference `mod_schemas`.
    ///
    /// The historical over-rejection: the simulator modeled residual's
    /// DFS fan-out as only the modules residual's statements
    /// reference, entered the SCC at `mod_schemas`, followed the
    /// emitted lazy-read import back to `mod_ids`, and flagged
    /// `post_order[mod_schemas] > post_order[mod_ids]` as TDZ. The
    /// emitted entry, however, has always imported every plan —
    /// `mod_ids` included — in Lemma 2's source-import order, which
    /// puts the dependent `mod_ids` first; the runtime DFS unwinds
    /// through `mod_schemas` and evaluates it before `mod_ids`. The
    /// simulator now models the entry's universal imports and
    /// accepts.
    #[test]
    fn pass_two_simulator_models_entry_universal_imports_for_runtime_dfs() {
        // owner_0: const schemas_target = "v"     (mod_schemas)
        // owner_1: function lazy_back() { return ids_val; }
        //                                         (mod_schemas; lazy_use ids_val)
        // owner_2: const ids_val = schemas_target (mod_ids; eager_use
        //          schemas_target — a PURE initializer, so no
        //          sequenced edges hand residual an incidental
        //          direct edge to mod_ids)
        // owner_3: console.log(schemas_target);   (residual; eager_use schemas_target)
        let source = "const schemas_target = \"v\"; function lazy_back() { return ids_val; } const ids_val = schemas_target; console.log(schemas_target);";
        let owner_graph = parse_and_build(source);
        let mut partition = Partition::new(&owner_graph, module_id(0));
        partition.set(OwnerId(0), module_id(1)); // schemas_target → mod_schemas
        partition.set(OwnerId(1), module_id(1)); // lazy_back     → mod_schemas
        partition.set(OwnerId(2), module_id(2)); // ids_val       → mod_ids
        // owner_3 (console.log) stays in residual.
        let verdict = check_realizability(&owner_graph, &partition);
        assert!(
            verdict.is_realizable(),
            "gaffer-shape asymmetric cycle must accept: entry imports \
             every plan in Lemma 2's source-import order, so the runtime \
             DFS enters the SCC at mod_ids (the dependent) and evaluates \
             mod_schemas first. verdict: {verdict:#?}",
        );
    }

    /// Differential pin: the simulator's predicted Phase-2 post-order
    /// for the gaffer shape equals the evaluation order Node produces
    /// for the emitted tree. The Node side is pinned by
    /// `e2e/asymmetric_non_residual_cycle_test::`
    /// `dependency_only_residual_reference_into_asymmetric_cycle_runs_under_node`
    /// — the same shape, which TDZ-crashes under Node unless
    /// `mod_schemas`' body evaluates before `mod_ids`'. Emitter and
    /// simulator both consume `EsmImportOrder`, so this pin guards
    /// the shared-ordering contract from the gate side.
    #[test]
    fn simulator_post_order_matches_emitted_evaluation_order() {
        // owner_0: const schemas_target = "v"      (mod_schemas)
        // owner_1: function lazy_back() { return ids_val; } (mod_schemas)
        // owner_2: const ids_val = schemas_target  (mod_ids)
        // owner_3: console.log(schemas_target)     (residual)
        let source = "const schemas_target = \"v\"; function lazy_back() { return ids_val; } const ids_val = schemas_target; console.log(schemas_target);";
        let owner_graph = parse_and_build(source);
        let mut partition = Partition::new(&owner_graph, module_id(0));
        partition.set(OwnerId(0), module_id(1)); // schemas_target → mod_schemas
        partition.set(OwnerId(1), module_id(1)); // lazy_back     → mod_schemas
        partition.set(OwnerId(2), module_id(2)); // ids_val       → mod_ids
        // owner_3 (console.log) stays in residual.
        let canonical = chunk_constraining_module_edges(&owner_graph, &partition);
        let pairs: BTreeSet<(ModuleId, ModuleId)> = canonical.pairs().collect();
        let simulator =
            EsmEvaluationSimulator::build(&canonical.i_successors, &pairs, partition.residual());
        // Node evaluates: mod_schemas body, mod_ids body, then the
        // entry (residual) body — entry's imports are
        // [mod_ids, mod_schemas] (intra-SCC reversal), DFS unwinds
        // through mod_schemas first, and the root body is last.
        let expected: BTreeMap<ModuleId, usize> =
            [(module_id(1), 0), (module_id(2), 1), (module_id(0), 2)]
                .into_iter()
                .collect();
        assert_eq!(
            simulator.post_order, expected,
            "simulated post-order must match the emitted tree's actual Node evaluation order",
        );
    }

    /// Residual is the source of a constraining edge into the SCC,
    /// but the SCC also has a constraining-target-residual edge.
    /// Lemma 2 fails: residual is the DFS root and evaluates last in
    /// post-order; the SCC member reading residual's binding TDZs.
    #[test]
    fn constraining_edge_into_residual_inside_scc_is_unrealizable() {
        // owner_0: class Backend { ... } (residual, TDZ-locked target)
        // owner_1: let currentLogger; (mod_logger)
        // owner_2: function setLogger(impl) { currentLogger = impl; ... } (mod_logger)
        // owner_3: setLogger(new Backend()); (mod_logger, at-init reads Backend)
        // owner_4: console.log(currentLogger.tag); (residual, lazy read of currentLogger from mod_logger via re-export)
        let source = "class Backend { constructor() { this.tag = \"B\"; } } let currentLogger; function setLogger(impl) { currentLogger = impl; globalThis.__tag = impl.tag; } setLogger(new Backend()); console.log(currentLogger);";
        let owner_graph = parse_and_build(source);
        let mut partition = Partition::new(&owner_graph, module_id(0));
        // Backend (owner 0) stays in residual.
        partition.set(OwnerId(1), module_id(1)); // currentLogger → mod_logger
        partition.set(OwnerId(2), module_id(1)); // setLogger → mod_logger
        partition.set(OwnerId(3), module_id(1)); // setLogger(new Backend()) → mod_logger
        // owner 4 (console.log) stays in residual.
        let verdict = check_realizability(&owner_graph, &partition);
        // mod_logger → residual EagerUse (constraining target = residual)
        // residual → mod_logger LazyUse (re-export / console.log)
        // SCC = {residual, mod_logger}. Constraining edge target = residual.
        // Residual is DFS root; mod_logger body runs first, reads Backend → TDZ.
        assert!(
            !verdict.is_realizable(),
            "constraining edge target=residual must TDZ; verdict: {verdict:#?}",
        );
    }

    /// Namespace-aggregator split: a module-level `const ids = {...sub1, ...sub2}`
    /// gets sub1 and sub2 extracted into separate modules. The aggregator's
    /// initializer carries at-init reads of sub1 and sub2 (the spread RHS
    /// reads them). If a sub-module also reads back into the residual or
    /// aggregator at-init, the resulting cross-module SCC must be detected
    /// by the gate or the emitted ESM will TDZ at runtime under Node.
    ///
    /// Shape used here: sub1 reads `seed` declared in residual at-init;
    /// residual reads `ids` at-init. Cycle:
    ///   residual --EagerUse--> mod_ids   (`const consumed = ids.foo`)
    ///   mod_ids  --EagerUse--> mod_sub1  (`const ids = {...sub1, ...sub2}`)
    ///   mod_sub1 --EagerUse--> residual  (`const sub1 = { foo: seed }`)
    /// The gate must reject this partition.
    #[test]
    fn namespace_aggregator_with_back_edge_through_sub_is_unrealizable() {
        // owner_0: const seed = "S"           (residual)
        // owner_1: const sub1 = { foo: seed }  (mod_sub1) — eager_read of seed
        // owner_2: const sub2 = { bar: 1 }     (mod_sub2) — no cross-module reads
        // owner_3: const ids = {...sub1, ...sub2} (mod_ids) — eager reads sub1, sub2
        // owner_4: const consumed = ids.foo + ids.bar (residual) — eager read of ids
        let source = "const seed = \"S\"; const sub1 = { foo: seed }; const sub2 = { bar: 1 }; const ids = {...sub1, ...sub2}; const consumed = ids.foo + ids.bar; console.log(consumed);";
        let owner_graph = parse_and_build(source);
        let mut partition = Partition::new(&owner_graph, module_id(0));
        partition.set(OwnerId(1), module_id(1)); // sub1 → mod_sub1
        partition.set(OwnerId(2), module_id(2)); // sub2 → mod_sub2
        partition.set(OwnerId(3), module_id(3)); // ids  → mod_ids
        let verdict = check_realizability(&owner_graph, &partition);
        assert!(
            !verdict.is_realizable(),
            "namespace-aggregator split with sub→residual back edge \
             must be flagged by the gate; verdict: {verdict:#?}",
        );
    }

    /// Same aggregator shape but with sub1 and sub2 INDEPENDENT of residual
    /// (pure literal initializers). The split is realizable: ESM evaluates
    /// sub1, sub2, then ids, then residual.
    #[test]
    fn namespace_aggregator_with_pure_subs_is_realizable() {
        // owner_0: const sub1 = { foo: 1 }
        // owner_1: const sub2 = { bar: 2 }
        // owner_2: const ids = {...sub1, ...sub2}
        // owner_3: console.log(ids)
        let source = "const sub1 = { foo: 1 }; const sub2 = { bar: 2 }; const ids = {...sub1, ...sub2}; console.log(ids);";
        let owner_graph = parse_and_build(source);
        let mut partition = Partition::new(&owner_graph, module_id(0));
        partition.set(OwnerId(0), module_id(1)); // sub1 → mod_sub1
        partition.set(OwnerId(1), module_id(2)); // sub2 → mod_sub2
        partition.set(OwnerId(2), module_id(3)); // ids  → mod_ids
        let verdict = check_realizability(&owner_graph, &partition);
        assert!(
            verdict.is_realizable(),
            "pure namespace-aggregator split must be realizable; verdict: {verdict:#?}",
        );
    }

    /// **RED regression test** for the namespace-aggregator TDZ hole.
    ///
    /// The cycle goes through a *promoted* edge — the sub-module's at-init
    /// `readSeed()` call has its body's read of `seed` (in residual) promoted
    /// to a sub→residual eager edge. The lenient projection view
    /// (`EndpointView::Lenient`) drops it under
    /// `EdgeRole::is_cross_module_promotion` because the call target
    /// `readSeed` lives in `mod_helpers`, not `mod_sub1`. With the
    /// drop, the gate sees no cycle. Without the drop, the cycle
    /// `residual→mod_ids→mod_sub1→residual` is closed.
    ///
    /// ESM runtime DFS from residual:
    ///   residual → mod_ids → mod_sub1 → mod_helpers (eval helpers)
    ///                                 → residual (on stack, skip).
    ///   Post-order: helpers, then mod_sub1.
    ///   When `mod_sub1`'s body evaluates `readSeed()`, the call reads
    ///   `seed` from residual — residual is mid-DFS, `seed` is TDZ-locked.
    ///   ⇒ `ReferenceError: Cannot access 'seed' before initialization`.
    ///
    /// The gate-side view (`EndpointView::Gate`) keeps the promoted
    /// edge so the cycle is detected; the test pins that behaviour.
    #[test]
    fn promoted_edge_in_aggregator_cycle_is_unrealizable() {
        // owner_0: const seed = "S"                  (residual)
        // owner_1: const readSeed = () => seed       (mod_helpers)
        // owner_2: const sub1 = { foo: readSeed() }  (mod_sub1) — at-init call into mod_helpers
        // owner_3: const ids = sub1.foo + "x"        (mod_ids)
        // owner_4: const consumed = ids              (residual)
        let source = "const seed = \"S\"; const readSeed = () => seed; const sub1 = { foo: readSeed() }; const ids = sub1.foo + \"x\"; const consumed = ids; console.log(consumed);";
        let owner_graph = parse_and_build(source);
        let mut partition = Partition::new(&owner_graph, module_id(0));
        partition.set(OwnerId(1), module_id(1)); // readSeed → mod_helpers
        partition.set(OwnerId(2), module_id(2)); // sub1 → mod_sub1
        partition.set(OwnerId(3), module_id(3)); // ids → mod_ids
        let verdict = check_realizability(&owner_graph, &partition);
        assert!(
            !verdict.is_realizable(),
            "promoted-edge aggregator cycle must be flagged by the gate \
             (mod_sub1's readSeed() at-init call reads `seed` in residual; \
             residual reads `ids` in mod_ids; mod_ids reads `sub1` in \
             mod_sub1 — closes a cycle through the promoted edge); \
             verdict: {verdict:#?}",
        );
    }

    /// All owners in the same module → no cross-destination edges of
    /// any kind → empty verdict.
    #[test]
    fn single_module_is_always_realizable() {
        let source = "const a = 1; const b = a + 1; const c = a * b;";
        let owner_graph = parse_and_build(source);
        let partition = Partition::new(&owner_graph, module_id(0));
        let verdict = check_realizability(&owner_graph, &partition);
        assert!(verdict.is_realizable());
    }

    /// Pushing a delta on the index and reading the verdict matches
    /// the pure function on the post-push partition. Undo restores the
    /// pre-push verdict exactly.
    #[test]
    fn index_push_undo_roundtrips_verdict() {
        let source = "const a = b + 1; const b = a + 1;";
        let owner_graph = parse_and_build(source);

        let baseline = Partition::new(&owner_graph, module_id(0));
        let baseline_verdict = check_realizability(&owner_graph, &baseline);
        assert!(baseline_verdict.is_realizable());

        let mut index = RealizabilityIndex::from_partition(&owner_graph, baseline.clone());
        let handle = index.push(
            &owner_graph,
            PartitionDelta::MoveOwners {
                owners: vec![OwnerId(1)],
                to: module_id(1),
            },
        );
        // After push: matches the explicitly-built post-delta partition.
        let mut hypothetical = baseline.clone();
        hypothetical.set(OwnerId(1), module_id(1));
        let hypothetical_verdict = check_realizability(&owner_graph, &hypothetical);
        assert_eq!(
            index.verdict().unrealizable_sccs.len(),
            hypothetical_verdict.unrealizable_sccs.len(),
        );
        assert!(!index.verdict().is_realizable());

        index.undo(&owner_graph, handle);
        // After undo: matches the baseline exactly.
        assert!(index.verdict().is_realizable());
        for owner_id in 0..owner_graph.num_nodes() {
            assert_eq!(
                index.partition().of(OwnerId(owner_id)),
                baseline.of(OwnerId(owner_id)),
                "partition slot {owner_id} should be restored by undo",
            );
        }
    }

    #[test]
    fn duplicate_owner_ids_are_journaled_once() {
        let source = "const a = 1; const b = a + 1;";
        let owner_graph = parse_and_build(source);
        let baseline = Partition::new(&owner_graph, module_id(0));
        let mut index = RealizabilityIndex::from_partition(&owner_graph, baseline.clone());

        let handle = index.push(
            &owner_graph,
            PartitionDelta::MoveOwners {
                owners: vec![OwnerId(1), OwnerId(1)],
                to: module_id(1),
            },
        );
        assert_eq!(index.partition().of(OwnerId(1)), module_id(1));

        index.undo(&owner_graph, handle);
        assert_eq!(index.partition().of(OwnerId(1)), baseline.of(OwnerId(1)));
        assert_eq!(
            normalize_verdict(index.verdict()),
            normalize_verdict(check_realizability(&owner_graph, &baseline)),
        );
    }

    #[test]
    fn commit_drops_journal_state_and_index_stays_queryable() {
        let source = "const a = 1; const b = a + 1;";
        let owner_graph = parse_and_build(source);
        let baseline = Partition::new(&owner_graph, module_id(0));
        let mut index = RealizabilityIndex::from_partition(&owner_graph, baseline);

        // Permanent push (the commit_merge shape: no matching undo).
        index.push(
            &owner_graph,
            PartitionDelta::MoveOwners {
                owners: vec![OwnerId(1)],
                to: module_id(1),
            },
        );
        let committed = normalize_verdict(index.verdict());
        index.commit();

        // Committed state is intact, and subsequent scoped
        // speculative work (push + undo) still balances correctly
        // against the new journal baseline.
        assert_eq!(index.partition().of(OwnerId(1)), module_id(1));
        assert_eq!(normalize_verdict(index.verdict()), committed);
        index.scoped(
            &owner_graph,
            PartitionDelta::MoveOwners {
                owners: vec![OwnerId(0)],
                to: module_id(2),
            },
            |idx| idx.verdict(),
        );
        assert_eq!(normalize_verdict(index.verdict()), committed);
        assert!(
            index.journal.is_empty(),
            "scoped work must not leak entries"
        );
    }

    #[test]
    fn move_overlay_matches_scoped_verdict_touching() {
        let source = "const a = b + 1; const b = a + 1;";
        let owner_graph = parse_and_build(source);
        let baseline = Partition::new(&owner_graph, module_id(0));
        let mut index = RealizabilityIndex::from_partition(&owner_graph, baseline.clone());
        let before = normalize_verdict(index.verdict());

        let overlay =
            index.verdict_after_moving_owners_touching(&owner_graph, &[OwnerId(1)], module_id(1));
        let scoped = index.scoped(
            &owner_graph,
            PartitionDelta::MoveOwners {
                owners: vec![OwnerId(1)],
                to: module_id(1),
            },
            |idx| idx.verdict_touching(module_id(1)),
        );

        assert_eq!(normalize_verdict(overlay), normalize_verdict(scoped));
        assert_eq!(
            normalize_verdict(index.verdict()),
            before,
            "overlay query must not mutate the working partition",
        );
        assert_eq!(index.partition().of(OwnerId(1)), baseline.of(OwnerId(1)));
    }

    #[test]
    fn move_overlay_reports_cross_rebinds_like_scoped_verdict() {
        let source = "let a = 0; function b() { a = 1; }";
        let owner_graph = parse_and_build(source);
        let baseline = Partition::new(&owner_graph, module_id(0));
        let mut index = RealizabilityIndex::from_partition(&owner_graph, baseline);

        let overlay =
            index.verdict_after_moving_owners_touching(&owner_graph, &[OwnerId(1)], module_id(1));
        let scoped = index.scoped(
            &owner_graph,
            PartitionDelta::MoveOwners {
                owners: vec![OwnerId(1)],
                to: module_id(1),
            },
            |idx| idx.verdict_touching(module_id(1)),
        );

        assert_eq!(
            normalize_verdict(overlay.clone()),
            normalize_verdict(scoped)
        );
        assert!(overlay.unrealizable_sccs.is_empty());
        assert_eq!(overlay.cross_rebinds.len(), 1);
    }

    #[test]
    fn move_overlay_masks_removed_current_edges() {
        let source = "const a = b + 1; const b = c + 1; const c = 1;";
        let owner_graph = parse_and_build(source);
        let mut baseline = Partition::new(&owner_graph, module_id(0));
        baseline.set(OwnerId(0), module_id(1));
        baseline.set(OwnerId(1), module_id(2));
        baseline.set(OwnerId(2), module_id(3));
        let mut explicit = baseline.clone();
        explicit.set(OwnerId(1), module_id(4));
        let mut index = RealizabilityIndex::from_partition(&owner_graph, baseline);

        let overlay =
            index.verdict_after_moving_owners_touching(&owner_graph, &[OwnerId(1)], module_id(4));
        let scoped = index.scoped(
            &owner_graph,
            PartitionDelta::MoveOwners {
                owners: vec![OwnerId(1)],
                to: module_id(4),
            },
            |idx| idx.verdict_touching(module_id(4)),
        );
        let pure =
            filter_verdict_touching(&check_realizability(&owner_graph, &explicit), module_id(4));

        assert_eq!(
            normalize_verdict(overlay.clone()),
            normalize_verdict(scoped)
        );
        assert_eq!(normalize_verdict(overlay), normalize_verdict(pure));
    }

    /// `scoped` runs the closure with the delta applied and undoes on
    /// return — even when the closure returns a value.
    #[test]
    fn index_scoped_isolates_per_call_state() {
        let source = "const a = b + 1; const b = a + 1;";
        let owner_graph = parse_and_build(source);

        let baseline = Partition::new(&owner_graph, module_id(0));
        let mut index = RealizabilityIndex::from_partition(&owner_graph, baseline.clone());

        let inside_verdict_realizable = index.scoped(
            &owner_graph,
            PartitionDelta::MoveOwners {
                owners: vec![OwnerId(1)],
                to: module_id(1),
            },
            |idx| idx.verdict().is_realizable(),
        );
        assert!(
            !inside_verdict_realizable,
            "inside the scope the cycle exists"
        );

        // After scoped: state restored exactly.
        assert!(index.verdict().is_realizable());
        assert_eq!(index.partition().of(OwnerId(1)), module_id(0));
    }

    #[test]
    fn incremental_index_matches_pure_verdict_through_nested_push_undo() {
        let source = "const a = b + 1; const b = a + 1; function c() { return a; }";
        let owner_graph = parse_and_build(source);

        let baseline = Partition::new(&owner_graph, module_id(0));
        let mut explicit = baseline.clone();
        let mut index = RealizabilityIndex::from_partition(&owner_graph, baseline.clone());

        assert_eq!(
            normalize_verdict(index.verdict()),
            normalize_verdict(check_realizability(&owner_graph, &explicit)),
        );

        let first = index.push(
            &owner_graph,
            PartitionDelta::MoveOwners {
                owners: vec![OwnerId(1)],
                to: module_id(1),
            },
        );
        explicit.set(OwnerId(1), module_id(1));
        assert_eq!(
            normalize_verdict(index.verdict()),
            normalize_verdict(check_realizability(&owner_graph, &explicit)),
        );

        let second = index.push(
            &owner_graph,
            PartitionDelta::MoveOwners {
                owners: vec![OwnerId(2)],
                to: module_id(2),
            },
        );
        explicit.set(OwnerId(2), module_id(2));
        assert_eq!(
            normalize_verdict(index.verdict()),
            normalize_verdict(check_realizability(&owner_graph, &explicit)),
        );

        index.undo(&owner_graph, second);
        explicit.set(OwnerId(2), module_id(0));
        assert_eq!(
            normalize_verdict(index.verdict()),
            normalize_verdict(check_realizability(&owner_graph, &explicit)),
        );

        index.undo(&owner_graph, first);
        explicit.set(OwnerId(1), module_id(0));
        assert_eq!(
            normalize_verdict(index.verdict()),
            normalize_verdict(check_realizability(&owner_graph, &explicit)),
        );
        for owner in 0..owner_graph.num_nodes() {
            assert_eq!(
                index.partition().of(OwnerId(owner)),
                baseline.of(OwnerId(owner))
            );
        }
    }

    #[test]
    fn verdict_touching_matches_full_verdict_filtered_to_module() {
        let source = "const a = b + 1; const b = a + 1; const c = 1;";
        let owner_graph = parse_and_build(source);
        let baseline = Partition::new(&owner_graph, module_id(0));
        let mut index = RealizabilityIndex::from_partition(&owner_graph, baseline);
        index.push(
            &owner_graph,
            PartitionDelta::MoveOwners {
                owners: vec![OwnerId(1)],
                to: module_id(1),
            },
        );
        index.push(
            &owner_graph,
            PartitionDelta::MoveOwners {
                owners: vec![OwnerId(2)],
                to: module_id(2),
            },
        );

        let full = index.verdict();
        assert_eq!(
            normalize_verdict(index.verdict_touching(module_id(1))),
            normalize_verdict(filter_verdict_touching(&full, module_id(1))),
        );
        assert_eq!(
            normalize_verdict(index.verdict_touching(module_id(2))),
            normalize_verdict(filter_verdict_touching(&full, module_id(2))),
            "unrelated module should not inherit the a/b SCC",
        );
    }

    #[test]
    fn incremental_index_reports_cross_rebinds_without_scc_edges() {
        let source = "let a = 0; function b() { a = 1; }";
        let owner_graph = parse_and_build(source);
        let baseline = Partition::new(&owner_graph, module_id(0));
        let mut explicit = baseline.clone();
        let mut index = RealizabilityIndex::from_partition(&owner_graph, baseline);

        index.push(
            &owner_graph,
            PartitionDelta::MoveOwners {
                owners: vec![OwnerId(1)],
                to: module_id(1),
            },
        );
        explicit.set(OwnerId(1), module_id(1));

        let verdict = index.verdict();
        assert_eq!(
            normalize_verdict(verdict.clone()),
            normalize_verdict(check_realizability(&owner_graph, &explicit)),
        );
        assert!(
            verdict.unrealizable_sccs.is_empty(),
            "rebinds are direct violations, not SCC edges: {verdict:#?}",
        );
        assert_eq!(verdict.cross_rebinds.len(), 1);
        assert_eq!(
            normalize_verdict(index.verdict_touching(module_id(1))),
            normalize_verdict(verdict),
        );
    }

    type NormalizedVerdict = (
        BTreeSet<(Vec<ModuleId>, Vec<usize>)>,
        BTreeSet<(ModuleId, ModuleId, usize)>,
    );

    fn normalize_verdict(verdict: RealizabilityVerdict) -> NormalizedVerdict {
        let sccs = verdict
            .unrealizable_sccs
            .into_iter()
            .map(|scc| {
                let modules: Vec<ModuleId> = scc.modules.into_iter().collect();
                let edges: Vec<usize> = scc
                    .constraining_owner_edges
                    .into_iter()
                    .map(|edge| edge.0)
                    .collect();
                (modules, edges)
            })
            .collect();
        let rebinds = verdict
            .cross_rebinds
            .into_iter()
            .map(|rebind| (rebind.from, rebind.to, rebind.owner_edge.0))
            .collect();
        (sccs, rebinds)
    }

    fn filter_verdict_touching(
        verdict: &RealizabilityVerdict,
        module: ModuleId,
    ) -> RealizabilityVerdict {
        RealizabilityVerdict {
            unrealizable_sccs: verdict
                .unrealizable_sccs
                .iter()
                .filter(|scc| scc.modules.contains(&module))
                .cloned()
                .collect(),
            cross_rebinds: verdict
                .cross_rebinds
                .iter()
                .filter(|rebind| rebind.from == module || rebind.to == module)
                .cloned()
                .collect(),
        }
    }

    /// Reach inside the `RealizabilityIndex` to assert that the
    /// `IncrementalQuotient`'s cached base simulator (when populated)
    /// matches a from-scratch `EsmEvaluationSimulator::build` against
    /// the live `i_graph` + `constraining_buckets`. Lives in the
    /// realizability.rs `mod tests` so it can name the private types
    /// (`IncrementalQuotient`, `EsmEvaluationSimulator`).
    fn assert_cached_simulator_matches_rebuild(
        index: &RealizabilityIndex,
        label: &str,
        phase: &str,
    ) {
        let quotient = &index.quotient;
        // Materialize the same inputs `EsmEvaluationSimulator::build`
        // would have walked from scratch, bypassing the cache so a
        // bug in the cached-input path can't mask a divergence here.
        let mut i_successors: BTreeMap<ModuleId, BTreeSet<ModuleId>> = BTreeMap::new();
        for (from, to) in quotient.i_graph.edge_pairs() {
            i_successors.entry(from).or_default().insert(to);
        }
        let constraining_pairs: BTreeSet<(ModuleId, ModuleId)> =
            quotient.constraining_buckets.keys().copied().collect();
        let rebuilt =
            EsmEvaluationSimulator::build(&i_successors, &constraining_pairs, quotient.residual);
        // Force the cache to populate (verdict() takes the base path).
        let cached = quotient.base_simulator().clone();
        assert_eq!(
            cached, rebuilt,
            "{label}: cached base simulator diverges from rebuild ({phase})",
        );

        // Property: the cached `(i_successors, constraining_pairs)`
        // inputs must match the from-scratch walk too. This pins the
        // overlay path's clone-and-patch correctness — overlay queries
        // mutate these cached snapshots, and a mismatched base would
        // taint every overlay query.
        let (cached_inputs_succs, cached_inputs_pairs) = quotient.effective_simulator_inputs(None);
        let mut fresh_succs: BTreeMap<ModuleId, BTreeSet<ModuleId>> = BTreeMap::new();
        for (from, to) in quotient.i_graph.edge_pairs() {
            fresh_succs.entry(from).or_default().insert(to);
        }
        let fresh_pairs: BTreeSet<(ModuleId, ModuleId)> =
            quotient.constraining_buckets.keys().copied().collect();
        assert_eq!(
            cached_inputs_succs, fresh_succs,
            "{label}: cached base i_successors diverges from rebuild ({phase})",
        );
        assert_eq!(
            cached_inputs_pairs, fresh_pairs,
            "{label}: cached base constraining pairs diverges from rebuild ({phase})",
        );
    }

    /// Property test pinning the incremental simulator cache to its
    /// from-scratch correctness reference. For each fixture, applies
    /// an arbitrary sequence of `MoveOwners` deltas through the
    /// `RealizabilityIndex`, asserting after every push and every
    /// undo that the `IncrementalQuotient`'s cached
    /// `EsmEvaluationSimulator` byte-equals what
    /// `EsmEvaluationSimulator::build(...)` would produce against the
    /// current `i_graph` / `constraining_buckets`. Also asserts the
    /// cached `(i_successors, constraining_pairs)` snapshots match.
    ///
    /// Initially RED before the cache is wired to invalidate on edge
    /// mutations; GREEN once `add_current_edge` /
    /// `remove_current_edge` / `rollback_graphs` all drop the cache.
    #[test]
    fn incremental_simulator_matches_rebuild_after_each_delta() {
        struct Fixture {
            label: &'static str,
            source: &'static str,
            deltas: Vec<(Vec<usize>, usize)>,
        }
        let fixtures = vec![
            // Two-cycle plus a lazy bystander.
            Fixture {
                label: "two_eager_plus_lazy",
                source: "const a = b + 1; const b = a + 1; function c() { return a; }",
                deltas: vec![(vec![1], 1), (vec![2], 2), (vec![1, 2], 3)],
            },
            // Asymmetric I-cycle with a residual mediator.
            Fixture {
                label: "asymmetric_with_mediator",
                source: "const dep_value = \"alpha\"; const cross_value = dep_value + \"-beta\"; \
                         function lazy_reader() { return cross_value; } \
                         function mediator_helper() { return dep_value + lazy_reader(); } \
                         const mediator_init = mediator_helper(); console.log(mediator_init);",
                deltas: vec![(vec![0, 2], 1), (vec![1], 2), (vec![3, 4], 3)],
            },
            // Cross-destination rebind — exercises the rebind-only
            // overlay code path (no simulator change).
            Fixture {
                label: "rebind_then_unmove",
                source: "let a = 0; function b() { a = 1; }",
                deltas: vec![(vec![1], 1), (vec![1], 0)],
            },
            // Single-module fixture (no cross-module edges → simulator
            // input set stays empty across all deltas).
            Fixture {
                label: "single_module",
                source: "const a = 1; const b = a + 1; const c = a * b;",
                deltas: vec![(vec![1], 1), (vec![2], 1), (vec![1, 2], 0)],
            },
        ];
        for fixture in fixtures {
            let owner_graph = parse_and_build(fixture.source);
            let baseline = Partition::new(&owner_graph, module_id(0));
            let mut index = RealizabilityIndex::from_partition(&owner_graph, baseline);
            assert_cached_simulator_matches_rebuild(&index, fixture.label, "initial");
            let mut handles: Vec<DeltaHandle> = Vec::new();
            for (owner_indices, dest) in &fixture.deltas {
                let owners: Vec<OwnerId> = owner_indices.iter().copied().map(OwnerId).collect();
                let handle = index.push(
                    &owner_graph,
                    PartitionDelta::MoveOwners {
                        owners,
                        to: module_id(*dest),
                    },
                );
                handles.push(handle);
                assert_cached_simulator_matches_rebuild(&index, fixture.label, "after-push");
                // verdict() pulls through the cached simulator; assert
                // it stays consistent with `check_realizability`.
                let projected = index.partition().clone();
                assert_eq!(
                    normalize_verdict(index.verdict()),
                    normalize_verdict(check_realizability(&owner_graph, &projected)),
                    "{}: verdict diverged from check_realizability after push",
                    fixture.label,
                );
            }
            while let Some(handle) = handles.pop() {
                index.undo(&owner_graph, handle);
                assert_cached_simulator_matches_rebuild(&index, fixture.label, "after-undo");
                let projected = index.partition().clone();
                assert_eq!(
                    normalize_verdict(index.verdict()),
                    normalize_verdict(check_realizability(&owner_graph, &projected)),
                    "{}: verdict diverged from check_realizability after undo",
                    fixture.label,
                );
            }
        }
    }
}
