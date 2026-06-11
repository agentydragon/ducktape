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

use std::collections::{BTreeMap, BTreeSet};
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

use petgraph::algo::tarjan_scc;
use petgraph::graphmap::DiGraphMap;

use crate::rollback_graph::RollbackDiGraph;
use analysis::OwnerId;
use analysis::graph::{OwnerEdgeId, OwnerGraph, chunk_constraining_module_edges};
use analysis::ids::ModuleId;
use analysis::partition::Partition;

mod condensation_order;
#[cfg(test)]
mod condensation_order_proptest;
mod esm_simulator;
mod incremental_quotient;

pub use condensation_order::CondensationOrder;
use esm_simulator::EsmEvaluationSimulator;
pub use incremental_quotient::{DeltaHandle, LadderDecision, PartitionDelta};
use incremental_quotient::{IncrementalQuotient, JournalEntry, QuotientOverlay};

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

/// Touching-filtered form of [`check_realizability`]: the same pure
/// from-scratch verdict, restricted to diagnoses involving `module`.
///
/// This is the gate ladder's **reference predicate**
/// (`plans/incremental_gate_unification.md` §2): a speculative merge
/// with post-merge module `M` is acceptable iff
/// `check_realizability_touching(owner_graph, post_partition, M)`
/// `.is_realizable()`. Pre-existing violations not touching `M` are
/// intentionally ignored, matching
/// [`RealizabilityIndex::verdict_after_moving_owners_touching`]'s
/// semantics on both the hot and diagnostic paths.
///
/// Differential-harness / oracle use only — `O(N + M)` per call, far
/// too slow for the proposer's per-pop gate.
pub fn check_realizability_touching(
    owner_graph: &OwnerGraph,
    partition: &Partition,
    module: ModuleId,
) -> RealizabilityVerdict {
    let full = check_realizability(owner_graph, partition);
    RealizabilityVerdict {
        unrealizable_sccs: full
            .unrealizable_sccs
            .into_iter()
            .filter(|scc| scc.modules.contains(&module))
            .collect(),
        cross_rebinds: full
            .cross_rebinds
            .into_iter()
            .filter(|rebind| rebind.from == module || rebind.to == module)
            .collect(),
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
        let verdict = self.quotient.verdict_with_overlay_touching(to, &overlay);
        if gate_oracle_enabled() {
            // Oracle mode (plan §7.2): the boolean tier ladder must
            // agree with the evidence-producing verdict on every
            // diagnostic-path query.
            let decision = self.quotient.ladder_decide(to, &overlay);
            assert_eq!(
                decision.accepts(),
                verdict.is_realizable(),
                "DEBUNDLE_GATE_ORACLE: ladder {decision:?} diverges from the overlay \
                 verdict for {to:?}: {verdict:#?}",
            );
        }
        verdict
    }

    /// Tier-laddered decision for a hypothetical owner move, filtered
    /// to the target module (`plans/incremental_gate_unification.md`
    /// §3; PR 3 of §8). Exactly equal to
    /// `verdict_after_moving_owners_touching(..).is_realizable()` with
    /// evidence materialization elided: tiers 0–2 are short-circuits
    /// whose skip conditions are theorems about the predicate, tier 3
    /// runs the shared simulator path. With `DEBUNDLE_GATE_ORACLE`
    /// set, every query is additionally cross-checked against the
    /// pure touching-filtered reference and divergence panics.
    pub fn ladder_decision_after_moving_owners_touching(
        &self,
        owner_graph: &OwnerGraph,
        owners: &[OwnerId],
        to: ModuleId,
    ) -> LadderDecision {
        let overlay = self
            .quotient
            .overlay_for_move(owner_graph, &self.partition, owners, to);
        let decision = self.quotient.ladder_decide(to, &overlay);
        if gate_oracle_enabled() {
            let mut post_partition = self.partition.clone();
            for &owner in owners {
                post_partition.set(owner, to);
            }
            let reference = check_realizability_touching(owner_graph, &post_partition, to);
            assert_eq!(
                decision.accepts(),
                reference.is_realizable(),
                "DEBUNDLE_GATE_ORACLE: ladder {decision:?} diverges from the pure \
                 reference for {to:?}: {reference:#?}",
            );
        }
        decision
    }

    /// Boolean form of
    /// [`Self::ladder_decision_after_moving_owners_touching`] — the
    /// gate-ladder entry point the kernel's `check_merge_boolean`
    /// routes through (via `QuotientGraph::ladder_decision_for_merge`).
    pub fn would_remain_realizable_after_moving_owners_touching(
        &self,
        owner_graph: &OwnerGraph,
        owners: &[OwnerId],
        to: ModuleId,
    ) -> bool {
        self.ladder_decision_after_moving_owners_touching(owner_graph, owners, to)
            .accepts()
    }

    /// `O(α)` DSU probe against the tier-1 condensation order: are
    /// `a` and `b` in the same multi-module constraining SCC? Backs
    /// the greedy's cycle-reduction sort key ("this merge dissolves
    /// part of an unrealizable SCC") without a cache that can drift.
    pub fn modules_share_constraining_multi_scc(&self, a: ModuleId, b: ModuleId) -> bool {
        self.quotient.modules_share_constraining_multi_scc(a, b)
    }
}

/// One-time cached `DEBUNDLE_GATE_ORACLE` probe (plan §7.2): when set,
/// every ladder query cross-checks against the reference predicate and
/// panics on divergence. Off by default — the reference is
/// `O(V + E)` per query.
fn gate_oracle_enabled() -> bool {
    static ORACLE_ENABLED: OnceLock<bool> = OnceLock::new();
    *ORACLE_ENABLED.get_or_init(|| std::env::var_os("DEBUNDLE_GATE_ORACLE").is_some())
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

    // -- Gate-ladder per-tier counters (plan §3; PR 3 of §8) ----------------

    pub(super) static LADDER_TIER0_ACCEPT: AtomicUsize = AtomicUsize::new(0);
    pub(super) static LADDER_TIER0_REJECT: AtomicUsize = AtomicUsize::new(0);
    pub(super) static LADDER_TIER1_CYCLE_REJECT: AtomicUsize = AtomicUsize::new(0);
    pub(super) static LADDER_TIER1_CROSS_REBIND_REJECT: AtomicUsize = AtomicUsize::new(0);
    pub(super) static LADDER_TIER2_NO_MULTI_ISCC_ACCEPT: AtomicUsize = AtomicUsize::new(0);
    pub(super) static LADDER_TIER2_NO_PAIR_ACCEPT: AtomicUsize = AtomicUsize::new(0);
    pub(super) static LADDER_TIER3_ACCEPT: AtomicUsize = AtomicUsize::new(0);
    pub(super) static LADDER_TIER3_REJECT: AtomicUsize = AtomicUsize::new(0);
    /// Per-tier cumulative wall time in nanoseconds
    /// (`DEBUNDLE_TIMING=1` only; zero otherwise).
    pub(super) static LADDER_TIER1_NANOS: AtomicU64 = AtomicU64::new(0);
    pub(super) static LADDER_TIER2_NANOS: AtomicU64 = AtomicU64::new(0);
    pub(super) static LADDER_TIER3_NANOS: AtomicU64 = AtomicU64::new(0);

    pub(super) fn record_ladder_decision(
        decision: LadderDecision,
        tier1_nanos: Option<u64>,
        tier2_nanos: Option<u64>,
        tier3_nanos: Option<u64>,
    ) {
        let counter = match decision {
            LadderDecision::DeltaFreeAccept => &LADDER_TIER0_ACCEPT,
            LadderDecision::DeltaFreeReject => &LADDER_TIER0_REJECT,
            LadderDecision::ConstrainingCycleReject => &LADDER_TIER1_CYCLE_REJECT,
            LadderDecision::CrossRebindReject => &LADDER_TIER1_CROSS_REBIND_REJECT,
            LadderDecision::NoMultiModuleISccAccept => &LADDER_TIER2_NO_MULTI_ISCC_ACCEPT,
            LadderDecision::NoConstrainingPairAccept => &LADDER_TIER2_NO_PAIR_ACCEPT,
            LadderDecision::SimulatorAccept => &LADDER_TIER3_ACCEPT,
            LadderDecision::SimulatorReject => &LADDER_TIER3_REJECT,
        };
        counter.fetch_add(1, Ordering::Relaxed);
        if let Some(nanos) = tier1_nanos {
            LADDER_TIER1_NANOS.fetch_add(nanos, Ordering::Relaxed);
        }
        if let Some(nanos) = tier2_nanos {
            LADDER_TIER2_NANOS.fetch_add(nanos, Ordering::Relaxed);
        }
        if let Some(nanos) = tier3_nanos {
            LADDER_TIER3_NANOS.fetch_add(nanos, Ordering::Relaxed);
        }
    }

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

        let tier0_accept = LADDER_TIER0_ACCEPT.load(Ordering::Relaxed);
        let tier0_reject = LADDER_TIER0_REJECT.load(Ordering::Relaxed);
        let tier1_cycle = LADDER_TIER1_CYCLE_REJECT.load(Ordering::Relaxed);
        let tier1_rebind = LADDER_TIER1_CROSS_REBIND_REJECT.load(Ordering::Relaxed);
        let tier2_no_scc = LADDER_TIER2_NO_MULTI_ISCC_ACCEPT.load(Ordering::Relaxed);
        let tier2_no_pair = LADDER_TIER2_NO_PAIR_ACCEPT.load(Ordering::Relaxed);
        let tier3_accept = LADDER_TIER3_ACCEPT.load(Ordering::Relaxed);
        let tier3_reject = LADDER_TIER3_REJECT.load(Ordering::Relaxed);
        let ladder_total = tier0_accept
            + tier0_reject
            + tier1_cycle
            + tier1_rebind
            + tier2_no_scc
            + tier2_no_pair
            + tier3_accept
            + tier3_reject;
        let _ = writeln!(
            out,
            "gate ladder: {ladder_total} queries; tier0 {tier0_accept} accept / {tier0_reject} reject; \
             tier1 {tier1_cycle} cycle-reject + {tier1_rebind} rebind-reject; \
             tier2 {tier2_no_scc} no-multi-iscc + {tier2_no_pair} no-pair accept; \
             tier3 {tier3_accept} accept / {tier3_reject} reject"
        );
        let tier1_secs = LADDER_TIER1_NANOS.load(Ordering::Relaxed) as f64 / 1e9;
        let tier2_secs = LADDER_TIER2_NANOS.load(Ordering::Relaxed) as f64 / 1e9;
        let tier3_secs = LADDER_TIER3_NANOS.load(Ordering::Relaxed) as f64 / 1e9;
        let _ = writeln!(
            out,
            "  ladder wall: tier1 {tier1_secs:.3}s, tier2 {tier2_secs:.3}s, tier3 {tier3_secs:.3}s"
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
mod tests;
