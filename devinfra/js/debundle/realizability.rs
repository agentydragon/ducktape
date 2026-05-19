//! Single source of truth for the three-clause validity predicate
//! (DESIGN.md "Valid peels and atomic modules"). The validator, the
//! peelability proposer, and the factorize closure all reach the
//! verdict through this module — see "Realizability primitive" in
//! `DESIGN.md`.
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
//!   `verdict()` reads the current state. The transactional API is
//!   the production shape: the proposer wraps each candidate in a
//!   scoped push/verdict/undo; factorize walks the index forward as
//!   the frontier grows and undoes on failed repair branches.
//!
//! Step 1a (this file) backs the transactional API by snapshotting
//! the partition on push and restoring it on undo, then recomputing
//! the verdict from scratch. Behaviour-correct, not yet fast. Step
//! 1b (separate change) replaces the backing with incremental
//! Pearce-Kelly SCC maintenance behind the same API.

use std::collections::{BTreeMap, BTreeSet};

use petgraph::algo::tarjan_scc;
use petgraph::graphmap::DiGraphMap;

use crate::OwnerId;
use crate::graph::{OwnerEdgeId, OwnerGraph};
use crate::ids::ModuleId;
use crate::partition::Partition;

/// Multi-module SCC of the constraining-edge subgraph of the
/// quotient. The presence of any such SCC violates clause 3.
#[derive(Debug, Clone)]
pub struct UnrealizableScc {
    /// Modules participating in the cycle.
    pub modules: BTreeSet<ModuleId>,
    /// Every constraining owner-edge whose endpoints both fall inside
    /// `modules` and that crosses module boundaries — i.e. the
    /// owner-level evidence the cycle is composed of. Stable order by
    /// `OwnerEdgeId`.
    pub constraining_owner_edges: Vec<OwnerEdgeId>,
}

/// Cross-destination rebinding write. ESM imports are read-only in the
/// importing module, so any such edge violates clause 2. One entry per
/// owner-edge.
#[derive(Debug, Clone)]
pub struct CrossRebindEdge {
    pub from: ModuleId,
    pub to: ModuleId,
    pub owner_edge: OwnerEdgeId,
}

/// Verdict on a (current or hypothetical) destination assignment.
/// Empty verdict ↔ realizable per clauses 2 and 3.
#[derive(Debug, Clone, Default)]
pub struct RealizabilityVerdict {
    pub unrealizable_sccs: Vec<UnrealizableScc>,
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

/// Pure-function form. Builds the constraining-edge quotient, runs
/// Tarjan, surfaces multi-module SCCs and cross-rebinds. The
/// correctness reference for the `RealizabilityIndex`'s incremental
/// backing (verified by differential test in the
/// `RealizabilityIndex` step 1b follow-up).
pub fn check_realizability(
    owner_graph: &OwnerGraph,
    partition: &Partition,
) -> RealizabilityVerdict {
    let mut verdict = RealizabilityVerdict::default();

    // Two parallel adjacency tables:
    //   - `constraining_adj`: only `EagerUse` + `Sequenced` (+
    //     `LocalEffect`) edges, deduped sequenced-per-pair. The
    //     evidence carrier — SCCs here are *the* clause-3 violation.
    //   - `i_adj`: every cross-module edge in the I-graph, including
    //     `LazyUse`. SCCs here catch the asymmetric-cycle shape
    //     `(at-init forward, lazy back)` whose constraining-only
    //     subgraph is acyclic but whose ESM evaluation still TDZs.
    //
    // Sequenced edges are deduped per (from, to) — multiple sequenced
    // reasons between the same module pair represent the same
    // ordering constraint and would over-weight evidence if counted
    // separately. (Matches `build_module_quotient`'s sequenced-edge
    // dedup.)
    let mut constraining_adj: BTreeMap<(ModuleId, ModuleId), Vec<OwnerEdgeId>> = BTreeMap::new();
    let mut i_adj: BTreeMap<(ModuleId, ModuleId), Vec<OwnerEdgeId>> = BTreeMap::new();
    let mut seen_sequenced_pairs: BTreeSet<(ModuleId, ModuleId)> = BTreeSet::new();

    for edge in &owner_graph.edges {
        // Skip edges whose endpoints aren't in this owner graph's
        // partition slot (defensive — Partition is dense, but
        // owner_graph.node() returning None should not crash here).
        if owner_graph.node(edge.from).is_none() || owner_graph.node(edge.to).is_none() {
            continue;
        }
        let from = partition.of(edge.from);
        let to = partition.of(edge.to);
        if from == to {
            continue;
        }
        if edge.reason.is_rebind() {
            verdict.cross_rebinds.push(CrossRebindEdge {
                from,
                to,
                owner_edge: edge.id,
            });
            continue;
        }
        // Every non-rebind cross-module edge participates in I.
        i_adj.entry((from, to)).or_default().push(edge.id);
        if !edge.reason.constrains_init_order() {
            continue;
        }
        if edge.reason.is_sequenced() && !seen_sequenced_pairs.insert((from, to)) {
            continue;
        }
        constraining_adj
            .entry((from, to))
            .or_default()
            .push(edge.id);
    }

    if i_adj.is_empty() {
        return verdict;
    }

    // Run Tarjan twice:
    //   1. Over the constraining-edge subgraph — the historical
    //      relaxed clause-3 rule. Catches **mutual** constraining
    //      cycles (both sides eager-read each other; no source order
    //      can satisfy both).
    //   2. Over the full I-graph (constraining + lazy), then filter
    //      to SCCs that **contain the residual module and a
    //      constraining edge whose target IS residual**. Catches the
    //      `(at-init forward, lazy back)` shape where one cycle
    //      member at-init reads from residual while residual lazily
    //      re-imports from the member. ESM's DFS starts at the
    //      chunk's runtime entry (= residual), so residual evaluates
    //      LAST in post-order — every other cycle member runs first
    //      and reads residual's class/const/let bindings in TDZ.
    //
    //      Asymmetric I-cycles where the constraining edge target
    //      is a non-residual module DO satisfy Lemma 2: the
    //      materializer's `source_import_position` puts the cycle
    //      dependent first in residual's import list, ESM DFS unwinds
    //      via the dependency, eval order respects the constraint.
    //      Those stay realizable.
    let residual = partition.residual();
    let mut con_graph: DiGraphMap<ModuleId, ()> = DiGraphMap::new();
    for &(from, to) in constraining_adj.keys() {
        con_graph.add_edge(from, to, ());
    }
    let mut reported: BTreeSet<BTreeSet<ModuleId>> = BTreeSet::new();
    for scc in tarjan_scc(&con_graph) {
        if scc.len() < 2 {
            continue;
        }
        let modules: BTreeSet<ModuleId> = scc.iter().copied().collect();
        let mut owner_edges: Vec<OwnerEdgeId> = Vec::new();
        for ((from, to), edges) in &constraining_adj {
            if modules.contains(from) && modules.contains(to) {
                owner_edges.extend_from_slice(edges);
            }
        }
        owner_edges.sort();
        reported.insert(modules.clone());
        verdict.unrealizable_sccs.push(UnrealizableScc {
            modules,
            constraining_owner_edges: owner_edges,
        });
    }

    let mut i_graph: DiGraphMap<ModuleId, ()> = DiGraphMap::new();
    for &(from, to) in i_adj.keys() {
        i_graph.add_edge(from, to, ());
    }
    for scc in tarjan_scc(&i_graph) {
        if scc.len() < 2 {
            continue;
        }
        let modules: BTreeSet<ModuleId> = scc.iter().copied().collect();
        if !modules.contains(&residual) {
            continue;
        }
        // Collect only the constraining edges into residual within
        // this SCC. If any exist, the cycle's TDZ shape is real.
        let mut owner_edges: Vec<OwnerEdgeId> = Vec::new();
        for ((from, to), edges) in &constraining_adj {
            if *to == residual && modules.contains(from) {
                owner_edges.extend_from_slice(edges);
            }
        }
        if owner_edges.is_empty() {
            continue;
        }
        if reported.contains(&modules) {
            continue;
        }
        owner_edges.sort();
        verdict.unrealizable_sccs.push(UnrealizableScc {
            modules,
            constraining_owner_edges: owner_edges,
        });
    }

    verdict
}

/// A reversible mutation of a `Partition`. The factorize closure and
/// the peelability proposer construct deltas to describe hypothetical
/// or actual destination assignments; the index applies and reverts
/// them.
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
}

/// Mutable index over a working partition. The single shared
/// implementation of the three-clause predicate, exposed in the
/// transactional shape DESIGN.md "Realizability primitive" prescribes.
///
/// Backing (step 1a): each `push` snapshots the prior assignments of
/// the touched owners; `undo` restores them. `verdict()` recomputes
/// from scratch via [`check_realizability`]. Correct but not yet
/// fast. Step 1b will swap the backing for incremental
/// Pearce-Kelly SCC maintenance behind the same API; no caller code
/// has to change.
pub struct RealizabilityIndex<'g> {
    owner_graph: &'g OwnerGraph,
    partition: Partition,
    journal: Vec<JournalEntry>,
}

impl<'g> RealizabilityIndex<'g> {
    pub fn from_partition(owner_graph: &'g OwnerGraph, partition: Partition) -> Self {
        Self {
            owner_graph,
            partition,
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
    /// LIFO-ordered without manual bookkeeping. Use raw `push`/`undo`
    /// only when the lifetime crosses control-flow boundaries that
    /// `scoped` can't span.
    pub fn push(&mut self, delta: PartitionDelta) -> DeltaHandle {
        let entry = match delta {
            PartitionDelta::MoveOwners { owners, to } => {
                let mut prior = Vec::with_capacity(owners.len());
                for owner in owners {
                    let was = self.partition.of(owner);
                    if was != to {
                        self.partition.set(owner, to);
                    }
                    prior.push((owner, was));
                }
                JournalEntry {
                    prior_assignments: prior,
                }
            }
        };
        let handle = DeltaHandle(self.journal.len());
        self.journal.push(entry);
        handle
    }

    /// Roll back the delta identified by `handle`. Must be the top of
    /// the journal; debug builds panic otherwise.
    pub fn undo(&mut self, handle: DeltaHandle) {
        debug_assert_eq!(
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
        for (owner, prior) in entry.prior_assignments {
            self.partition.set(owner, prior);
        }
    }

    /// Apply `delta`, run `f` against the index in its post-push
    /// state, then undo. The scoped form guarantees the per-call
    /// push/undo pair regardless of `f`'s control flow.
    pub fn scoped<F, R>(&mut self, delta: PartitionDelta, f: F) -> R
    where
        F: FnOnce(&mut Self) -> R,
    {
        let handle = self.push(delta);
        let result = f(self);
        self.undo(handle);
        result
    }

    /// Verdict against the current working partition. Step 1a backing
    /// recomputes from scratch; step 1b will read incrementally
    /// maintained state instead.
    pub fn verdict(&self) -> RealizabilityVerdict {
        check_realizability(self.owner_graph, &self.partition)
    }
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeSet;

    use super::*;
    use crate::OwnerId;
    use crate::facts::analyze_chunk;
    use crate::graph::build_owner_graph;
    use crate::ids::{LogicalModuleIndex, ModuleId};
    use crate::partition::Partition;
    use crate::{AnalysisHints, OwnerGraph};
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
        let handle = index.push(PartitionDelta::MoveOwners {
            owners: vec![OwnerId(1)],
            to: module_id(1),
        });
        // After push: matches the explicitly-built post-delta partition.
        let mut hypothetical = baseline.clone();
        hypothetical.set(OwnerId(1), module_id(1));
        let hypothetical_verdict = check_realizability(&owner_graph, &hypothetical);
        assert_eq!(
            index.verdict().unrealizable_sccs.len(),
            hypothetical_verdict.unrealizable_sccs.len(),
        );
        assert!(!index.verdict().is_realizable());

        index.undo(handle);
        // After undo: matches the baseline exactly.
        assert!(index.verdict().is_realizable());
        for owner_id in 0..owner_graph.nodes.len() {
            assert_eq!(
                index.partition().of(OwnerId(owner_id)),
                baseline.of(OwnerId(owner_id)),
                "partition slot {owner_id} should be restored by undo",
            );
        }
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
}
