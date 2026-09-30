//! Incremental SCC-condensation order: the tier-1/2 structure of the
//! incremental-gate unification design.
//!
//! Generalizes Pearce–Kelly `TopoOrder` from "topological order over a
//! DAG, degrade to `!is_dag` when cycles appear" to "topological order over the
//! **condensation** of an arbitrary directed graph": a union-find
//! tracks SCC membership, a PK rank order is maintained over the
//! condensation DAG, and cycles are **unioned** instead of degrading
//! the order — condensations are DAGs by construction, so the kernel's
//! `is_dag` escape hatch and cone-DFS fallback have no analogue here.
//!
//! ## Node model
//!
//! Nodes are the caller's module identifiers (`N: Copy + Ord`). A
//! union-find tracks SCC membership: recomputed on rebuild, coarsened
//! incrementally by edge insertions in between. An SCC is
//! **multi-module** iff it has at least two nodes; that is the
//! predicate tier 1 of the gate ladder rejects on.
//!
//! ## Mutation protocol
//!
//! The caller owns the base graph (a [`RollbackDiGraph`]) and reports
//! every committed mutation *after* applying it to the base:
//!
//! - [`Self::insert_edge`] / [`Self::remove_edge`] after
//!   `increment_edge` / `decrement_edge`. Parallel-edge count changes
//!   that do not add or drop a distinct adjacency pair are no-ops.
//! - [`Self::invalidate`] when the base changed out-of-band (the undo
//!   path): the structure marks itself stale and lazily rebuilds from
//!   the base on the next query — undo is off the hot path everywhere,
//!   so no journaled DSU is maintained (plan §4, journal interaction).
//!
//! **Monotonicity**: within a committed run, insertions only ever
//! coarsen the SCC partition, which is why a plain (non-rollbackable)
//! union-find suffices. Edge *removals* can split an SCC; a committed
//! removal internal to a multi-module SCC marks the structure stale
//! instead of attempting a split, and the next query rebuilds in
//! `O(|V| + |E|)`.
//!
//! ## Speculative queries
//!
//! [`Self::would_join_multi_scc`] answers, without mutating: in the
//! base graph patched by a `±edge` overlay (the same delta shape
//! `QuotientOverlay` maintains), does the query node's SCC contain any
//! other module? Overlay removals are applied exactly during
//! traversal; an overlay removal internal to a multi-module SCC — the
//! one case where the maintained membership is too coarse — routes
//! through an exact bidirectional reachability fallback.
//!
//! Fast-path cost: an `O(α)` union-find probe. A singleton SCC can
//! only become multi-module through an overlay-added adjacency pair
//! (removals only shrink the graph), so without additions the answer
//! is immediate; with additions a cone-bounded DFS decides exactly.

use std::collections::{BTreeMap, BTreeSet};

use petgraph::algo::tarjan_scc;
use petgraph::graphmap::DiGraphMap;

use crate::rollback_graph::RollbackDiGraph;

/// Sentinel rank for interned nodes that are not live condensation
/// representatives (absorbed into another SCC, or awaiting a rebuild).
const DEAD_RANK: u32 = u32::MAX;

/// Find with path halving over a parent vector.
fn find(parent: &mut [u32], mut x: u32) -> u32 {
    while parent[x as usize] != x {
        let grandparent = parent[parent[x as usize] as usize];
        parent[x as usize] = grandparent;
        x = grandparent;
    }
    x
}

/// Per-call overlay classification: effective additions (new
/// adjacency pairs) and whether any effective removal lands inside a
/// multi-module SCC (the exact-fallback trigger).
struct OverlayShape {
    /// Node-index pairs whose effective count is positive while the
    /// base count is zero.
    additions: Vec<(u32, u32)>,
    removal_inside_multi_scc: bool,
}

#[derive(Debug, Clone)]
pub struct CondensationOrder<N> {
    /// Interned nodes; `nodes[idx]` is the caller-side identifier.
    nodes: Vec<N>,
    idx_of: BTreeMap<N, u32>,
    /// SCC union-find.
    scc_parent: Vec<u32>,
    /// Members per SCC representative.
    scc_members: Vec<Vec<u32>>,
    /// PK topological rank per SCC representative over the
    /// condensation DAG; `DEAD_RANK` for non-representatives.
    rank: Vec<u32>,
    /// Inverse of `rank` over live representatives; `None` holes are
    /// slots vacated by unions.
    pos_to_rep: Vec<Option<u32>>,
    /// Per-DFS visited marker, keyed by node index; a slot is visited
    /// in the current traversal iff it equals `current_epoch`.
    visited_epoch: Vec<u32>,
    current_epoch: u32,
    /// Set by committed removals inside multi-module SCCs and by
    /// `invalidate`; cleared by the lazy rebuild.
    stale: bool,
}

impl<N> Default for CondensationOrder<N>
where
    N: Copy + Ord,
{
    fn default() -> Self {
        Self::new()
    }
}

impl<N> CondensationOrder<N>
where
    N: Copy + Ord,
{
    /// Construct an empty, stale order. The first query rebuilds from
    /// the base graph passed to it — there is no separate init call.
    pub fn new() -> Self {
        Self {
            nodes: Vec::new(),
            idx_of: BTreeMap::new(),
            scc_parent: Vec::new(),
            scc_members: Vec::new(),
            rank: Vec::new(),
            pos_to_rep: Vec::new(),
            visited_epoch: Vec::new(),
            current_epoch: 0,
            stale: true,
        }
    }

    /// Mark the maintained order stale. The next query rebuilds from
    /// the base graph. Use after out-of-band base mutations (undo).
    pub fn invalidate(&mut self) {
        self.stale = true;
    }

    /// Whether `u` and `v` sit in the same multi-module SCC. The
    /// `O(α)` DSU probe backing the greedy's cycle-reduction sort key:
    /// a merge of two modules inside one multi-module SCC dissolves part
    /// of an unrealizable cycle. Rebuilds first if stale.
    pub fn same_multi_scc(&mut self, base: &RollbackDiGraph<N>, u: N, v: N) -> bool {
        self.ensure_fresh(base);
        let (Some(&iu), Some(&iv)) = (self.idx_of.get(&u), self.idx_of.get(&v)) else {
            return false;
        };
        let su = find(&mut self.scc_parent, iu);
        let sv = find(&mut self.scc_parent, iv);
        su == sv && self.scc_members[su as usize].len() >= 2
    }

    /// Report a committed edge insertion. Call **after**
    /// `base.increment_edge(u, v)`. No-op for parallel edges (the
    /// adjacency pair already existed) and self-edges; otherwise the
    /// standard PK insertion path runs, unioning any cycle the new
    /// edge closes and re-ranking the affected window.
    pub fn insert_edge(&mut self, base: &RollbackDiGraph<N>, u: N, v: N) {
        let iu = self.intern(u);
        let iv = self.intern(v);
        if self.stale || u == v {
            return;
        }
        debug_assert!(
            base.edge_count(u, v) > 0,
            "insert_edge must be called after base.increment_edge",
        );
        if base.edge_count(u, v) > 1 {
            return;
        }
        let su = find(&mut self.scc_parent, iu);
        let sv = find(&mut self.scc_parent, iv);
        if su == sv {
            return;
        }
        let ru = self.rank[su as usize];
        let rv = self.rank[sv as usize];
        debug_assert!(ru != DEAD_RANK && rv != DEAD_RANK);
        if ru < rv {
            return;
        }
        self.rerank_window(base, rv, ru);
    }

    /// Report a committed edge removal. Call **after**
    /// `base.decrement_edge(u, v)`. No-op while parallel edges remain
    /// or when the removed pair crossed two condensation nodes (a
    /// sub-DAG of a DAG keeps the rank order valid). A removal
    /// internal to an SCC may split it — the structure marks itself
    /// stale instead of attempting the split, and the next query
    /// rebuilds.
    pub fn remove_edge(&mut self, base: &RollbackDiGraph<N>, u: N, v: N) {
        if self.stale || u == v {
            return;
        }
        let (Some(&iu), Some(&iv)) = (self.idx_of.get(&u), self.idx_of.get(&v)) else {
            return;
        };
        if base.edge_count(u, v) > 0 {
            return;
        }
        if find(&mut self.scc_parent, iu) == find(&mut self.scc_parent, iv) {
            self.stale = true;
        }
    }

    /// Speculative query: in the base graph patched by `overlay`
    /// (`(from, to) → ±count` deltas, the `QuotientOverlay` shape), does
    /// `n`'s SCC contain any module other than `n`?
    ///
    /// Exact by construction: the fast path's skip conditions are
    /// theorems about the maintained condensation (see module docs),
    /// and every case they cannot decide routes to a cone-bounded DFS
    /// or the exact fallback over the effective adjacency. Rebuilds
    /// first if stale.
    pub fn would_join_multi_scc(
        &mut self,
        base: &RollbackDiGraph<N>,
        overlay: &BTreeMap<(N, N), isize>,
        n: N,
    ) -> bool {
        self.ensure_fresh(base);
        let idx = self.intern(n);
        for &(a, b) in overlay.keys() {
            self.intern(a);
            self.intern(b);
        }
        let shape = self.classify_overlay(base, overlay);
        if shape.removal_inside_multi_scc {
            // The maintained SCC membership may be too coarse under
            // this overlay; run the exact bidirectional fallback.
            return self.exact_multi_scc(base, overlay, &shape, idx);
        }
        let scc = find(&mut self.scc_parent, idx);
        // A surviving multi-module SCC stays mutually reachable (no
        // overlay removal touches a multi-module SCC interior on this
        // path).
        if self.scc_members[scc as usize].len() >= 2 {
            return true;
        }
        // A singleton SCC becomes multi-module only through a cycle
        // that uses an overlay-added adjacency pair; removals only
        // shrink it.
        !shape.additions.is_empty() && self.cone_cycle_through(base, overlay, &shape.additions, scc)
    }

    fn intern(&mut self, n: N) -> u32 {
        if let Some(&idx) = self.idx_of.get(&n) {
            return idx;
        }
        let idx = self.nodes.len() as u32;
        self.nodes.push(n);
        self.idx_of.insert(n, idx);
        self.scc_parent.push(idx);
        self.scc_members.push(vec![idx]);
        self.visited_epoch.push(0);
        if self.stale {
            self.rank.push(DEAD_RANK);
        } else {
            // A fresh node has no edges yet; appending it at the end
            // of the order is trivially valid.
            self.rank.push(self.pos_to_rep.len() as u32);
            self.pos_to_rep.push(Some(idx));
        }
        idx
    }

    fn ensure_fresh(&mut self, base: &RollbackDiGraph<N>) {
        if self.stale {
            self.rebuild(base);
        }
    }

    /// Full `O(|V| + |E|)` recompute: SCCs of the base graph via
    /// Tarjan, then Kahn over the condensation for ranks.
    fn rebuild(&mut self, base: &RollbackDiGraph<N>) {
        for (a, b) in base.edge_pairs() {
            self.intern(a);
            self.intern(b);
        }
        let n = self.nodes.len();
        self.scc_parent = (0..n as u32).collect();
        self.scc_members = (0..n as u32).map(|i| vec![i]).collect();
        self.rank = vec![DEAD_RANK; n];
        let pairs: Vec<(N, N)> = base.edge_pairs().collect();
        let mut graph: DiGraphMap<u32, ()> = DiGraphMap::new();
        for i in 0..n as u32 {
            graph.add_node(i);
        }
        for (a, b) in &pairs {
            let (ia, ib) = (self.idx_of[a], self.idx_of[b]);
            if ia != ib {
                graph.add_edge(ia, ib, ());
            }
        }
        for group in tarjan_scc(&graph) {
            if group.len() < 2 {
                continue;
            }
            let mut members = group.into_iter();
            let mut acc = members.next().expect("group.len() >= 2");
            for member in members {
                acc = self.scc_union(acc, member);
            }
        }
        // Ranks: Kahn over the condensation (a DAG by construction).
        let mut reps: BTreeSet<u32> = BTreeSet::new();
        for i in 0..n as u32 {
            reps.insert(find(&mut self.scc_parent, i));
        }
        let mut edges: BTreeSet<(u32, u32)> = BTreeSet::new();
        for (a, b) in &pairs {
            let ra = find(&mut self.scc_parent, self.idx_of[a]);
            let rb = find(&mut self.scc_parent, self.idx_of[b]);
            if ra != rb {
                edges.insert((ra, rb));
            }
        }
        self.pos_to_rep = vec![None; reps.len()];
        self.kahn_assign_ranks(&reps, &edges, 0);
        self.visited_epoch.clear();
        self.visited_epoch.resize(n, 0);
        self.current_epoch = 0;
        self.stale = false;
    }

    /// Union two SCC groups (arguments need not be representatives, but
    /// must belong to different groups). Returns the surviving
    /// representative. The absorbed representative's rank is cleared;
    /// the survivor's rank is left for the caller to (re)assign.
    fn scc_union(&mut self, a: u32, b: u32) -> u32 {
        let ra = find(&mut self.scc_parent, a);
        let rb = find(&mut self.scc_parent, b);
        debug_assert_ne!(ra, rb);
        let (survivor, absorbed) =
            if self.scc_members[ra as usize].len() >= self.scc_members[rb as usize].len() {
                (ra, rb)
            } else {
                (rb, ra)
            };
        self.scc_parent[absorbed as usize] = survivor;
        let moved = std::mem::take(&mut self.scc_members[absorbed as usize]);
        self.scc_members[survivor as usize].extend(moved);
        self.rank[absorbed as usize] = DEAD_RANK;
        survivor
    }

    /// Re-rank the condensation window `[lo, hi]` (inclusive rank
    /// bounds): collect the window's representatives, union any cycle
    /// among them (Tarjan over the window-induced condensation
    /// subgraph), then Kahn-assign consecutive ranks from `lo`.
    /// `O(|window| + |E_window|)` — the PK affected-region bound.
    fn rerank_window(&mut self, base: &RollbackDiGraph<N>, lo: u32, hi: u32) {
        debug_assert!(!self.stale);
        let window: Vec<u32> = self.pos_to_rep[lo as usize..=hi as usize]
            .iter()
            .flatten()
            .copied()
            .collect();
        let window_set: BTreeSet<u32> = window.iter().copied().collect();
        // Window-internal condensation edges, derived by mapping
        // member-level base edges through the SCC union-find — no
        // separate condensation edge store exists (plan §4).
        let mut edges: BTreeSet<(u32, u32)> = BTreeSet::new();
        for &r in &window {
            for member_pos in 0..self.scc_members[r as usize].len() {
                let m = self.scc_members[r as usize][member_pos];
                let targets: Vec<N> = base.successors(self.nodes[m as usize]).collect();
                for t in targets {
                    let it = self.idx_of[&t];
                    let st = find(&mut self.scc_parent, it);
                    if st != r && window_set.contains(&st) {
                        edges.insert((r, st));
                    }
                }
            }
        }
        // Any cycle closed by the triggering mutation lies entirely in
        // the window: pre-mutation ranks were valid, so every path
        // between the window's endpoints stays inside `[lo, hi]`.
        let mut graph: DiGraphMap<u32, ()> = DiGraphMap::new();
        for &r in &window {
            graph.add_node(r);
        }
        for &(a, b) in &edges {
            graph.add_edge(a, b, ());
        }
        for group in tarjan_scc(&graph) {
            if group.len() < 2 {
                continue;
            }
            let mut members = group.into_iter();
            let mut acc = members.next().expect("group.len() >= 2");
            for member in members {
                acc = self.scc_union(acc, member);
            }
        }
        let reps: BTreeSet<u32> = window
            .iter()
            .map(|&r| find(&mut self.scc_parent, r))
            .collect();
        let edges: BTreeSet<(u32, u32)> = edges
            .iter()
            .map(|&(a, b)| (find(&mut self.scc_parent, a), find(&mut self.scc_parent, b)))
            .filter(|(a, b)| a != b)
            .collect();
        let next = self.kahn_assign_ranks(&reps, &edges, lo);
        for slot in next..=hi {
            self.pos_to_rep[slot as usize] = None;
        }
    }

    /// Assign consecutive ranks starting at `start` to `reps` in a
    /// topological order of `edges` (which must be acyclic — cycles
    /// are unioned before this runs). Deterministic: ties break by
    /// node index. Returns the first unassigned rank.
    fn kahn_assign_ranks(
        &mut self,
        reps: &BTreeSet<u32>,
        edges: &BTreeSet<(u32, u32)>,
        start: u32,
    ) -> u32 {
        let mut indegree: BTreeMap<u32, usize> = reps.iter().map(|&r| (r, 0)).collect();
        let mut adjacency: BTreeMap<u32, Vec<u32>> = BTreeMap::new();
        for &(a, b) in edges {
            adjacency.entry(a).or_default().push(b);
            *indegree.get_mut(&b).expect("edge endpoint in reps") += 1;
        }
        let mut queue: Vec<u32> = indegree
            .iter()
            .filter(|&(_, &degree)| degree == 0)
            .map(|(&r, _)| r)
            .collect();
        queue.sort_unstable();
        queue.reverse(); // Pop smallest first.
        let mut next = start;
        let mut placed = 0usize;
        while let Some(r) = queue.pop() {
            self.rank[r as usize] = next;
            self.pos_to_rep[next as usize] = Some(r);
            next += 1;
            placed += 1;
            let Some(successors) = adjacency.get(&r) else {
                continue;
            };
            let mut newly_free: Vec<u32> = Vec::new();
            for &s in successors {
                let degree = indegree.get_mut(&s).expect("successor in reps");
                *degree -= 1;
                if *degree == 0 {
                    newly_free.push(s);
                }
            }
            newly_free.sort_unstable();
            for s in newly_free.into_iter().rev() {
                queue.push(s);
            }
        }
        assert_eq!(
            placed,
            reps.len(),
            "condensation Kahn incomplete — cycles must have been unioned first",
        );
        next
    }

    /// Classify the overlay at the effective level: which entries add
    /// a new adjacency pair, and whether any entry removes an edge
    /// internal to a multi-module SCC (the exact-fallback trigger —
    /// such a removal can split the SCC, making the maintained
    /// membership too coarse).
    fn classify_overlay(
        &mut self,
        base: &RollbackDiGraph<N>,
        overlay: &BTreeMap<(N, N), isize>,
    ) -> OverlayShape {
        let mut shape = OverlayShape {
            additions: Vec::new(),
            removal_inside_multi_scc: false,
        };
        for (&(a, b), &delta) in overlay {
            if a == b {
                continue;
            }
            let ia = self.idx_of[&a];
            let ib = self.idx_of[&b];
            let base_count = base.edge_count(a, b) as isize;
            let effective = base_count + delta;
            if base_count > 0 && effective <= 0 {
                if find(&mut self.scc_parent, ia) == find(&mut self.scc_parent, ib) {
                    shape.removal_inside_multi_scc = true;
                }
            } else if base_count == 0 && effective > 0 {
                shape.additions.push((ia, ib));
            }
        }
        shape
    }

    fn bump_epoch(&mut self) -> u32 {
        if self.current_epoch == u32::MAX {
            // Wraparound: zero the buffer so stale "epoch 1" marks
            // from long-ago traversals cannot prune the next DFS.
            self.visited_epoch.fill(0);
            self.current_epoch = 1;
        } else {
            self.current_epoch += 1;
        }
        self.current_epoch
    }

    /// Cone-bounded exact search used when the overlay adds adjacency
    /// pairs: does the effective condensation contain a cycle through
    /// the singleton SCC `scc`?
    fn cone_cycle_through(
        &mut self,
        base: &RollbackDiGraph<N>,
        overlay: &BTreeMap<(N, N), isize>,
        additions: &[(u32, u32)],
        scc: u32,
    ) -> bool {
        // Overlay-added adjacency keyed by source SCC representative.
        let mut added_out: BTreeMap<u32, Vec<u32>> = BTreeMap::new();
        for &(ia, ib) in additions {
            let sa = find(&mut self.scc_parent, ia);
            added_out.entry(sa).or_default().push(ib);
        }
        let epoch = self.bump_epoch();
        let mut stack: Vec<u32> = Vec::new();
        for st in self.effective_successor_reps(base, overlay, &added_out, scc) {
            if self.visited_epoch[st as usize] != epoch {
                self.visited_epoch[st as usize] = epoch;
                stack.push(st);
            }
        }
        while let Some(r) = stack.pop() {
            for st in self.effective_successor_reps(base, overlay, &added_out, r) {
                if st == scc {
                    return true;
                }
                if self.visited_epoch[st as usize] != epoch {
                    self.visited_epoch[st as usize] = epoch;
                    stack.push(st);
                }
            }
        }
        false
    }

    /// Effective condensation successors of representative `r`:
    /// member-level base edges with positive effective count plus
    /// overlay-added pairs, mapped through the SCC union-find.
    /// Materialized (the union-find needs `&mut` for path halving).
    fn effective_successor_reps(
        &mut self,
        base: &RollbackDiGraph<N>,
        overlay: &BTreeMap<(N, N), isize>,
        added_out: &BTreeMap<u32, Vec<u32>>,
        r: u32,
    ) -> Vec<u32> {
        let mut successors: Vec<u32> = Vec::new();
        for member_pos in 0..self.scc_members[r as usize].len() {
            let m = self.scc_members[r as usize][member_pos];
            let from = self.nodes[m as usize];
            let targets: Vec<N> = base.successors(from).collect();
            for t in targets {
                if effective_count(base, overlay, from, t) <= 0 {
                    continue;
                }
                let st = find(&mut self.scc_parent, self.idx_of[&t]);
                if st != r {
                    successors.push(st);
                }
            }
        }
        for &ib in added_out.get(&r).into_iter().flatten() {
            let st = find(&mut self.scc_parent, ib);
            if st != r {
                successors.push(st);
            }
        }
        successors
    }

    /// Exact fallback for overlays that remove an edge inside a
    /// multi-module SCC: bidirectional reachability over the effective
    /// graph (the maintained SCC layer is bypassed entirely, since the
    /// overlay may have split it). True iff some other node is mutually
    /// reachable with `idx`.
    fn exact_multi_scc(
        &self,
        base: &RollbackDiGraph<N>,
        overlay: &BTreeMap<(N, N), isize>,
        shape: &OverlayShape,
        idx: u32,
    ) -> bool {
        let mut added_out: BTreeMap<u32, Vec<u32>> = BTreeMap::new();
        let mut added_in: BTreeMap<u32, Vec<u32>> = BTreeMap::new();
        for &(ia, ib) in &shape.additions {
            added_out.entry(ia).or_default().push(ib);
            added_in.entry(ib).or_default().push(ia);
        }
        let forward = self.reach(base, overlay, &added_out, idx, Direction::Forward);
        let reverse = self.reach(base, overlay, &added_in, idx, Direction::Reverse);
        !forward.is_disjoint(&reverse)
    }

    /// Nodes reachable from `start` over the effective graph in the
    /// given direction, excluding `start` itself (paths must leave it).
    fn reach(
        &self,
        base: &RollbackDiGraph<N>,
        overlay: &BTreeMap<(N, N), isize>,
        added: &BTreeMap<u32, Vec<u32>>,
        start: u32,
        direction: Direction,
    ) -> BTreeSet<u32> {
        let mut seen: BTreeSet<u32> = BTreeSet::new();
        let mut stack: Vec<u32> = vec![start];
        while let Some(i) = stack.pop() {
            let node = self.nodes[i as usize];
            let neighbors: Vec<N> = match direction {
                Direction::Forward => base.successors(node).collect(),
                Direction::Reverse => base.predecessors(node).collect(),
            };
            for t in neighbors {
                let effective = match direction {
                    Direction::Forward => effective_count(base, overlay, node, t),
                    Direction::Reverse => effective_count(base, overlay, t, node),
                };
                if effective <= 0 {
                    continue;
                }
                let it = self.idx_of[&t];
                if it != start && seen.insert(it) {
                    stack.push(it);
                }
            }
            for &it in added.get(&i).into_iter().flatten() {
                if it != start && seen.insert(it) {
                    stack.push(it);
                }
            }
        }
        seen
    }

    /// Whether the structure is awaiting a lazy rebuild.
    #[cfg(test)]
    pub(super) fn is_stale(&self) -> bool {
        self.stale
    }

    /// Internal-consistency check: rank/inverse-index agreement, the
    /// topological invariant over the condensation, and member
    /// bookkeeping. A no-op while stale (nothing is
    /// maintained). SCC-partition correctness against `tarjan_scc` is
    /// asserted separately by the differential tests.
    #[cfg(test)]
    pub(super) fn validate(&mut self, base: &RollbackDiGraph<N>) -> Result<(), String>
    where
        N: std::fmt::Debug,
    {
        if self.stale {
            return Ok(());
        }
        let n = self.nodes.len();
        for i in 0..n as u32 {
            let rep = find(&mut self.scc_parent, i);
            let rank = self.rank[i as usize];
            if rep == i {
                if rank == DEAD_RANK {
                    return Err(format!("live rep {i} has DEAD rank"));
                }
                if self.pos_to_rep.get(rank as usize).copied().flatten() != Some(i) {
                    return Err(format!("rank inverse broken for rep {i} at rank {rank}"));
                }
            } else if rank != DEAD_RANK {
                return Err(format!("non-rep {i} carries live rank {rank}"));
            }
        }
        for (pos, slot) in self.pos_to_rep.iter().enumerate() {
            if let Some(r) = *slot {
                if find(&mut self.scc_parent, r) != r {
                    return Err(format!("pos_to_rep[{pos}] = {r} is not a representative"));
                }
                if self.rank[r as usize] as usize != pos {
                    return Err(format!(
                        "pos_to_rep[{pos}] = {r} but rank[{r}] = {}",
                        self.rank[r as usize]
                    ));
                }
            }
        }
        let pairs: Vec<(N, N)> = base.edge_pairs().collect();
        for (a, b) in pairs {
            let ra = find(&mut self.scc_parent, self.idx_of[&a]);
            let rb = find(&mut self.scc_parent, self.idx_of[&b]);
            if ra != rb && self.rank[ra as usize] >= self.rank[rb as usize] {
                return Err(format!(
                    "condensation edge {a:?} → {b:?} violates rank order \
                     ({} >= {})",
                    self.rank[ra as usize], self.rank[rb as usize]
                ));
            }
        }
        // Members partition the nodes.
        let mut seen_members = vec![false; n];
        for i in 0..n as u32 {
            if find(&mut self.scc_parent, i) != i {
                continue;
            }
            let member_list = self.scc_members[i as usize].clone();
            for m in member_list {
                if std::mem::replace(&mut seen_members[m as usize], true) {
                    return Err(format!("node {m} appears in two member lists"));
                }
                if find(&mut self.scc_parent, m) != i {
                    return Err(format!("member {m} of rep {i} resolves elsewhere"));
                }
            }
        }
        if let Some(missing) = seen_members.iter().position(|&seen| !seen) {
            return Err(format!("node {missing} missing from every member list"));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy)]
enum Direction {
    Forward,
    Reverse,
}

/// Effective multiplicity of the `(from, to)` adjacency under the
/// overlay: base count plus the overlay's signed delta.
fn effective_count<N: Copy + Ord>(
    base: &RollbackDiGraph<N>,
    overlay: &BTreeMap<(N, N), isize>,
    from: N,
    to: N,
) -> isize {
    base.edge_count(from, to) as isize + overlay.get(&(from, to)).copied().unwrap_or(0)
}

/// Brute-force reference shared by the unit tests and the proptest
/// differential suite (`condensation_order_proptest.rs`).
#[cfg(test)]
pub(super) mod test_support {
    use std::collections::{BTreeMap, BTreeSet};

    use petgraph::algo::tarjan_scc;
    use petgraph::graphmap::DiGraphMap;

    use super::CondensationOrder;
    use crate::rollback_graph::RollbackDiGraph;

    pub fn graph(edges: &[(usize, usize)]) -> RollbackDiGraph<usize> {
        let mut graph = RollbackDiGraph::new();
        for &(a, b) in edges {
            graph.increment_edge(a, b);
        }
        graph
    }

    /// Whether `n` sits in a multi-module SCC of the maintained order.
    pub fn in_multi_scc(
        order: &mut CondensationOrder<usize>,
        base: &RollbackDiGraph<usize>,
        n: usize,
    ) -> bool {
        order.same_multi_scc(base, n, n)
    }

    /// Brute-force reference for `would_join_multi_scc`: tarjan over
    /// `base` patched by `overlay`; true iff `n`'s SCC has size ≥ 2.
    pub fn brute_would_join(
        base: &RollbackDiGraph<usize>,
        overlay: &BTreeMap<(usize, usize), isize>,
        n: usize,
    ) -> bool {
        let mut pairs: BTreeSet<(usize, usize)> = base.edge_pairs().collect();
        pairs.extend(overlay.keys().copied());
        let mut graph: DiGraphMap<usize, ()> = DiGraphMap::new();
        graph.add_node(n);
        for (a, b) in pairs {
            let effective =
                base.edge_count(a, b) as isize + overlay.get(&(a, b)).copied().unwrap_or(0);
            if a != b && effective > 0 {
                graph.add_edge(a, b, ());
            }
        }
        tarjan_scc(&graph)
            .into_iter()
            .any(|scc| scc.len() >= 2 && scc.contains(&n))
    }
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;

    use super::test_support::*;
    use super::*;

    /// Build a fresh `CondensationOrder` that has seen every edge of
    /// `base` via `insert_edge` (after a forced initial rebuild on an
    /// empty graph, so the incremental insertion path is exercised).
    fn order_via_inserts(base: &RollbackDiGraph<usize>) -> CondensationOrder<usize> {
        let mut order = CondensationOrder::new();
        let empty = RollbackDiGraph::<usize>::new();
        // Force the initial rebuild on the empty graph so subsequent
        // insert_edge calls run the incremental PK path, not rebuild.
        assert!(!in_multi_scc(&mut order, &empty, 0));
        let mut shadow = RollbackDiGraph::new();
        for (a, b) in base.edge_pairs() {
            for _ in 0..base.edge_count(a, b) {
                shadow.increment_edge(a, b);
                order.insert_edge(&shadow, a, b);
            }
        }
        order
    }

    #[test]
    fn acyclic_and_unseen_nodes_are_not_multi_scc() {
        let base = graph(&[(0, 1), (2, 3)]);
        let mut order = CondensationOrder::new();
        for n in [0, 1, 2, 3, 7] {
            assert!(!in_multi_scc(&mut order, &base, n), "{n}");
        }
    }

    #[test]
    fn insert_edge_closing_cycle_unions_scc_instead_of_degrading() {
        // 0 → 1 → 2, then insert 2 → 0: a plain PK topological order
        // has no valid order here; the condensation order unions.
        let mut base = graph(&[(0, 1), (1, 2), (2, 3)]);
        let mut order = order_via_inserts(&base);
        assert!(!in_multi_scc(&mut order, &base, 0));
        base.increment_edge(2, 0);
        order.insert_edge(&base, 2, 0);
        assert!(!order.is_stale(), "cycle insertion must not go stale");
        for n in [0, 1, 2] {
            assert!(in_multi_scc(&mut order, &base, n), "{n} joined the SCC");
        }
        assert!(!in_multi_scc(&mut order, &base, 3), "3 is downstream only");
        order.validate(&base).expect("valid after cycle union");
    }

    #[test]
    fn removal_inside_multi_scc_goes_stale_and_rebuild_splits() {
        let mut base = graph(&[(0, 1), (1, 2), (2, 0)]);
        let mut order = CondensationOrder::new();
        assert!(in_multi_scc(&mut order, &base, 0));
        base.decrement_edge(2, 0);
        order.remove_edge(&base, 2, 0);
        assert!(order.is_stale(), "in-SCC removal marks stale");
        for n in [0, 1, 2] {
            assert!(
                !in_multi_scc(&mut order, &base, n),
                "SCC split after rebuild"
            );
        }
        assert!(!order.is_stale(), "query rebuilt lazily");
        order.validate(&base).expect("valid after rebuild");
    }

    #[test]
    fn cross_scc_removal_stays_fresh() {
        let mut base = graph(&[(0, 1), (1, 2)]);
        let mut order = CondensationOrder::new();
        assert!(!in_multi_scc(&mut order, &base, 0)); // force initial build
        base.decrement_edge(0, 1);
        order.remove_edge(&base, 0, 1);
        assert!(!order.is_stale(), "cross-condensation removal is free");
        order.validate(&base).expect("still valid");
    }

    #[test]
    fn parallel_edge_count_changes_are_noops() {
        let mut base = graph(&[(0, 1), (0, 1), (1, 2)]);
        let mut order = CondensationOrder::new();
        assert!(!in_multi_scc(&mut order, &base, 0));
        // Dropping one of two parallel edges keeps the adjacency pair.
        base.decrement_edge(0, 1);
        order.remove_edge(&base, 0, 1);
        assert!(!order.is_stale());
        base.increment_edge(0, 1);
        order.insert_edge(&base, 0, 1);
        order
            .validate(&base)
            .expect("valid across parallel changes");
    }

    #[test]
    fn invalidate_forces_rebuild_reflecting_out_of_band_edits() {
        let mut base = graph(&[(0, 1)]);
        let mut order = CondensationOrder::new();
        assert!(!in_multi_scc(&mut order, &base, 0));
        // Mutate the base without telling the structure (the undo
        // path), then invalidate.
        base.increment_edge(1, 0);
        order.invalidate();
        assert!(in_multi_scc(&mut order, &base, 0), "rebuild sees the cycle");
        order
            .validate(&base)
            .expect("valid after invalidate+rebuild");
    }

    #[test]
    fn overlay_addition_closing_cycle_is_detected() {
        // Base 0 → 1 → 2; the overlay adds 2 → 0, closing a 3-cycle.
        let base = graph(&[(0, 1), (1, 2)]);
        let mut order = CondensationOrder::new();
        let overlay = BTreeMap::from([((2usize, 0usize), 1isize)]);
        for n in [0, 1, 2] {
            assert!(order.would_join_multi_scc(&base, &overlay, n), "{n}");
            assert!(!in_multi_scc(&mut order, &base, n), "{n} without overlay");
        }
    }

    #[test]
    fn overlay_removal_inside_multi_scc_takes_exact_fallback() {
        // SCC {0, 1, 2} via 0 ⇄ 1 ⇄ 2. The overlay removes 1 → 2,
        // splitting 2 off while {0, 1} stays a cycle. The coarse
        // membership would still report 2 as multi; the exact
        // fallback must not.
        let base = graph(&[(0, 1), (1, 0), (1, 2), (2, 1)]);
        let mut order = CondensationOrder::new();
        assert!(in_multi_scc(&mut order, &base, 2));
        let overlay = BTreeMap::from([((1usize, 2usize), -1isize)]);
        assert!(order.would_join_multi_scc(&base, &overlay, 0));
        assert!(order.would_join_multi_scc(&base, &overlay, 1));
        assert!(!order.would_join_multi_scc(&base, &overlay, 2));
    }

    #[test]
    fn epoch_wraparound_resets_visited_buffer() {
        // Drive the cone DFS across the u32::MAX epoch boundary:
        // without zeroing on wraparound, a stale "epoch 1" mark on the
        // intermediate would prune the post-wrap search and flip the
        // answer to false.
        let base = graph(&[(0, 1), (1, 2)]);
        let overlay = BTreeMap::from([((2usize, 0usize), 1isize)]);
        let mut order = CondensationOrder::new();
        assert!(order.would_join_multi_scc(&base, &overlay, 0));
        order.current_epoch = u32::MAX - 1;
        let intermediate = order.idx_of[&1];
        order.visited_epoch[intermediate as usize] = 1;
        assert!(order.would_join_multi_scc(&base, &overlay, 0));
        assert_eq!(order.current_epoch, u32::MAX);
        assert!(order.would_join_multi_scc(&base, &overlay, 0));
        assert_eq!(order.current_epoch, 1);
    }
}
