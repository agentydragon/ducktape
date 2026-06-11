//! Incremental topological order for the `QuotientGraph`'s class
//! adjacency. Used by the per-pop cycle check in the lazy-PQ greedy
//! driver.
//!
//! ## Why an incremental topological order?
//!
//! Before this module, `merge_creates_new_constraining_cycle` did a
//! fresh DFS in the class graph per merge attempt, allocating new
//! sets every time. That's `O(|cone|)` per check, and the greedy
//! attempts `~M` merges per pop with `~M` pops total → `O(M² ·
//! |cone|)` in the worst case.
//!
//! The natural data structure for "is merging two nodes safe in a
//! DAG?" is an incremental topological order maintained across
//! mutations. We follow the Pearce-Kelly 2007 algorithm
//! ("A Dynamic Topological Sort Algorithm for Directed Acyclic
//! Graphs", ACM JEA 11): each node carries a `topo_ord` integer; on
//! edge insertion `u → v` with `ord[u] < ord[v]` the order is already
//! valid (`O(1)`); otherwise we walk forward from `v` collecting
//! `Δ_f = {w : reachable from v, ord[w] < ord[u]}` and backward from
//! `u` collecting `Δ_b = {w : reaches u, ord[w] > ord[v]}`. If `Δ_f`
//! hits `u`, the insertion would create a cycle. Otherwise we reorder
//! only `Δ_f ∪ Δ_b` by interleaving their topological orders. Work is
//! `O(|Δ_f| + |Δ_b|)` — bounded by the **affected region**, not by
//! the whole reachable cone.
//!
//! ## Adapting PK to node contraction
//!
//! Our mutation is `contract(c1, c2)`: every edge incident on `c2`
//! gets relabeled to be incident on `c1`. Equivalently, every
//! `(c2, x)` edge becomes a `(c1, x)` edge insertion (and every
//! `(x, c2)` becomes `(x, c1)`).
//!
//! Crucially, the safety check ("would contracting create a new
//! cycle?") reduces to ordinary node-pair reachability in the
//! pre-merge DAG:
//!
//! > A new cycle through the merged class exists iff `c1 ⇝ c2`
//! > **or** `c2 ⇝ c1` in the pre-merge DAG.
//!
//! Proof sketch: any new cycle must visit the merged node and return
//! to it. Walking the cycle, the exit edge belonged to either `c1`
//! or `c2` pre-merge; same for the re-entry edge. If both belonged
//! to the same side, the cycle already existed pre-merge (and is not
//! "new"). So a NEW cycle requires exiting from one side and
//! re-entering via the other — i.e. a path from one to the other.
//!
//! In a DAG with topological order, `lo ⇝ hi` is possible only if
//! `ord[lo] < ord[hi]`; the reverse direction is structurally
//! impossible. So we only check one direction, and the forward
//! search is bounded by the window `ord ≤ ord[hi]` (PK's
//! `dfs_forward`).
//!
//! Once the merge is confirmed safe and committed, we run a
//! single PK-style reorder over the affected window
//! `[ord[lo], ord[hi]]`. The winner survives with the lower order,
//! and predecessors of `loser` that fall in the window are shifted
//! to come before the survivor. This is the contraction-specific
//! specialization of PK's "interleave Δ_f and Δ_b" step.
//!
//! ## Cost
//!
//! - `init_from_dag`: `O(|V| + |E|)` (Kahn).
//! - `would_create_cycle(a, b)`: `O(|Δ|)` where `Δ` is the set of
//!   classes with `ord` in `(ord[lo], ord[hi]]` that are reachable
//!   from `lo`. Strictly bounded by `|{v : ord[v] ≤ ord[hi]}|`,
//!   typically much smaller than the cone.
//! - `apply_contract(winner, loser)`: `O(|Δ_b|)` where `Δ_b` is the
//!   set of pre-loser predecessors with `ord ∈ [ord[winner],
//!   ord[loser])`.
//!
//! All costs are bounded by the affected region, not the whole
//! reachable cone — that's the win.
//!
//! ## Adjacency shape
//!
//! After the EdgeState refactor (commit `6effc3356`) the underlying
//! adjacency is a vector of `FxHashMap<ClassId, EdgeState>` keyed by
//! source class, plus a parallel `FxHashSet<ClassId>` per class for
//! back-pointers. PK consumes the **constraining** subgraph only
//! (edges with `EdgeState::constraining_count > 0`); the helpers
//! below filter on that.

use std::collections::BTreeMap;
use std::collections::BTreeSet;

use rustc_hash::{FxHashMap, FxHashSet};

use super::quotient::{ClassId, EdgeState};

/// Sentinel for a dead (emptied) class. Live classes always carry a
/// value in `0..self.next_rank`.
const DEAD: u32 = u32::MAX;

/// Iterate the constraining out-neighbors of `c` from the given
/// `out_edges` adjacency. Filters to edges with `constraining_count > 0`
/// — PK is a constraining-only cycle gate.
fn constraining_outs<'a>(
    out_edges: &'a [FxHashMap<ClassId, EdgeState>],
    c: ClassId,
) -> impl Iterator<Item = ClassId> + 'a {
    out_edges[c.0]
        .iter()
        .filter(|(_, e)| e.constraining_count > 0)
        .map(|(&n, _)| n)
}

/// Iterate the constraining in-neighbors of `c`. The back-pointer
/// index `in_neighbors[c.0]` carries every predecessor (weighted or
/// constraining); filter on the source's out-edge state to keep only
/// the constraining ones.
fn constraining_ins<'a>(
    out_edges: &'a [FxHashMap<ClassId, EdgeState>],
    in_neighbors: &'a [FxHashSet<ClassId>],
    c: ClassId,
) -> impl Iterator<Item = ClassId> + 'a {
    in_neighbors[c.0].iter().copied().filter(move |&s| {
        out_edges[s.0]
            .get(&c)
            .is_some_and(|e| e.constraining_count > 0)
    })
}

/// Incremental topological order maintained alongside the
/// constraining-edge class adjacency.
///
/// The order is consulted on every cycle check (`would_create_cycle`)
/// and maintained after every commit (`apply_contract`). Construction
/// uses Kahn's algorithm; updates use the Pearce-Kelly contraction
/// adaptation described above.
///
/// When the underlying class graph contains cycles, the topological
/// order is **not** a valid ordering (no such ordering exists). In
/// that case, the `is_dag` flag is set to `false` and
/// `would_create_cycle` rejects the fast-path and signals the caller
/// to use the slow fallback. Once a sequence of merges restores the
/// DAG property (no more `cached_cycles`), the caller can call
/// `init_from_dag` again to re-establish a valid order.
#[derive(Debug, Clone)]
pub(super) struct TopoOrder {
    /// `ord[c.0]` is the topological rank of class `c`. Lower ranks
    /// come earlier in the topological order. Dead classes carry
    /// `DEAD = u32::MAX`.
    ord: Vec<u32>,
    /// Inverse of `ord` over live classes. `pos_to_class[rank as
    /// usize] == Some(c)` iff `ord[c.0] == rank` (i.e. class `c` is
    /// live and sits at topological position `rank`). Positions that
    /// belong to dead classes carry `None`. The vector's length
    /// equals `next_rank` after a `Kahn` pass — i.e. the number of
    /// ranks ever assigned. After `apply_contract` the length is
    /// preserved (we leave a `None` hole where the loser was, and
    /// overwrite the surviving slots in place during the window
    /// re-rank).
    ///
    /// Maintained in lockstep with `ord`; the invariant is enforced
    /// in `validate()`.
    pos_to_class: Vec<Option<ClassId>>,
    /// `true` iff `ord` is a valid topological order over the live
    /// classes. Set by `init_from_dag` based on whether Kahn's
    /// visited every live node. Reset to `false` by
    /// `mark_potentially_cyclic` (defensive — called by partition-
    /// driven mutations that bypass `apply_contract`). Cleared back
    /// to `true` by a successful re-init.
    is_dag: bool,
    /// Per-DFS visited marker. `visited_epoch[c.0] == current_epoch`
    /// iff `c` was reached in the current `would_create_cycle` call.
    /// Bump `current_epoch` on each call instead of clearing this
    /// vector — that turns the per-call visited-set allocation into
    /// a single u32 bump.
    ///
    /// Sized to `ord.len()` (the `ClassId` index space) at
    /// `init_from_dag`; never grows after that (the index space is
    /// fixed by the kernel construction).
    visited_epoch: Vec<u32>,
    /// Monotonically increasing epoch counter for `visited_epoch`.
    /// Bumped at the top of every `would_create_cycle`. When it would
    /// overflow `u32::MAX`, the vector is zeroed and the counter is
    /// reset to 1 (a class with `visited_epoch == 0` and
    /// `current_epoch == 1` is unvisited, preserving the invariant).
    current_epoch: u32,
}

impl TopoOrder {
    /// Construct an empty TopoOrder. `init_from_dag` must be called
    /// before any cycle-check or contract call.
    pub(super) fn empty() -> Self {
        Self {
            ord: Vec::new(),
            pos_to_class: Vec::new(),
            is_dag: false,
            visited_epoch: Vec::new(),
            current_epoch: 0,
        }
    }

    /// Whether the maintained order is a valid topological order
    /// (i.e. the class graph is a DAG). When false, callers must
    /// fall back to the slow cone-DFS for cycle checks.
    pub(super) fn is_dag(&self) -> bool {
        self.is_dag
    }

    /// Rebuild the topological order from scratch via Kahn's algorithm.
    ///
    /// `num_classes` is the size of the `ClassId` index space (i.e.
    /// `classes.len()` in the parent quotient). `live` enumerates the
    /// `ClassId`s that should receive an order; classes outside this
    /// set get `DEAD`. The adjacency is consumed from `out_edges` /
    /// `in_neighbors`, filtered to constraining edges.
    ///
    /// The class graph MUST be a DAG. If a cycle exists, the
    /// algorithm marks `is_dag = false` (callers fall back to the
    /// slow cone-DFS) — this can happen when seeding produces a
    /// non-realizable partition.
    pub(super) fn init_from_dag(
        &mut self,
        num_classes: usize,
        live: impl Iterator<Item = ClassId>,
        out_edges: &[FxHashMap<ClassId, EdgeState>],
        in_neighbors: &[FxHashSet<ClassId>],
    ) {
        self.ord = vec![DEAD; num_classes];
        self.pos_to_class = Vec::new();
        // Resize the epoch buffer to match the ClassId index space
        // and zero it. Resetting the epoch to 0 means the next
        // would_create_cycle call bumps to 1 and any class that still
        // carries 0 reads as unvisited, which is what we want.
        self.visited_epoch.clear();
        self.visited_epoch.resize(num_classes, 0);
        self.current_epoch = 0;
        // Kahn's: start with nodes that have in-degree 0 (among live
        // classes), pop in any order, decrement successors' in-degree.
        let live: Vec<ClassId> = live.collect();
        self.pos_to_class.reserve(live.len());
        let mut indegree: BTreeMap<ClassId, usize> = BTreeMap::new();
        for &c in &live {
            let deg = constraining_ins(out_edges, in_neighbors, c)
                .filter(|n| *n != c)
                .count();
            indegree.insert(c, deg);
        }
        let mut queue: Vec<ClassId> = live
            .iter()
            .copied()
            .filter(|c| indegree.get(c).copied().unwrap_or(0) == 0)
            .collect();
        // Deterministic order within the same in-degree level: sort
        // by ClassId. The choice doesn't affect correctness — any
        // valid topo order is fine — but determinism eases debugging.
        queue.sort();
        let mut rank: u32 = 0;
        let mut visited = 0usize;
        while let Some(c) = queue.pop() {
            self.ord[c.0] = rank;
            self.pos_to_class.push(Some(c));
            debug_assert_eq!(self.pos_to_class.len(), (rank as usize) + 1);
            rank = rank.checked_add(1).expect("topo rank exhausted u32::MAX");
            visited += 1;
            let mut new_zero: Vec<ClassId> = Vec::new();
            for n in constraining_outs(out_edges, c) {
                if n == c {
                    continue; // defensive: self-loops are excluded
                }
                let entry = indegree.get_mut(&n);
                if let Some(deg) = entry {
                    if *deg == 0 {
                        // Already enqueued; skip.
                        continue;
                    }
                    *deg -= 1;
                    if *deg == 0 {
                        new_zero.push(n);
                    }
                }
            }
            new_zero.sort();
            // Push in reverse so smaller IDs pop first (stable
            // determinism: queue is a Vec used as a stack).
            for n in new_zero.into_iter().rev() {
                queue.push(n);
            }
        }
        if visited == live.len() {
            self.is_dag = true;
        } else {
            // The class graph contains cycles — no valid topological
            // order exists. Assign arbitrary ranks to the unvisited
            // (cycle-bound) classes so `ord` is still a well-formed
            // function, but mark `is_dag = false` so callers fall
            // back to the slow cone-DFS.
            self.is_dag = false;
            for c in live {
                if self.ord[c.0] == DEAD {
                    self.ord[c.0] = rank;
                    self.pos_to_class.push(Some(c));
                    debug_assert_eq!(self.pos_to_class.len(), (rank as usize) + 1);
                    rank = rank.checked_add(1).expect("topo rank exhausted u32::MAX");
                }
            }
        }
    }

    /// PK-style cycle check: would contracting `c1` and `c2` create a
    /// NEW multi-class cycle through the merged class?
    ///
    /// A merge introduces a new multi-class cycle iff there is a
    /// directed path from one of `{c1, c2}` to the other passing
    /// through at least one **intermediate** class. A direct edge
    /// `c1 → c2` (or `c2 → c1`) just becomes a self-loop after the
    /// merge and is NOT considered a new cycle — matching the
    /// behavior of the original `merge_creates_new_constraining_cycle`
    /// (which dropped self-loops in its frontier).
    ///
    /// In a DAG only one direction is possible — the one going from
    /// the lower `ord` to the higher `ord`. We do a forward DFS from
    /// the **out-neighborhood** of `lo` (excluding `hi` itself, to
    /// skip the direct edge), pruned to nodes with `ord ≤ ord[hi]`.
    /// If `hi` is reached, the merge would create a cycle.
    pub(super) fn would_create_cycle(
        &mut self,
        c1: ClassId,
        c2: ClassId,
        out_edges: &[FxHashMap<ClassId, EdgeState>],
    ) -> bool {
        if c1 == c2 {
            return false;
        }
        let o1 = self.ord_of(c1);
        let o2 = self.ord_of(c2);
        if o1 == DEAD || o2 == DEAD {
            return false;
        }
        let (lo, hi, hi_ord) = if o1 < o2 { (c1, c2, o2) } else { (c2, c1, o1) };
        // Bump the epoch. The visited check is "is
        // visited_epoch[c.0] == current_epoch?". On wraparound, zero
        // the buffer and reset to 1 so the invariant ("0 means
        // unvisited") still holds.
        if self.current_epoch == u32::MAX {
            for slot in &mut self.visited_epoch {
                *slot = 0;
            }
            self.current_epoch = 1;
        } else {
            self.current_epoch += 1;
        }
        let epoch = self.current_epoch;
        // Seed the DFS with `lo`'s out-neighbors, EXCLUDING `hi`
        // itself. A direct edge `lo → hi` is just a self-loop after
        // merge, not a new cycle.
        let mut stack: Vec<ClassId> = Vec::new();
        for n in constraining_outs(out_edges, lo) {
            if n == lo || n == hi {
                continue;
            }
            let on = self.ord_of(n);
            if on <= hi_ord && self.visited_epoch[n.0] != epoch {
                self.visited_epoch[n.0] = epoch;
                stack.push(n);
            }
        }
        // From here on, encountering `hi` means a path of length ≥ 2
        // — a genuine new cycle through the merged class.
        while let Some(c) = stack.pop() {
            for n in constraining_outs(out_edges, c) {
                if n == c {
                    continue;
                }
                if n == hi {
                    return true;
                }
                let on = self.ord_of(n);
                if on <= hi_ord && self.visited_epoch[n.0] != epoch {
                    self.visited_epoch[n.0] = epoch;
                    stack.push(n);
                }
            }
        }
        false
    }

    /// Update the topological order after committing a contraction.
    /// The merge has just happened in the parent kernel: `loser` has
    /// been emptied and its incident edges relabeled to `winner` in
    /// the class adjacency. The caller MUST have verified the merge
    /// is safe via `would_create_cycle` first.
    ///
    /// `out_edges` / `in_neighbors` reflect the **post-merge** class
    /// adjacency (with loser already removed). The function reorders
    /// the affected window `[survivor_ord, other_ord]` so the topo
    /// invariant is restored.
    ///
    /// Strategy: collect all live classes whose current `ord` is in
    /// `[survivor_ord, other_ord]` (which includes `winner` itself
    /// if `ow < ol`, or excludes it if `ow > ol`, in which case
    /// we add it explicitly). Run Kahn's on the induced subgraph,
    /// and assign them consecutive positions starting at
    /// `survivor_ord`.
    ///
    /// Cost is `O(|W| + |E_W|)` where `W` is the window classes and
    /// `E_W` is the edges among them — bounded by the affected
    /// region, not the global graph.
    pub(super) fn apply_contract(
        &mut self,
        winner: ClassId,
        loser: ClassId,
        out_edges: &[FxHashMap<ClassId, EdgeState>],
        in_neighbors: &[FxHashSet<ClassId>],
    ) {
        if winner == loser {
            return;
        }
        if !self.is_dag {
            // No valid order to maintain. Just mark loser dead so
            // it doesn't accidentally come back via ord lookups.
            // The caller decides when to re-init (e.g. once
            // `cached_cycles` drains and the graph becomes a DAG).
            if loser.0 < self.ord.len() {
                self.ord[loser.0] = DEAD;
            }
            return;
        }
        let ow = self.ord_of(winner);
        let ol = self.ord_of(loser);
        if ow == DEAD || ol == DEAD {
            // One side wasn't tracked — fall back to leaving the
            // order untouched. The caller should re-init if it cares.
            self.ord[loser.0] = DEAD;
            // Best-effort: drop loser from the reverse index if it's
            // still there (it should be when ol != DEAD; but we
            // handle both cases defensively).
            if ol != DEAD && (ol as usize) < self.pos_to_class.len() {
                self.pos_to_class[ol as usize] = None;
            }
            return;
        }
        let (lo_ord, hi_ord) = if ow < ol { (ow, ol) } else { (ol, ow) };
        // Mark loser dead immediately so it doesn't show up in window
        // scans.
        self.ord[loser.0] = DEAD;
        self.pos_to_class[ol as usize] = None;
        if lo_ord + 1 == hi_ord {
            // Empty interior. The two slots are loser (dead) and
            // winner (kept its lo_ord). If winner had hi_ord, move
            // it to lo_ord.
            if ow == hi_ord {
                self.ord[winner.0] = lo_ord;
                // hi_ord slot becomes empty; winner now occupies
                // lo_ord. (lo_ord slot held loser, already cleared if
                // ol == lo_ord; otherwise it held winner pre-move and
                // we now move winner to it.)
                self.pos_to_class[lo_ord as usize] = Some(winner);
                self.pos_to_class[hi_ord as usize] = None;
            }
            return;
        }
        // Collect window classes via the reverse index — O(|window|)
        // rather than O(num_classes). The slice
        // `pos_to_class[lo_ord..=hi_ord]` gives every class currently
        // at a rank in the window, in rank order; dead slots are
        // `None` and skipped.
        let lo_idx = lo_ord as usize;
        let hi_idx = hi_ord as usize;
        debug_assert!(hi_idx < self.pos_to_class.len());
        let mut window_classes: Vec<ClassId> = Vec::with_capacity(hi_idx - lo_idx + 1);
        for slot in &self.pos_to_class[lo_idx..=hi_idx] {
            if let Some(c) = *slot {
                window_classes.push(c);
            }
        }
        // Edge case: window_classes might or might not contain
        // winner. If `ow == hi_ord` and we just killed loser at
        // lo_ord, winner is at hi_ord ∈ window. If `ow == lo_ord`,
        // winner is at lo_ord ∈ window. Either way it's included.
        debug_assert!(window_classes.contains(&winner));

        // Run Kahn's on the induced subgraph. Edges considered are
        // those between window classes (both endpoints in the
        // window).
        let window_set: BTreeSet<ClassId> = window_classes.iter().copied().collect();
        let mut indegree: BTreeMap<ClassId, usize> = BTreeMap::new();
        for &c in &window_classes {
            let mut deg = 0usize;
            for x in constraining_ins(out_edges, in_neighbors, c) {
                if x != c && window_set.contains(&x) {
                    deg += 1;
                }
            }
            indegree.insert(c, deg);
        }
        let mut queue: Vec<ClassId> = window_classes
            .iter()
            .copied()
            .filter(|c| indegree.get(c).copied().unwrap_or(0) == 0)
            .collect();
        queue.sort();
        // Pop largest first so the next-popped (smallest after sort)
        // is processed first when used as a stack.
        queue.reverse();
        let mut new_ord = lo_ord;
        let mut visited = 0usize;
        // The window's pos_to_class slots will be overwritten in
        // order. Any slot we don't overwrite (because the window
        // shrunk by 1 — loser is dead) ends up holding the trailing
        // hi_ord with `None`; we clear that explicitly after the
        // loop.
        while let Some(c) = queue.pop() {
            self.ord[c.0] = new_ord;
            self.pos_to_class[new_ord as usize] = Some(c);
            new_ord += 1;
            visited += 1;
            let mut new_zero: Vec<ClassId> = Vec::new();
            for n in constraining_outs(out_edges, c) {
                if n == c || !window_set.contains(&n) {
                    continue;
                }
                let entry = indegree.get_mut(&n);
                if let Some(deg) = entry {
                    if *deg == 0 {
                        continue;
                    }
                    *deg -= 1;
                    if *deg == 0 {
                        new_zero.push(n);
                    }
                }
            }
            new_zero.sort();
            // Reverse so smaller ClassIds pop first.
            for n in new_zero.into_iter().rev() {
                queue.push(n);
            }
        }
        assert_eq!(
            visited,
            window_classes.len(),
            "apply_contract: window subgraph has a cycle (visited {} of {})",
            visited,
            window_classes.len()
        );
        // The window had `hi_ord - lo_ord + 1` slots; we filled the
        // first `window_classes.len()` (which equals `visited`),
        // leaving one trailing slot (the loser's rank vacated the
        // window) un-overwritten. Clear it so the reverse index
        // stays consistent.
        for slot_idx in (lo_idx + visited)..=hi_idx {
            self.pos_to_class[slot_idx] = None;
        }
    }

    /// Return `ord[c]`. Panics if `c` is out of bounds; returns
    /// `DEAD` for live-but-emptied classes.
    fn ord_of(&self, c: ClassId) -> u32 {
        self.ord[c.0]
    }

    /// Total live classes (for tests).
    #[cfg(test)]
    pub(super) fn live_count(&self) -> usize {
        self.ord.iter().filter(|&&o| o != DEAD).count()
    }

    /// Get the order rank of a class (for tests / debugging).
    #[cfg(test)]
    pub(super) fn rank_of(&self, c: ClassId) -> Option<u32> {
        let o = self.ord[c.0];
        if o == DEAD { None } else { Some(o) }
    }

    /// Validate that the stored order is a valid topological order
    /// over the given class adjacency. Returns `Ok(())` if every
    /// live constraining edge `a → b` has `ord[a] < ord[b]`. Used by
    /// tests + `debug_assertions` after `apply_contract`.
    ///
    /// When `is_dag` is false (the underlying graph contains
    /// cycles), validation is a no-op — no valid topological order
    /// exists, so the maintained `ord` is arbitrary and cannot be
    /// validated.
    #[cfg(any(test, debug_assertions))]
    pub(super) fn validate(
        &self,
        out_edges: &[FxHashMap<ClassId, EdgeState>],
    ) -> Result<(), String> {
        if !self.is_dag {
            return Ok(());
        }
        // Invariant: pos_to_class is the inverse of ord over live
        // classes.
        //   - For every live class `c`,
        //     `pos_to_class[ord[c.0] as usize] == Some(c)`.
        //   - For every live position `i` (i.e. `pos_to_class[i] ==
        //     Some(c)`), `ord[c.0] as usize == i`.
        // Dead classes carry `ord == DEAD` and must not appear in
        // `pos_to_class`; dead positions are `None`.
        for (idx, &o) in self.ord.iter().enumerate() {
            if o == DEAD {
                continue;
            }
            let pos = o as usize;
            if pos >= self.pos_to_class.len() {
                return Err(format!(
                    "TopoOrder: class {idx:?}@{o} but pos_to_class len {}",
                    self.pos_to_class.len()
                ));
            }
            match self.pos_to_class[pos] {
                Some(c) if c.0 == idx => {}
                Some(c) => {
                    return Err(format!(
                        "TopoOrder: ord[{idx}]={o} but pos_to_class[{pos}]={c:?}"
                    ));
                }
                None => {
                    return Err(format!(
                        "TopoOrder: ord[{idx}]={o} but pos_to_class[{pos}]=None"
                    ));
                }
            }
        }
        for (i, slot) in self.pos_to_class.iter().enumerate() {
            if let Some(c) = *slot {
                let o = self.ord[c.0];
                if o == DEAD {
                    return Err(format!(
                        "TopoOrder: pos_to_class[{i}]={c:?} but ord[{}] is DEAD",
                        c.0
                    ));
                }
                if (o as usize) != i {
                    return Err(format!(
                        "TopoOrder: pos_to_class[{i}]={c:?} but ord[{}]={o}",
                        c.0
                    ));
                }
            }
        }
        for (s_idx, outs) in out_edges.iter().enumerate() {
            let from = ClassId(s_idx);
            let of = self.ord_of(from);
            if of == DEAD {
                // Dead source: no constraining out-edges may remain.
                for (to, state) in outs {
                    if state.constraining_count > 0 {
                        return Err(format!(
                            "TopoOrder: dead class {from:?} has constraining out-edge to {to:?}"
                        ));
                    }
                }
                continue;
            }
            for (to, state) in outs {
                if state.constraining_count == 0 {
                    continue;
                }
                if *to == from {
                    continue;
                }
                let ot = self.ord_of(*to);
                if ot == DEAD {
                    return Err(format!(
                        "TopoOrder: live constraining edge {from:?} → {to:?} but {to:?} is dead"
                    ));
                }
                if of >= ot {
                    return Err(format!(
                        "TopoOrder: edge {from:?}@{of} → {to:?}@{ot} violates topo"
                    ));
                }
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cid(i: usize) -> ClassId {
        ClassId(i)
    }

    /// Build a simple adjacency from a list of edges. Every edge is
    /// stored as a constraining edge (constraining_count = 1,
    /// weighted_count = 1, weighted_sum = 0). Returns
    /// (out_edges, in_neighbors).
    fn adj(
        n: usize,
        edges: &[(usize, usize)],
    ) -> (Vec<FxHashMap<ClassId, EdgeState>>, Vec<FxHashSet<ClassId>>) {
        let mut out: Vec<FxHashMap<ClassId, EdgeState>> = vec![FxHashMap::default(); n];
        let mut in_: Vec<FxHashSet<ClassId>> = vec![FxHashSet::default(); n];
        for &(a, b) in edges {
            let entry = out[a].entry(cid(b)).or_default();
            entry.constraining_count += 1;
            entry.weighted_count += 1;
            in_[b].insert(cid(a));
        }
        (out, in_)
    }

    /// Remove the constraining edge (a, b) from the adjacency.
    fn remove_edge(
        out: &mut [FxHashMap<ClassId, EdgeState>],
        in_: &mut [FxHashSet<ClassId>],
        a: ClassId,
        b: ClassId,
    ) {
        out[a.0].remove(&b);
        in_[b.0].remove(&a);
    }

    /// Insert a constraining edge (a, b).
    fn add_edge(
        out: &mut [FxHashMap<ClassId, EdgeState>],
        in_: &mut [FxHashSet<ClassId>],
        a: ClassId,
        b: ClassId,
    ) {
        let entry = out[a.0].entry(b).or_default();
        entry.constraining_count += 1;
        entry.weighted_count += 1;
        in_[b.0].insert(a);
    }

    #[test]
    fn init_singleton_classes_no_edges() {
        let (out, in_) = adj(3, &[]);
        let mut t = TopoOrder::empty();
        t.init_from_dag(3, (0..3).map(cid), &out, &in_);
        assert_eq!(t.live_count(), 3);
        // All ranks distinct, all in 0..3.
        let mut ranks: Vec<u32> = (0..3).map(|i| t.rank_of(cid(i)).unwrap()).collect();
        ranks.sort();
        assert_eq!(ranks, vec![0, 1, 2]);
        t.validate(&out).expect("topo valid on empty graph");
    }

    #[test]
    fn init_chain_orders_by_dependency() {
        // 0 → 1 → 2 → 3
        let (out, in_) = adj(4, &[(0, 1), (1, 2), (2, 3)]);
        let mut t = TopoOrder::empty();
        t.init_from_dag(4, (0..4).map(cid), &out, &in_);
        let r = |i| t.rank_of(cid(i)).unwrap();
        assert!(r(0) < r(1));
        assert!(r(1) < r(2));
        assert!(r(2) < r(3));
        t.validate(&out).expect("topo valid on chain");
    }

    #[test]
    fn would_create_cycle_detects_direct_reachability() {
        // 0 → 1 → 2; merging 0 and 2 should be unsafe (cycle through
        // the merged class via the 0→1→2 path).
        let (out, in_) = adj(3, &[(0, 1), (1, 2)]);
        let mut t = TopoOrder::empty();
        t.init_from_dag(3, (0..3).map(cid), &out, &in_);
        assert!(t.would_create_cycle(cid(0), cid(2), &out));
        assert!(t.would_create_cycle(cid(2), cid(0), &out));
        // 0 and 1 are directly connected — merging them yields a
        // direct edge from merged to merged, but that's a self-loop
        // which the caller filters. Should still return false here
        // (no multi-class cycle).
        // Actually our check is for "is there a path through other
        // classes". A direct 0→1 edge merged is a self-loop; not a
        // "new cycle". Verify:
        assert!(!t.would_create_cycle(cid(0), cid(1), &out));
        assert!(!t.would_create_cycle(cid(1), cid(2), &out));
    }

    #[test]
    fn would_create_cycle_false_on_unrelated_classes() {
        // Two disconnected chains: 0 → 1, 2 → 3.
        let (out, in_) = adj(4, &[(0, 1), (2, 3)]);
        let mut t = TopoOrder::empty();
        t.init_from_dag(4, (0..4).map(cid), &out, &in_);
        assert!(!t.would_create_cycle(cid(0), cid(2), &out));
        assert!(!t.would_create_cycle(cid(0), cid(3), &out));
        assert!(!t.would_create_cycle(cid(1), cid(3), &out));
    }

    #[test]
    fn would_create_cycle_diamond() {
        // 0 → 1, 0 → 2, 1 → 3, 2 → 3
        let (out, in_) = adj(4, &[(0, 1), (0, 2), (1, 3), (2, 3)]);
        let mut t = TopoOrder::empty();
        t.init_from_dag(4, (0..4).map(cid), &out, &in_);
        // 0 reaches 3, so merging 0 and 3 unsafe.
        assert!(t.would_create_cycle(cid(0), cid(3), &out));
        // 1 doesn't reach 2 and 2 doesn't reach 1 — safe.
        assert!(!t.would_create_cycle(cid(1), cid(2), &out));
        // 1 and 3: 1 → 3 direct, no multi-class path. Should be safe.
        assert!(!t.would_create_cycle(cid(1), cid(3), &out));
        // 0 reaches 1 directly; safe (direct → self-loop).
        assert!(!t.would_create_cycle(cid(0), cid(1), &out));
    }

    #[test]
    fn apply_contract_simple_chain() {
        // 0 → 1 → 2 → 3. Merge 1 and 2 (safe — directly connected,
        // no multi-class cycle). Post-merge: 0 → merged → 3.
        let (mut out, mut in_) = adj(4, &[(0, 1), (1, 2), (2, 3)]);
        let mut t = TopoOrder::empty();
        t.init_from_dag(4, (0..4).map(cid), &out, &in_);
        // Simulate the kernel's adjacency update: merge 1 (winner)
        // and 2 (loser). Edges (1,2) and (2,3) become (1,3); (0,1)
        // unchanged.
        remove_edge(&mut out, &mut in_, cid(1), cid(2));
        remove_edge(&mut out, &mut in_, cid(2), cid(3));
        add_edge(&mut out, &mut in_, cid(1), cid(3));
        t.apply_contract(cid(1), cid(2), &out, &in_);
        t.validate(&out).expect("topo valid post-contract");
        // The merged class (winner=1) should come between 0 and 3.
        let r = |i| t.rank_of(cid(i)).unwrap();
        assert!(r(0) < r(1));
        assert!(r(1) < r(3));
        assert!(t.rank_of(cid(2)).is_none(), "loser is dead");
    }

    #[test]
    fn apply_contract_winner_has_larger_order_with_predecessor_in_window() {
        // 0 → 1, 0 → 3, 2 → 3 (no edge between 1 and 2 etc).
        // Topo init: any of [0,1,2,3] orderings with 0 first and 3
        // last. The Kahn's deterministic tie-break sorts by ClassId:
        // expected ord = [0:0, 1:1, 2:2, 3:3] (because we drain
        // smallest first when in-degree hits 0).
        let (mut out, mut in_) = adj(4, &[(0, 1), (0, 3), (2, 3)]);
        let mut t = TopoOrder::empty();
        t.init_from_dag(4, (0..4).map(cid), &out, &in_);
        let r0 = t.rank_of(cid(0)).unwrap();
        let r1 = t.rank_of(cid(1)).unwrap();
        let r2 = t.rank_of(cid(2)).unwrap();
        let r3 = t.rank_of(cid(3)).unwrap();
        // Sanity: valid topo.
        t.validate(&out).expect("init valid");
        assert!(r0 < r1);
        assert!(r0 < r3);
        assert!(r2 < r3);

        // Merge 1 (winner) and 3 (loser). Both directions safe:
        // 1 doesn't reach 3, 3 doesn't reach 1.
        assert!(!t.would_create_cycle(cid(1), cid(3), &out));
        // Simulate adjacency: edges (0,3) and (2,3) become (0,1) and
        // (2,1).
        remove_edge(&mut out, &mut in_, cid(0), cid(3));
        remove_edge(&mut out, &mut in_, cid(2), cid(3));
        add_edge(&mut out, &mut in_, cid(0), cid(1));
        add_edge(&mut out, &mut in_, cid(2), cid(1));
        t.apply_contract(cid(1), cid(3), &out, &in_);
        t.validate(&out).expect("topo valid post-merge");
        let r0_b = t.rank_of(cid(0)).unwrap();
        let r1_b = t.rank_of(cid(1)).unwrap();
        let r2_b = t.rank_of(cid(2)).unwrap();
        assert!(t.rank_of(cid(3)).is_none());
        assert!(r0_b < r1_b);
        assert!(r2_b < r1_b);
    }

    #[test]
    fn apply_contract_transitive_predecessor_chain() {
        // 5 classes with edges: 0 → 1 → 2 → 3 → 4. Plus a stray
        // class 5 — no, let's use just 5 nodes 0..4.
        // After Kahn init: ord = [0:0, 1:1, 2:2, 3:3, 4:4].
        // Now suppose we want to merge 0 (winner) and 4 (loser).
        // would_create_cycle? Yes (0 → ... → 4). So unsafe; can't
        // test that.
        //
        // Use a different shape: 5 classes with edges 1 → 2 → 3, no
        // edges to or from 0 or 4. Merge 0 and 4. would_create_cycle:
        // No (0 doesn't reach 4 and vice versa). Apply contract:
        // winner=0 (smaller ClassId), loser=4. Post-merge: 1 → 2 →
        // 3 unchanged. Winner survives at ord 0; loser dead.
        let (out, in_) = adj(5, &[(1, 2), (2, 3)]);
        let mut t = TopoOrder::empty();
        t.init_from_dag(5, (0..5).map(cid), &out, &in_);
        t.validate(&out).expect("init valid");

        assert!(!t.would_create_cycle(cid(0), cid(4), &out));
        // No edge changes since 0 and 4 are isolated.
        t.apply_contract(cid(0), cid(4), &out, &in_);
        t.validate(&out).expect("topo valid");
        assert!(t.rank_of(cid(4)).is_none());

        // Now test transitive shift: build 4 classes 0..3 with
        // edges 1 → 2 → 3 (so ord = [0,1,2,3]). Merge 0 (winner)
        // and 3 (loser). 0 doesn't reach 3, 3 doesn't reach 0. Safe.
        // Post-merge: edges 1 → 2 → 0 (formerly → 3). Topo must
        // satisfy 1 < 2 < 0. So 0 (winner) needs to move from ord 0
        // to ord 3.
        let (mut out2, mut in2) = adj(4, &[(1, 2), (2, 3)]);
        let mut t2 = TopoOrder::empty();
        t2.init_from_dag(4, (0..4).map(cid), &out2, &in2);
        t2.validate(&out2).expect("init valid");
        assert!(!t2.would_create_cycle(cid(0), cid(3), &out2));
        // Relabel (2, 3) to (2, 0).
        remove_edge(&mut out2, &mut in2, cid(2), cid(3));
        add_edge(&mut out2, &mut in2, cid(2), cid(0));
        t2.apply_contract(cid(0), cid(3), &out2, &in2);
        t2.validate(&out2).expect("topo valid post-merge");
        assert!(t2.rank_of(cid(3)).is_none());
        let r = |c| t2.rank_of(cid(c)).unwrap();
        // 1 → 2 → 0 means r(1) < r(2) < r(0).
        assert!(r(1) < r(2));
        assert!(r(2) < r(0));
    }

    #[test]
    fn random_dag_cycle_check_matches_brute_force() {
        // Property-style check: generate a few small random DAGs and
        // verify would_create_cycle == brute_force_reachable.
        let seeds: &[u64] = &[0xC0FFEE, 0xDEADBEEF, 0x1234, 0xABCD, 0xF00BA1];
        for &seed in seeds {
            let mut rng = SimpleRng::new(seed);
            let n = 12;
            // Construct edges respecting an arbitrary node order
            // (0..n) to guarantee DAG.
            let mut edges: Vec<(usize, usize)> = Vec::new();
            for i in 0..n {
                for j in (i + 1)..n {
                    if rng.next_u32() % 3 == 0 {
                        edges.push((i, j));
                    }
                }
            }
            let (out, in_) = adj(n, &edges);
            let mut t = TopoOrder::empty();
            t.init_from_dag(n, (0..n).map(cid), &out, &in_);
            t.validate(&out).expect("random DAG init valid");
            // For each pair, compare PK check to brute-force reach.
            for a in 0..n {
                for b in 0..n {
                    if a == b {
                        continue;
                    }
                    let pk = t.would_create_cycle(cid(a), cid(b), &out);
                    // Brute force: a "new cycle" through the merged
                    // class exists iff there is a directed path of
                    // length ≥ 2 in either direction. A direct edge
                    // becomes a self-loop and doesn't count.
                    let bf = reachable_via_intermediate(&out, cid(a), cid(b))
                        || reachable_via_intermediate(&out, cid(b), cid(a));
                    assert_eq!(pk, bf, "seed={seed:x} pair=({a},{b})");
                }
            }
        }
    }

    #[test]
    fn random_dag_contract_sequence_maintains_topo() {
        // Generate a random DAG, run a sequence of safe contractions,
        // and verify the order stays valid.
        let seeds: &[u64] = &[0xC0FFEE, 0xDEADBEEF, 0x1234];
        for &seed in seeds {
            let mut rng = SimpleRng::new(seed);
            let n = 10;
            let mut edges: Vec<(usize, usize)> = Vec::new();
            for i in 0..n {
                for j in (i + 1)..n {
                    if rng.next_u32() % 3 == 0 {
                        edges.push((i, j));
                    }
                }
            }
            let (mut out, mut in_) = adj(n, &edges);
            let mut t = TopoOrder::empty();
            t.init_from_dag(n, (0..n).map(cid), &out, &in_);
            t.validate(&out).expect("init valid");

            // Try up to 20 random merges; skip any that would cycle.
            let mut alive: BTreeSet<ClassId> = (0..n).map(cid).collect();
            for _ in 0..20 {
                if alive.len() < 2 {
                    break;
                }
                let alive_vec: Vec<ClassId> = alive.iter().copied().collect();
                let a_idx = (rng.next_u32() as usize) % alive_vec.len();
                let b_idx = (rng.next_u32() as usize) % alive_vec.len();
                if a_idx == b_idx {
                    continue;
                }
                let a = alive_vec[a_idx];
                let b = alive_vec[b_idx];
                if t.would_create_cycle(a, b, &out) {
                    continue;
                }
                // Choose winner = min(a,b), loser = max(a,b).
                let (winner, loser) = if a < b { (a, b) } else { (b, a) };
                // Relabel loser's edges to winner.
                let loser_outs: Vec<ClassId> = out[loser.0].keys().copied().collect();
                let loser_ins: Vec<ClassId> = in_[loser.0].iter().copied().collect();
                for x in &loser_outs {
                    remove_edge(&mut out, &mut in_, loser, *x);
                    if *x == winner {
                        continue;
                    }
                    add_edge(&mut out, &mut in_, winner, *x);
                }
                for x in &loser_ins {
                    remove_edge(&mut out, &mut in_, *x, loser);
                    if *x == winner {
                        continue;
                    }
                    add_edge(&mut out, &mut in_, *x, winner);
                }
                t.apply_contract(winner, loser, &out, &in_);
                t.validate(&out).expect("post-merge topo valid");
                alive.remove(&loser);
            }
        }
    }

    /// Assert the reverse-index invariant on a `TopoOrder`:
    ///
    /// - For every live class `c`,
    ///   `pos_to_class[ord[c.0] as usize] == Some(c)`.
    /// - For every live slot `i` (`pos_to_class[i] == Some(c)`),
    ///   `ord[c.0] as usize == i`.
    ///
    /// Dead classes (`ord == DEAD`) must not appear in
    /// `pos_to_class`; the corresponding positions are `None`.
    fn assert_pos_to_class_inverse(t: &TopoOrder, alive: &BTreeSet<ClassId>) {
        for &c in alive {
            let o = t.ord[c.0];
            assert_ne!(o, DEAD, "live class {c:?} has DEAD ord");
            let pos = o as usize;
            assert!(
                pos < t.pos_to_class.len(),
                "ord[{c:?}]={o} out of bounds (len={})",
                t.pos_to_class.len()
            );
            assert_eq!(
                t.pos_to_class[pos],
                Some(c),
                "pos_to_class[{pos}] != Some({c:?})"
            );
        }
        for (i, slot) in t.pos_to_class.iter().enumerate() {
            if let Some(c) = *slot {
                assert!(alive.contains(&c), "pos_to_class[{i}]={c:?} not alive");
                assert_eq!(
                    t.ord[c.0] as usize, i,
                    "pos_to_class[{i}]={c:?} but ord[{}]={}",
                    c.0, t.ord[c.0]
                );
            }
        }
        // Dead classes must not appear in the reverse index.
        for (idx, &o) in t.ord.iter().enumerate() {
            if o == DEAD {
                for slot in &t.pos_to_class {
                    assert_ne!(
                        *slot,
                        Some(ClassId(idx)),
                        "dead class {idx} appears in pos_to_class"
                    );
                }
            }
        }
    }

    #[test]
    fn pos_to_class_is_inverse_of_ord_after_many_contracts() {
        // Build random DAGs, run a long sequence of safe contractions,
        // and assert the reverse-index invariant holds throughout.
        let seeds: &[u64] = &[
            0xC0FFEE, 0xDEADBEEF, 0x1234, 0xABCD, 0xF00BA1, 0x5EED, 0xBADBEEF, 0xFEEDFACE,
        ];
        for &seed in seeds {
            let mut rng = SimpleRng::new(seed);
            let n = 14;
            let mut edges: Vec<(usize, usize)> = Vec::new();
            for i in 0..n {
                for j in (i + 1)..n {
                    if rng.next_u32() % 3 == 0 {
                        edges.push((i, j));
                    }
                }
            }
            let (mut out, mut in_) = adj(n, &edges);
            let mut t = TopoOrder::empty();
            t.init_from_dag(n, (0..n).map(cid), &out, &in_);
            t.validate(&out).expect("init valid");
            let mut alive: BTreeSet<ClassId> = (0..n).map(cid).collect();
            assert_pos_to_class_inverse(&t, &alive);

            // Run up to 30 random merge attempts; skip cycles.
            for _ in 0..30 {
                if alive.len() < 2 {
                    break;
                }
                let alive_vec: Vec<ClassId> = alive.iter().copied().collect();
                let a_idx = (rng.next_u32() as usize) % alive_vec.len();
                let b_idx = (rng.next_u32() as usize) % alive_vec.len();
                if a_idx == b_idx {
                    continue;
                }
                let a = alive_vec[a_idx];
                let b = alive_vec[b_idx];
                if t.would_create_cycle(a, b, &out) {
                    continue;
                }
                let (winner, loser) = if a < b { (a, b) } else { (b, a) };
                // Relabel loser's edges to winner (same as the
                // sibling random test).
                let loser_outs: Vec<ClassId> = out[loser.0].keys().copied().collect();
                let loser_ins: Vec<ClassId> = in_[loser.0].iter().copied().collect();
                for x in &loser_outs {
                    remove_edge(&mut out, &mut in_, loser, *x);
                    if *x == winner {
                        continue;
                    }
                    add_edge(&mut out, &mut in_, winner, *x);
                }
                for x in &loser_ins {
                    remove_edge(&mut out, &mut in_, *x, loser);
                    if *x == winner {
                        continue;
                    }
                    add_edge(&mut out, &mut in_, *x, winner);
                }
                t.apply_contract(winner, loser, &out, &in_);
                t.validate(&out).expect("post-merge topo valid");
                alive.remove(&loser);
                assert_pos_to_class_inverse(&t, &alive);
            }
        }
    }

    #[test]
    fn epoch_wraparound_resets_visited_buffer() {
        // Drive `would_create_cycle` across the u32::MAX boundary by
        // injecting `current_epoch = u32::MAX - 1` and calling twice.
        // The first call sets the epoch to u32::MAX; the second
        // triggers the wraparound branch (zero the buffer, reset to
        // 1).
        //
        // The wraparound branch is the only one that can produce
        // wrong answers if implemented incorrectly: without zeroing,
        // any class whose visited_epoch happens to equal 1 (the post-
        // wrap epoch) from a long-ago DFS would be erroneously pruned.
        // We pre-seed a stale "epoch 1" marker on an intermediate
        // class and verify the post-wrap call still walks through it.
        let (out, in_) = adj(5, &[(0, 1), (1, 2), (2, 3), (0, 4)]);
        let mut t = TopoOrder::empty();
        t.init_from_dag(5, (0..5).map(cid), &out, &in_);
        // Pre-seed: stale "epoch 1" marker on class 1 (an
        // intermediate on the 0→1→2→3 path). After wraparound the
        // buffer must be zeroed, otherwise the DFS for
        // would_create_cycle(0, 3) at post-wrap epoch=1 would
        // wrongly prune class 1 and miss the cycle.
        t.current_epoch = u32::MAX - 1;
        t.visited_epoch[cid(1).0] = 1;
        // First call (epoch bumps to u32::MAX): merge 0 and 3 is a
        // cycle via 0→1→2→3.
        assert!(t.would_create_cycle(cid(0), cid(3), &out));
        assert_eq!(t.current_epoch, u32::MAX);
        // Second call triggers wraparound. The stale "epoch 1"
        // marker on class 1 from the pre-seed must be cleared,
        // otherwise the answer would flip to false (incorrect).
        assert!(t.would_create_cycle(cid(0), cid(3), &out));
        assert_eq!(t.current_epoch, 1);
        // And the answer for a known-safe pair stays false (class 4
        // is isolated except for the direct 0→4 edge).
        assert!(!t.would_create_cycle(cid(1), cid(4), &out));
    }

    /// Reachable through at least one intermediate node (path length
    /// ≥ 2). A direct `a → b` edge does NOT count.
    fn reachable_via_intermediate(
        out: &[FxHashMap<ClassId, EdgeState>],
        a: ClassId,
        b: ClassId,
    ) -> bool {
        if a == b {
            return false;
        }
        // Start the search from out-neighbors of `a` other than `b`.
        let mut visited: BTreeSet<ClassId> = BTreeSet::new();
        let mut stack: Vec<ClassId> = Vec::new();
        for &n in out[a.0].keys() {
            if n == a || n == b {
                continue;
            }
            if visited.insert(n) {
                stack.push(n);
            }
        }
        while let Some(c) = stack.pop() {
            for &n in out[c.0].keys() {
                if n == b {
                    return true;
                }
                if visited.insert(n) {
                    stack.push(n);
                }
            }
        }
        false
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
}
