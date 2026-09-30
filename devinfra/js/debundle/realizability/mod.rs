//! Single source of truth for the validity predicate
//! (docs/design.md "Valid peels and atomic modules"). The validator and any
//! hypothetical-move planner checks reach the verdict through this module —
//! see "Realizability primitive" in `docs/design.md`.
//!
//! Scope: clauses 2 (no cross-destination rebinding writes) and 3 (the
//! two-pass gate: Pass 1, no multi-module SCC in the constraining-edge
//! subgraph of the quotient; Pass 2, no TDZ proved by the ESM evaluation
//! simulator in any I-graph SCC that carries a constraining edge). Clause 1
//! (importability) is policy that lives in
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
//!   `verdict()` reads the current state. A non-mutating overlay query
//!   answers hypothetical owner moves for planner checks without pushing.
//!
//! The transactional API is backed by a rollbackable quotient index:
//! owner-graph edges are fixed, so `push`/`undo` only updates quotient
//! edge buckets incident to moved owners. Full verdicts run SCC over
//! the maintained quotient; candidate verdicts use localized
//! reachability around the hypothetical destination.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::OnceLock;

use petgraph::algo::tarjan_scc;
use petgraph::graphmap::DiGraphMap;

use analysis::OwnerId;
use analysis::graph::{OwnerEdgeId, OwnerGraph, chunk_constraining_module_edges};
use analysis::ids::ModuleId;
use analysis::partition::Partition;
use analysis::reports::SccCore;

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
/// [`RealizabilityVerdict`] violates clause 3 (Pass 1: multi-module SCC
/// in the constraining-edge subgraph of the quotient; Pass 2: a TDZ
/// proved by the ESM evaluation simulator).
///
/// This is the in-memory consumer of the shared [`SccCore`] shape
/// (typed `ModuleId`s + `OwnerEdgeId` evidence, no rendering) plus one
/// decoration — `rejection`. The exact edge set in `core` depends on
/// `rejection`:
///
/// - [`SccRejection::MutualConstrainingCycle`]: every constraining
///   cross-module owner edge whose endpoints both fall inside
///   `core.modules`.
/// - [`SccRejection::EsmEvaluationTdz`]: only the owner edges backing
///   the constraining `(from, to)` pairs whose simulated post-order
///   check failed — the surgical set whose removal (by co-locating the
///   binding pair) lifts the violation.
///
/// [`SccCore`] documents the rendered/wire projections of the same
/// shape ([`crate::validation::CycleReport`],
/// [`analysis::reports::schema::QuotientSccReport`]).
#[derive(Debug, Clone, Eq, PartialEq)]
pub struct SccDiagnosis {
    pub core: SccCore,
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
            for &m in &scc.core.modules {
                out.insert(m);
            }
        }
        out
    }
}

/// Pure-function form. Builds the canonical constraining edge set,
/// runs Tarjan, surfaces multi-module SCCs and cross-rebinds. The
/// correctness reference for the `RealizabilityIndex`'s incremental
/// backing.
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
            core: SccCore {
                modules,
                constraining_owner_edges: owner_edges,
            },
            rejection: SccRejection::MutualConstrainingCycle,
        });
    }

    // Pass 2: Tarjan over the full I-graph (canonical
    // `i_successors`, which includes lazy back-edges). Multi-module
    // I-SCCs not already in `reported` are the asymmetric
    // `(at-init forward, lazy back)` candidates: the constraining
    // subgraph alone is acyclic, but the lazy back-edge closes a
    // cycle in the runtime DFS topology. Lemma 2
    // (`chunk_source_import_order_from_adjacency`) reverses entry's
    // import order within each I-SCC so DFS lands on the dependent
    // first and unwinds through the dependency; the simulator below
    // checks whether that reversal actually rescues evaluation given
    // the spec's full import topology.
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
                core: SccCore {
                    modules,
                    constraining_owner_edges: owner_edges,
                },
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
/// for a speculative merge: a post-merge module `M` is acceptable iff
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
            .filter(|scc| scc.core.modules.contains(&module))
            .collect(),
        cross_rebinds: full
            .cross_rebinds
            .into_iter()
            .filter(|rebind| rebind.from == module || rebind.to == module)
            .collect(),
    }
}

/// Simulator-predicted ECMA-262 Phase-2 evaluation post-order for a
/// partition's emitted module tree: lower index = body evaluates
/// earlier; modules unreachable from residual are absent. Exposed for
/// Node-differential pins (`e2e/simulator_node_differential_sweep_test`)
/// that compare this prediction against the evaluation order Node
/// actually produces for the emitted tree.
pub fn simulated_evaluation_post_order(
    owner_graph: &OwnerGraph,
    partition: &Partition,
) -> BTreeMap<ModuleId, usize> {
    let canonical = chunk_constraining_module_edges(owner_graph, partition);
    let constraining_pairs: BTreeSet<(ModuleId, ModuleId)> = canonical.pairs().collect();
    EsmEvaluationSimulator::build(
        &canonical.i_successors,
        &constraining_pairs,
        partition.residual(),
    )
    .post_order
}

/// Mutable index over a working partition: the incremental form of the
/// predicate `check_realizability` computes (docs/design.md
/// "Realizability primitive").
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
    /// to the target module. Exactly equal to
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

#[cfg(test)]
mod tests;
