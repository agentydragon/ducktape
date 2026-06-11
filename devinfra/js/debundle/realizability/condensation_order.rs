//! Incremental SCC-condensation order: the tier-1/2 structure of the
//! incremental-gate unification design
//! (`plans/incremental_gate_unification.md` §4).
//!
//! Generalizes the peel kernel's deleted Pearce–Kelly `TopoOrder`
//! (formerly `peel/topo_order.rs`; absorbed here by the §8 PR 4
//! cutover) from "topological order over a DAG, degrade to `!is_dag`
//! when cycles appear" to "topological order over the
//! **condensation** of an arbitrary directed graph": a union-find
//! tracks SCC membership, a PK rank order is maintained over the
//! condensation DAG, and cycles are **unioned** instead of degrading
//! the order — condensations are DAGs by construction, so the kernel's
//! `is_dag` escape hatch and cone-DFS fallback have no analogue here.
//!
//! ## Node model
//!
//! Nodes are the caller's module identifiers (`N: Copy + Ord`). Two
//! distinct equivalence layers are maintained:
//!
//! - **Contraction aliases** (`apply_contract`): committed module
//!   identifications. Persistent — they survive `invalidate` +
//!   rebuild, because the base graph may keep edges keyed by either
//!   pre-contraction node.
//! - **SCC membership**: alias classes that are mutually reachable in
//!   the base graph. Recomputed on rebuild; coarsened incrementally by
//!   edge insertions and contractions in between.
//!
//! Each SCC representative carries a `module_count` — the number of
//! distinct alias classes (post-contraction modules) inside it. An SCC
//! is **multi-module** iff `module_count ≥ 2`; that is the predicate
//! tier 1 of the gate ladder rejects on. Contracting two modules that
//! already share an SCC therefore *decrements* the count (a 2-module
//! mutual cycle contracted into one module is realizable — the
//! atomic-unit anomaly fix of plan §2).
//!
//! ## Mutation protocol
//!
//! The caller owns the base graph (a [`RollbackDiGraph`]) and reports
//! every committed mutation *after* applying it to the base:
//!
//! - [`Self::insert_edge`] / [`Self::remove_edge`] after
//!   `increment_edge` / `decrement_edge`. Parallel-edge count changes
//!   that do not add or drop a distinct adjacency pair are no-ops.
//! - [`Self::apply_contract`] for a committed module identification.
//!   The base may keep the loser's edges keyed by the loser or be
//!   relabeled to the winner before the call — traversal maps every
//!   node through the alias layer either way.
//! - [`Self::invalidate`] when the base changed out-of-band (the undo
//!   path): the structure marks itself stale and lazily rebuilds from
//!   the base on the next query — undo is off the hot path everywhere,
//!   so no journaled DSU is maintained (plan §4, journal interaction).
//!
//! **Monotonicity**: within a committed run, insertions and
//! contractions only ever coarsen the SCC partition, which is why a
//! plain (non-rollbackable) union-find suffices. Edge *removals* can
//! split an SCC; a committed removal internal to a multi-module SCC
//! marks the structure stale instead of attempting a split, and the
//! next query rebuilds in `O(|V| + |E|)`.
//!
//! ## Speculative queries
//!
//! [`Self::would_join_multi_scc`] answers, without mutating: in the
//! base graph patched by a `±edge` overlay (the same delta shape
//! `QuotientOverlay` maintains) and with two query nodes `u`, `v`
//! identified, does the merged node's SCC contain any module besides
//! the merged one? Overlay removals are applied exactly during
//! traversal; an overlay removal internal to a multi-module SCC — the
//! one case where the maintained membership is too coarse — routes
//! through an exact bidirectional reachability fallback (plan §3,
//! tier-2 exactness caveat).
//!
//! Fast-path cost mirrors PK: `O(α)` union-find probes plus one
//! rank-window-bounded DFS when the overlay adds no new adjacency
//! pairs; overlay additions disable the rank prune for that query
//! (ranks only order the *base* condensation) and fall back to a
//! cone-bounded DFS that is still exact.

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

/// How a union combines the two sides' module counts.
#[derive(Debug, Clone, Copy)]
enum McRule {
    /// Contraction: two modules become one. `mc = mc(a) + mc(b) - 1`,
    /// or `mc - 1` when both already share an SCC.
    Contract,
    /// Cycle formation: distinct modules joined into one SCC.
    /// `mc = mc(a) + mc(b)`.
    CycleSum,
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
    /// Contraction-alias union-find (persistent across rebuilds).
    alias_parent: Vec<u32>,
    /// Original-node members per alias representative (small-to-large
    /// merged; only meaningful at representatives).
    alias_members: Vec<Vec<u32>>,
    /// SCC union-find. Refines-then-coarsens the alias layer: every
    /// alias union is mirrored here, and cycle unions coarsen further.
    scc_parent: Vec<u32>,
    /// Original-node members per SCC representative.
    scc_members: Vec<Vec<u32>>,
    /// Number of distinct alias classes per SCC representative.
    module_count: Vec<u32>,
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
            alias_parent: Vec::new(),
            alias_members: Vec::new(),
            scc_parent: Vec::new(),
            scc_members: Vec::new(),
            module_count: Vec::new(),
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

    /// Whether `n`'s SCC contains at least two modules (alias
    /// classes). The `O(α)` tier-1 probe. Rebuilds first if stale.
    pub fn is_in_multi_scc(&mut self, base: &RollbackDiGraph<N>, n: N) -> bool {
        self.ensure_fresh(base);
        let Some(&idx) = self.idx_of.get(&n) else {
            return false;
        };
        let rep = find(&mut self.scc_parent, idx);
        self.module_count[rep as usize] >= 2
    }

    /// Whether `u` and `v` sit in the same multi-module SCC. The
    /// `O(α)` DSU probe backing the greedy's cycle-reduction sort key
    /// (`plans/incremental_gate_unification.md` §6): a merge of two
    /// modules inside one multi-module SCC dissolves part of an
    /// unrealizable cycle. Rebuilds first if stale.
    pub fn same_multi_scc(&mut self, base: &RollbackDiGraph<N>, u: N, v: N) -> bool {
        self.ensure_fresh(base);
        let (Some(&iu), Some(&iv)) = (self.idx_of.get(&u), self.idx_of.get(&v)) else {
            return false;
        };
        let su = find(&mut self.scc_parent, iu);
        let sv = find(&mut self.scc_parent, iv);
        su == sv && self.module_count[su as usize] >= 2
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
    /// internal to a multi-module SCC may split it — per plan §4 the
    /// structure marks itself stale instead of attempting the split,
    /// and the next query rebuilds.
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
        let su = find(&mut self.scc_parent, iu);
        let sv = find(&mut self.scc_parent, iv);
        if su != sv {
            return;
        }
        let au = find(&mut self.alias_parent, iu);
        let av = find(&mut self.alias_parent, iv);
        if au != av && self.module_count[su as usize] >= 2 {
            self.stale = true;
        }
    }

    /// Report a committed contraction: `winner` and `loser` are one
    /// module from now on. The identification is recorded in the
    /// persistent alias layer; if it closes condensation cycles, the
    /// affected rank window is re-ranked with every new cycle unioned
    /// (never a degraded order). When both already share an SCC the
    /// module count decrements instead — contraction inside a
    /// multi-module SCC moves it *toward* realizability.
    pub fn apply_contract(&mut self, base: &RollbackDiGraph<N>, winner: N, loser: N) {
        let iw = self.intern(winner);
        let il = self.intern(loser);
        let aw = find(&mut self.alias_parent, iw);
        let al = find(&mut self.alias_parent, il);
        if aw == al {
            return;
        }
        // Alias union (persistent), small-to-large on member lists.
        let (survivor, absorbed) =
            if self.alias_members[aw as usize].len() >= self.alias_members[al as usize].len() {
                (aw, al)
            } else {
                (al, aw)
            };
        self.alias_parent[absorbed as usize] = survivor;
        let moved = std::mem::take(&mut self.alias_members[absorbed as usize]);
        self.alias_members[survivor as usize].extend(moved);

        if self.stale {
            return;
        }
        let sw = find(&mut self.scc_parent, iw);
        let sl = find(&mut self.scc_parent, il);
        if sw == sl {
            self.module_count[sw as usize] -= 1;
            return;
        }
        let rw = self.rank[sw as usize];
        let rl = self.rank[sl as usize];
        debug_assert!(rw != DEAD_RANK && rl != DEAD_RANK);
        let (lo, hi) = if rw < rl { (rw, rl) } else { (rl, rw) };
        self.pos_to_rep[rw as usize] = None;
        self.pos_to_rep[rl as usize] = None;
        self.rank[sw as usize] = DEAD_RANK;
        self.rank[sl as usize] = DEAD_RANK;
        let merged = self.scc_union(sw, sl, McRule::Contract);
        self.rank[merged as usize] = lo;
        self.pos_to_rep[lo as usize] = Some(merged);
        self.rerank_window(base, lo, hi);
    }

    /// Speculative query: in the base graph patched by `overlay`
    /// (`(from, to) → ±count` deltas, the `QuotientOverlay` shape) and
    /// with `u` and `v` identified into one node, does the merged
    /// node's SCC contain any module other than the merged one?
    ///
    /// Exact by construction: the fast path's skip conditions are
    /// theorems about the maintained condensation (see module docs),
    /// and every case they cannot decide routes to a window-bounded or
    /// cone-bounded DFS over the effective adjacency. Rebuilds first
    /// if stale.
    pub fn would_join_multi_scc(
        &mut self,
        base: &RollbackDiGraph<N>,
        overlay: &BTreeMap<(N, N), isize>,
        u: N,
        v: N,
    ) -> bool {
        self.ensure_fresh(base);
        let iu = self.intern(u);
        let iv = self.intern(v);
        for &(a, b) in overlay.keys() {
            self.intern(a);
            self.intern(b);
        }
        let shape = self.classify_overlay(base, overlay);
        if shape.removal_inside_multi_scc {
            // The maintained SCC membership may be too coarse under
            // this overlay; run the exact bidirectional fallback.
            return self.exact_merged_multi(base, overlay, &shape, iu, iv);
        }
        let au = find(&mut self.alias_parent, iu);
        let av = find(&mut self.alias_parent, iv);
        let su = find(&mut self.scc_parent, iu);
        let sv = find(&mut self.scc_parent, iv);
        if su != sv {
            // A surviving multi-module SCC at either endpoint stays
            // mutually reachable with the merged node (no overlay
            // removal touches a multi-SCC interior on this path).
            if self.module_count[su as usize] >= 2 || self.module_count[sv as usize] >= 2 {
                return true;
            }
        } else if au == av {
            // No-op merge of one module with itself: a surviving
            // multi-module SCC decides; otherwise only an
            // overlay-added cycle can make the verdict true.
            if self.module_count[su as usize] >= 2 {
                return true;
            }
        } else if self.module_count[su as usize] >= 3 {
            return true;
        }
        // Remaining undecided shapes: su == sv with module_count == 2
        // (the SCC is exactly {alias(u), alias(v)} — its mutual edges
        // become self-loops after the identification), su == sv with
        // a single module, or two singleton modules. A multi-module
        // merged SCC now requires a cycle through the merged node and
        // at least one intermediate.
        if shape.additions.is_empty() {
            if su == sv {
                // A condensation node cannot reach itself through an
                // intermediate in a DAG, and removals only shrink it.
                return false;
            }
            self.windowed_path_through_intermediate(base, overlay, su, sv)
        } else {
            self.cone_cycle_through_merged(base, overlay, &shape.additions, su, sv)
        }
    }

    fn intern(&mut self, n: N) -> u32 {
        if let Some(&idx) = self.idx_of.get(&n) {
            return idx;
        }
        let idx = self.nodes.len() as u32;
        self.nodes.push(n);
        self.idx_of.insert(n, idx);
        self.alias_parent.push(idx);
        self.alias_members.push(vec![idx]);
        self.scc_parent.push(idx);
        self.scc_members.push(vec![idx]);
        self.module_count.push(1);
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

    /// Full `O(|V| + |E|)` recompute: SCCs of the alias-level base
    /// graph via Tarjan, then Kahn over the condensation for ranks.
    /// Contraction aliases are preserved; SCC membership and module
    /// counts are derived fresh.
    fn rebuild(&mut self, base: &RollbackDiGraph<N>) {
        for (a, b) in base.edge_pairs() {
            self.intern(a);
            self.intern(b);
        }
        let n = self.nodes.len();
        // Reset the SCC layer to the alias classes.
        self.scc_parent = (0..n as u32).collect();
        self.scc_members = (0..n as u32).map(|i| vec![i]).collect();
        self.module_count = vec![1; n];
        self.rank = vec![DEAD_RANK; n];
        for i in 0..n as u32 {
            let a = find(&mut self.alias_parent, i);
            if a != i {
                self.scc_union(i, a, McRule::Contract);
            }
        }
        // SCCs of the alias-level graph.
        let mut graph: DiGraphMap<u32, ()> = DiGraphMap::new();
        for i in 0..n as u32 {
            let rep = find(&mut self.scc_parent, i);
            graph.add_node(rep);
        }
        let pairs: Vec<(N, N)> = base.edge_pairs().collect();
        for (a, b) in &pairs {
            let ra = find(&mut self.scc_parent, self.idx_of[a]);
            let rb = find(&mut self.scc_parent, self.idx_of[b]);
            if ra != rb {
                graph.add_edge(ra, rb, ());
            }
        }
        for group in tarjan_scc(&graph) {
            if group.len() < 2 {
                continue;
            }
            let mut members = group.into_iter();
            let mut acc = members.next().expect("group.len() >= 2");
            for member in members {
                acc = self.scc_union(acc, member, McRule::CycleSum);
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

    /// Union two SCC groups (arguments need not be representatives).
    /// Returns the surviving representative. The absorbed
    /// representative's rank is cleared; the survivor's rank is left
    /// for the caller to (re)assign.
    fn scc_union(&mut self, a: u32, b: u32, rule: McRule) -> u32 {
        let ra = find(&mut self.scc_parent, a);
        let rb = find(&mut self.scc_parent, b);
        if ra == rb {
            if matches!(rule, McRule::Contract) {
                self.module_count[ra as usize] -= 1;
            }
            return ra;
        }
        let combined = self.module_count[ra as usize] + self.module_count[rb as usize];
        let (survivor, absorbed) =
            if self.scc_members[ra as usize].len() >= self.scc_members[rb as usize].len() {
                (ra, rb)
            } else {
                (rb, ra)
            };
        self.scc_parent[absorbed as usize] = survivor;
        let moved = std::mem::take(&mut self.scc_members[absorbed as usize]);
        self.scc_members[survivor as usize].extend(moved);
        self.module_count[survivor as usize] = match rule {
            McRule::Contract => combined - 1,
            McRule::CycleSum => combined,
        };
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
                acc = self.scc_union(acc, member, McRule::CycleSum);
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
                let sa = find(&mut self.scc_parent, ia);
                let sb = find(&mut self.scc_parent, ib);
                let aa = find(&mut self.alias_parent, ia);
                let ab = find(&mut self.alias_parent, ib);
                // Intra-alias edges are condensation self-loops; their
                // removal cannot split anything.
                if sa == sb && aa != ab && self.module_count[sa as usize] >= 2 {
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
            for slot in &mut self.visited_epoch {
                *slot = 0;
            }
            self.current_epoch = 1;
        } else {
            self.current_epoch += 1;
        }
        self.current_epoch
    }

    /// PK window search: is there an effective condensation path from
    /// `lo`'s side to `hi`'s side through at least one intermediate
    /// node? Sound to rank-prune because the overlay adds no adjacency
    /// pairs here (the effective graph is a subgraph of the base, and
    /// a subgraph of a DAG respects the DAG's rank order).
    fn windowed_path_through_intermediate(
        &mut self,
        base: &RollbackDiGraph<N>,
        overlay: &BTreeMap<(N, N), isize>,
        su: u32,
        sv: u32,
    ) -> bool {
        let (lo, hi) = if self.rank[su as usize] < self.rank[sv as usize] {
            (su, sv)
        } else {
            (sv, su)
        };
        let hi_rank = self.rank[hi as usize];
        let epoch = self.bump_epoch();
        let mut stack: Vec<u32> = Vec::new();
        // Seed with lo's effective successors, excluding hi (a direct
        // edge becomes a self-loop after the merge, not a new cycle).
        for member_pos in 0..self.scc_members[lo as usize].len() {
            let m = self.scc_members[lo as usize][member_pos];
            let from = self.nodes[m as usize];
            let targets: Vec<N> = base.successors(from).collect();
            for t in targets {
                if effective_count(base, overlay, from, t) <= 0 {
                    continue;
                }
                let st = find(&mut self.scc_parent, self.idx_of[&t]);
                if st == lo || st == hi {
                    continue;
                }
                if self.rank[st as usize] <= hi_rank && self.visited_epoch[st as usize] != epoch {
                    self.visited_epoch[st as usize] = epoch;
                    stack.push(st);
                }
            }
        }
        while let Some(r) = stack.pop() {
            for member_pos in 0..self.scc_members[r as usize].len() {
                let m = self.scc_members[r as usize][member_pos];
                let from = self.nodes[m as usize];
                let targets: Vec<N> = base.successors(from).collect();
                for t in targets {
                    if effective_count(base, overlay, from, t) <= 0 {
                        continue;
                    }
                    let st = find(&mut self.scc_parent, self.idx_of[&t]);
                    if st == hi {
                        return true;
                    }
                    if st == r || st == lo {
                        continue;
                    }
                    if self.rank[st as usize] <= hi_rank && self.visited_epoch[st as usize] != epoch
                    {
                        self.visited_epoch[st as usize] = epoch;
                        stack.push(st);
                    }
                }
            }
        }
        false
    }

    /// Cone-bounded exact search used when the overlay adds adjacency
    /// pairs (rank pruning is unsound for paths through added edges):
    /// does the effective condensation contain a path from the merged
    /// node set `{su, sv}` back into it through ≥ 1 intermediate?
    fn cone_cycle_through_merged(
        &mut self,
        base: &RollbackDiGraph<N>,
        overlay: &BTreeMap<(N, N), isize>,
        additions: &[(u32, u32)],
        su: u32,
        sv: u32,
    ) -> bool {
        // Overlay-added adjacency keyed by source SCC representative.
        let mut added_out: BTreeMap<u32, Vec<u32>> = BTreeMap::new();
        for &(ia, ib) in additions {
            let sa = find(&mut self.scc_parent, ia);
            added_out.entry(sa).or_default().push(ib);
        }
        let epoch = self.bump_epoch();
        let mut stack: Vec<u32> = Vec::new();
        // Seed with the merged set's effective successors. Direct
        // edges back into the merged set become self-loops after the
        // identification, so a merged-set hit during the seed phase is
        // skipped; any hit during expansion went through ≥ 1
        // intermediate and is a genuine cycle.
        let mut seeds: Vec<u32> = vec![su];
        if sv != su {
            seeds.push(sv);
        }
        for r in seeds {
            for st in self.effective_successor_reps(base, overlay, &added_out, r) {
                if st == su || st == sv {
                    continue;
                }
                if self.visited_epoch[st as usize] != epoch {
                    self.visited_epoch[st as usize] = epoch;
                    stack.push(st);
                }
            }
        }
        while let Some(r) = stack.pop() {
            for st in self.effective_successor_reps(base, overlay, &added_out, r) {
                if st == su || st == sv {
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
    /// multi-module SCC: bidirectional reachability at the *alias*
    /// level (the maintained SCC layer is bypassed entirely, since the
    /// overlay may have split it). True iff some alias class outside
    /// the merged pair is mutually reachable with it in the effective
    /// graph.
    fn exact_merged_multi(
        &mut self,
        base: &RollbackDiGraph<N>,
        overlay: &BTreeMap<(N, N), isize>,
        shape: &OverlayShape,
        iu: u32,
        iv: u32,
    ) -> bool {
        let au = find(&mut self.alias_parent, iu);
        let av = find(&mut self.alias_parent, iv);
        let merged: BTreeSet<u32> = BTreeSet::from([au, av]);
        let mut added_out: BTreeMap<u32, Vec<u32>> = BTreeMap::new();
        let mut added_in: BTreeMap<u32, Vec<u32>> = BTreeMap::new();
        for &(ia, ib) in &shape.additions {
            let aa = find(&mut self.alias_parent, ia);
            let ab = find(&mut self.alias_parent, ib);
            added_out.entry(aa).or_default().push(ib);
            added_in.entry(ab).or_default().push(ia);
        }
        let forward = self.alias_reach(base, overlay, &added_out, &merged, Direction::Forward);
        let reverse = self.alias_reach(base, overlay, &added_in, &merged, Direction::Reverse);
        forward
            .intersection(&reverse)
            .any(|rep| !merged.contains(rep))
    }

    /// Alias-level reachability from the merged set over the effective
    /// graph, in the given direction. Excludes the merged set from the
    /// result (paths must leave it).
    fn alias_reach(
        &mut self,
        base: &RollbackDiGraph<N>,
        overlay: &BTreeMap<(N, N), isize>,
        added: &BTreeMap<u32, Vec<u32>>,
        merged: &BTreeSet<u32>,
        direction: Direction,
    ) -> BTreeSet<u32> {
        let mut seen: BTreeSet<u32> = BTreeSet::new();
        let mut stack: Vec<u32> = merged.iter().copied().collect();
        while let Some(a) = stack.pop() {
            for member_pos in 0..self.alias_members[a as usize].len() {
                let m = self.alias_members[a as usize][member_pos];
                let node = self.nodes[m as usize];
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
                    let at = find(&mut self.alias_parent, self.idx_of[&t]);
                    if merged.contains(&at) {
                        continue;
                    }
                    if seen.insert(at) {
                        stack.push(at);
                    }
                }
            }
            for neighbor_idx in added.get(&a).cloned().unwrap_or_default() {
                let at = find(&mut self.alias_parent, neighbor_idx);
                if merged.contains(&at) {
                    continue;
                }
                if seen.insert(at) {
                    stack.push(at);
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
    /// topological invariant over the condensation, and module-count /
    /// member bookkeeping. A no-op while stale (nothing is
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
        // module_count == number of distinct alias classes among the
        // representative's members, and members partition the nodes.
        let mut seen_members = vec![false; n];
        for i in 0..n as u32 {
            if find(&mut self.scc_parent, i) != i {
                continue;
            }
            let member_list = self.scc_members[i as usize].clone();
            let mut alias_classes: BTreeSet<u32> = BTreeSet::new();
            for m in member_list {
                if std::mem::replace(&mut seen_members[m as usize], true) {
                    return Err(format!("node {m} appears in two member lists"));
                }
                if find(&mut self.scc_parent, m) != i {
                    return Err(format!("member {m} of rep {i} resolves elsewhere"));
                }
                alias_classes.insert(find(&mut self.alias_parent, m));
            }
            if alias_classes.len() != self.module_count[i as usize] as usize {
                return Err(format!(
                    "rep {i}: module_count {} != {} distinct alias classes",
                    self.module_count[i as usize],
                    alias_classes.len()
                ));
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

/// Brute-force reference implementations shared by the pinned-seed
/// xorshift suites below and the proptest differential suite
/// (`condensation_order_proptest.rs`).
#[cfg(test)]
pub(super) mod test_support {
    use std::collections::{BTreeMap, BTreeSet};

    use petgraph::algo::tarjan_scc;
    use petgraph::graphmap::DiGraphMap;

    use super::CondensationOrder;
    use crate::rollback_graph::RollbackDiGraph;

    /// Sentinel for the identified `{u, v}` node in the brute-force
    /// reference graphs.
    const MERGED: usize = usize::MAX;

    pub fn no_overlay() -> BTreeMap<(usize, usize), isize> {
        BTreeMap::new()
    }

    pub fn graph(edges: &[(usize, usize)]) -> RollbackDiGraph<usize> {
        let mut graph = RollbackDiGraph::new();
        for &(a, b) in edges {
            graph.increment_edge(a, b);
        }
        graph
    }

    /// Test-side contraction-alias mirror, independent of the
    /// structure's internal union-find.
    #[derive(Clone, Default)]
    pub struct TestAlias {
        parent: BTreeMap<usize, usize>,
    }

    impl TestAlias {
        pub fn find(&self, mut x: usize) -> usize {
            while let Some(&p) = self.parent.get(&x) {
                x = p;
            }
            x
        }

        pub fn union(&mut self, winner: usize, loser: usize) {
            let rw = self.find(winner);
            let rl = self.find(loser);
            if rw != rl {
                self.parent.insert(rl, rw);
            }
        }
    }

    /// Effective edge pairs (count > 0) of `base ± overlay`, mapped
    /// through the alias mirror, self-loops dropped.
    fn effective_alias_edges(
        base: &RollbackDiGraph<usize>,
        alias: &TestAlias,
        overlay: &BTreeMap<(usize, usize), isize>,
    ) -> BTreeSet<(usize, usize)> {
        let mut pairs: BTreeSet<(usize, usize)> = base.edge_pairs().collect();
        pairs.extend(overlay.keys().copied());
        pairs
            .into_iter()
            .filter(|&(a, b)| {
                base.edge_count(a, b) as isize + overlay.get(&(a, b)).copied().unwrap_or(0) > 0
            })
            .map(|(a, b)| (alias.find(a), alias.find(b)))
            .filter(|(a, b)| a != b)
            .collect()
    }

    /// Brute-force reference for `would_join_multi_scc`: tarjan over
    /// the effective graph with `u`'s and `v`'s alias classes
    /// identified into a sentinel node; true iff the sentinel's SCC
    /// has size ≥ 2.
    pub fn brute_would_join(
        base: &RollbackDiGraph<usize>,
        alias: &TestAlias,
        overlay: &BTreeMap<(usize, usize), isize>,
        u: usize,
        v: usize,
    ) -> bool {
        let (au, av) = (alias.find(u), alias.find(v));
        let map = |x: usize| if x == au || x == av { MERGED } else { x };
        let mut graph: DiGraphMap<usize, ()> = DiGraphMap::new();
        graph.add_node(MERGED);
        for (a, b) in effective_alias_edges(base, alias, overlay) {
            let (ma, mb) = (map(a), map(b));
            if ma != mb {
                graph.add_edge(ma, mb, ());
            }
        }
        tarjan_scc(&graph)
            .into_iter()
            .any(|scc| scc.len() >= 2 && scc.contains(&MERGED))
    }

    /// Brute-force SCC partition of `base` at alias level (no
    /// overlay): map node → sorted SCC members, multi SCCs only.
    fn brute_multi_sccs(
        base: &RollbackDiGraph<usize>,
        alias: &TestAlias,
        node_universe: &BTreeSet<usize>,
    ) -> BTreeSet<BTreeSet<usize>> {
        let mut graph: DiGraphMap<usize, ()> = DiGraphMap::new();
        for &n in node_universe {
            graph.add_node(alias.find(n));
        }
        for (a, b) in effective_alias_edges(base, alias, &no_overlay()) {
            graph.add_edge(a, b, ());
        }
        tarjan_scc(&graph)
            .into_iter()
            .filter(|scc| scc.len() >= 2)
            .map(|scc| scc.into_iter().collect())
            .collect()
    }

    /// Assert `is_in_multi_scc` matches the brute-force partition for
    /// every node in the universe.
    pub fn assert_multi_matches_brute(
        order: &mut CondensationOrder<usize>,
        base: &RollbackDiGraph<usize>,
        alias: &TestAlias,
        node_universe: &BTreeSet<usize>,
        context: &str,
    ) {
        let multi = brute_multi_sccs(base, alias, node_universe);
        let in_multi: BTreeSet<usize> = multi.iter().flatten().copied().collect();
        for &n in node_universe {
            assert_eq!(
                order.is_in_multi_scc(base, n),
                in_multi.contains(&alias.find(n)),
                "{context}: is_in_multi_scc({n}) diverges from tarjan",
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use std::collections::{BTreeMap, BTreeSet};

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
        assert!(!order.is_in_multi_scc(&empty, 0));
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
    fn would_join_path_through_intermediate_detected() {
        // 0 → 1 → 2: merging 0 and 2 closes a cycle through 1.
        let base = graph(&[(0, 1), (1, 2)]);
        let mut order = CondensationOrder::new();
        assert!(order.would_join_multi_scc(&base, &no_overlay(), 0, 2));
        assert!(order.would_join_multi_scc(&base, &no_overlay(), 2, 0));
        // Direct edge only: becomes a self-loop, not a multi SCC.
        assert!(!order.would_join_multi_scc(&base, &no_overlay(), 0, 1));
        assert!(!order.would_join_multi_scc(&base, &no_overlay(), 1, 2));
    }

    #[test]
    fn would_join_false_for_unrelated_nodes() {
        let base = graph(&[(0, 1), (2, 3)]);
        let mut order = CondensationOrder::new();
        assert!(!order.would_join_multi_scc(&base, &no_overlay(), 0, 2));
        assert!(!order.would_join_multi_scc(&base, &no_overlay(), 1, 3));
        // Nodes the base graph never saw are isolated singletons.
        assert!(!order.would_join_multi_scc(&base, &no_overlay(), 7, 8));
        assert!(!order.is_in_multi_scc(&base, 9));
    }

    #[test]
    fn insert_edge_closing_cycle_unions_scc_instead_of_degrading() {
        // 0 → 1 → 2, then insert 2 → 0: a plain PK topological order
        // has no valid order here; the condensation order unions.
        let mut base = graph(&[(0, 1), (1, 2), (2, 3)]);
        let mut order = order_via_inserts(&base);
        assert!(!order.is_in_multi_scc(&base, 0));
        base.increment_edge(2, 0);
        order.insert_edge(&base, 2, 0);
        assert!(!order.is_stale(), "cycle insertion must not go stale");
        for n in [0, 1, 2] {
            assert!(order.is_in_multi_scc(&base, n), "{n} joined the SCC");
        }
        assert!(!order.is_in_multi_scc(&base, 3), "3 is downstream only");
        order.validate(&base).expect("valid after cycle union");
        // Membership probe: a no-op merge inside the multi SCC reports
        // the SCC's verdict.
        assert!(order.would_join_multi_scc(&base, &no_overlay(), 0, 0));
    }

    #[test]
    fn apply_contract_closing_cycle_unions_and_recovers_order() {
        // 0 → 1 → 2; contracting 0 and 2 closes a cycle through 1
        // (the gate-bypass case the kernel degrades on).
        let base = graph(&[(0, 1), (1, 2)]);
        let mut order = order_via_inserts(&base);
        order.apply_contract(&base, 0, 2);
        assert!(!order.is_stale());
        assert!(order.is_in_multi_scc(&base, 1), "1 is inside the new SCC");
        assert!(order.is_in_multi_scc(&base, 0));
        order.validate(&base).expect("valid after contract union");
        // The SCC has 2 modules: merged{0,2} and 1.
        // Contracting the remaining pair dissolves it.
        order.apply_contract(&base, 0, 1);
        assert!(!order.is_in_multi_scc(&base, 0), "single module remains");
        order.validate(&base).expect("valid after dissolution");
    }

    #[test]
    fn apply_contract_inside_two_module_cycle_dissolves_multi_scc() {
        // Mutual 2-cycle 0 ⇄ 1: contracting the pair makes it one
        // module — the atomic-unit dissolution semantics of plan §2.
        let base = graph(&[(0, 1), (1, 0)]);
        let mut order = CondensationOrder::new();
        assert!(order.is_in_multi_scc(&base, 0));
        order.apply_contract(&base, 0, 1);
        assert!(!order.is_in_multi_scc(&base, 0));
        assert!(!order.is_in_multi_scc(&base, 1));
        order
            .validate(&base)
            .expect("valid after intra-SCC contract");
    }

    #[test]
    fn contraction_aliases_survive_rebuild() {
        let mut base = graph(&[(0, 1), (1, 2), (2, 0), (3, 0)]);
        let mut order = CondensationOrder::new();
        order.apply_contract(&base, 0, 3);
        assert!(order.is_in_multi_scc(&base, 3) == order.is_in_multi_scc(&base, 0));
        // Force a stale → rebuild transition via an in-SCC removal.
        base.decrement_edge(2, 0);
        order.remove_edge(&base, 2, 0);
        assert!(order.is_stale());
        // After the rebuild, 3 still resolves through the alias: an
        // edge 2 → 3 closes the cycle through the merged module.
        base.increment_edge(2, 3);
        order.insert_edge(&base, 2, 3); // no-op while stale; rebuild covers it
        assert!(order.is_in_multi_scc(&base, 0), "cycle 0→1→2→(3=0)");
        assert!(order.is_in_multi_scc(&base, 3), "alias survived rebuild");
        order
            .validate(&base)
            .expect("valid after rebuild with alias");
    }

    #[test]
    fn removal_inside_multi_scc_goes_stale_and_rebuild_splits() {
        let mut base = graph(&[(0, 1), (1, 2), (2, 0)]);
        let mut order = CondensationOrder::new();
        assert!(order.is_in_multi_scc(&base, 0));
        base.decrement_edge(2, 0);
        order.remove_edge(&base, 2, 0);
        assert!(order.is_stale(), "in-SCC removal marks stale");
        for n in [0, 1, 2] {
            assert!(!order.is_in_multi_scc(&base, n), "SCC split after rebuild");
        }
        assert!(!order.is_stale(), "query rebuilt lazily");
        order.validate(&base).expect("valid after rebuild");
    }

    #[test]
    fn cross_scc_removal_stays_fresh() {
        let mut base = graph(&[(0, 1), (1, 2)]);
        let mut order = CondensationOrder::new();
        assert!(!order.is_in_multi_scc(&base, 0)); // force initial build
        base.decrement_edge(0, 1);
        order.remove_edge(&base, 0, 1);
        assert!(!order.is_stale(), "cross-condensation removal is free");
        order.validate(&base).expect("still valid");
        assert!(!order.would_join_multi_scc(&base, &no_overlay(), 0, 2));
    }

    #[test]
    fn parallel_edge_count_changes_are_noops() {
        let mut base = graph(&[(0, 1), (0, 1), (1, 2)]);
        let mut order = CondensationOrder::new();
        assert!(!order.is_in_multi_scc(&base, 0));
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
        assert!(!order.is_in_multi_scc(&base, 0));
        // Mutate the base without telling the structure (the undo
        // path), then invalidate.
        base.increment_edge(1, 0);
        order.invalidate();
        assert!(order.is_in_multi_scc(&base, 0), "rebuild sees the cycle");
        order
            .validate(&base)
            .expect("valid after invalidate+rebuild");
    }

    #[test]
    fn overlay_addition_closing_cycle_is_detected() {
        // Base 0 → 1; overlay adds 1 → 2. Merging 0 and 2 then closes
        // 0 → 1 → 2 = merged → ... → merged through intermediate 1.
        let base = graph(&[(0, 1)]);
        let mut order = CondensationOrder::new();
        let overlay = BTreeMap::from([((1usize, 2usize), 1isize)]);
        assert!(order.would_join_multi_scc(&base, &overlay, 0, 2));
        assert!(!order.would_join_multi_scc(&base, &no_overlay(), 0, 2));
        // The committed structure is untouched by speculative queries.
        assert!(!order.is_in_multi_scc(&base, 1));
    }

    #[test]
    fn overlay_removal_breaking_the_path_is_respected() {
        let base = graph(&[(0, 1), (1, 2)]);
        let mut order = CondensationOrder::new();
        let overlay = BTreeMap::from([((1usize, 2usize), -1isize)]);
        assert!(!order.would_join_multi_scc(&base, &overlay, 0, 2));
        assert!(order.would_join_multi_scc(&base, &no_overlay(), 0, 2));
    }

    #[test]
    fn overlay_removal_inside_multi_scc_takes_exact_fallback() {
        // SCC {0, 1} via 0 ⇄ 1, plus bystander 2. The overlay removes
        // 1 → 0, splitting the SCC. The coarse membership would still
        // report 1 as multi; the exact fallback must say merging
        // {1, 2} yields a singleton merged SCC.
        let base = graph(&[(0, 1), (1, 0)]);
        let mut order = CondensationOrder::new();
        assert!(order.is_in_multi_scc(&base, 1));
        let overlay = BTreeMap::from([((1usize, 0usize), -1isize)]);
        assert!(!order.would_join_multi_scc(&base, &overlay, 1, 2));
        // Same overlay, but merging {0, 1} themselves: also singleton
        // (the surviving 0 → 1 edge becomes a self-loop).
        assert!(!order.would_join_multi_scc(&base, &overlay, 0, 1));
        // Sanity: without the removal the merge keeps... {0,1} is the
        // whole SCC, so merging them dissolves it too.
        assert!(!order.would_join_multi_scc(&base, &no_overlay(), 0, 1));
    }

    #[test]
    fn merged_scc_with_third_module_survives_pair_merge() {
        // 3-cycle 0 → 1 → 2 → 0: merging any two leaves a 2-module
        // cycle with the third — still multi.
        let base = graph(&[(0, 1), (1, 2), (2, 0)]);
        let mut order = CondensationOrder::new();
        for (u, v) in [(0, 1), (1, 2), (0, 2)] {
            assert!(
                order.would_join_multi_scc(&base, &no_overlay(), u, v),
                "({u},{v}) keeps a third module in the cycle",
            );
        }
    }

    #[test]
    fn epoch_wraparound_resets_visited_buffer() {
        // Drive the windowed DFS across the u32::MAX epoch boundary:
        // without zeroing on wraparound, a stale "epoch 1" mark on the
        // intermediate would prune the post-wrap search and flip the
        // answer to false.
        let base = graph(&[(0, 1), (1, 2), (2, 3)]);
        let mut order = CondensationOrder::new();
        assert!(order.would_join_multi_scc(&base, &no_overlay(), 0, 3));
        order.current_epoch = u32::MAX - 1;
        let intermediate = order.idx_of[&1];
        order.visited_epoch[intermediate as usize] = 1;
        assert!(order.would_join_multi_scc(&base, &no_overlay(), 0, 3));
        assert_eq!(order.current_epoch, u32::MAX);
        assert!(order.would_join_multi_scc(&base, &no_overlay(), 0, 3));
        assert_eq!(order.current_epoch, 1);
    }

    /// Simple xorshift RNG for deterministic tests.
    struct SimpleRng(u64);
    impl SimpleRng {
        fn new(seed: u64) -> Self {
            Self(seed.max(1))
        }
        fn next_u32(&mut self) -> u32 {
            let mut x = self.0;
            x ^= x << 13;
            x ^= x >> 7;
            x ^= x << 17;
            self.0 = x;
            x as u32
        }
    }

    #[test]
    fn random_graph_would_join_matches_brute_force() {
        // Random (cyclic) graphs + random overlays; every pair's
        // would_join_multi_scc must equal the tarjan-based reference
        // on the effective identified graph. Mirrors the kernel's
        // random_dag_cycle_check_matches_brute_force.
        let seeds: &[u64] = &[0xC0FFEE, 0xDEADBEEF, 0x1234, 0xABCD, 0xF00BA1, 0x5EED];
        for &seed in seeds {
            let mut rng = SimpleRng::new(seed);
            let n = 10usize;
            let mut base = RollbackDiGraph::new();
            let mut edges: Vec<(usize, usize)> = Vec::new();
            for a in 0..n {
                for b in 0..n {
                    if a != b && rng.next_u32() % 5 == 0 {
                        base.increment_edge(a, b);
                        edges.push((a, b));
                    }
                }
            }
            let alias = TestAlias::default();
            let mut order = CondensationOrder::new();
            for trial in 0..30 {
                // Random overlay: remove up to 2 existing edges, add
                // up to 2 random pairs.
                let mut overlay: BTreeMap<(usize, usize), isize> = BTreeMap::new();
                for _ in 0..(rng.next_u32() % 3) {
                    if edges.is_empty() {
                        break;
                    }
                    let (a, b) = edges[(rng.next_u32() as usize) % edges.len()];
                    overlay.insert((a, b), -(base.edge_count(a, b) as isize));
                }
                for _ in 0..(rng.next_u32() % 3) {
                    let a = (rng.next_u32() as usize) % n;
                    let b = (rng.next_u32() as usize) % n;
                    if a != b {
                        *overlay.entry((a, b)).or_insert(0) += 1;
                    }
                }
                let u = (rng.next_u32() as usize) % n;
                let v = (rng.next_u32() as usize) % n;
                let got = order.would_join_multi_scc(&base, &overlay, u, v);
                let want = brute_would_join(&base, &alias, &overlay, u, v);
                assert_eq!(got, want, "seed={seed:x} trial={trial} pair=({u},{v})");
            }
        }
    }

    #[test]
    fn random_mutation_sequences_match_tarjan_partition() {
        // Random graphs driven through interleaved insert / remove /
        // contract / invalidate sequences: after every step, the
        // internal invariants must validate and the multi-SCC verdict
        // for every node must equal a fresh tarjan recompute.
        let seeds: &[u64] = &[0xC0FFEE, 0xDEADBEEF, 0x1234, 0xBADBEEF, 0xFEEDFACE];
        for &seed in seeds {
            let mut rng = SimpleRng::new(seed);
            let n = 9usize;
            let universe: BTreeSet<usize> = (0..n).collect();
            let mut base = RollbackDiGraph::new();
            let mut alias = TestAlias::default();
            let mut order = CondensationOrder::new();
            for step in 0..120 {
                let a = (rng.next_u32() as usize) % n;
                let b = (rng.next_u32() as usize) % n;
                match rng.next_u32() % 10 {
                    0..=4 => {
                        if a != b {
                            base.increment_edge(a, b);
                            order.insert_edge(&base, a, b);
                        }
                    }
                    5..=7 => {
                        if a != b && base.edge_count(a, b) > 0 {
                            base.decrement_edge(a, b);
                            order.remove_edge(&base, a, b);
                        }
                    }
                    8 => {
                        if a != b {
                            order.apply_contract(&base, a, b);
                            alias.union(a, b);
                        }
                    }
                    _ => order.invalidate(),
                }
                let context = format!("seed={seed:x} step={step}");
                assert_multi_matches_brute(&mut order, &base, &alias, &universe, &context);
                order
                    .validate(&base)
                    .unwrap_or_else(|e| panic!("{context}: {e}"));
                // Speculative query differential on top of the
                // mutated state: random pair + small random overlay.
                let mut overlay: BTreeMap<(usize, usize), isize> = BTreeMap::new();
                for _ in 0..(rng.next_u32() % 3) {
                    let x = (rng.next_u32() as usize) % n;
                    let y = (rng.next_u32() as usize) % n;
                    if x == y {
                        continue;
                    }
                    if base.edge_count(x, y) > 0 && rng.next_u32() % 2 == 0 {
                        overlay.insert((x, y), -(base.edge_count(x, y) as isize));
                    } else {
                        *overlay.entry((x, y)).or_insert(0) += 1;
                    }
                }
                let u = (rng.next_u32() as usize) % n;
                let v = (rng.next_u32() as usize) % n;
                assert_eq!(
                    order.would_join_multi_scc(&base, &overlay, u, v),
                    brute_would_join(&base, &alias, &overlay, u, v),
                    "{context}: would_join({u},{v}) overlay={overlay:?}",
                );
            }
        }
    }

    #[test]
    fn random_contraction_sequences_union_instead_of_degrading() {
        // Pure contraction runs over random DAGs — contractions may
        // freely close cycles (no gating), and the structure must keep
        // a valid condensation order throughout (a plain PK order over
        // the raw graph has no valid order once a cycle closes).
        let seeds: &[u64] = &[0xC0FFEE, 0xDEADBEEF, 0x1234];
        for &seed in seeds {
            let mut rng = SimpleRng::new(seed);
            let n = 12usize;
            let universe: BTreeSet<usize> = (0..n).collect();
            let mut base = RollbackDiGraph::new();
            for a in 0..n {
                for b in (a + 1)..n {
                    if rng.next_u32() % 3 == 0 {
                        base.increment_edge(a, b);
                    }
                }
            }
            let mut alias = TestAlias::default();
            let mut order = CondensationOrder::new();
            // Force the initial lazy rebuild; the property under test
            // is that contractions never *introduce* staleness.
            order.is_in_multi_scc(&base, 0);
            let mut alive: Vec<usize> = (0..n).collect();
            for step in 0..(n - 1) {
                let i = (rng.next_u32() as usize) % alive.len();
                let mut j = (rng.next_u32() as usize) % alive.len();
                if i == j {
                    j = (j + 1) % alive.len();
                }
                let (winner, loser) = (alive[i], alive[j]);
                order.apply_contract(&base, winner, loser);
                alias.union(winner, loser);
                alive.retain(|&x| x != loser);
                assert!(!order.is_stale(), "contractions never go stale");
                let context = format!("seed={seed:x} step={step}");
                order
                    .validate(&base)
                    .unwrap_or_else(|e| panic!("{context}: {e}"));
                assert_multi_matches_brute(&mut order, &base, &alias, &universe, &context);
            }
        }
    }
}
