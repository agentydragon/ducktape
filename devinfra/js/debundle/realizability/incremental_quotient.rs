//! Incremental quotient maintenance: `PartitionDelta`, the
//! `QuotientOverlay` scratch layer, and the `IncrementalQuotient` the
//! `RealizabilityIndex` queries. Split from `realizability/mod.rs`.

use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet};

use analysis::OwnerId;
use analysis::graph::{EndpointView, OwnerEdge, OwnerEdgeId, OwnerGraph, project_endpoints};
use analysis::ids::ModuleId;
use analysis::partition::Partition;
use analysis::reports::SccCore;

use crate::counted_digraph::CountedDiGraph;

use super::condensation_order::CondensationOrder;
use super::esm_simulator::EsmEvaluationSimulator;
use super::{
    CrossRebindEdge, RealizabilityVerdict, SccDiagnosis, SccRejection, impacted_owner_edges,
    overlay_is_simulator_noop,
};

/// A mutation of a `Partition` that `RealizabilityIndex::apply`
/// commits to the index.
#[derive(Debug, Clone)]
pub enum PartitionDelta {
    /// Reassign every owner in `owners` to `to`. Owners not in the
    /// list keep their current assignment.
    MoveOwners { owners: Vec<OwnerId>, to: ModuleId },
}

#[derive(Debug, Clone, Default)]
pub(super) struct ConstrainingBucket {
    pub(super) non_sequenced: BTreeSet<OwnerEdgeId>,
    pub(super) sequenced: BTreeSet<OwnerEdgeId>,
}

impl ConstrainingBucket {
    pub(super) fn is_empty(&self) -> bool {
        self.non_sequenced.is_empty() && self.sequenced.is_empty()
    }

    pub(super) fn insert_edge(&mut self, edge_id: OwnerEdgeId, sequenced: bool) {
        if sequenced {
            self.sequenced.insert(edge_id);
        } else {
            self.non_sequenced.insert(edge_id);
        }
    }

    pub(super) fn remove_edge(&mut self, edge_id: OwnerEdgeId, sequenced: bool) {
        if sequenced {
            self.sequenced.remove(&edge_id);
        } else {
            self.non_sequenced.remove(&edge_id);
        }
    }

    pub(super) fn extend_from(&mut self, other: &Self) {
        self.non_sequenced
            .extend(other.non_sequenced.iter().copied());
        self.sequenced.extend(other.sequenced.iter().copied());
    }

    pub(super) fn remove_from(&mut self, other: &Self) {
        for edge_id in &other.non_sequenced {
            self.non_sequenced.remove(edge_id);
        }
        for edge_id in &other.sequenced {
            self.sequenced.remove(edge_id);
        }
    }

    pub(super) fn evidence_edges(&self) -> Vec<OwnerEdgeId> {
        let mut edges: Vec<OwnerEdgeId> = self.non_sequenced.iter().copied().collect();
        if let Some(first_sequenced) = self.sequenced.first() {
            edges.push(*first_sequenced);
        }
        edges.sort();
        edges
    }
}

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub(super) struct EdgeContribution {
    pub(super) from: ModuleId,
    pub(super) to: ModuleId,
    pub(super) owner_edge: OwnerEdgeId,
    pub(super) kind: EdgeContributionKind,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub(super) enum EdgeContributionKind {
    Rebind,
    Import { constraining: bool, sequenced: bool },
}

/// How the gate ladder decided one boolean realizability query. Each
/// variant names the tier that decided and the skip-condition theorem
/// (or exact evaluation) certifying the decision — the differential
/// harness asserts per-variant tier-skip soundness against the pure
/// reference predicate.
#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub enum LadderDecision {
    /// Tier 0: the move's overlay is empty (post-state == pre-state)
    /// and the committed pre-state touching verdict is clean.
    DeltaFreeAccept,
    /// Tier 0: empty overlay, but the pre-state touching verdict
    /// already carries a violation at the target module.
    DeltaFreeReject,
    /// Tier 1: the target's post-move constraining SCC is
    /// multi-module — precisely a `MutualConstrainingCycle` diagnosis
    /// touching the target (clause 3, Pass 1).
    ConstrainingCycleReject,
    /// Tier 1: a cross-module rebinding write touches the target
    /// (clause 2).
    CrossRebindReject,
    /// Tier 2: the target's post-move I-SCC is single-module — Pass 2
    /// is vacuous (modules outside any I-cycle cannot be rejected by
    /// the simulator).
    NoMultiModuleISccAccept,
    /// Tier 2: the target's post-move I-SCC is multi-module but
    /// carries no effective constraining pair — pure-lazy I-cycles
    /// never TDZ (Lemma 2), so Pass 2 is vacuous.
    NoConstrainingPairAccept,
    /// Tier 3: the scoped ESM evaluation simulator found no TDZ pair.
    SimulatorAccept,
    /// Tier 3: the simulator proved a TDZ — an `EsmEvaluationTdz`
    /// diagnosis touching the target.
    SimulatorReject,
}

impl LadderDecision {
    pub fn accepts(self) -> bool {
        matches!(
            self,
            Self::DeltaFreeAccept
                | Self::NoMultiModuleISccAccept
                | Self::NoConstrainingPairAccept
                | Self::SimulatorAccept
        )
    }
}

#[derive(Debug, Clone, Default)]
pub(super) struct QuotientOverlay {
    pub(super) i_delta: BTreeMap<(ModuleId, ModuleId), isize>,
    pub(super) constraining_delta: BTreeMap<(ModuleId, ModuleId), isize>,
    pub(super) constraining_added: BTreeMap<(ModuleId, ModuleId), ConstrainingBucket>,
    pub(super) constraining_removed: BTreeMap<(ModuleId, ModuleId), ConstrainingBucket>,
    pub(super) cross_rebind_added: BTreeMap<OwnerEdgeId, CrossRebindEdge>,
    pub(super) cross_rebind_removed: BTreeSet<OwnerEdgeId>,
}

impl QuotientOverlay {
    /// True when the move changes no quotient contribution at all —
    /// the ladder's tier-0 "delta-free" condition (post-state ==
    /// pre-state). Stricter than [`super::overlay_is_simulator_noop`],
    /// which ignores cross-rebind edits.
    pub(super) fn is_empty(&self) -> bool {
        self.i_delta.is_empty()
            && self.constraining_delta.is_empty()
            && self.constraining_added.is_empty()
            && self.constraining_removed.is_empty()
            && self.cross_rebind_added.is_empty()
            && self.cross_rebind_removed.is_empty()
    }

    pub(super) fn add_contribution(&mut self, contribution: EdgeContribution) {
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

    pub(super) fn remove_contribution(&mut self, contribution: EdgeContribution) {
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

pub(super) fn increment_delta(
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

/// What `edge` contributes to the quotient under the owner → module
/// assignment `module_of`, projected through the gate view exactly as
/// `IncrementalQuotient::add_current_edge` does for the committed state.
pub(super) fn edge_contribution(
    edge: &OwnerEdge,
    residual: ModuleId,
    module_of: impl Fn(OwnerId) -> ModuleId,
) -> Option<EdgeContribution> {
    let (from, to) = project_endpoints(edge, residual, EndpointView::Gate, module_of)?;
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

pub(super) struct OverlayGraphView<'a> {
    pub(super) base: &'a CountedDiGraph<ModuleId>,
    pub(super) delta: &'a BTreeMap<(ModuleId, ModuleId), isize>,
    pub(super) added_out: BTreeMap<ModuleId, BTreeSet<ModuleId>>,
    pub(super) added_in: BTreeMap<ModuleId, BTreeSet<ModuleId>>,
}

impl<'a> OverlayGraphView<'a> {
    pub(super) fn new(
        base: &'a CountedDiGraph<ModuleId>,
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

    pub(super) fn scc_containing(&self, node: ModuleId) -> BTreeSet<ModuleId> {
        if !self.has_neighbor(node, WalkDirection::Forward)
            || !self.has_neighbor(node, WalkDirection::Reverse)
        {
            return BTreeSet::from([node]);
        }
        let forward = self.reachable_from(node, WalkDirection::Forward);
        let reverse = self.reachable_from(node, WalkDirection::Reverse);
        forward.intersection(&reverse).copied().collect()
    }

    pub(super) fn reachable_from(
        &self,
        start: ModuleId,
        direction: WalkDirection,
    ) -> BTreeSet<ModuleId> {
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

    pub(super) fn has_neighbor(&self, node: ModuleId, direction: WalkDirection) -> bool {
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
                if let Some(overlay) = self.added_out.get(&node)
                    && overlay.iter().any(|&n| check(n))
                {
                    return true;
                }
            }
            WalkDirection::Reverse => {
                if self.base.predecessors(node).any(check) {
                    return true;
                }
                if let Some(overlay) = self.added_in.get(&node)
                    && overlay.iter().any(|&n| check(n))
                {
                    return true;
                }
            }
        }
        false
    }

    pub(super) fn effective_count(&self, from: ModuleId, to: ModuleId) -> isize {
        self.base.edge_count(from, to) as isize + self.delta.get(&(from, to)).copied().unwrap_or(0)
    }
}

#[derive(Debug, Clone, Copy)]
pub(super) enum WalkDirection {
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
pub(super) struct IncrementalQuotient {
    pub(super) i_graph: CountedDiGraph<ModuleId>,
    pub(super) constraining_graph: CountedDiGraph<ModuleId>,
    pub(super) constraining_buckets: BTreeMap<(ModuleId, ModuleId), ConstrainingBucket>,
    pub(super) cross_rebinds: BTreeMap<OwnerEdgeId, CrossRebindEdge>,
    /// Chunk's residual module — the ESM DFS root. The Lemma 2
    /// simulator that decides candidate asymmetric I-SCCs needs to
    /// know which module gets the source_import_position reversal
    /// (residual) vs which use plain linker_position
    /// (every other module).
    pub(super) residual: ModuleId,
    /// Lazily-computed base `EsmEvaluationSimulator` for the current
    /// committed I-graph / constraining-buckets state. Invalidated on
    /// every `add_current_edge` / `remove_current_edge` that mutates
    /// the underlying graphs. Used by `verdict()` and
    /// `verdict_touching()` directly, and by `build_simulator(Some(_))`
    /// when the overlay introduces no I-graph or constraining-edge
    /// changes (the no-op overlay short-circuit).
    pub(super) cached_base_simulator: RefCell<Option<EsmEvaluationSimulator>>,
    /// Lazily-computed materialization of the base I-graph as an
    /// adjacency map keyed by source module. See `ISuccessorsMap`.
    /// Invalidated alongside the simulator cache.
    pub(super) cached_base_i_successors: RefCell<Option<ISuccessorsMap>>,
    /// Lazily-computed snapshot of the constraining pairs set
    /// (`constraining_buckets.keys()`). See `ConstrainingPairs`.
    pub(super) cached_base_constraining_pairs: RefCell<Option<ConstrainingPairs>>,
    /// Tier-1 structure of the gate ladder (plan §3/§4): SCC
    /// condensation order maintained over `constraining_graph`.
    /// Updated in the same `add_current_edge` / `remove_current_edge`
    /// funnel that maintains the graph. `RefCell` because queries need
    /// `&mut` (path halving, lazy rebuild) while the verdict/ladder
    /// API is `&self`, matching the simulator caches.
    pub(super) constraining_order: RefCell<CondensationOrder<ModuleId>>,
    /// Tier-2 structure: the same condensation order over `i_graph`.
    pub(super) i_order: RefCell<CondensationOrder<ModuleId>>,
    /// Tier-0 memo: `verdict_touching(module).is_realizable()` for the
    /// current committed quotient state. Cleared on every quotient
    /// mutation — including cross-rebind edits, which bypass
    /// `invalidate_cached_simulator`.
    pub(super) cached_touching_clean: RefCell<BTreeMap<ModuleId, bool>>,
}

impl IncrementalQuotient {
    pub(super) fn new(owner_graph: &OwnerGraph, partition: &Partition) -> Self {
        let mut quotient = Self {
            i_graph: CountedDiGraph::new(),
            constraining_graph: CountedDiGraph::new(),
            constraining_buckets: BTreeMap::new(),
            cross_rebinds: BTreeMap::new(),
            residual: partition.residual(),
            cached_base_simulator: RefCell::new(None),
            cached_base_i_successors: RefCell::new(None),
            cached_base_constraining_pairs: RefCell::new(None),
            // Both orders start stale and lazily rebuild from their
            // base graph on the first ladder query.
            constraining_order: RefCell::new(CondensationOrder::new()),
            i_order: RefCell::new(CondensationOrder::new()),
            cached_touching_clean: RefCell::new(BTreeMap::new()),
        };
        for edge in owner_graph.iter_edges() {
            quotient.add_current_edge(edge, partition);
        }
        quotient
    }

    /// Invalidate the cached base simulator and its precomputed input
    /// snapshots. Called from every mutating path that changes the
    /// I-graph adjacency or the constraining-edge buckets. Each is
    /// rebuilt lazily on the next read.
    pub(super) fn invalidate_cached_simulator(&mut self) {
        *self.cached_base_simulator.borrow_mut() = None;
        *self.cached_base_i_successors.borrow_mut() = None;
        *self.cached_base_constraining_pairs.borrow_mut() = None;
        // Every path that invalidates the simulator also changed the
        // graphs/buckets the tier-0 touching verdict reads.
        self.cached_touching_clean.get_mut().clear();
    }

    /// Borrow the base simulator, building it on demand if the cache
    /// is empty. The simulator is a function of `(i_graph,
    /// constraining_buckets, residual)` — all of which are stable
    /// between mutating calls — so the cached instance is reused
    /// across queries that don't apply an overlay.
    pub(super) fn base_simulator(&self) -> std::cell::Ref<'_, EsmEvaluationSimulator> {
        {
            let mut slot = self.cached_base_simulator.borrow_mut();
            if slot.is_none() {
                let (i_successors, constraining_pairs) = self.effective_simulator_inputs(None);
                *slot = Some(EsmEvaluationSimulator::build(
                    &i_successors,
                    &constraining_pairs,
                    self.residual,
                ));
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
    pub(super) fn base_i_successors(
        &self,
    ) -> std::cell::Ref<'_, BTreeMap<ModuleId, BTreeSet<ModuleId>>> {
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
    pub(super) fn base_constraining_pairs(
        &self,
    ) -> std::cell::Ref<'_, BTreeSet<(ModuleId, ModuleId)>> {
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

    pub(super) fn add_current_edge(
        &mut self,
        edge: &analysis::graph::OwnerEdge,
        partition: &Partition,
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
            // Rebinds bypass the simulator/graph invalidation but DO
            // change the touching verdict the tier-0 memo caches.
            self.cached_touching_clean.get_mut().clear();
            return;
        }

        // I-graph or constraining-bucket mutation invalidates the
        // cached base simulator.
        self.invalidate_cached_simulator();
        self.i_graph.increment_edge(from, to);
        self.i_order.get_mut().insert_edge(&self.i_graph, from, to);
        if !edge.reason.constrains_init_order() {
            return;
        }
        self.constraining_graph.increment_edge(from, to);
        self.constraining_order
            .get_mut()
            .insert_edge(&self.constraining_graph, from, to);
        let bucket = self.constraining_buckets.entry((from, to)).or_default();
        bucket.insert_edge(edge.id, edge.reason.is_sequenced());
    }

    pub(super) fn remove_current_edge(
        &mut self,
        edge: &analysis::graph::OwnerEdge,
        partition: &Partition,
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
            // Mirror `add_current_edge`: rebind edits change the
            // touching verdict the tier-0 memo caches.
            self.cached_touching_clean.get_mut().clear();
            return;
        }

        // I-graph or constraining-bucket mutation invalidates the
        // cached base simulator.
        self.invalidate_cached_simulator();
        self.i_graph.decrement_edge(from, to);
        self.i_order.get_mut().remove_edge(&self.i_graph, from, to);
        if !edge.reason.constrains_init_order() {
            return;
        }
        self.constraining_graph.decrement_edge(from, to);
        self.constraining_order
            .get_mut()
            .remove_edge(&self.constraining_graph, from, to);
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

    pub(super) fn verdict(&self) -> RealizabilityVerdict {
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
                core: SccCore {
                    modules,
                    constraining_owner_edges,
                },
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
                    core: SccCore {
                        modules,
                        constraining_owner_edges,
                    },
                    rejection: SccRejection::EsmEvaluationTdz,
                });
            }
        }

        verdict
    }

    pub(super) fn verdict_touching(&self, module: ModuleId) -> RealizabilityVerdict {
        let mut verdict = RealizabilityVerdict {
            unrealizable_sccs: Vec::new(),
            cross_rebinds: self.cross_rebinds_touching(module),
        };
        let mut reported = BTreeSet::<BTreeSet<ModuleId>>::new();

        let no_delta = BTreeMap::new();
        let constraining_modules =
            OverlayGraphView::new(&self.constraining_graph, &no_delta).scc_containing(module);
        if constraining_modules.len() >= 2 {
            let constraining_owner_edges = self.constraining_edges_inside(&constraining_modules);
            reported.insert(constraining_modules.clone());
            verdict.unrealizable_sccs.push(SccDiagnosis {
                core: SccCore {
                    modules: constraining_modules,
                    constraining_owner_edges,
                },
                rejection: SccRejection::MutualConstrainingCycle,
            });
        }

        let i_modules = OverlayGraphView::new(&self.i_graph, &no_delta).scc_containing(module);
        if i_modules.len() >= 2
            && !reported.contains(&i_modules)
            && self
                .constraining_buckets
                .keys()
                .any(|(from, to)| i_modules.contains(from) && i_modules.contains(to))
        {
            let simulation = self.build_simulator(None);
            let constraining_pairs: BTreeSet<(ModuleId, ModuleId)> =
                self.constraining_buckets.keys().copied().collect();
            let tdz_pairs: Vec<(ModuleId, ModuleId)> = simulation
                .tdz_pairs(&i_modules, &constraining_pairs)
                .collect();
            if !tdz_pairs.is_empty() {
                let constraining_owner_edges = self.tdz_constraining_edges(&tdz_pairs, None);
                verdict.unrealizable_sccs.push(SccDiagnosis {
                    core: SccCore {
                        modules: i_modules,
                        constraining_owner_edges,
                    },
                    rejection: SccRejection::EsmEvaluationTdz,
                });
            }
        }

        verdict
    }

    pub(super) fn verdict_with_overlay_touching(
        &self,
        module: ModuleId,
        overlay: &QuotientOverlay,
    ) -> RealizabilityVerdict {
        let mut verdict = RealizabilityVerdict {
            unrealizable_sccs: Vec::new(),
            cross_rebinds: self.cross_rebinds_touching_with_overlay(module, overlay),
        };
        let mut reported = BTreeSet::<BTreeSet<ModuleId>>::new();

        let constraining_graph =
            OverlayGraphView::new(&self.constraining_graph, &overlay.constraining_delta);
        let constraining_modules = constraining_graph.scc_containing(module);
        if constraining_modules.len() >= 2 {
            let constraining_owner_edges =
                self.constraining_edges_inside_with_overlay(&constraining_modules, overlay);
            reported.insert(constraining_modules.clone());
            verdict.unrealizable_sccs.push(SccDiagnosis {
                core: SccCore {
                    modules: constraining_modules,
                    constraining_owner_edges,
                },
                rejection: SccRejection::MutualConstrainingCycle,
            });
        }

        let i_graph_view = OverlayGraphView::new(&self.i_graph, &overlay.i_delta);
        let i_modules = i_graph_view.scc_containing(module);
        if i_modules.len() >= 2
            && !reported.contains(&i_modules)
            && self.overlay_constraining_pair_inside(&i_modules, overlay)
        {
            let tdz_pairs = self.overlay_tdz_pairs(&i_modules, overlay);
            if !tdz_pairs.is_empty() {
                let constraining_owner_edges =
                    self.tdz_constraining_edges(&tdz_pairs, Some(overlay));
                verdict.unrealizable_sccs.push(SccDiagnosis {
                    core: SccCore {
                        modules: i_modules,
                        constraining_owner_edges,
                    },
                    rejection: SccRejection::EsmEvaluationTdz,
                });
            }
        }

        verdict
    }

    /// Whether `modules` contains both endpoints of an effective
    /// constraining pair under `overlay` — the Pass-2 candidacy test
    /// shared by `verdict_with_overlay_touching` and the ladder's
    /// tier 2 (pure-lazy I-SCCs never TDZ).
    pub(super) fn overlay_constraining_pair_inside(
        &self,
        modules: &BTreeSet<ModuleId>,
        overlay: &QuotientOverlay,
    ) -> bool {
        self.constraining_pairs_with_overlay(overlay)
            .iter()
            .any(|(from, to)| {
                modules.contains(from)
                    && modules.contains(to)
                    && !self
                        .constraining_bucket_with_overlay((*from, *to), overlay)
                        .is_empty()
            })
    }

    /// TDZ-violating constraining pairs inside `modules` under
    /// `overlay` — the exact Pass-2 evaluation (overlay-patched
    /// simulator build + post-order check). Shared by the
    /// evidence-producing `verdict_with_overlay_touching` and the
    /// ladder's tier 3, so the two cannot drift.
    pub(super) fn overlay_tdz_pairs(
        &self,
        modules: &BTreeSet<ModuleId>,
        overlay: &QuotientOverlay,
    ) -> Vec<(ModuleId, ModuleId)> {
        let simulation = self.build_simulator(Some(overlay));
        let effective_pairs: BTreeSet<(ModuleId, ModuleId)> = self
            .constraining_pairs_with_overlay(overlay)
            .into_iter()
            .filter(|pair| {
                !self
                    .constraining_bucket_with_overlay(*pair, overlay)
                    .is_empty()
            })
            .collect();
        simulation.tdz_pairs(modules, &effective_pairs).collect()
    }

    /// Whether `a` and `b` sit in the same multi-module SCC of the
    /// maintained constraining condensation — the `O(α)` DSU probe
    /// behind the greedy's cycle-reduction sort key. On realizable
    /// committed states (clause 3, Pass 1 clean) no multi-module
    /// constraining SCC exists, so this is `false` for every pair.
    pub(super) fn modules_share_constraining_multi_scc(&self, a: ModuleId, b: ModuleId) -> bool {
        self.constraining_order
            .borrow_mut()
            .same_multi_scc(&self.constraining_graph, a, b)
    }

    /// Tier-0 memo: `verdict_touching(module).is_realizable()` against
    /// the committed quotient state, cached until the next mutation.
    fn touching_is_clean(&self, module: ModuleId) -> bool {
        if let Some(&clean) = self.cached_touching_clean.borrow().get(&module) {
            return clean;
        }
        let clean = self.verdict_touching(module).is_realizable();
        self.cached_touching_clean
            .borrow_mut()
            .insert(module, clean);
        clean
    }

    /// Allocation-free boolean twin of
    /// `cross_rebinds_touching_with_overlay` for the ladder's tier-1
    /// clause-2 check.
    fn any_cross_rebind_touching_with_overlay(
        &self,
        module: ModuleId,
        overlay: &QuotientOverlay,
    ) -> bool {
        self.cross_rebinds.iter().any(|(edge_id, rebind)| {
            !overlay.cross_rebind_removed.contains(edge_id)
                && (rebind.from == module || rebind.to == module)
        }) || overlay
            .cross_rebind_added
            .values()
            .any(|rebind| rebind.from == module || rebind.to == module)
    }

    /// Tier-laddered boolean evaluation of the touching predicate: each tier either
    /// decides — its skip condition is a theorem about
    /// `verdict_with_overlay_touching(module, overlay)` — or
    /// escalates, and tier 3 runs the same scoped-simulator
    /// evaluation the verdict path runs, so the boolean cannot drift
    /// from the evidence-producing form.
    pub(super) fn ladder_decide(
        &self,
        module: ModuleId,
        overlay: &QuotientOverlay,
    ) -> LadderDecision {
        // Tier 0: delta-free move — post-state == pre-state, so the
        // (cached) committed-state touching verdict decides.
        if overlay.is_empty() {
            return if self.touching_is_clean(module) {
                LadderDecision::DeltaFreeAccept
            } else {
                LadderDecision::DeltaFreeReject
            };
        }

        // Tier 1: clause 2 (cross-rebinds touching `module`) and
        // Pass 1 — is `module`'s post-move constraining SCC
        // multi-module? — on the maintained constraining condensation.
        if self.constraining_order.borrow_mut().would_join_multi_scc(
            &self.constraining_graph,
            &overlay.constraining_delta,
            module,
        ) {
            return LadderDecision::ConstrainingCycleReject;
        }
        if self.any_cross_rebind_touching_with_overlay(module, overlay) {
            return LadderDecision::CrossRebindReject;
        }

        // Tier 2: Pass-2 vacuity on the I-condensation. Overlay
        // removals inside a multi-module I-SCC route through the
        // exact bidirectional fallback inside `would_join_multi_scc`
        // (plan §3, tier-2 exactness caveat).
        if !self
            .i_order
            .borrow_mut()
            .would_join_multi_scc(&self.i_graph, &overlay.i_delta, module)
        {
            return LadderDecision::NoMultiModuleISccAccept;
        }
        // Multi-module I-SCC (rare): materialize its member set —
        // the constraining-pair lookup needs it, exactly as
        // `verdict_with_overlay_touching` computes it.
        let i_modules =
            OverlayGraphView::new(&self.i_graph, &overlay.i_delta).scc_containing(module);
        if !self.overlay_constraining_pair_inside(&i_modules, overlay) {
            return LadderDecision::NoConstrainingPairAccept;
        }

        // Tier 3: exact Pass 2 — the shared scoped-simulator
        // evaluation.
        if self.overlay_tdz_pairs(&i_modules, overlay).is_empty() {
            LadderDecision::SimulatorAccept
        } else {
            LadderDecision::SimulatorReject
        }
    }

    /// Resolve a list of TDZ-violating `(from, to)` pairs to their
    /// owner-edge ids, optionally applying `overlay`'s edits. Used
    /// by `verdict*` to surface only the surgical set of
    /// constraining edges the simulator flagged.
    pub(super) fn tdz_constraining_edges(
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
    pub(super) fn build_simulator(
        &self,
        overlay: Option<&QuotientOverlay>,
    ) -> EsmEvaluationSimulator {
        if overlay_is_simulator_noop(overlay) {
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
    pub(super) fn build_simulator_from_scratch(
        &self,
        overlay: Option<&QuotientOverlay>,
    ) -> EsmEvaluationSimulator {
        let (i_successors, constraining_pairs) = self.effective_simulator_inputs(overlay);
        EsmEvaluationSimulator::build(&i_successors, &constraining_pairs, self.residual)
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
    pub(super) fn effective_simulator_inputs(
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

    pub(super) fn overlay_for_move(
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
            let current =
                edge_contribution(edge, partition.residual(), |owner| partition.of(owner));
            let next = edge_contribution(edge, partition.residual(), |owner| {
                if owners.contains(&owner) {
                    to
                } else {
                    partition.of(owner)
                }
            });
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

    pub(super) fn cross_rebinds_touching_with_overlay(
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

    pub(super) fn cross_rebinds_touching(&self, module: ModuleId) -> Vec<CrossRebindEdge> {
        let mut rebinds: Vec<CrossRebindEdge> = self
            .cross_rebinds
            .values()
            .filter(|rebind| rebind.from == module || rebind.to == module)
            .cloned()
            .collect();
        rebinds.sort_by_key(|rebind| rebind.owner_edge);
        rebinds
    }

    pub(super) fn constraining_edges_inside(
        &self,
        modules: &BTreeSet<ModuleId>,
    ) -> Vec<OwnerEdgeId> {
        let mut edges = Vec::new();
        for ((from, to), bucket) in &self.constraining_buckets {
            if modules.contains(from) && modules.contains(to) {
                edges.extend(bucket.evidence_edges());
            }
        }
        edges.sort();
        edges
    }

    pub(super) fn constraining_edges_inside_with_overlay(
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

    pub(super) fn constraining_pairs_with_overlay(
        &self,
        overlay: &QuotientOverlay,
    ) -> BTreeSet<(ModuleId, ModuleId)> {
        let mut pairs: BTreeSet<(ModuleId, ModuleId)> =
            self.constraining_buckets.keys().copied().collect();
        pairs.extend(overlay.constraining_added.keys().copied());
        pairs.extend(overlay.constraining_removed.keys().copied());
        pairs
    }

    pub(super) fn constraining_bucket_with_overlay(
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
