//! Quotient-graph kernel for the peel proposer.
//!
//! `QuotientGraph` represents the current equivalence relation `~` over
//! owners, plus the cross-class edge structure derived from the
//! constraining-init-order owner edges. The kernel exposes one mutation
//! (`contract`) and two non-mutating queries
//! (`merge_preserves_invariants`, `would_be_cycles_after_contract`).
//!
//! Splits are forbidden. There is no public API on `QuotientGraph` that
//! refines the equivalence relation. The seeding protocol applies
//! forced contractions one at a time, gating each on
//! `merge_preserves_invariants` and recording a
//! `SeedContractionRejected` diagnostic if the contraction would have
//! created cycles. See
//! `plans/peel_proposer_contraction_model.md` (commit 1) for the
//! mental model.
//!
//! Commit 2 of the plan adds:
//! - `greedy_merge_to_convergence` — the greedy contraction loop
//!   with deterministic `pick_best` tiebreaks.
//! - `is_pre_existing_module` per-class metadata. Required by the
//!   commit-2 greedy mergeability restriction ("extension of
//!   existing module by orphaned residual class").
//!
//! ## The unified realizability gate (Track A)
//!
//! The kernel's cycle gate is the realizability primitive itself
//! (`gate::check_realizability(&OwnerGraph, &Partition)` — the same
//! predicate the materializer's `validate_factorization` computes),
//! evaluated at **module** granularity under the kernel's projection
//! by checking whether a speculative merge's post-merge module `M`
//! has no clause-2 or clause-3 violation touching `M`.
//!
//! The kernel must not reimplement the gate over the JSON
//! `OwnerGraphReport` adjacency: a constraining-only projection drops
//! non-constraining edges and misses asymmetric `(eager forward, lazy
//! back)` I-cycles that the materializer catches. The kernel
//! reconstructs an `OwnerGraph` from the report, stores it on the
//! kernel, and keeps a persistent `gate::RealizabilityIndex` in sync
//! with its class projection.
//!
//! ## Cost of the unified gate
//!
//! `check_realizability` is `O(|V| + |E|)` per call. The kernel's
//! merge-candidate queries run per (c1, c2) pair, so a from-scratch
//! implementation would cost `O(|V|² · |E|)` per planner round.
//!
//! Instead, every query routes through the index's tier ladder
//! (`ladder_decision_after_moving_owners_touching`, plan §3): tier 0
//! short-circuits delta-free moves, tiers 1–2 answer Pass 1 /
//! Pass-2-vacuity from maintained `CondensationOrder` structures in
//! `O(α)`–`O(|Δ|)`, and tier 3 runs the shared scoped
//! `EsmEvaluationSimulator` only for merges that land the target in a
//! constraining-edge-bearing multi-module I-SCC. The boolean hot path
//! (`check_merge_boolean`) and the evidence-producing diagnostic path
//! (`would_be_cycles_after_contract`) are the same evaluation — one
//! entry point, two output shapes — so the gate cannot drift from the
//! materializer's verdict. Per committed contract, the kernel pushes
//! `PartitionDelta::MoveOwners` deltas onto the index; speculative
//! queries read it non-mutatingly through the overlay path (every
//! speculative merge delta targets the single post-merge module,
//! asserted in `realizability_cycles_after_contract`).

use std::cmp::Reverse;
use std::collections::{BTreeMap, BTreeSet, BinaryHeap};

use analysis::{DepKind, ModuleId, OwnerGraph, OwnerGraphReport, OwnerId, Partition};
use gate::{
    LadderDecision, PartitionDelta, RealizabilityIndex, RealizabilityVerdict,
    record_gate_diagnostic_translation,
};
use rustc_hash::{FxHashMap, FxHashSet};
use serde::Serialize;

/// Owner index into the `OwnerGraphReport.nodes` vector. Stable for
/// the lifetime of one quotient graph.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash, Serialize)]
pub struct OwnerIdx(pub usize);

/// Class identifier in the current quotient. Class IDs are assigned
/// densely starting at 0; the owner with the lowest `OwnerIdx` in a
/// class is canonical (used for tiebreaking and as the class
/// representative in diagnostics).
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash, Serialize)]
pub struct ClassId(pub usize);

/// One unrealizable multi-class SCC in the constraining-edge quotient.
/// Used by both `cycle_set()` and `would_be_cycles_after_contract` as
/// the evidence shape.
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Serialize)]
pub struct CycleClassSet {
    /// Sorted, deduplicated class IDs participating in the cycle.
    pub classes: Vec<ClassId>,
    /// Sorted, deduplicated owner IDs (as strings, from the report)
    /// participating in any class in `classes`. Stable for
    /// diagnostic byte-equality.
    pub owner_ids: Vec<String>,
}

/// Aggregated cycle evidence: zero or more multi-class SCCs.
#[derive(Debug, Clone, Default, Eq, PartialEq, Serialize)]
pub struct CycleEvidence {
    pub cycles: Vec<CycleClassSet>,
}

impl CycleEvidence {
    pub fn is_empty(&self) -> bool {
        self.cycles.is_empty()
    }
}

/// Reason a `contract` call could not proceed. Surfaces the cycle
/// evidence so the caller can attribute the rejection to a specific
/// owner pair.
#[derive(Debug, Clone, Eq, PartialEq, Serialize)]
pub enum ContractRejected {
    WouldCreateCycle { cycle: CycleEvidence },
    ExceedsCap { combined_lines: usize, cap: usize },
    ResidualSticky,
    SameClass,
}

/// Per-contraction rejection diagnostic emitted by `build_seed_quotient`.
/// Stable JSON shape — `reports/tree/<chunk>/seed_rejections.json`
/// consumers depend on field order.
#[derive(Debug, Clone, Eq, PartialEq, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum SeedContractionRejected {
    AtomicUnit {
        unit_id: String,
        owner_ids: Vec<String>,
        rejected_pair: (String, String),
        cycle: CycleEvidence,
    },
    SpecModule {
        module_id: String,
        owner_ids: Vec<String>,
        rejected_pair: (String, String),
        cycle: CycleEvidence,
    },
    /// Pass-3 (atomic-DAG reachability closure) contraction rejected.
    /// Pre-commit-4 behavior was: silently form the closure into a
    /// "cell" even when cyclic; downstream realizability gate would
    /// then report the cycle as a generic SCC. Post-commit-4: the
    /// kernel refuses the merge at seeding time and emits this
    /// diagnostic naming the source/target atomic-DAG edge whose
    /// contraction would have created the cycle. See
    /// `plans/peel_proposer_contraction_model.md`, commit 4.
    AtomicReachability {
        /// The atomic-DAG edge id whose contraction was refused.
        edge_id: String,
        source_unit_id: String,
        target_unit_id: String,
        /// `(source_owner_id, target_owner_id)` — the
        /// representative owners (lowest `OwnerIdx` in each unit at
        /// rejection time) whose class-level contraction tripped
        /// the gate.
        rejected_pair: (String, String),
        /// Cycle evidence when the rejection is cycle-driven. Empty
        /// for non-cycle rejections (cap, residual stickiness).
        cycle: CycleEvidence,
    },
    /// Track A: unrealizable SCC surfaced by the unified gate
    /// (`gate::check_realizability`) on the **final** seed
    /// quotient. Catches asymmetric `(eager forward, lazy back)`
    /// I-cycles plus mutual constraining SCCs assembled across
    /// multiple per-merge contractions whose individual
    /// rejections the per-merge gate did not fire on (each
    /// contraction was locally realizable; the assembled
    /// partition is not).
    ///
    /// `owner_ids` lists every owner in the unrealizable SCC, as
    /// reported by `RealizabilityVerdict::unrealizable_sccs`.
    /// `cycle` carries the kernel-shape evidence for callers that
    /// already render the other rejection variants.
    PostSeedUnrealizableScc {
        owner_ids: Vec<String>,
        cycle: CycleEvidence,
    },
}

/// One spec-module declaration the seeding protocol consumes. The
/// `owner_ids` list is the set of owners the module's `members:`
/// resolves to; the seed pre-contracts them all into one class.
#[derive(Debug, Clone)]
pub struct SpecModuleGroup {
    pub module_id: String,
    pub owner_ids: Vec<String>,
}

/// One input group for `QuotientGraph::from_report_with_partition_extended`.
/// Used by the renderer to materialize cells-derived partitions with
/// per-class metadata the commit-2 greedy needs.
#[derive(Debug, Clone)]
pub struct PartitionGroup {
    pub owner_idxs: Vec<OwnerIdx>,
    /// `true` if this group corresponds to a pre-existing active
    /// spec module (the greedy may extend it by absorbing orphan
    /// residual classes). `false` if this group is a residual
    /// atomic-DAG closure or an ad-hoc grouping.
    pub is_pre_existing_module: bool,
    /// Optional human-readable label (e.g., module id). Carried by
    /// the kernel for diagnostic purposes only.
    pub label: Option<String>,
}

/// One step of the greedy merge loop. Returned by
/// `greedy_step` so callers (incremental-invariant property tests,
/// dry-run diagnostics) can step through one contraction at a time.
#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub struct GreedyStep {
    /// The two classes the step picked, in canonical (lower, higher)
    /// order. After the contract, only `surviving` remains.
    pub picked: (ClassId, ClassId),
    /// The class id that survived the contraction (always equals
    /// `picked.0.min(picked.1)`).
    pub surviving: ClassId,
}

/// Internal: one class's metadata. Class membership is tracked by
/// `owner_to_class`; this struct caches per-class aggregates that
/// `merge_preserves_invariants` consults.
#[derive(Debug, Clone)]
struct ClassData {
    /// Owners in this class, by `OwnerIdx`. Sorted.
    members: BTreeSet<OwnerIdx>,
    /// Summed source-line count across members.
    lines: usize,
    /// `true` if this class contains the residual catch-all.
    is_residual: bool,
    /// `true` if this class was seeded by a `PartitionGroup` with
    /// `is_pre_existing_module = true`. Sticky across merges (a
    /// merge of two pre-existing-module classes — only allowed in
    /// commit 3 — produces a class that is itself pre-existing).
    /// Default `false` for singletons constructed from
    /// `from_report` or for residual atomic-DAG-closure classes
    /// seeded by `from_report_with_partition`.
    is_pre_existing_module: bool,
}

/// The quotient graph: owners partitioned into classes, with the
/// cross-class constraining edges materialized on demand.
#[derive(Debug, Clone)]
pub struct QuotientGraph {
    /// Typed IR reconstructed from the source report. Used by the
    /// unified realizability gate
    /// (`gate::check_realizability`). Stored once at
    /// construction; never mutated.
    owner_graph: OwnerGraph,
    /// Owners whose `OwnerGraphNodeReport.destination.residual` is
    /// `true`. Set by `from_report` from the JSON wire flag (the
    /// same residual identification `factorize.rs` uses). Used by
    /// `project_partition` to decide which class projects to the
    /// partition's residual `ModuleId` for the realizability gate;
    /// distinct from the legacy `ClassData::is_residual` field
    /// which the rest of the kernel keys off of for residual
    /// stickiness.
    gate_residual_owners: BTreeSet<OwnerIdx>,
    /// Stable owner IDs in `OwnerIdx.0` order. Inherited from the
    /// source `OwnerGraphReport.nodes`.
    owner_ids: Vec<String>,
    /// Reverse index for `owner_ids`: owner-id string → `OwnerIdx`.
    /// Built once at construction; `owner_ids` is never mutated after
    /// that, so this stays in sync. Backs `owner_idx_of` so callers
    /// avoid an O(n) linear scan per lookup in the proposer.
    owner_id_to_idx: FxHashMap<String, OwnerIdx>,
    /// Owner → current class. Dense, indexed by `OwnerIdx.0`.
    owner_to_class: Vec<ClassId>,
    /// Class metadata. Indexed by `ClassId.0`. Entries for emptied
    /// classes (post-contract) remain in place with empty `members`
    /// to keep IDs stable; queries skip them.
    classes: Vec<ClassData>,
    /// Constraining-edge owner adjacency: `(from_owner, to_owner)`
    /// pairs from `OwnerGraphReport.edges` whose
    /// `constrains_init_order` is true and whose endpoints both
    /// resolve to known owners. Source-of-truth for the cycle set.
    owner_constraining_edges: Vec<(OwnerIdx, OwnerIdx)>,
    /// **All** owner edges, with their weight (from `DepKind`).
    /// Used by the greedy's coupling metric. Self-loops and
    /// non-constraining edges are included; `class_cross_edges`
    /// filters out same-class pairs at query time.
    owner_weighted_edges: Vec<WeightedOwnerEdge>,
    /// Cap on per-class combined lines. Exceeding this is a rejected
    /// merge.
    cap_lines: usize,
    /// Unified class-level out-edge adjacency. Indexed by `ClassId.0`
    /// (dense; dead classes have an empty map). For each source class
    /// `s`, `out_edges[s.0]` is a map from target class `t` to an
    /// `EdgeState` aggregating all underlying owner edges from members
    /// of `s` to members of `t`. Self-loops are filtered out at insert
    /// time. Replaces the prior 7-way lockstep maintenance of
    /// `class_out`, `class_edge_multiplicity`, `class_edge_weight`,
    /// `class_weighted_out`, `class_weighted_edge_count`, and
    /// `class_out_edge_count`.
    out_edges: Vec<FxHashMap<ClassId, EdgeState>>,
    /// Back-pointer index for maintenance only. `in_neighbors[c.0]`
    /// holds every source class `s` such that
    /// `out_edges[s.0].contains_key(&c)`. Maintained in lockstep with
    /// `out_edges` on every insert/relabel/remove. Replaces the prior
    /// `class_in` + `class_weighted_in` pair (the constraining-only
    /// `class_in` is recovered by filtering for sources with
    /// `out_edges[s.0][&c].constraining_count > 0`).
    in_neighbors: Vec<FxHashSet<ClassId>>,
    /// Persistent-state realizability index over `owner_graph`.
    /// Synced to the kernel's current class projection after every
    /// committed mutation (`contract`, `from_report*`). Speculative
    /// queries (`merge_preserves_invariants`,
    /// `would_be_cycles_after_contract`) read it non-mutatingly via
    /// `verdict_after_moving_owners_touching` (all speculative moves
    /// are single-target). See the module-level docstring's "Cost of
    /// the unified gate" section.
    realizability_index: RealizabilityIndex,
    /// Cached `class_id -> module_id` mapping mirroring what
    /// `project_partition(None)` would assign. Maintained alongside
    /// the realizability index so query deltas can be computed
    /// without walking every owner per call.
    class_module_id: BTreeMap<ClassId, ModuleId>,
    /// Next free synthetic `ModuleId` index. Assigned densely from 1
    /// (0 is the residual catch-all). Incremented when a previously
    /// residual class transitions to non-residual via a contract
    /// that brings in a non-gate-residual member.
    next_module_idx: usize,
}

/// One owner-edge with its `DepKind`-derived weight. Stored on the
/// kernel so the greedy can evaluate the coupling metric without
/// re-parsing the input report.
#[derive(Debug, Clone, Copy)]
struct WeightedOwnerEdge {
    from: OwnerIdx,
    to: OwnerIdx,
    weight: u32,
}

fn edge_weight(kind: DepKind) -> u32 {
    match kind {
        DepKind::EagerUse | DepKind::EagerRebind => 4,
        DepKind::Sequenced => 2,
        DepKind::LazyUse | DepKind::LazyRebind | DepKind::DeferredRebind => 1,
        DepKind::LocalEffect => 2,
    }
}

/// Aggregated state for one directed class-pair `(s, t)`. Stored
/// inline in `QuotientGraph::out_edges[s.0]` keyed by `t`. Per-edge
/// counts roll up the underlying owner edges between the two classes.
///
/// Invariant: `constraining_count <= weighted_count`. An entry is
/// present iff `weighted_count > 0`; the empty default is never
/// stored.
#[derive(Debug, Clone, Copy, Default, Eq, PartialEq)]
pub(super) struct EdgeState {
    /// Number of underlying owner edges from members of the source
    /// class to members of the target class whose
    /// `constrains_init_order` is true.
    pub(super) constraining_count: u32,
    /// Total underlying owner edges (constraining or not) from
    /// members of the source class to members of the target class.
    /// Always `>= constraining_count`.
    pub(super) weighted_count: u32,
    /// Sum of `edge_weight` over all underlying owner edges.
    weighted_sum: u64,
}

impl QuotientGraph {
    /// Build a fresh quotient over `report.nodes`, each owner in its
    /// own singleton class. `cap_lines` is the size cap consulted by
    /// `merge_preserves_invariants`.
    ///
    /// The kernel also reconstructs the typed `OwnerGraph` IR via
    /// `OwnerGraph::from_report` and stashes it for the unified
    /// realizability gate. The reconstructed IR carries every edge
    /// the report listed — constraining and non-constraining alike —
    /// so the gate sees the same I-graph the materializer does.
    ///
    /// The reconstructed graph's per-owner `declared` set is left
    /// empty: this kernel only consumes the edge topology + module
    /// quotient for the gate, never the declared-binding join used
    /// by `factor_assembly::compute_owner_claims`. If a future
    /// consumer (e.g. running `assemble_partition` against the
    /// reconstructed graph) needs `declared`, switch this call to
    /// pass the chunk's `StatementFactsReport` slice.
    pub fn from_report(
        report: &OwnerGraphReport,
        cap_lines: usize,
    ) -> Result<Self, analysis::UnresolvedOwnerEdgeEndpoint> {
        let (owner_graph, _) = OwnerGraph::from_report(report, &[])?;
        let owner_ids: Vec<String> = report.nodes.iter().map(|n| n.id.clone()).collect();
        let owner_id_to_idx: FxHashMap<String, OwnerIdx> = owner_ids
            .iter()
            .enumerate()
            .map(|(i, id)| (id.clone(), OwnerIdx(i)))
            .collect();

        let mut classes = Vec::<ClassData>::with_capacity(owner_ids.len());
        let mut owner_to_class = Vec::<ClassId>::with_capacity(owner_ids.len());
        let mut gate_residual_owners: BTreeSet<OwnerIdx> = BTreeSet::new();
        for (i, node) in report.nodes.iter().enumerate() {
            let mut members = BTreeSet::new();
            members.insert(OwnerIdx(i));
            // `ClassData::is_residual` marks the single residual
            // catch-all CLASS that the greedy refuses to merge into —
            // a distinct concept from "this owner is destined for
            // residual" (which is the whole peelable pile). At seed
            // time no class is the catch-all: every owner is its own
            // singleton and the factorizer peels residual-destined
            // owners OUT into fresh-module proposals. So this starts
            // `false`; the residual class is established later. The
            // authoritative "destined for residual" signal lives in
            // the module table (`OwnerGraphReport::is_residual`) and is
            // consumed via `gate_residual_owners` below + the
            // projection in `realizability_index`.
            classes.push(ClassData {
                members,
                lines: owner_line_count_from_report(node),
                is_residual: false,
                is_pre_existing_module: false,
            });
            owner_to_class.push(ClassId(i));
            // The realizability gate's ESM simulator DFS starts at the
            // partition's residual; any residual owner projects to that
            // residual ModuleId so the simulator's DFS reaches the
            // candidate SCCs.
            if report.is_residual(&node.destination) {
                gate_residual_owners.insert(OwnerIdx(i));
            }
        }

        let mut owner_constraining_edges: Vec<(OwnerIdx, OwnerIdx)> = Vec::new();
        let mut owner_weighted_edges: Vec<WeightedOwnerEdge> =
            Vec::with_capacity(report.edges.len());
        for edge in &report.edges {
            // Endpoint resolution can't fail here: the
            // `OwnerGraph::from_report` call above already errored on
            // any edge whose endpoint is missing from the node table.
            let resolve = |endpoint: &str| {
                owner_id_to_idx
                    .get(endpoint)
                    .copied()
                    .expect("validated by OwnerGraph::from_report")
            };
            let (s, t) = (resolve(&edge.source), resolve(&edge.target));
            owner_weighted_edges.push(WeightedOwnerEdge {
                from: s,
                to: t,
                weight: edge_weight(edge.edge_kind),
            });
            if !edge.constrains_init_order || s == t {
                continue;
            }
            owner_constraining_edges.push((s, t));
        }

        // Build the initial realizability index from a partition
        // projection that mirrors `project_partition(None)`'s output
        // for the singleton-class shape: each non-residual non-gate-
        // residual owner gets a fresh `ModuleId::logical(N)`, the
        // residual class plus all gate-residual owners share
        // `ModuleId::logical(0)`.
        let residual_module = ModuleId::logical(0);
        let mut class_module_id: BTreeMap<ClassId, ModuleId> = BTreeMap::new();
        let mut next_module_idx: usize = 1;
        for (class_idx, data) in classes.iter().enumerate() {
            let c = ClassId(class_idx);
            let module = if data.is_residual {
                residual_module
            } else {
                let only_gate_residual = !data.members.is_empty()
                    && data
                        .members
                        .iter()
                        .all(|m| gate_residual_owners.contains(m))
                    && !data.is_pre_existing_module;
                if only_gate_residual {
                    residual_module
                } else {
                    let m = ModuleId::logical(next_module_idx);
                    next_module_idx += 1;
                    m
                }
            };
            class_module_id.insert(c, module);
        }
        let partition_assignments: Vec<ModuleId> = owner_to_class
            .iter()
            .map(|c| class_module_id.get(c).copied().unwrap_or(residual_module))
            .collect();
        let initial_partition = Partition::from_assignments(partition_assignments, residual_module);
        let realizability_index =
            RealizabilityIndex::from_partition(&owner_graph, initial_partition);

        let num_classes = classes.len();
        let mut q = QuotientGraph {
            owner_graph,
            gate_residual_owners,
            owner_ids,
            owner_id_to_idx,
            owner_to_class,
            classes,
            owner_constraining_edges,
            owner_weighted_edges,
            cap_lines,
            out_edges: vec![FxHashMap::default(); num_classes],
            in_neighbors: vec![FxHashSet::default(); num_classes],
            realizability_index,
            class_module_id,
            next_module_idx,
        };
        q.rebuild_class_adjacency();
        Ok(q)
    }

    /// Build a quotient over `report.nodes` and immediately contract
    /// each owner group into a single class. Unlike `contract`, this
    /// bypasses the realizability gate — the partition is taken as
    /// authoritative. Returns the quotient plus a list of class IDs,
    /// one per input group, in the same order as `groups`.
    ///
    /// Used by `peel::factorize::emit_proposals` to render off a
    /// cells-derived quotient (Path B in
    /// `plans/peel_proposer_contraction_model.md`'s commit 1b): the
    /// cell-discovery pass produces equivalence classes that are not
    /// derivable from the seeding protocol's gated contractions, so
    /// the kernel hosts them as a partition rather than as a sequence
    /// of gated contractions.
    ///
    /// Groups containing owners already implicitly co-located with
    /// other groups (overlap) are not supported and will panic; the
    /// caller is expected to pre-coalesce overlapping groups, which
    /// `proposal_cells_from_atomic_graph` already does.
    pub fn from_report_with_partition(
        report: &OwnerGraphReport,
        cap_lines: usize,
        groups: &[Vec<OwnerIdx>],
    ) -> Result<(Self, Vec<ClassId>), analysis::UnresolvedOwnerEdgeEndpoint> {
        let extended_groups: Vec<PartitionGroup> = groups
            .iter()
            .map(|owner_idxs| PartitionGroup {
                owner_idxs: owner_idxs.clone(),
                is_pre_existing_module: false,
                label: None,
            })
            .collect();
        Self::from_report_with_partition_extended(report, cap_lines, &extended_groups)
    }

    /// Like `from_report_with_partition`, but each group carries
    /// per-class metadata (`is_pre_existing_module`, optional
    /// `label`). The greedy's mergeability check consults
    /// `is_pre_existing_module` to restrict commit-2 merges to
    /// "extend existing module by orphaned residual class."
    pub fn from_report_with_partition_extended(
        report: &OwnerGraphReport,
        cap_lines: usize,
        groups: &[PartitionGroup],
    ) -> Result<(Self, Vec<ClassId>), analysis::UnresolvedOwnerEdgeEndpoint> {
        let mut q = Self::from_report(report, cap_lines)?;
        let mut group_class_ids = Vec::with_capacity(groups.len());
        for group in groups {
            let mut winner: Option<ClassId> = None;
            for &owner in &group.owner_idxs {
                let c = q.class_of(owner);
                match winner {
                    None => winner = Some(c),
                    Some(w) if c == w => {}
                    Some(w) => {
                        let merged = q
                            .merge_classes_unchecked(w, c)
                            .expect("partition group owners are pre-coalesced");
                        winner = Some(merged);
                    }
                }
            }
            let survivor = winner.expect("partition group must be non-empty");
            if group.is_pre_existing_module {
                q.classes[survivor.0].is_pre_existing_module = true;
            }
            group_class_ids.push(survivor);
        }
        // Partition seeding bypasses the gate, so the cached
        // adjacency / realizability index can drift from the
        // per-merge incremental update. Rebuild from scratch so
        // callers see the correct initial state.
        q.rebuild_class_adjacency();
        q.rebuild_realizability_index();
        Ok((q, group_class_ids))
    }

    /// The class an owner currently belongs to.
    pub fn class_of(&self, o: OwnerIdx) -> ClassId {
        self.owner_to_class[o.0]
    }

    /// Look up the owner index for a stable owner-id string. Returns
    /// `None` for ids not present in the source report. O(1) via the
    /// `owner_id_to_idx` FxHashMap.
    pub fn owner_idx_of(&self, owner_id: &str) -> Option<OwnerIdx> {
        self.owner_id_to_idx.get(owner_id).copied()
    }

    /// Stable owner id string for an `OwnerIdx`.
    pub fn owner_id(&self, o: OwnerIdx) -> &str {
        &self.owner_ids[o.0]
    }

    /// Members of a class in `OwnerIdx` order.
    pub fn class_members(&self, c: ClassId) -> impl Iterator<Item = OwnerIdx> + '_ {
        self.classes[c.0].members.iter().copied()
    }

    /// Total source-line count summed across a class's members.
    pub fn class_lines(&self, c: ClassId) -> usize {
        self.classes[c.0].lines
    }

    /// `true` if a class contains the residual catch-all owner.
    pub fn class_is_residual(&self, c: ClassId) -> bool {
        self.classes[c.0].is_residual
    }

    /// Designate `c` as the residual catch-all class — the class the
    /// commit-2 greedy refuses to merge into. Seed-time construction
    /// leaves every class non-residual (the factorizer peels
    /// residual-destined owners OUT into fresh modules); callers that
    /// model a sticky residual sink mark it explicitly.
    pub fn mark_class_residual(&mut self, c: ClassId) {
        self.classes[c.0].is_residual = true;
    }

    /// Iterator over all live (non-empty) class IDs.
    pub fn iter_classes(&self) -> impl Iterator<Item = ClassId> + '_ {
        self.classes
            .iter()
            .enumerate()
            .filter_map(|(i, c)| (!c.members.is_empty()).then_some(ClassId(i)))
    }

    /// Current unrealizable cycle evidence. Reads the persistent
    /// realizability index's maintained state and translates the
    /// verdict back into the kernel's class/owner-id vocabulary.
    ///
    /// Cost is dominated by the verdict's SCC-listing step. Per-call
    /// overhead is proportional to the number of unrealizable SCCs;
    /// the underlying constraining/I graphs are maintained
    /// incrementally on each `contract`.
    pub fn cycle_set(&self) -> CycleEvidence {
        let verdict = self.realizability_index.verdict();
        self.translate_verdict_to_evidence(&verdict)
    }

    /// Public realizability verdict against the kernel's current
    /// class projection. Reads the persistent realizability index
    /// directly. The verdict's `unrealizable_sccs` carry
    /// `ModuleId`s; callers consult `class_module_id_of` to map
    /// them back to `ClassId`s, or call `cycle_set()` instead for a
    /// kernel-shape evidence.
    pub fn realizability_verdict(&self) -> RealizabilityVerdict {
        self.realizability_index.verdict()
    }

    /// The partition currently held by the persistent realizability
    /// index. Equal to `project_partition(None)` modulo bijective
    /// `ModuleId` renaming (the index's partition reuses the same
    /// ModuleIds across queries; `project_partition` mints fresh
    /// densely-numbered IDs per call). Read-only — mutation goes
    /// through the kernel's contract/query APIs.
    pub fn realizability_partition(&self) -> &Partition {
        self.realizability_index.partition()
    }

    /// Cheap query: would contracting `c1` and `c2` preserve the
    /// kernel's invariants? Specifically:
    ///
    /// 1. `c1 != c2`.
    /// 2. Not both classes are residual; if one is residual, the
    ///    other must be too (residual is sticky — never absorb a
    ///    non-residual class into the residual catch-all).
    /// 3. `class_lines(c1) + class_lines(c2) <= cap_lines`.
    /// 4. The post-merge partition, under the kernel's module
    ///    projection, has no clause-2 or clause-3 violation touching
    ///    the post-merge module (the tier-laddered realizability
    ///    predicate).
    ///
    /// State mutation: this hot boolean path does not materialize
    /// diagnostic evidence — the ladder short-circuits without
    /// building verdicts. `would_be_cycles_after_contract` is the
    /// same evaluation with evidence materialization enabled.
    pub fn merge_preserves_invariants(&self, c1: ClassId, c2: ClassId) -> bool {
        self.check_merge_boolean(c1, c2)
    }

    /// Diagnostic: what violations would the merge create or surface?
    /// Returns `None` if the post-merge partition is realizable
    /// (touching the post-merge module). Returns `Some(evidence)`
    /// otherwise.
    ///
    /// Same predicate as the boolean gate (`check_merge_boolean`):
    /// the ladder decides, and only on a reject does this path
    /// materialize owner-level evidence through the index's
    /// non-mutating overlay verdict — one entry point, two output
    /// shapes.
    ///
    /// The evidence shape (class IDs + owner ID strings) is
    /// preserved from the pre-Track-A kernel for compatibility with
    /// existing `SeedContractionRejected` JSON consumers.
    pub fn would_be_cycles_after_contract(
        &self,
        c1: ClassId,
        c2: ClassId,
    ) -> Option<CycleEvidence> {
        if c1 == c2 {
            return None;
        }
        if self.ladder_decision_for_merge(c1, c2).accepts() {
            return None;
        }
        let evidence = self.realizability_cycles_after_contract(c1, c2);
        if !evidence.is_empty() {
            return Some(evidence);
        }
        // The ladder rejected but the verdict translation produced no
        // multi-class SCC evidence — a clause-2 cross-rebind reject
        // (no SCC to render), or an SCC whose owners collapse into a
        // single class under the projection. Surface a minimal
        // two-class evidence shape so callers still see a rejection.
        Some(CycleEvidence {
            cycles: vec![CycleClassSet {
                classes: vec![c1.min(c2), c1.max(c2)],
                owner_ids: Vec::new(),
            }],
        })
    }

    /// Apply a contraction. Returns `Err(ContractRejected)` if any
    /// invariant would be violated; the caller should have checked
    /// via `merge_preserves_invariants` first. Belt-and-braces.
    ///
    /// On success, `c1` (the lower of the two class IDs) absorbs
    /// `c2`; `c2`'s members are reassigned, its slot is left empty
    /// in `classes`. Subsequent calls should use the surviving class
    /// id (`c1.min(c2)`).
    pub fn contract(&mut self, c1: ClassId, c2: ClassId) -> Result<ClassId, ContractRejected> {
        self.check_merge(c1, c2)?;
        self.merge_classes_unchecked(c1, c2)
    }

    /// Merge two classes without consulting the realizability /
    /// residual / cap gates. The lower of the two `ClassId`s
    /// survives; the higher is emptied. Returns the survivor.
    ///
    /// `SameClass` is still rejected (caller error). All other gate
    /// clauses are bypassed — this method is the partition-driven
    /// entrypoint used by `from_report_with_partition`. External
    /// callers should prefer `contract`.
    fn merge_classes_unchecked(
        &mut self,
        c1: ClassId,
        c2: ClassId,
    ) -> Result<ClassId, ContractRejected> {
        if c1 == c2 {
            return Err(ContractRejected::SameClass);
        }
        let (winner, loser) = if c1 < c2 { (c1, c2) } else { (c2, c1) };
        // Compute the realizability-index deltas *before* mutating
        // class membership — the deltas key off pre-merge winner /
        // loser composition.
        let (post_module, deltas) = self.compute_merge_deltas(winner, loser);
        // Move members from loser to winner.
        let loser_members = std::mem::take(&mut self.classes[loser.0].members);
        let loser_lines = self.classes[loser.0].lines;
        let loser_residual = self.classes[loser.0].is_residual;
        let loser_pre_existing = self.classes[loser.0].is_pre_existing_module;
        self.classes[loser.0].lines = 0;
        self.classes[loser.0].is_residual = false;
        self.classes[loser.0].is_pre_existing_module = false;
        for member in &loser_members {
            self.owner_to_class[member.0] = winner;
        }
        self.classes[winner.0].members.extend(loser_members);
        self.classes[winner.0].lines = self.classes[winner.0].lines.saturating_add(loser_lines);
        if loser_residual {
            self.classes[winner.0].is_residual = true;
        }
        if loser_pre_existing {
            self.classes[winner.0].is_pre_existing_module = true;
        }
        self.update_class_adjacency_after_merge(winner, loser);
        // Commit the realizability-index deltas. These are pushed
        // permanently (no undo) because the kernel mutation just
        // landed. Update the class -> module map: winner gets
        // post_module, loser is dropped.
        for delta in deltas {
            self.realizability_index.push(&self.owner_graph, delta);
        }
        // The deltas above are committed — nothing will undo them —
        // so drop their rollback state. Without this, the index's
        // journals grow without bound across the greedy's committed
        // merges (speculative queries read through the non-mutating
        // overlay and never touch the journal).
        self.realizability_index.commit();
        // If `post_module` was freshly minted, bump the counter so
        // subsequent merges don't collide.
        if post_module.0.0 >= self.next_module_idx {
            self.next_module_idx = post_module.0.0 + 1;
        }
        self.class_module_id.insert(winner, post_module);
        self.class_module_id.remove(&loser);
        Ok(winner)
    }

    fn check_merge(&self, c1: ClassId, c2: ClassId) -> Result<(), ContractRejected> {
        self.check_merge_preconditions(c1, c2)?;
        if let Some(cycle) = self.would_be_cycles_after_contract(c1, c2) {
            return Err(ContractRejected::WouldCreateCycle { cycle });
        }
        Ok(())
    }

    /// Hot-path boolean gate used by the greedy candidate loop.
    ///
    /// This deliberately avoids `would_be_cycles_after_contract`'s
    /// evidence materialization. On large corpora, most rejected
    /// candidates only need a yes/no answer; constructing
    /// `CycleEvidence` routes through the full realizability verdict,
    /// simulator, and owner-module diagnostic translation. The ladder
    /// answers the same predicate with the evidence elided.
    fn check_merge_boolean(&self, c1: ClassId, c2: ClassId) -> bool {
        if self.check_merge_preconditions(c1, c2).is_err() {
            return false;
        }
        self.ladder_decision_for_merge(c1, c2).accepts()
    }

    fn check_merge_preconditions(&self, c1: ClassId, c2: ClassId) -> Result<(), ContractRejected> {
        if c1 == c2 {
            return Err(ContractRejected::SameClass);
        }
        let cls1 = &self.classes[c1.0];
        let cls2 = &self.classes[c2.0];
        if cls1.members.is_empty() || cls2.members.is_empty() {
            return Err(ContractRejected::SameClass);
        }
        // Residual stickiness: if exactly one is residual, reject.
        // (Two residual classes never coexist with the canonical
        // construction since only one owner is residual today; the
        // check is defensive.)
        if cls1.is_residual != cls2.is_residual {
            return Err(ContractRejected::ResidualSticky);
        }
        let combined = cls1.lines.saturating_add(cls2.lines);
        if combined > self.cap_lines {
            return Err(ContractRejected::ExceedsCap {
                combined_lines: combined,
                cap: self.cap_lines,
            });
        }
        Ok(())
    }

    /// Project the kernel's current class assignment back to an
    /// `analysis::Partition` so the unified realizability gate
    /// (`check_realizability`) can run on the typed `OwnerGraph`.
    ///
    /// The projection assigns ModuleIds densely:
    /// - `ModuleId::logical(0)` is the **residual catch-all**.
    ///   Every owner belonging to the residual class (per
    ///   `class_is_residual`) maps to this id.
    /// - Non-residual classes are numbered starting at 1, in
    ///   ascending `ClassId` order.
    ///
    /// The optional `overlay` argument lets the caller ask "what if
    /// I contracted `(a, b)` first?" — class `b` is projected as if
    /// it had already been absorbed into `a`. Used by
    /// `would_be_cycles_after_contract` to ask the gate about a
    /// hypothetical post-merge state without mutating the kernel.
    pub fn project_partition_for_tests(&self) -> Partition {
        self.project_partition(None)
    }

    /// Borrow the typed `OwnerGraph` IR reconstructed at
    /// construction time. Used by property tests to compare the
    /// kernel's incremental verdict to a from-scratch
    /// `check_realizability(&owner_graph, &project_partition(None))`.
    pub fn owner_graph_for_tests(&self) -> &OwnerGraph {
        &self.owner_graph
    }

    fn project_partition(&self, overlay: Option<(ClassId, ClassId)>) -> Partition {
        // Walk all classes, assigning each a ModuleId. Residual
        // (and the absorbed loser side of the overlay) share an
        // id with their target.
        let project = |c: ClassId| -> ClassId {
            if let Some((a, b)) = overlay {
                if c == a || c == b {
                    return if a < b { a } else { b };
                }
            }
            c
        };
        // The synthesized residual module is `ModuleId::logical(0)`.
        // Only classes whose `ClassData::is_residual` flag is true
        // map there — i.e., the residual catchall the kernel was
        // constructed with. Other classes (including those whose
        // owners have `destination.residual = true` but were pulled
        // into a spec-module group) get distinct ModuleIds so the
        // realizability gate sees them as separate modules. The
        // alternative (any residual-destined owner → residual
        // ModuleId) collapses spec-module candidate contractions
        // back into residual, blinding the gate to the cycle they
        // would create. See the `seed_skips_unrealizable_spec_module_contraction_and_reports`
        // test for the regression.
        let residual = ModuleId::logical(0);
        let mut class_to_module: BTreeMap<ClassId, ModuleId> = BTreeMap::new();
        let mut next_idx = 1usize;
        for c in self.iter_classes() {
            let projected = project(c);
            if self.classes[projected.0].is_residual {
                class_to_module.entry(projected).or_insert(residual);
            } else {
                class_to_module.entry(projected).or_insert_with(|| {
                    let m = ModuleId::logical(next_idx);
                    next_idx += 1;
                    m
                });
            }
        }
        // Default-fill of `of` with residual so owners not currently
        // mapped (e.g., owners that ended up in an empty class via
        // overlay) land in residual. Then overwrite per owner with
        // its projected class's ModuleId.
        let mut of: Vec<ModuleId> = vec![residual; self.owner_graph.num_nodes()];
        for (owner_idx, slot) in of.iter_mut().enumerate() {
            if owner_idx >= self.owner_to_class.len() {
                continue;
            }
            let c = self.owner_to_class[owner_idx];
            let projected = project(c);
            if let Some(&m) = class_to_module.get(&projected) {
                *slot = m;
            }
        }
        // Track A: also consider gate-only residual marker — owners
        // whose `destination.residual = true` should appear as
        // residual in the projection IF and ONLY IF their class is
        // not already mapped (i.e., the class wasn't promoted to a
        // distinct ModuleId). This affects the rare path where the
        // kernel's `class_is_residual` is false but `factorize.rs`
        // semantics say the owner is residual (e.g., the production
        // ModuleReportRef whose `id` is `logical:N` but `residual: true`).
        // In production, every chunk has at least one residual owner
        // (the synthesized residual entry), so this guarantees the
        // simulator's DFS reaches the SCC candidates the gate needs
        // to evaluate.
        for &owner in &self.gate_residual_owners {
            if owner.0 >= self.owner_to_class.len() {
                continue;
            }
            let c = self.owner_to_class[owner.0];
            let projected = project(c);
            // Only override if this class's mapping is non-residual
            // AND the class is purely composed of gate-residual
            // owners (i.e., it wasn't merged with a spec-module
            // group via `is_pre_existing_module`). Without this
            // guard, a spec-module contraction that pulls a
            // destination.residual=true owner into a class would
            // demote the class back to residual.
            if self.classes[projected.0].is_pre_existing_module {
                continue;
            }
            // Check that every member of this class has
            // `destination.residual = true`. If any member is
            // non-residual, the class shouldn't be residual either.
            let all_residual = self.classes[projected.0]
                .members
                .iter()
                .all(|m| self.gate_residual_owners.contains(m));
            if !all_residual {
                continue;
            }
            // Override the class's mapping to residual.
            class_to_module.insert(projected, residual);
            of[owner.0] = residual;
        }
        // Re-walk owners to ensure consistency after the override.
        for (owner_idx, slot) in of.iter_mut().enumerate() {
            if owner_idx >= self.owner_to_class.len() {
                continue;
            }
            let c = self.owner_to_class[owner_idx];
            let projected = project(c);
            if let Some(&m) = class_to_module.get(&projected) {
                *slot = m;
            }
        }
        Partition::from_assignments(of, residual)
    }

    /// Translate an `gate::RealizabilityVerdict` (in ModuleId
    /// space) back into the kernel's `CycleEvidence` shape (in
    /// ClassId space, with owner-id strings for diagnostics).
    fn translate_verdict_to_evidence(&self, verdict: &RealizabilityVerdict) -> CycleEvidence {
        let active = !verdict.is_realizable();
        record_gate_diagnostic_translation(
            active,
            self.owner_ids.len().min(self.owner_graph.num_nodes()),
            verdict.unrealizable_sccs.len(),
        );
        if !active {
            return CycleEvidence::default();
        }
        let partition = self.realizability_index.partition();
        let mut cycles: Vec<CycleClassSet> = Vec::new();
        for scc in &verdict.unrealizable_sccs {
            let modules_in_scc: BTreeSet<ModuleId> = scc.modules.iter().copied().collect();
            let mut owner_ids: BTreeSet<String> = BTreeSet::new();
            let mut class_set: BTreeSet<ClassId> = BTreeSet::new();
            for (owner_idx, owner_id_str) in self.owner_ids.iter().enumerate() {
                if owner_idx >= self.owner_graph.num_nodes() {
                    continue;
                }
                let module = partition.of(OwnerId(owner_idx));
                if !modules_in_scc.contains(&module) {
                    continue;
                }
                owner_ids.insert(owner_id_str.clone());
                class_set.insert(self.owner_to_class[owner_idx]);
            }
            if class_set.len() < 2 {
                continue;
            }
            let mut classes: Vec<ClassId> = class_set.into_iter().collect();
            classes.sort();
            cycles.push(CycleClassSet {
                classes,
                owner_ids: owner_ids.into_iter().collect(),
            });
        }
        cycles.sort();
        cycles.dedup();
        CycleEvidence { cycles }
    }

    /// Incremental hypothetical query: cycle evidence after merging
    /// `(c1, c2)`, without committing the merge. Routes through the
    /// persistent realizability index's
    /// `verdict_after_moving_owners_touching` overlay path: every
    /// delta moves owners into the single post-merge `ModuleId`, so
    /// the index's graphs are never mutated and only SCCs touching
    /// the target module are read — the key cost saving on
    /// gaffer-scale inputs.
    fn realizability_cycles_after_contract(&self, c1: ClassId, c2: ClassId) -> CycleEvidence {
        let (winner, loser) = if c1 < c2 { (c1, c2) } else { (c2, c1) };
        let (post_module, deltas) = self.compute_merge_deltas(winner, loser);
        // Producing invariant: `compute_merge_deltas` only ever emits
        // `MoveOwners` into the single post-merge module.
        assert!(
            deltas.iter().all(|d| match d {
                PartitionDelta::MoveOwners { to, .. } => *to == post_module,
            }),
            "speculative deltas are single-target by construction (merges contract \
             two classes into one); implement a multi-target overlay deliberately \
             before adding a non-merge mutation"
        );
        let mut owners: Vec<OwnerId> = Vec::new();
        for d in &deltas {
            let PartitionDelta::MoveOwners { owners: o, .. } = d;
            owners.extend(o.iter().copied());
        }
        owners.sort();
        owners.dedup();
        let verdict = self
            .realizability_index
            .verdict_after_moving_owners_touching(&self.owner_graph, &owners, post_module);
        let owner_count = self.owner_ids.len().min(self.owner_graph.num_nodes());
        let owners_set: BTreeSet<OwnerId> = owners.iter().copied().collect();
        let owner_modules: Vec<ModuleId> = (0..owner_count)
            .map(|i| {
                let id = OwnerId(i);
                if owners_set.contains(&id) {
                    post_module
                } else {
                    self.realizability_index.partition().of(id)
                }
            })
            .collect();
        self.translate_verdict_with_owner_modules(&verdict, &owner_modules, Some((c1, c2)))
    }

    /// Tier-laddered boolean gate decision for the speculative merge
    /// `(c1, c2)`.
    /// Derives the same single-target move
    /// `realizability_cycles_after_contract` computes and routes it
    /// through the index's ladder instead of the evidence-producing
    /// verdict. This is the production boolean gate —
    /// `check_merge_boolean` and `would_be_cycles_after_contract`
    /// both decide through it; with `DEBUNDLE_GATE_ORACLE` set every
    /// query is cross-checked against the pure reference predicate.
    ///
    /// Caller contract: `(c1, c2)` must pass the non-cycle merge
    /// preconditions (same as the other speculative cycle queries).
    pub fn ladder_decision_for_merge(&self, c1: ClassId, c2: ClassId) -> LadderDecision {
        let (winner, loser) = if c1 < c2 { (c1, c2) } else { (c2, c1) };
        let (post_module, deltas) = self.compute_merge_deltas(winner, loser);
        let mut owners: Vec<OwnerId> = Vec::new();
        for delta in &deltas {
            let PartitionDelta::MoveOwners { owners: moved, to } = delta;
            debug_assert_eq!(
                *to, post_module,
                "speculative deltas are single-target by construction",
            );
            owners.extend(moved.iter().copied());
        }
        owners.sort();
        owners.dedup();
        self.realizability_index
            .ladder_decision_after_moving_owners_touching(&self.owner_graph, &owners, post_module)
    }

    /// `translate_verdict_to_evidence` parameterized by an explicit
    /// per-owner ModuleId vector. Used by speculative queries whose
    /// verdict comes from a non-mutating overlay, so the index's
    /// partition never reflects the hypothetical move.
    ///
    /// Perf (#12prime): the naive translation iterated all
    /// `self.owner_ids` per SCC — O(K * num_owners), ~10% self in
    /// `modules propose` profiles on tana (9709 owners, many tiny
    /// SCCs per call). We instead build a per-call inverse index
    /// `module_to_owners: FxHashMap<ModuleId, Vec<OwnerIdx>>` from
    /// `owner_modules` in one O(num_owners) pass, then visit only
    /// the owners actually in each SCC's modules. New cost is
    /// `num_owners + sum_scc(|scc.modules| * owners_in_those_modules)`,
    /// which is a strict win whenever the SCCs don't cover most
    /// owners — the common case for the proposer. We chose the
    /// per-call inverse over a kernel-maintained `class_to_owners`
    /// to keep the surface contained: maintaining a kernel field
    /// would touch every `owner_to_class` write site
    /// (rebuild_class_adjacency, contract, merge_classes_unchecked,
    /// etc.) for marginal additional savings.
    fn translate_verdict_with_owner_modules(
        &self,
        verdict: &RealizabilityVerdict,
        owner_modules: &[ModuleId],
        overlay: Option<(ClassId, ClassId)>,
    ) -> CycleEvidence {
        let max_idx = self.owner_ids.len().min(owner_modules.len());
        let active = !verdict.is_realizable();
        record_gate_diagnostic_translation(active, max_idx, verdict.unrealizable_sccs.len());
        if !active {
            return CycleEvidence::default();
        }
        let project = |c: ClassId| -> ClassId {
            if let Some((a, b)) = overlay {
                if c == a || c == b {
                    return if a < b { a } else { b };
                }
            }
            c
        };
        // One pass over owner_modules to build the inverse index.
        // Bounded length: we only consider owner indices that exist
        // in BOTH self.owner_ids and owner_modules — the old code
        // also skipped `owner_idx >= owner_modules.len()`.
        let mut module_to_owners: FxHashMap<ModuleId, Vec<usize>> = FxHashMap::default();
        for (owner_idx, &module) in owner_modules.iter().enumerate().take(max_idx) {
            module_to_owners.entry(module).or_default().push(owner_idx);
        }
        let mut cycles: Vec<CycleClassSet> = Vec::new();
        for scc in &verdict.unrealizable_sccs {
            let mut owner_ids: BTreeSet<String> = BTreeSet::new();
            let mut class_set: BTreeSet<ClassId> = BTreeSet::new();
            for module in &scc.modules {
                let Some(idxs) = module_to_owners.get(module) else {
                    continue;
                };
                for &owner_idx in idxs {
                    owner_ids.insert(self.owner_ids[owner_idx].clone());
                    let c = self.owner_to_class[owner_idx];
                    class_set.insert(project(c));
                }
            }
            if class_set.len() < 2 {
                continue;
            }
            let mut classes: Vec<ClassId> = class_set.into_iter().collect();
            classes.sort();
            cycles.push(CycleClassSet {
                classes,
                owner_ids: owner_ids.into_iter().collect(),
            });
        }
        cycles.sort();
        cycles.dedup();
        CycleEvidence { cycles }
    }

    // ---------------------------------------------------------------
    // Incremental class adjacency + cycle-cache maintenance.
    // ---------------------------------------------------------------

    /// Rebuild class-level edge adjacency from scratch.
    /// O(|owner edges|). Called in `from_report*` and as a fallback;
    /// merges should use `update_class_adjacency_after_merge`.
    fn rebuild_class_adjacency(&mut self) {
        for slot in &mut self.out_edges {
            slot.clear();
        }
        for slot in &mut self.in_neighbors {
            slot.clear();
        }
        // Constraining edges: bump constraining_count. Also seeds the
        // back-pointer in `in_neighbors`. Note constraining edges are
        // a subset of the owner_weighted_edges loop below, which bumps
        // weighted_count and weighted_sum for *every* owner edge
        // (constraining or not).
        for &(s, t) in &self.owner_constraining_edges {
            let cs = self.owner_to_class[s.0];
            let ct = self.owner_to_class[t.0];
            if cs == ct {
                continue;
            }
            let entry = self.out_edges[cs.0].entry(ct).or_default();
            entry.constraining_count += 1;
            self.in_neighbors[ct.0].insert(cs);
        }
        for &edge in &self.owner_weighted_edges {
            let cs = self.owner_to_class[edge.from.0];
            let ct = self.owner_to_class[edge.to.0];
            if cs == ct {
                continue;
            }
            let entry = self.out_edges[cs.0].entry(ct).or_default();
            entry.weighted_count += 1;
            entry.weighted_sum += edge.weight as u64;
            self.in_neighbors[ct.0].insert(cs);
        }
        #[cfg(debug_assertions)]
        self.debug_assert_edge_invariants();
    }

    /// Debug-only bidirectional consistency check. Verifies:
    /// 1. Every `(s, t)` in `out_edges` has `s` in `in_neighbors[t]`.
    /// 2. Every `s` in `in_neighbors[c]` has `out_edges[s.0][&c]`.
    /// 3. `constraining_count <= weighted_count` for every entry.
    /// 4. Dead classes (empty `members`) have empty out/in sets.
    #[cfg(debug_assertions)]
    fn debug_assert_edge_invariants(&self) {
        for (s_idx, outs) in self.out_edges.iter().enumerate() {
            for (&t, edge) in outs {
                debug_assert!(
                    edge.constraining_count <= edge.weighted_count,
                    "constraining_count exceeds weighted_count at ({}, {})",
                    s_idx,
                    t.0
                );
                debug_assert!(
                    edge.weighted_count > 0,
                    "empty EdgeState left in out_edges at ({}, {})",
                    s_idx,
                    t.0
                );
                debug_assert!(
                    self.in_neighbors[t.0].contains(&ClassId(s_idx)),
                    "out_edges[{}][{}] present but in_neighbors[{}] missing source",
                    s_idx,
                    t.0,
                    t.0
                );
            }
        }
        for (t_idx, ins) in self.in_neighbors.iter().enumerate() {
            for &s in ins {
                debug_assert!(
                    self.out_edges[s.0].contains_key(&ClassId(t_idx)),
                    "in_neighbors[{}] contains {} but out_edges[{}][{}] missing",
                    t_idx,
                    s.0,
                    s.0,
                    t_idx
                );
            }
        }
    }

    /// Rebuild the persistent realizability index from scratch using
    /// the current class projection. O(|V| + |E|). Used after
    /// partition-driven mutations that bypass `contract`
    /// (`from_report_with_partition_extended`'s group merges,
    /// `set_class_pre_existing_module` followed by gate-residual
    /// transitions). After `contract`s, the index is maintained
    /// incrementally via `sync_index_after_merge`, not via this
    /// rebuild.
    fn rebuild_realizability_index(&mut self) {
        let residual_module = ModuleId::logical(0);
        let mut class_module_id: BTreeMap<ClassId, ModuleId> = BTreeMap::new();
        let mut next_module_idx: usize = 1;
        for c in self.iter_classes() {
            let data = &self.classes[c.0];
            let module = if data.is_residual {
                residual_module
            } else {
                let only_gate_residual = !data.members.is_empty()
                    && data
                        .members
                        .iter()
                        .all(|m| self.gate_residual_owners.contains(m))
                    && !data.is_pre_existing_module;
                if only_gate_residual {
                    residual_module
                } else {
                    let m = ModuleId::logical(next_module_idx);
                    next_module_idx += 1;
                    m
                }
            };
            class_module_id.insert(c, module);
        }
        let partition_assignments: Vec<ModuleId> = (0..self.owner_graph.num_nodes())
            .map(|owner_idx| {
                let c = self.owner_to_class[owner_idx];
                class_module_id.get(&c).copied().unwrap_or(residual_module)
            })
            .collect();
        let partition = Partition::from_assignments(partition_assignments, residual_module);
        self.realizability_index = RealizabilityIndex::from_partition(&self.owner_graph, partition);
        self.class_module_id = class_module_id;
        self.next_module_idx = next_module_idx;
    }

    /// The `ModuleId` the surviving (winner) class would map to after
    /// `winner` absorbs `loser`. Mirrors `project_partition`'s
    /// projection logic but evaluated against the **post-merge**
    /// class composition (members of both winner and loser combined).
    fn projected_winner_module_after_merge(&self, winner: ClassId, loser: ClassId) -> ModuleId {
        let residual_module = ModuleId::logical(0);
        let winner_data = &self.classes[winner.0];
        let loser_data = &self.classes[loser.0];
        // Residual stickiness: if either side carries the literal
        // residual catch-all, the merged class is residual.
        // (`check_merge` only allows the merge when residual matches
        // on both sides, but we evaluate symmetrically here in case
        // we're called speculatively.)
        if winner_data.is_residual || loser_data.is_residual {
            return residual_module;
        }
        let is_pre_existing =
            winner_data.is_pre_existing_module || loser_data.is_pre_existing_module;
        if is_pre_existing {
            // Pre-existing-module classes get a stable non-residual
            // ModuleId; reuse the winner's slot if it has one,
            // otherwise mint a fresh idx.
            return self
                .class_module_id
                .get(&winner)
                .copied()
                .filter(|m| *m != residual_module)
                .or_else(|| {
                    self.class_module_id
                        .get(&loser)
                        .copied()
                        .filter(|m| *m != residual_module)
                })
                .unwrap_or_else(|| ModuleId::logical(self.next_module_idx));
        }
        // Both sides are non-pre-existing, non-residual. The
        // projection collapses a class to residual iff every member
        // is in `gate_residual_owners`. The post-merge class's
        // members are winner.members ∪ loser.members.
        let all_gate_residual = winner_data
            .members
            .iter()
            .chain(loser_data.members.iter())
            .all(|m| self.gate_residual_owners.contains(m));
        if all_gate_residual {
            return residual_module;
        }
        // Mixed (some non-gate-residual owner present). Reuse the
        // winner's non-residual slot if it has one; otherwise reuse
        // the loser's; otherwise mint a fresh idx.
        self.class_module_id
            .get(&winner)
            .copied()
            .filter(|m| *m != residual_module)
            .or_else(|| {
                self.class_module_id
                    .get(&loser)
                    .copied()
                    .filter(|m| *m != residual_module)
            })
            .unwrap_or_else(|| ModuleId::logical(self.next_module_idx))
    }

    /// Compute the `PartitionDelta::MoveOwners` deltas needed to
    /// transition the realizability index's working partition to the
    /// post-merge projection for `(winner, loser)`. Returns the
    /// post-merge winner `ModuleId` (which may differ from
    /// `class_module_id[winner]` when a gate-residual transition
    /// promotes the merged class from residual to non-residual).
    ///
    /// At most two deltas are produced:
    /// 1. `loser`'s owners → post-merge winner module.
    /// 2. `winner`'s owners → post-merge winner module (only if the
    ///    winner's module changed).
    fn compute_merge_deltas(
        &self,
        winner: ClassId,
        loser: ClassId,
    ) -> (ModuleId, Vec<PartitionDelta>) {
        let post_module = self.projected_winner_module_after_merge(winner, loser);
        let mut deltas: Vec<PartitionDelta> = Vec::new();
        // Loser members move to post_module (if their current
        // assignment differs).
        let loser_members: Vec<OwnerId> = self.classes[loser.0]
            .members
            .iter()
            .filter(|m| self.realizability_index.partition().of(OwnerId(m.0)) != post_module)
            .map(|m| OwnerId(m.0))
            .collect();
        if !loser_members.is_empty() {
            deltas.push(PartitionDelta::MoveOwners {
                owners: loser_members,
                to: post_module,
            });
        }
        // Winner members move iff post_module differs from winner's
        // current module (gate-residual promotion case).
        let winner_current = self
            .class_module_id
            .get(&winner)
            .copied()
            .unwrap_or(ModuleId::logical(0));
        if winner_current != post_module {
            let winner_members: Vec<OwnerId> = self.classes[winner.0]
                .members
                .iter()
                .filter(|m| self.realizability_index.partition().of(OwnerId(m.0)) != post_module)
                .map(|m| OwnerId(m.0))
                .collect();
            if !winner_members.is_empty() {
                deltas.push(PartitionDelta::MoveOwners {
                    owners: winner_members,
                    to: post_module,
                });
            }
        }
        (post_module, deltas)
    }

    /// Update class adjacency after `loser` is absorbed into `winner`.
    /// Relabels all loser-incident entries to winner, dropping self-
    /// loops. O(|out_edges[loser.0]| + |in_neighbors[loser.0]|) —
    /// typically small for the commit-2 orphan shape.
    ///
    /// Walks `loser`'s outgoing edges first, folding each `(loser, x)`
    /// into `(winner, x)` by summing `EdgeState` fields and
    /// maintaining `in_neighbors[x]`. Then symmetrically walks
    /// `in_neighbors[loser.0]` and relabels each `(predecessor, loser)`
    /// to `(predecessor, winner)`. `EdgeState` carries the constraining
    /// and weighted counts together, so a single relabel pass updates
    /// all bookkeeping atomically (the prior implementation split this
    /// into `update_class_adjacency_after_merge` +
    /// `relabel_weighted_edges_after_merge`).
    fn update_class_adjacency_after_merge(&mut self, winner: ClassId, loser: ClassId) {
        // Drain loser's outgoing edges. The slot is left empty so the
        // dead-class invariant holds.
        let loser_outs = std::mem::take(&mut self.out_edges[loser.0]);
        for (x, state) in loser_outs {
            // Detach loser from x's in-set; we'll re-attach winner
            // below if it isn't a self-loop after merge.
            self.in_neighbors[x.0].remove(&loser);
            if x == winner {
                // (loser, winner) -> intra-class self-loop after the
                // contract; drop entirely.
                continue;
            }
            let dest = self.out_edges[winner.0].entry(x).or_default();
            dest.constraining_count += state.constraining_count;
            dest.weighted_count += state.weighted_count;
            dest.weighted_sum += state.weighted_sum;
            self.in_neighbors[x.0].insert(winner);
        }
        // Drain loser's incoming back-pointers. Look up each
        // (predecessor, loser) edge state on `out_edges[predecessor.0]`
        // and fold it into (predecessor, winner). A predecessor equal
        // to `winner` collapses into a self-loop and is dropped.
        let loser_ins = std::mem::take(&mut self.in_neighbors[loser.0]);
        for predecessor in loser_ins {
            // Remove the (predecessor, loser) entry; the matching
            // in_neighbors[loser.0] slot was already drained above.
            let Some(state) = self.out_edges[predecessor.0].remove(&loser) else {
                // Should not happen — invariant violation. Skip
                // defensively.
                debug_assert!(
                    false,
                    "in_neighbors[{}] listed {} but out_edges[{}][{}] missing",
                    loser.0, predecessor.0, predecessor.0, loser.0
                );
                continue;
            };
            if predecessor == winner {
                // (winner, loser) -> intra-class self-loop after
                // merge; drop.
                continue;
            }
            // (predecessor, loser) -> (predecessor, winner).
            // in_neighbors[winner] gets the predecessor as a source.
            let dest = self.out_edges[predecessor.0].entry(winner).or_default();
            dest.constraining_count += state.constraining_count;
            dest.weighted_count += state.weighted_count;
            dest.weighted_sum += state.weighted_sum;
            self.in_neighbors[winner.0].insert(predecessor);
        }
        // Post-merge invariants: dead class has no edges in either
        // direction.
        debug_assert!(self.out_edges[loser.0].is_empty());
        debug_assert!(self.in_neighbors[loser.0].is_empty());
        #[cfg(debug_assertions)]
        self.debug_assert_edge_invariants();
    }

    /// `true` if a class is **pre-existing module-anchored** —
    /// i.e., it was constructed from a `PartitionGroup` with
    /// `is_pre_existing_module = true`, or marked by the seeding
    /// protocol's spec-module pass. The greedy uses this to
    /// restrict commit-2 merges to "extend module by orphan" and
    /// commit-3 merges to module↔module fusions.
    pub fn class_is_pre_existing_module(&self, c: ClassId) -> bool {
        self.classes[c.0].is_pre_existing_module
    }

    /// Mark a class as pre-existing-module-anchored. Called by the
    /// seeding protocol's spec-module pass; sticky across merges
    /// (any subsequent contraction propagates the bit).
    ///
    /// Setting this bit can change the class's `ModuleId` projection
    /// — a previously gate-residual-only class is promoted from the
    /// residual module to a fresh non-residual one (the
    /// `gate_residual_owners` override in `project_partition`
    /// short-circuits on `is_pre_existing_module=true`). The
    /// realizability index is synced via a `MoveOwners` delta when
    /// the transition fires.
    pub fn set_class_pre_existing_module(&mut self, c: ClassId) {
        if self.classes[c.0].is_pre_existing_module {
            return;
        }
        self.classes[c.0].is_pre_existing_module = true;
        // Promotion shifts the class's ModuleId from residual to a
        // fresh non-residual idx iff it was previously residual-mapped
        // AND the class is not the literal residual catchall.
        let residual_module = ModuleId::logical(0);
        let current = self
            .class_module_id
            .get(&c)
            .copied()
            .unwrap_or(residual_module);
        if current != residual_module || self.classes[c.0].is_residual {
            return;
        }
        let new_module = ModuleId::logical(self.next_module_idx);
        self.next_module_idx += 1;
        let owners_to_move: Vec<OwnerId> = self.classes[c.0]
            .members
            .iter()
            .map(|m| OwnerId(m.0))
            .collect();
        if !owners_to_move.is_empty() {
            self.realizability_index.push(
                &self.owner_graph,
                PartitionDelta::MoveOwners {
                    owners: owners_to_move,
                    to: new_module,
                },
            );
            // Permanent push — drop its rollback state (see
            // `merge_classes_unchecked`).
            self.realizability_index.commit();
        }
        self.class_module_id.insert(c, new_module);
    }

    /// Number of owner edges between `a` and `b` (in either
    /// direction), as constraining-edge multiplicities. Used by the
    /// coupling metric and `mergeable_commit2_preconditions`'s
    /// cross-edge presence check.
    fn cross_edge_count(&self, a: ClassId, b: ClassId) -> u64 {
        let ab = self.out_edges[a.0]
            .get(&b)
            .map_or(0, |e| e.constraining_count);
        let ba = self.out_edges[b.0]
            .get(&a)
            .map_or(0, |e| e.constraining_count);
        (ab + ba) as u64
    }

    fn coupling_weight(&self, a: ClassId, b: ClassId) -> u64 {
        let ab = self.out_edges[a.0].get(&b).map_or(0, |e| e.weighted_sum);
        let ba = self.out_edges[b.0].get(&a).map_or(0, |e| e.weighted_sum);
        ab + ba
    }

    fn class_out_count(&self, c: ClassId) -> u64 {
        // Sum of `weighted_count` across all out-edges. Matches the
        // prior `class_out_edge_count[c]` invariant
        // (= total owner edges originating in c's members and crossing
        // class boundaries). Computed on demand; the per-class
        // out-degree is small in practice.
        self.out_edges[c.0]
            .values()
            .map(|e| e.weighted_count as u64)
            .sum()
    }

    /// All neighboring classes of `c` reachable through a
    /// **constraining** edge (out + in directions, deduplicated).
    /// Used by the greedy to enumerate candidate merge partners
    /// restricted to classes connected by a constraining cross-edge.
    ///
    /// Returns a borrowed iterator — no allocation. Out-side is
    /// yielded first in `FxHashMap` iteration order; then in-side
    /// entries that are not already on the out-side are yielded.
    /// Per-element dedup is O(1) average via `FxHashMap::contains_key`.
    ///
    /// **Constraining-only:** the unified `EdgeState` carries both
    /// counts; this method filters by `constraining_count > 0` on the
    /// out side and looks up the source's outbound `EdgeState` on the
    /// in side. The prior 7-field implementation used
    /// `class_out` + `class_in` (both constraining-only). Byte-
    /// identical output depends on preserving this semantic — e.g.,
    /// `mergeable_commit2_preconditions`' `module_neighbors` count
    /// would otherwise pick up weighted-only neighbors and shift the
    /// "unambiguous extension target" verdict.
    fn class_neighbors(&self, c: ClassId) -> impl Iterator<Item = ClassId> + '_ {
        let out_map = &self.out_edges[c.0];
        let in_set = &self.in_neighbors[c.0];
        let out_iter = out_map
            .iter()
            .filter(|(_, e)| e.constraining_count > 0)
            .map(|(&n, _)| n);
        let in_iter = in_set.iter().copied().filter(move |s| {
            // Drop if already yielded on the out side, and drop
            // weighted-only predecessors.
            self.out_edges[s.0]
                .get(&c)
                .is_some_and(|e| e.constraining_count > 0)
                && out_map.get(s).is_none_or(|e| e.constraining_count == 0)
        });
        out_iter.chain(in_iter)
    }
}

// ---------------------------------------------------------------------
// Greedy merge to convergence (commit 2).
// ---------------------------------------------------------------------

/// Commit-3 mergeability gate. Allows two shapes:
///   1. Two pre-existing-module classes (merge modules A and B).
///      May happen with or without first absorbing residual orphans
///      into either side via successive shape-(2) merges.
///   2. One pre-existing-module class + one orphan residual class
///      (extend module A with an orphan); requires the orphan's
///      cross-edges to pre-existing modules to target exactly one
///      module — the merge partner. The "unambiguous extension"
///      check matches today's
///      `promote_anonymous_only_cell_to_extension` post-pass.
///
/// Orphan↔orphan merges are NOT permitted by this gate: today's
/// cell-discovery pass already closes residual atomic-DAG
/// reachability into single classes, so any orphan↔orphan grouping
/// that should happen is already represented as a single class
/// pre-greedy. Allowing orphan↔orphan here would let greedy fuse
/// unrelated residuals based purely on cross-edge presence, which
/// is over-aggressive on real inputs.
///
/// Common preconditions:
/// - Distinct classes.
/// - Neither is the residual catchall.
/// - At least one cross-edge connects the two.
/// - Combined lines under the cap (`merge_preserves_invariants`).
/// - Cycle gate holds (`merge_preserves_invariants`).
pub fn mergeable_commit2(q: &QuotientGraph, c1: ClassId, c2: ClassId) -> bool {
    if !mergeable_commit2_preconditions(q, c1, c2) {
        return false;
    }
    q.merge_preserves_invariants(c1, c2)
}

/// Cheap mergeability preconditions: every clause of `mergeable_commit2`
/// EXCEPT the final `merge_preserves_invariants` verdict. Splitting
/// this out lets the lazy-PQ driver run the cheap preconditions
/// before the (expensive) verdict — and lets coupling-drift
/// detection happen between the two.
fn mergeable_commit2_preconditions(q: &QuotientGraph, c1: ClassId, c2: ClassId) -> bool {
    if c1 == c2 {
        return false;
    }
    // Residual is sticky.
    if q.class_is_residual(c1) || q.class_is_residual(c2) {
        return false;
    }
    // Connected by at least one cross-edge.
    if q.cross_edge_count(c1, c2) == 0 {
        return false;
    }
    let pre1 = q.class_is_pre_existing_module(c1);
    let pre2 = q.class_is_pre_existing_module(c2);
    // At least one operand must be a pre-existing-module class.
    if !pre1 && !pre2 {
        return false;
    }
    // Shape (2): orphan + module → require unambiguous extension
    // target. If the orphan touches multiple modules via cross-edges,
    // the spec author must disambiguate; the greedy refuses.
    if pre1 != pre2 {
        let orphan = if pre1 { c2 } else { c1 };
        let mut module_neighbors: usize = 0;
        for n in q.class_neighbors(orphan) {
            if n == orphan {
                continue;
            }
            if q.class_is_pre_existing_module(n) && !q.class_is_residual(n) {
                module_neighbors += 1;
            }
        }
        if module_neighbors != 1 {
            return false;
        }
    }
    // Shape (1): pre↔pre — no additional precondition beyond the
    // common checks above.
    true
}

/// One pass of the greedy: enumerate candidate merges, pick the best,
/// apply. Returns `None` at convergence (no candidates).
///
/// Implementation: full O(|V|+|E|) scan of candidates each call. The
/// production `greedy_merge_to_convergence` uses the lazy-PQ driver
/// (`greedy_merge_to_convergence_lazy_pq`); this entrypoint is kept
/// for callers/tests that want a single-step driver and for the
/// reference `greedy_merge_to_convergence_full_scan` byte-equality
/// gate.
pub fn greedy_step(q: &mut QuotientGraph) -> Option<GreedyStep> {
    let candidate = pick_best_candidate(q)?;
    let (a, b) = candidate.pair;
    let survivor = q.contract(a, b).ok()?;
    Some(GreedyStep {
        picked: (a.min(b), a.max(b)),
        surviving: survivor,
    })
}

/// Run the greedy contraction loop to convergence. Returns the
/// sequence of (c1, c2) contractions in the order they were applied.
/// Each returned pair uses canonical (lower, higher) ClassId order —
/// the surviving class is always the lower of the two.
///
/// Driver: lazy priority-queue candidate enumeration. The PQ is
/// initialized once over all cross-class pairs; each contract pushes
/// fresh entries for the winner's new neighborhood and pop-time
/// staleness checks re-rank entries whose coupling has drifted. See
/// `plans/peel_lazy_pq_greedy.md` for the algorithm and
/// `greedy_merge_to_convergence_full_scan` for the byte-equal
/// reference driver retained as a correctness gate.
pub fn greedy_merge_to_convergence(q: &mut QuotientGraph) -> Vec<(ClassId, ClassId)> {
    greedy_merge_to_convergence_lazy_pq(q)
}

/// Reference implementation of the greedy loop using a per-iteration
/// full scan. Retained as the load-bearing correctness reference for
/// the property test
/// `lazy_pq_greedy_matches_full_scan_greedy_on_corpus`. Do not call
/// from production code — `O(|V|·|E|)` outer shape is the reason the
/// lazy-PQ driver exists.
#[doc(hidden)]
pub fn greedy_merge_to_convergence_full_scan(q: &mut QuotientGraph) -> Vec<(ClassId, ClassId)> {
    let mut steps: Vec<(ClassId, ClassId)> = Vec::new();
    while let Some(step) = greedy_step(q) {
        steps.push(step.picked);
    }
    steps
}

// ---------------------------------------------------------------------
// Lazy priority-queue greedy driver.
//
// See `plans/peel_lazy_pq_greedy.md` for the spec. The PQ stores one
// entry per unordered cross-class pair, ordered by the same sort key
// `pick_best_candidate` uses. On pop we re-check class existence,
// mergeability, and coupling drift; on a successful contract we push
// fresh entries for the winner's new neighborhood and drain the
// discard pile (transiently-failing entries from this iteration).
// ---------------------------------------------------------------------

/// One entry in the lazy-PQ candidate queue. The ordering is on
/// `Reverse<sort_key>` so `BinaryHeap` (a max-heap) returns the
/// smallest sort_key first — matching `pick_best_candidate`'s
/// "lower is better" semantics. `c1`/`c2` are stored in canonical
/// order (low, high) for class-existence checks; `stored_sort_key`
/// equals the sort key at push time and is compared to a freshly
/// computed key on pop to detect coupling/cycle-score drift.
#[derive(Debug, Clone, Copy, Eq, PartialEq)]
struct CandidateEntry {
    /// Reverse-wrapped 33-byte sort key. BinaryHeap pops the largest
    /// `Ord` value; `Reverse<sort_key>` means "largest reversed" =
    /// "smallest sort_key" pops first.
    ordering: Reverse<[u8; 33]>,
    /// Canonical low ClassId of the pair.
    c1: ClassId,
    /// Canonical high ClassId of the pair.
    c2: ClassId,
}

impl Ord for CandidateEntry {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        // BinaryHeap pops the maximum. We want smallest sort_key
        // first; `Reverse` flips it. Tiebreak on (c1, c2) is already
        // encoded in the sort_key tail, so a tie here means the
        // entries are literally for the same pair (a duplicate
        // re-push at the same key).
        self.ordering.cmp(&other.ordering)
    }
}

impl PartialOrd for CandidateEntry {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}

/// True if both classes still exist (non-empty members). Used as the
/// class-existence guard at step 1 of the pop loop.
fn classes_alive(q: &QuotientGraph, c1: ClassId, c2: ClassId) -> bool {
    !q.classes[c1.0].members.is_empty() && !q.classes[c2.0].members.is_empty()
}

/// Push one entry per unordered cross-class pair (low, high) with
/// `low < high` and a non-zero constraining-edge multiplicity. The
/// candidate space matches `pick_best_candidate`'s iteration: anchored
/// at pre-existing-module classes, neighbor reached via the
/// constraining adjacency. Mergeability filtering is deferred to pop
/// time so non-monotone gate failures (e.g., the "unambiguous
/// extension target" rule in `mergeable_commit2`) get retried after
/// later contracts unlock them.
fn initialize_candidate_queue(q: &QuotientGraph) -> BinaryHeap<CandidateEntry> {
    let mut heap: BinaryHeap<CandidateEntry> = BinaryHeap::new();
    // Pre-existing-module classes are the iteration anchor — same as
    // `pick_best_candidate`. A pair (a, b) is anchored iff at least
    // one side is pre-existing-module non-residual; we iterate from
    // the pre-existing side to avoid double-pushing symmetric pairs.
    let anchors: Vec<ClassId> = q
        .iter_classes()
        .filter(|c| q.class_is_pre_existing_module(*c) && !q.class_is_residual(*c))
        .collect();
    let mut seen: BTreeSet<(ClassId, ClassId)> = BTreeSet::new();
    for c in anchors {
        let neighbors = q.class_neighbors(c);
        for n in neighbors {
            if n == c {
                continue;
            }
            let (low, high) = if c < n { (c, n) } else { (n, c) };
            if !seen.insert((low, high)) {
                continue;
            }
            let candidate = rank_candidate(q, low, high);
            heap.push(CandidateEntry {
                ordering: Reverse(candidate.sort_key),
                c1: low,
                c2: high,
            });
        }
    }
    heap
}

/// Push fresh `(winner, X)` entries for every X in winner's current
/// cross-class neighborhood. Called after each successful contract.
/// Step 3 of the pop loop catches coupling drift on other pairs
/// lazily — we don't eagerly re-push X's other neighbors.
fn repush_affected_neighborhood(
    q: &QuotientGraph,
    heap: &mut BinaryHeap<CandidateEntry>,
    winner: ClassId,
) {
    for n in q.class_neighbors(winner) {
        if n == winner {
            continue;
        }
        let (low, high) = if winner < n { (winner, n) } else { (n, winner) };
        let candidate = rank_candidate(q, low, high);
        heap.push(CandidateEntry {
            ordering: Reverse(candidate.sort_key),
            c1: low,
            c2: high,
        });
    }
}

/// Lazy-PQ greedy driver. Same output as
/// `greedy_merge_to_convergence_full_scan` modulo the byte-equality
/// gate. See `plans/peel_lazy_pq_greedy.md` for the algorithm.
fn greedy_merge_to_convergence_lazy_pq(q: &mut QuotientGraph) -> Vec<(ClassId, ClassId)> {
    let mut steps: Vec<(ClassId, ClassId)> = Vec::new();
    let mut heap = initialize_candidate_queue(q);
    loop {
        let mut discard_pile: Vec<CandidateEntry> = Vec::new();
        let mut committed_winner: Option<ClassId> = None;
        while let Some(entry) = heap.pop() {
            // 1. Class-existence guard. Once a ClassId is the loser
            //    of a contract, its `members` are emptied; it never
            //    comes back.
            if !classes_alive(q, entry.c1, entry.c2) {
                continue;
            }

            // 2. Cheap mergeability preconditions (non-monotone).
            //    Residual stickiness, cross-edge presence, the
            //    pre-existing-module-anchor rule, and the
            //    unambiguous-extension rule. The unambiguous-extension
            //    clause especially can flip from false to true after
            //    an unrelated contract (when an orphan's competing
            //    module neighbor merges away), so we stash on the
            //    discard pile rather than dropping the entry
            //    permanently.
            if !mergeable_commit2_preconditions(q, entry.c1, entry.c2) {
                discard_pile.push(entry);
                continue;
            }

            // 3. Lazy coupling/cycle-score drift detection. The sort
            //    key depends on coupling_weight, class_out_count, and
            //    the cycle index — all of which can change when an
            //    unrelated merge collapses a shared neighbor. We
            //    recompute the full sort key and, if it differs from
            //    the stored value, re-push at the new key and continue
            //    popping. The true max-coupling surfaces because a
            //    stale-high entry cannot sit at the top.
            let candidate = rank_candidate(q, entry.c1, entry.c2);
            if candidate.sort_key != entry.ordering.0 {
                heap.push(CandidateEntry {
                    ordering: Reverse(candidate.sort_key),
                    c1: entry.c1,
                    c2: entry.c2,
                });
                continue;
            }

            // 4. Verdict check (non-monotone). The dominant inner
            //    cost — deferred until we're about to commit. A
            //    cycle through (c1, c2) can be broken by a merge
            //    elsewhere along the cycle, so verdict failures go
            //    to the discard pile to get retried after the next
            //    successful contract.
            if !q.merge_preserves_invariants(entry.c1, entry.c2) {
                discard_pile.push(entry);
                continue;
            }

            // 5. Commit. `contract` re-runs `check_merge`, which
            //    repeats the verdict we just confirmed; succeeds
            //    except in racy edge cases (none today). On the off
            //    chance it fails we treat it as a non-monotone
            //    failure (discard-pile) for the same reasons as step
            //    4.
            match q.contract(entry.c1, entry.c2) {
                Ok(winner) => {
                    steps.push((entry.c1, entry.c2));
                    committed_winner = Some(winner);
                    break;
                }
                Err(_) => {
                    discard_pile.push(entry);
                    continue;
                }
            }
        }

        match committed_winner {
            Some(winner) => {
                // State just changed: push fresh entries for the
                // winner's new neighborhood and drain the discard pile
                // back into the queue. Every transiently-failing
                // candidate gets a fresh chance to commit.
                repush_affected_neighborhood(q, &mut heap, winner);
                for entry in discard_pile.drain(..) {
                    heap.push(entry);
                }
            }
            None => {
                // Inner loop exited because the queue is empty
                // without a commit. No state has changed since the
                // discarded entries failed — re-checking would fail
                // the same way. Drop the pile and terminate.
                break;
            }
        }
    }
    steps
}

/// Ranked candidate for `pick_best`.
#[derive(Debug, Clone, Copy)]
struct RankedCandidate {
    /// Canonical pair (lower ClassId, higher ClassId).
    pair: (ClassId, ClassId),
    /// `pick_best` sort key (lower is better). Construction:
    /// - byte 0 (most significant): inverse of "cycle-set
    ///   reduction" — 0 if the merge strictly reduces the cycle set,
    ///   1 otherwise. Vestigial in normal flow (seed is realizable);
    ///   tiebreaker for unrealizable seeds.
    /// - bytes 1..9: inverse of coupling-numerator (so higher
    ///   coupling sorts earlier).
    /// - bytes 9..17: result-size (lines), smaller first.
    /// - bytes 17..25: canonical pair (a, b) lex.
    sort_key: [u8; 33],
}

fn pick_best_candidate(q: &QuotientGraph) -> Option<RankedCandidate> {
    // Enumerate candidate pairs: any (c, n) where c is a pre-existing
    // module class and n is a non-pre-existing non-residual neighbor.
    // The mergeable_commit2 gate is the source of truth; we use the
    // pre-existing-module side as the iteration anchor so we don't
    // re-evaluate symmetric pairs twice.
    let anchors: Vec<ClassId> = q
        .iter_classes()
        .filter(|c| q.class_is_pre_existing_module(*c) && !q.class_is_residual(*c))
        .collect();
    let mut best: Option<RankedCandidate> = None;
    for c in anchors {
        let neighbors: Vec<ClassId> = q.class_neighbors(c).collect();
        for n in neighbors {
            if n == c {
                continue;
            }
            if !mergeable_commit2(q, c, n) {
                continue;
            }
            let candidate = rank_candidate(q, c, n);
            best = match best {
                None => Some(candidate),
                Some(prev) if candidate.sort_key < prev.sort_key => Some(candidate),
                Some(prev) => Some(prev),
            };
        }
    }
    best
}

fn rank_candidate(q: &QuotientGraph, a: ClassId, b: ClassId) -> RankedCandidate {
    let (low, high) = if a < b { (a, b) } else { (b, a) };
    let mut key = [0u8; 33];

    // Cycle-reduction key: 0 if the merge dissolves part of an
    // unrealizable SCC — i.e. both classes' modules sit in the same
    // multi-module SCC of the maintained constraining condensation —
    // 1 otherwise. On realizable committed states (the normal
    // greedy regime) Pass 1 is clean, so no multi-module constraining
    // SCC exists and the key is 1 for every pair — matching the
    // deleted `cached_cycles` probe, which was empty on healthy
    // corpora. On unrealizable seeds the ranking may drift from the
    // (deliberately stale) cache the old probe read; accepted per the
    // plan's open question 5.
    let reduces = match (q.class_module_id.get(&low), q.class_module_id.get(&high)) {
        (Some(&m_low), Some(&m_high)) => q
            .realizability_index
            .modules_share_constraining_multi_scc(m_low, m_high),
        _ => false,
    };
    key[0] = if reduces { 0 } else { 1 };

    // Coupling: higher = better → invert.
    let coupling_num = q.coupling_weight(low, high);
    let coupling_denom = q.class_out_count(low).min(q.class_out_count(high)).max(1);
    // Encode as 16-bit fixed-point: scale by 1e6 to keep precision.
    let coupling_fixed: u64 =
        ((coupling_num as u128) * 1_000_000 / (coupling_denom as u128)) as u64;
    let inv_coupling: u64 = u64::MAX - coupling_fixed;
    key[1..9].copy_from_slice(&inv_coupling.to_be_bytes());

    // Result size (lines) — smaller better, natural order.
    let combined_lines = (q.class_lines(low) + q.class_lines(high)) as u64;
    key[9..17].copy_from_slice(&combined_lines.to_be_bytes());

    // Canonical pair lex.
    key[17..25].copy_from_slice(&(low.0 as u64).to_be_bytes());
    key[25..33].copy_from_slice(&(high.0 as u64).to_be_bytes());

    RankedCandidate {
        pair: (low, high),
        sort_key: key,
    }
}

/// Estimate of one owner's line count from the JSON report node.
/// Mirrors `peel::factorize::owner_line_count`.
fn owner_line_count_from_report(node: &analysis::OwnerGraphNodeReport) -> usize {
    node.source_location
        .as_ref()
        .map(|loc| {
            loc.end_line
                .saturating_sub(loc.start_line)
                .saturating_add(1)
        })
        .unwrap_or(0)
}

/// Apply the seeding protocol: atomic units first, then spec modules.
/// Each forced contraction is gated by
/// `merge_preserves_invariants`; rejected contractions push a
/// `SeedContractionRejected` diagnostic into the returned vec and the
/// kernel continues with the remaining contractions.
///
/// `canonical_order` matches the plan: atomic units by lowest
/// `OwnerIdx` member (then unit id for ties); spec modules by module
/// path lex (then module id for ties). Within a group, members merge
/// into the lowest-`OwnerIdx` pivot in `OwnerIdx` order.
pub fn build_seed_quotient(
    report: &OwnerGraphReport,
    atomic_units: &[analysis::AtomicUnitReport],
    spec_modules: &[SpecModuleGroup],
    cap_lines: usize,
) -> Result<(QuotientGraph, Vec<SeedContractionRejected>), analysis::UnresolvedOwnerEdgeEndpoint> {
    let mut q = QuotientGraph::from_report(report, cap_lines)?;
    let mut rejected = Vec::<SeedContractionRejected>::new();

    // ---- Pass 1: atomic units. Canonical order: lowest OwnerIdx
    //      member, then unit id.
    let mut units: Vec<(&analysis::AtomicUnitReport, Option<OwnerIdx>)> = atomic_units
        .iter()
        .map(|unit| {
            (
                unit,
                lowest_owner_idx(&q, unit.owner_ids.iter().map(String::as_str)),
            )
        })
        .collect();
    units.sort_by(|(a, ai), (b, bi)| ai.cmp(bi).then_with(|| a.id.cmp(&b.id)));
    for (unit, _) in units {
        let owner_idxs = resolve_owner_idxs(&q, &unit.owner_ids);
        if owner_idxs.len() < 2 {
            continue;
        }
        let pivot = owner_idxs[0];
        for &member in &owner_idxs[1..] {
            let c_pivot = q.class_of(pivot);
            let c_member = q.class_of(member);
            if c_pivot == c_member {
                continue;
            }
            match q.contract(c_pivot, c_member) {
                Ok(_) => {}
                Err(ContractRejected::WouldCreateCycle { cycle }) => {
                    rejected.push(SeedContractionRejected::AtomicUnit {
                        unit_id: unit.id.clone(),
                        owner_ids: unit.owner_ids.clone(),
                        rejected_pair: (
                            q.owner_id(pivot).to_string(),
                            q.owner_id(member).to_string(),
                        ),
                        cycle,
                    });
                }
                Err(_) => {
                    rejected.push(SeedContractionRejected::AtomicUnit {
                        unit_id: unit.id.clone(),
                        owner_ids: unit.owner_ids.clone(),
                        rejected_pair: (
                            q.owner_id(pivot).to_string(),
                            q.owner_id(member).to_string(),
                        ),
                        cycle: CycleEvidence::default(),
                    });
                }
            }
        }
    }

    // ---- Pass 2: spec modules. Canonical order: module id lex.
    //      Every spec-module owner's surviving class is marked
    //      `is_pre_existing_module = true` so the downstream greedy
    //      can identify it as a viable absorption target (single-
    //      owner modules included).
    let mut modules: Vec<&SpecModuleGroup> = spec_modules.iter().collect();
    modules.sort_by(|a, b| a.module_id.cmp(&b.module_id));
    for module in modules {
        let owner_idxs = resolve_owner_idxs(&q, &module.owner_ids);
        if owner_idxs.is_empty() {
            continue;
        }
        let pivot = owner_idxs[0];
        for &member in owner_idxs.iter().skip(1) {
            let c_pivot = q.class_of(pivot);
            let c_member = q.class_of(member);
            if c_pivot == c_member {
                continue;
            }
            match q.contract(c_pivot, c_member) {
                Ok(_) => {}
                Err(ContractRejected::WouldCreateCycle { cycle }) => {
                    rejected.push(SeedContractionRejected::SpecModule {
                        module_id: module.module_id.clone(),
                        owner_ids: module.owner_ids.clone(),
                        rejected_pair: (
                            q.owner_id(pivot).to_string(),
                            q.owner_id(member).to_string(),
                        ),
                        cycle,
                    });
                }
                Err(_) => {
                    rejected.push(SeedContractionRejected::SpecModule {
                        module_id: module.module_id.clone(),
                        owner_ids: module.owner_ids.clone(),
                        rejected_pair: (
                            q.owner_id(pivot).to_string(),
                            q.owner_id(member).to_string(),
                        ),
                        cycle: CycleEvidence::default(),
                    });
                }
            }
        }
        // Mark every spec-module owner's surviving class as
        // pre-existing-module-anchored (sticky across later
        // pass-3 / greedy merges; needed for the greedy gate to
        // recognize the orphan-absorption shape).
        for &owner in &owner_idxs {
            let c = q.class_of(owner);
            q.set_class_pre_existing_module(c);
        }
    }

    // ---- Pass 3: atomic-DAG reachability. For each atomic-DAG
    //      edge `u → v` whose target unit has any residual member,
    //      contract `class(rep(u))` with `class(rep(v))` through
    //      the gated protocol. Subsumes today's
    //      `proposal_cells_from_atomic_graph` (atomic-DAG
    //      transitive closure + overlap coalesce) by reading the
    //      same edge set, but rejections fire at the per-edge
    //      granularity instead of silently forming cyclic cells.
    //
    //      Overlap coalesce: when two edges `u₁ → v` and `u₂ → v`
    //      both contract into the same target class, the second
    //      contraction sees the merged class (because the kernel's
    //      `class_of` is read after the first contract). The
    //      effect equivalent to today's `coalesce_overlapping_sets`
    //      falls out for free.
    //
    //      Iteration to fixed point: a single linear scan over
    //      atomic edges is enough for the success case (contractions
    //      commute when only merging). Cycle-driven rejections may
    //      become success after other merges (or remain rejections);
    //      we iterate until a pass produces zero successful
    //      contractions, then emit diagnostics from the final state.
    //      Termination: each successful pass strictly reduces the
    //      class count by ≥1; bounded by initial class count.
    //
    //      Diagnostic emission: dedupe by atomic-DAG edge id (each
    //      edge produces at most one diagnostic). Diagnostics are
    //      emitted in canonical (atomic-DAG edge id lex) order on
    //      the final, fixed-point quotient state.
    let atomic_edges_relevant: Vec<&analysis::AtomicUnitEdgeReport> = {
        let unit_has_residual: std::collections::HashMap<&str, bool> = atomic_units
            .iter()
            .map(|unit| {
                (
                    unit.id.as_str(),
                    unit.destinations
                        .iter()
                        .any(|dest| report.is_residual(dest)),
                )
            })
            .collect();
        let mut edges: Vec<&analysis::AtomicUnitEdgeReport> = report
            .atomic_graph
            .edges
            .iter()
            .filter(|e| e.constrains_init_order && e.source != e.target)
            .filter(|e| {
                unit_has_residual
                    .get(e.target.as_str())
                    .copied()
                    .unwrap_or(false)
            })
            .collect();
        edges.sort_by(|a, b| a.id.cmp(&b.id));
        edges
    };
    let unit_owner_idxs: std::collections::HashMap<&str, Vec<OwnerIdx>> = atomic_units
        .iter()
        .map(|unit| (unit.id.as_str(), resolve_owner_idxs(&q, &unit.owner_ids)))
        .collect();
    loop {
        let mut applied = 0usize;
        for edge in &atomic_edges_relevant {
            let Some(source_owners) = unit_owner_idxs.get(edge.source.as_str()) else {
                continue;
            };
            let Some(target_owners) = unit_owner_idxs.get(edge.target.as_str()) else {
                continue;
            };
            let (Some(&src_pivot), Some(&tgt_pivot)) =
                (source_owners.first(), target_owners.first())
            else {
                continue;
            };
            let cs = q.class_of(src_pivot);
            let ct = q.class_of(tgt_pivot);
            if cs == ct {
                continue;
            }
            if q.contract(cs, ct).is_ok() {
                applied += 1;
            }
        }
        if applied == 0 {
            break;
        }
    }
    // Walk edges once more to record diagnostics for the pairs that
    // still cannot merge at fixed point.
    for edge in &atomic_edges_relevant {
        let Some(source_owners) = unit_owner_idxs.get(edge.source.as_str()) else {
            continue;
        };
        let Some(target_owners) = unit_owner_idxs.get(edge.target.as_str()) else {
            continue;
        };
        let (Some(&src_pivot), Some(&tgt_pivot)) = (source_owners.first(), target_owners.first())
        else {
            continue;
        };
        let cs = q.class_of(src_pivot);
        let ct = q.class_of(tgt_pivot);
        if cs == ct {
            continue;
        }
        // Diagnostic-only: classify *why* this edge's pair did not
        // contract using the kernel's read-only predicates. This walk
        // must NOT commit a merge: a stray successful `contract` here
        // would mutate the partition the post-seed realizability gate
        // below reads, committing a merge that is never counted or
        // looped — silently corrupting the partition. The fixed-point
        // loop above already applied every legitimate contraction, so
        // any pair reaching here is expected to be rejected.
        //
        // We reproduce `check_merge`'s own classification order
        // (preconditions, then cycle gate) without committing:
        //   - preconditions fail -> the non-cycle `Err(_)` arm
        //     (ResidualSticky / ExceedsCap / SameClass),
        //   - cycle evidence     -> the `WouldCreateCycle` arm,
        //   - neither            -> the merge *would* have succeeded;
        //     unreachable at fixed point. Do nothing (and, critically,
        //     never commit it) — the old `Ok(_)` arm's stray mutation.
        if q.check_merge_preconditions(cs, ct).is_err() {
            rejected.push(SeedContractionRejected::AtomicReachability {
                edge_id: edge.id.clone(),
                source_unit_id: edge.source.clone(),
                target_unit_id: edge.target.clone(),
                rejected_pair: (
                    q.owner_id(src_pivot).to_string(),
                    q.owner_id(tgt_pivot).to_string(),
                ),
                cycle: CycleEvidence::default(),
            });
        } else if let Some(cycle) = q.would_be_cycles_after_contract(cs, ct) {
            rejected.push(SeedContractionRejected::AtomicReachability {
                edge_id: edge.id.clone(),
                source_unit_id: edge.source.clone(),
                target_unit_id: edge.target.clone(),
                rejected_pair: (
                    q.owner_id(src_pivot).to_string(),
                    q.owner_id(tgt_pivot).to_string(),
                ),
                cycle,
            });
        }
    }

    // ---- Post-seed: run the unified realizability gate once on
    //      the assembled partition. Catches asymmetric I-cycles
    //      and mutual constraining cycles that no individual
    //      contraction created on its own — the materializer
    //      catches these on `validate_factorization`, and Track A
    //      wires the planner's seed-rejection diagnostic to the
    //      same verdict so `plan-work` and `bazelisk build` agree
    //      on whether a spec is realizable.
    //
    //      One O(|V|+|E|) call, not |V|·|V| — the per-merge
    //      `would_be_cycles_after_contract` queries use the fast
    //      constraining-only cone check for the greedy's hot
    //      path. See the function's docstring for the perf
    //      trade-off (and docs/design.md's "Peel planner unification"
    //      section).
    let verdict = q.realizability_verdict();
    if !verdict.is_realizable() {
        let mut sccs_with_evidence: Vec<(BTreeSet<String>, CycleEvidence)> = Vec::new();
        // Translate verdict SCCs into kernel-shape evidence in
        // canonical sorted order.
        let partition = q.realizability_partition();
        for scc in &verdict.unrealizable_sccs {
            // Walk owners; bucket those whose current partition
            // assignment falls in this SCC's module set.
            let modules_in_scc: BTreeSet<analysis::ModuleId> =
                scc.modules.iter().copied().collect();
            let mut owners: BTreeSet<String> = BTreeSet::new();
            let mut class_set: BTreeSet<ClassId> = BTreeSet::new();
            for (owner_idx, owner_id) in q.owner_ids.iter().enumerate() {
                if owner_idx >= q.owner_graph.num_nodes() {
                    continue;
                }
                let module = partition.of(analysis::OwnerId(owner_idx));
                if !modules_in_scc.contains(&module) {
                    continue;
                }
                owners.insert(owner_id.clone());
                class_set.insert(q.owner_to_class[owner_idx]);
            }
            if owners.is_empty() {
                continue;
            }
            let mut classes: Vec<ClassId> = class_set.into_iter().collect();
            classes.sort();
            let evidence = CycleEvidence {
                cycles: vec![CycleClassSet {
                    classes,
                    owner_ids: owners.iter().cloned().collect(),
                }],
            };
            sccs_with_evidence.push((owners, evidence));
        }
        // Stable diagnostic order: sort by the SCC's owner-id set
        // lex.
        sccs_with_evidence.sort_by(|a, b| a.0.cmp(&b.0));
        for (owner_set, cycle) in sccs_with_evidence {
            let owner_ids: Vec<String> = owner_set.into_iter().collect();
            rejected.push(SeedContractionRejected::PostSeedUnrealizableScc { owner_ids, cycle });
        }
    }

    Ok((q, rejected))
}

fn resolve_owner_idxs(q: &QuotientGraph, owner_ids: &[String]) -> Vec<OwnerIdx> {
    let mut idxs: Vec<OwnerIdx> = owner_ids
        .iter()
        .filter_map(|id| q.owner_idx_of(id))
        .collect();
    idxs.sort();
    idxs
}

fn lowest_owner_idx<'a>(
    q: &QuotientGraph,
    owner_ids: impl Iterator<Item = &'a str>,
) -> Option<OwnerIdx> {
    owner_ids.filter_map(|id| q.owner_idx_of(id)).min()
}

// ---------- Compile-time guarantee: no public refinement op exists ----------
//
// The kernel API exposes `contract`, `merge_preserves_invariants`,
// `would_be_cycles_after_contract`, and accessor methods only. There
// is no `split`, no `un_contract`, no `set_class`, no method that
// takes `&mut self` other than `contract`. Adding one would have to be
// done deliberately by editing this file, at which point the
// reviewer's eye would catch it. This is the "easiest as a
// compile-time guarantee (no public method exists)" approach the
// plan calls for; the test `contract_never_un_contracts` in
// `quotient_integration_test.rs` exercises the post-condition.
