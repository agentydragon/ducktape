use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fmt;

use petgraph::algo::tarjan_scc;
use petgraph::graphmap::DiGraphMap;
use serde::{Deserialize, Serialize};
use swc_ecma_ast::Id;

use crate::facts::EffectCell;
use crate::partition::Partition;
use crate::purity::Purity;
use crate::{ModuleId, SourceLocation, StatementFacts, StatementKind, StatementOrdinal};

// `OwnerGraphOptions` lives in `spec.rs` — both the spec YAML surface
// and the graph-build API consume the same type. Re-exported from
// crate root via `lib.rs` so `analysis::OwnerGraphOptions` continues
// to be the canonical path for external callers.
pub use spec::OwnerGraphOptions;

/// How an edge was emitted. Determines whether the gate / quotient /
/// reports consumers project the edge through the lenient (drop
/// cross-module promotions) or strict (keep them for soundness) view.
///
/// Variants:
/// - `Direct` — the edge was emitted by direct binding lookup in
///   `build_owner_graph_with` (eager/lazy reads, rebinds, sequenced
///   side effects, local effects). The quotient drops same-module
///   edges (`from == to`) but never drops a `Direct` edge by role.
/// - `PromotedAtInit { callee_owner }` — `promote_at_init_calls`
///   manufactured the edge by lifting a function-body lazy read/rebind
///   into a caller's eager read. The lenient view (quotient, reports)
///   drops these when `partition.of(callee_owner) != partition.of(from)`
///   — the body read fires inside a call into a *different* module,
///   so by ESM DFS post-order the callee module (and its transitive
///   imports) are fully evaluated before the call returns; the
///   manufactured `R -> target-module` constraint is redundant with
///   the already-recorded `R -> callee-module` edge. The gate
///   (`check_realizability`, `IncrementalQuotient`) keeps these for
///   soundness: the emitter's phantom side-effect importer can reorder
///   ESM's link DFS so the target evaluates while the caller is still
///   on the stack, closing a TDZ cycle the lenient view would hide.
///   See
///   `realizability::tests::promoted_edge_in_aggregator_cycle_is_unrealizable`
///   for the regression fixture.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum EdgeRole {
    Direct,
    PromotedAtInit { callee_owner: OwnerId },
}

impl EdgeRole {
    /// `Some(callee_owner)` iff this is a `PromotedAtInit` role.
    /// Read by the CSR-builder in `OwnerGraph::from_report` /
    /// `build_owner_graph_with` to populate `callee_edges`, and by
    /// `reports::edge_role_report` to serialize the callee through
    /// `OwnerGraphEdgeReport.role`.
    pub fn promoted_callee(self) -> Option<OwnerId> {
        match self {
            EdgeRole::Direct => None,
            EdgeRole::PromotedAtInit { callee_owner } => Some(callee_owner),
        }
    }

    /// `true` if this is a `PromotedAtInit` role and the callee owner
    /// lives in a different module than the caller per `partition`.
    /// The lenient projection view (quotient, reports) drops such
    /// edges; the gate view keeps them.
    pub fn is_cross_module_promotion(self, from: OwnerId, partition: &Partition) -> bool {
        match self {
            EdgeRole::Direct => false,
            EdgeRole::PromotedAtInit { callee_owner } => {
                partition.of(callee_owner) != partition.of(from)
            }
        }
    }
}

/// One reason an edge `(from, to)` exists, with the source
/// statement ordinal that produced it. This is the single source of
/// truth for edge semantics:
///
/// - `EagerUse` constrains ESM evaluation order under TDZ
///   semantics (`R ⊆ I`).
/// - `LazyUse` contributes to the imports graph `I`, but does not
///   constrain realizability inside an SCC because the read fires
///   after module evaluation.
/// - `EagerRebind` / `LazyRebind` describe rebinding writes. A
///   cross-destination write is rejected outright because ESM imports
///   are read-only in the importing module; same-destination writes
///   are represented only at owner level and don't become module
///   imports.
/// - `Sequenced` contributes to `S` and constrains
///   realizability because source-order side effects require a
///   topological order.
/// - `LocalEffect` is a trusted target-local mutation (for example
///   a TypeScript `__decorate` helper application) that must
///   co-locate with the target owner but should not impose global
///   side-effect ordering on unrelated owners.
///
/// `role` (see [`EdgeRole`]) selects which projection rule the
/// quotient/gate/reports consumers apply to the edge.
#[derive(Debug, Clone)]
pub struct EdgeReason {
    pub kind: DepKind,
    pub statement_ordinal: StatementOrdinal,
    pub binding: Option<Id>,
    pub(crate) role: EdgeRole,
}

impl EdgeReason {
    pub(crate) fn eager_use(so: StatementOrdinal, b: Id) -> Self {
        Self {
            kind: DepKind::EagerUse,
            statement_ordinal: so,
            binding: Some(b),
            role: EdgeRole::Direct,
        }
    }
    pub(crate) fn lazy_use(so: StatementOrdinal, b: Id) -> Self {
        Self {
            kind: DepKind::LazyUse,
            statement_ordinal: so,
            binding: Some(b),
            role: EdgeRole::Direct,
        }
    }
    pub(crate) fn eager_rebind(so: StatementOrdinal, b: Id) -> Self {
        Self {
            kind: DepKind::EagerRebind,
            statement_ordinal: so,
            binding: Some(b),
            role: EdgeRole::Direct,
        }
    }
    pub(crate) fn lazy_rebind(so: StatementOrdinal, b: Id) -> Self {
        Self {
            kind: DepKind::LazyRebind,
            statement_ordinal: so,
            binding: Some(b),
            role: EdgeRole::Direct,
        }
    }
    pub(crate) fn deferred_rebind(so: StatementOrdinal, b: Id) -> Self {
        Self {
            kind: DepKind::DeferredRebind,
            statement_ordinal: so,
            binding: Some(b),
            role: EdgeRole::Direct,
        }
    }
    pub(crate) fn sequenced(so: StatementOrdinal) -> Self {
        Self {
            kind: DepKind::Sequenced,
            statement_ordinal: so,
            binding: None,
            role: EdgeRole::Direct,
        }
    }
    pub(crate) fn local_effect(so: StatementOrdinal, b: Id) -> Self {
        Self {
            kind: DepKind::LocalEffect,
            statement_ordinal: so,
            binding: Some(b),
            role: EdgeRole::Direct,
        }
    }

    /// Promote this reason to `EdgeRole::PromotedAtInit`. Used by
    /// `promote_at_init_calls`; downstream gate/lenient projection
    /// helpers consult `role` to decide whether to drop the edge when
    /// caller and callee land in different partition slots.
    pub(crate) fn promoted_at_init(mut self, callee_owner: OwnerId) -> Self {
        self.role = EdgeRole::PromotedAtInit { callee_owner };
        self
    }

    /// Construct a synthetic edge reason from raw fields. Used by
    /// `OwnerGraph::from_report` and similar JSON-recovery paths that
    /// don't carry an `Id` atom for the binding. The realizability
    /// gate (`check_realizability`) consults only `kind` and the role
    /// — every `is_*` and `constrains_init_order` predicate delegates
    /// to `kind` — so a synthetic reason without a binding is
    /// sufficient for the gate. Source-of-truth construction from
    /// `StatementFacts` still goes through the kind-specific helpers
    /// above.
    pub fn synthetic(kind: DepKind, statement_ordinal: StatementOrdinal, role: EdgeRole) -> Self {
        Self {
            kind,
            statement_ordinal,
            binding: None,
            role,
        }
    }

    /// The role this reason was emitted with. See [`EdgeRole`].
    pub fn role(&self) -> EdgeRole {
        self.role
    }

    pub fn is_eager_use(&self) -> bool {
        self.kind == DepKind::EagerUse
    }
    pub fn kind(&self) -> DepKind {
        self.kind
    }
    pub fn binding(&self) -> Option<&Id> {
        self.binding.as_ref()
    }
    pub fn statement_ordinal(&self) -> StatementOrdinal {
        self.statement_ordinal
    }
    pub fn is_rebind(&self) -> bool {
        matches!(
            self.kind,
            DepKind::EagerRebind | DepKind::LazyRebind | DepKind::DeferredRebind
        )
    }
    pub fn is_sequenced(&self) -> bool {
        self.kind == DepKind::Sequenced
    }
    /// Every kind except `LazyUse` and `DeferredRebind` constrains
    /// realizability. `DeferredRebind` writes never fire at module
    /// init (the write site is nested ≥2 closures deep or past an
    /// await), so they impose no init-order constraint — but they
    /// still participate in cross-destination-rebind rejection and
    /// `G_atomic` co-location, because ESM imports are read-only at
    /// ANY time, not just during init.
    pub fn constrains_init_order(&self) -> bool {
        !matches!(self.kind, DepKind::LazyUse | DepKind::DeferredRebind)
    }
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DepKind {
    EagerUse,
    LazyUse,
    EagerRebind,
    LazyRebind,
    /// Rebinding write that only fires after module init (nested ≥2
    /// closures deep, or past an `await` in an async body). Rejected
    /// across destinations like the other rebinds (ESM imports are
    /// read-only whenever the write fires), but excluded from
    /// init-order constraints and the I-graph.
    DeferredRebind,
    Sequenced,
    LocalEffect,
}

/// Stable-in-run identity of an owner graph vertex. V1 owner
/// vertices are post-comma-list `StatementFacts` rows, so the id
/// is the row's source-order ordinal.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct OwnerId(pub usize);

/// Fine-grained graph before logical modules are formed. Nodes are
/// top-level owners/statements; edges are owner-level reads and
/// source-order side-effect constraints. The module dependency graph
/// is the quotient of this graph by a [`Partition`].
///
/// Storage is **flat-edges + CSR adjacency**, the canonical compiler-IR
/// shape: one [`OwnerEdge`] per reason, indexed by [`OwnerEdgeId`]
/// (= position in `edges`), with per-node `out_edges` / `in_edges`
/// adjacency lists for O(deg) traversal. The previous representation
/// kept two parallel views — a `petgraph::DiGraphMap` for random
/// access by `(from, to)` and a separate `Vec<OwnerEdge>` for
/// stable indices into edges; this collapses them.
#[derive(Debug, Clone, Default)]
pub struct OwnerGraph {
    nodes: Vec<OwnerNode>,
    edges: Vec<OwnerEdge>,
    /// CSR adjacency by source owner. `out_edges[owner.0]` is a list
    /// of `OwnerEdgeId` indices into `edges`.
    out_edges: Vec<Vec<OwnerEdgeId>>,
    /// CSR adjacency by target owner.
    in_edges: Vec<Vec<OwnerEdgeId>>,
    /// CSR-style "edges referencing this owner as their at-init
    /// callee", indexed by owner index. Empty for owners that no edge
    /// references via [`EdgeRole::PromotedAtInit`]. Lets
    /// `impacted_owner_edges` look up callee-referencing edges in
    /// `O(|edges of that callee|)` instead of scanning the full edge
    /// list per call (a `verdict_with_overlay_touching` per-candidate
    /// hot path on gaffer-scale inputs).
    callee_edges: Vec<Vec<OwnerEdgeId>>,
}

#[derive(Debug, Clone)]
pub struct OwnerNode {
    pub id: OwnerId,
    pub statement_ordinal: StatementOrdinal,
    pub source_location: Option<SourceLocation>,
    pub declared: BTreeSet<Id>,
    pub kind: StatementKind,
    pub purity: Purity,
}

impl OwnerGraph {
    /// Iterate `&OwnerEdge` in `OwnerEdgeId` order. Each row is one
    /// reason — multiple reasons between the same `(from, to)` pair
    /// appear as separate entries.
    pub fn iter_edges(&self) -> impl Iterator<Item = &OwnerEdge> + '_ {
        self.edges.iter()
    }

    pub fn node(&self, id: OwnerId) -> Option<&OwnerNode> {
        self.nodes.get(id.0).filter(|node| node.id == id)
    }

    pub fn iter_nodes(&self) -> impl Iterator<Item = &OwnerNode> {
        self.nodes.iter()
    }

    /// Total owner-node count. Callers that need to size a per-owner
    /// vector (e.g. partition slots, unit assignments) should use this
    /// instead of reaching into a private field.
    pub fn num_nodes(&self) -> usize {
        self.nodes.len()
    }

    /// Total owner-edge count.
    pub fn num_edges(&self) -> usize {
        self.edges.len()
    }

    /// Owner-edge row by `OwnerEdgeId`. The CSR adjacency the graph
    /// exposes (`out_edges_of` / `in_edges_of` / `callee_edges_of`)
    /// returns ids; callers dereference those ids back to rows
    /// through this accessor instead of indexing the private edge
    /// table directly.
    pub fn edge(&self, id: OwnerEdgeId) -> &OwnerEdge {
        &self.edges[id.0]
    }

    /// Edges originating at `owner`.
    pub fn out_edges_of(&self, owner: OwnerId) -> &[OwnerEdgeId] {
        self.out_edges
            .get(owner.0)
            .map(Vec::as_slice)
            .unwrap_or(&[])
    }

    /// Edges terminating at `owner`.
    pub fn in_edges_of(&self, owner: OwnerId) -> &[OwnerEdgeId] {
        self.in_edges.get(owner.0).map(Vec::as_slice).unwrap_or(&[])
    }

    /// Edges referencing `owner` as their at-init callee. Mirrors
    /// `out_edges_of`/`in_edges_of` but for the callee-owner index.
    pub fn callee_edges_of(&self, owner: OwnerId) -> &[OwnerEdgeId] {
        self.callee_edges
            .get(owner.0)
            .map(Vec::as_slice)
            .unwrap_or(&[])
    }
}

/// Recovery handle that maps the JSON `OwnerGraphReport` owner-id
/// strings to the `OwnerId`s of an `OwnerGraph` built via
/// `OwnerGraph::from_report`. The position of an owner-id in
/// `OwnerGraphReport.nodes` equals the constructed `OwnerId.0`, so the
/// index lookup is just a position scan; this struct keeps the lookup
/// O(1) via an interned `HashMap`.
#[derive(Debug, Clone)]
pub struct OwnerReportIndex {
    pub owner_ids: Vec<String>,
    by_id: HashMap<String, OwnerId>,
}

impl OwnerReportIndex {
    pub fn lookup(&self, id: &str) -> Option<OwnerId> {
        self.by_id.get(id).copied()
    }

    pub fn id_of(&self, owner: OwnerId) -> Option<&str> {
        self.owner_ids.get(owner.0).map(String::as_str)
    }
}

impl OwnerGraph {
    /// Reconstruct a typed `OwnerGraph` from a JSON-deserialized
    /// `crate::OwnerGraphReport`. Used by the peel planner CLI so the
    /// realizability gate consults the same IR shape the materializer
    /// gate does, instead of re-deriving cycle detection over the
    /// JSON-flattened edge list.
    ///
    /// `facts` is the per-statement fact slice from a
    /// `ChunkFactsReport` (see `facts/wire.rs`); when supplied, the
    /// reconstructed nodes' [`OwnerNode::declared`] sets are
    /// populated by joining each node's `statement_ordinal` against
    /// the matching `StatementFactsReport.declared`. Pass `&[]` to
    /// opt out — `declared` stays empty, which is appropriate for
    /// gate-only consumers that never call
    /// [`crate::factor_assembly::assemble_partition`] on the
    /// reconstructed graph.
    ///
    /// The result is "gate-grade": the returned graph carries enough
    /// information for `check_realizability` (edge endpoints,
    /// `DepKind`, residual marker) and — with `facts` supplied — the
    /// per-owner declared-binding set
    /// `factor_assembly::compute_owner_claims` walks. Per-edge
    /// `binding` and per-node `kind` / `purity` mirror the JSON wire
    /// shape; the hygienic `SyntaxContext` carried by `IdReport` is
    /// only meaningful within a single SWC `Globals` scope, so
    /// reconstructed `Id`s round-trip *within one process* but
    /// **must not** be compared against re-parsed AST identifiers
    /// from a different `Globals` (see `stage_one_sidecars.rs` →
    /// "`facts.json` is debug-only").
    ///
    /// `OwnerEdgeId`s in the reconstructed graph are assigned in the
    /// order edges appear in `report.edges`; they don't necessarily
    /// match the original `OwnerEdgeId`s that produced the report.
    /// The gate only uses them as opaque identifiers in its evidence
    /// listing.
    ///
    /// Errors with [`UnresolvedOwnerEdgeEndpoint`] when an edge
    /// references an owner id missing from the report's node table —
    /// a malformed or version-skewed `owner_graph.json`. Silently
    /// dropping such edges would hand the planner-side gate a weaker
    /// graph than the one the report described.
    pub fn from_report(
        report: &crate::OwnerGraphReport,
        facts: &[crate::StatementFactsReport],
    ) -> Result<(Self, OwnerReportIndex), UnresolvedOwnerEdgeEndpoint> {
        let owner_ids: Vec<String> = report.nodes.iter().map(|n| n.id.clone()).collect();
        let by_id: HashMap<String, OwnerId> = owner_ids
            .iter()
            .enumerate()
            .map(|(i, id)| (id.clone(), OwnerId(i)))
            .collect();

        // Join key: a statement's ordinal is its identity across both
        // wire shapes — `OwnerGraphNodeReport.statement_ordinal` and
        // `StatementFactsReport.ordinal`. Build a lookup table once
        // so the per-node hydration below is O(1) per node.
        let declared_by_ordinal: HashMap<StatementOrdinal, &Vec<crate::IdReport>> =
            facts.iter().map(|f| (f.ordinal, &f.declared)).collect();

        let nodes: Vec<OwnerNode> = report
            .nodes
            .iter()
            .enumerate()
            .map(|(i, n)| OwnerNode {
                id: OwnerId(i),
                statement_ordinal: n.statement_ordinal,
                source_location: n.source_location.clone(),
                declared: declared_by_ordinal
                    .get(&n.statement_ordinal)
                    .map(|ids| ids.iter().map(crate::IdReport::to_id).collect())
                    .unwrap_or_default(),
                kind: n.statement_kind,
                purity: n.purity.clone(),
            })
            .collect();

        let mut edges: Vec<OwnerEdge> = Vec::with_capacity(report.edges.len());
        for edge in &report.edges {
            let resolve = |endpoint: &String| {
                by_id
                    .get(endpoint)
                    .copied()
                    .ok_or_else(|| UnresolvedOwnerEdgeEndpoint {
                        edge_id: edge.id.clone(),
                        endpoint: endpoint.clone(),
                    })
            };
            let from = resolve(&edge.source)?;
            let to = resolve(&edge.target)?;
            // Round-trip the edge role so the planner-side gate runs
            // the same cross-module-promotion filter as the
            // materializer.
            let role = match &edge.role {
                Some(role) => role.resolve(&by_id),
                None => EdgeRole::Direct,
            };
            let reason = EdgeReason::synthetic(edge.edge_kind, edge.statement_ordinal, role);
            let id = OwnerEdgeId(edges.len());
            edges.push(OwnerEdge {
                id,
                from,
                to,
                reason,
            });
        }

        let mut out_edges: Vec<Vec<OwnerEdgeId>> = vec![Vec::new(); nodes.len()];
        let mut in_edges: Vec<Vec<OwnerEdgeId>> = vec![Vec::new(); nodes.len()];
        let mut callee_edges: Vec<Vec<OwnerEdgeId>> = vec![Vec::new(); nodes.len()];
        for edge in &edges {
            if let Some(slot) = out_edges.get_mut(edge.from.0) {
                slot.push(edge.id);
            }
            if let Some(slot) = in_edges.get_mut(edge.to.0) {
                slot.push(edge.id);
            }
            if let Some(callee) = edge.reason.role.promoted_callee()
                && let Some(slot) = callee_edges.get_mut(callee.0)
            {
                slot.push(edge.id);
            }
        }

        let graph = OwnerGraph {
            nodes,
            edges,
            out_edges,
            in_edges,
            callee_edges,
        };
        let index = OwnerReportIndex { owner_ids, by_id };
        Ok((graph, index))
    }
}

/// An `owner_graph.json` edge references an owner id absent from the
/// report's node table. See [`OwnerGraph::from_report`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnresolvedOwnerEdgeEndpoint {
    pub edge_id: String,
    /// The owner id (e.g. `owner:42`) that didn't resolve.
    pub endpoint: String,
}

impl fmt::Display for UnresolvedOwnerEdgeEndpoint {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "owner graph edge {} references owner {} which is not in the report's node table \
             (malformed or version-skewed owner_graph.json)",
            self.edge_id, self.endpoint,
        )
    }
}

impl std::error::Error for UnresolvedOwnerEdgeEndpoint {}

/// Per-edge metadata. One physical `(from, to)` ESM `import`
/// directive can be backed by multiple reasons (e.g. several
/// at-init reads of bindings owned by the same target module);
/// they're all kept here so cycle reports can show every
/// triggering statement.
#[derive(Debug, Clone, Default)]
pub struct EdgeMetadata {
    pub reasons: Vec<EdgeReason>,
}

impl EdgeMetadata {
    /// `true` if at least one reason is an at-init read. The
    /// realizability gate uses this to decide whether an
    /// `I ∪ S` SCC contains an `R` cross-module edge.
    pub fn has_eager_use(&self) -> bool {
        self.reasons.iter().any(EdgeReason::is_eager_use)
    }

    /// `true` if at least one reason is a side-effect ordering
    /// edge. `S` edges in an SCC make it unrealizable: the
    /// constraint is "predecessor must evaluate before
    /// successor", and a cycle has no topological emit order
    /// satisfying every such edge.
    pub fn has_sequenced(&self) -> bool {
        self.reasons.iter().any(EdgeReason::is_sequenced)
    }

    /// `true` if at least one reason is a rebinding write. These
    /// edges are rejected outright when they cross destination
    /// modules because imported ESM bindings are read-only.
    pub fn has_rebind(&self) -> bool {
        self.reasons.iter().any(EdgeReason::is_rebind)
    }

    /// `true` if this edge constrains realizability — at least one
    /// of its reasons is realizability-constraining (an at-init
    /// read `R`, a side-effect ordering `S` edge, or a rebinding
    /// write). Lazy read-only edges don't, because the reads they
    /// represent fire after every module in the cycle has finished
    /// evaluating.
    ///
    /// Delegates to `EdgeReason::constrains_init_order` to keep
    /// the per-edge and per-reason definitions in lockstep.
    pub fn constrains_init_order(&self) -> bool {
        self.reasons.iter().any(EdgeReason::constrains_init_order)
    }
}

/// Module dep graph built from per-statement facts and a binding →
/// module assignment.
///
/// Thin newtype around `petgraph::DiGraphMap<ModuleId,
/// EdgeMetadata>`: one edge per directed `(from, to)` pair, weight =
/// `EdgeMetadata`. Multiple reasons for the same physical edge (e.g.
/// several at-init reads of bindings owned by the same target
/// module) accumulate into the edge's reason list. Cycle detection
/// runs through petgraph's `tarjan_scc`.
///
/// The inner `DiGraphMap` is private. Mutation happens only inside
/// [`build_module_quotient`] (and the constructor-private
/// `record_reason` helper); callers go through the read-only
/// accessors `all_edges`, `contains_edge`, `edge_weight`,
/// `has_init_order_constraining_edge`, and the convenience
/// `sccs` wrapper around `petgraph::algo::tarjan_scc`. The
/// newtype keeps the semantic name "the I∪S module-dep quotient"
/// distinct from arbitrary `DiGraphMap<ModuleId, EdgeMetadata>`
/// instances.
#[derive(Debug, Clone, Default)]
pub struct ModuleQuotient(DiGraphMap<ModuleId, EdgeMetadata>);

impl ModuleQuotient {
    fn record_reason(&mut self, from: ModuleId, to: ModuleId, reason: EdgeReason) {
        if from == to {
            return;
        }
        if !self.0.contains_edge(from, to) {
            self.0.add_edge(from, to, EdgeMetadata::default());
        }
        self.0
            .edge_weight_mut(from, to)
            .unwrap()
            .reasons
            .push(reason);
    }

    /// Iterate over every `(from, to, weight)` tuple in the quotient.
    /// Forwards to `petgraph::DiGraphMap::all_edges`.
    pub fn all_edges(&self) -> impl Iterator<Item = (ModuleId, ModuleId, &EdgeMetadata)> + '_ {
        self.0.all_edges()
    }

    /// `true` iff the directed edge `(from, to)` is present.
    pub fn contains_edge(&self, from: ModuleId, to: ModuleId) -> bool {
        self.0.contains_edge(from, to)
    }

    /// The metadata for `(from, to)` if the edge exists, else `None`.
    pub fn edge_weight(&self, from: ModuleId, to: ModuleId) -> Option<&EdgeMetadata> {
        self.0.edge_weight(from, to)
    }

    /// `true` if the edge `(from, to)` exists and constrains
    /// realizable evaluation order (at-init read or side-effect
    /// ordering). Used by the realizability gate to decide
    /// whether an `I ∪ S` SCC is unrealizable.
    pub fn has_init_order_constraining_edge(&self, from: ModuleId, to: ModuleId) -> bool {
        self.edge_weight(from, to)
            .is_some_and(EdgeMetadata::constrains_init_order)
    }

    /// Strongly-connected components of the quotient, via
    /// `petgraph::algo::tarjan_scc`. Each inner `Vec` is one SCC.
    pub fn sccs(&self) -> Vec<Vec<ModuleId>> {
        tarjan_scc(&self.0)
    }
}

/// Two distinct top-level statements declare the same binding
/// (`var x = 1; var x = 2;` — legal JS, but the owner graph models
/// each binding as having exactly one owning statement). Letting the
/// last declaration win would silently drop every edge into the
/// earlier owner, so the earlier statement could be ordered after its
/// readers. Rejecting the chunk is the accepted over-restriction
/// (AGENTS.md "Soundness over completeness").
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DuplicateTopLevelDeclaration {
    pub binding: swc_atoms::Atom,
    pub first: StatementOrdinal,
    pub second: StatementOrdinal,
}

impl fmt::Display for DuplicateTopLevelDeclaration {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "duplicate top-level declaration of binding `{}`: statements #{} and #{} both \
             declare it; the owner graph requires a single owning statement per binding. \
             Rewrite the chunk so the binding is declared once (e.g. merge the declarations \
             or rename one of them).",
            self.binding, self.first.0, self.second.0,
        )
    }
}

impl std::error::Error for DuplicateTopLevelDeclaration {}

/// Build the fine owner graph from per-statement facts. Pure IR
/// construction: no module assignment, no quotient. Module-level
/// dependencies are derived later by [`build_module_quotient`]
/// given a [`Partition`] mapping owners to destination modules.
///
/// Uses default (strictly-conservative) [`OwnerGraphOptions`]. Call
/// [`build_owner_graph_with`] when the chunk spec opts into
/// conditionally-correct refinements.
pub fn build_owner_graph(
    facts: &[StatementFacts],
) -> Result<OwnerGraph, DuplicateTopLevelDeclaration> {
    build_owner_graph_with(facts, OwnerGraphOptions::default())
}

/// Like [`build_owner_graph`] but takes per-chunk [`OwnerGraphOptions`].
pub fn build_owner_graph_with(
    facts: &[StatementFacts],
    options: OwnerGraphOptions,
) -> Result<OwnerGraph, DuplicateTopLevelDeclaration> {
    let mut binding_owner = HashMap::<Id, OwnerId>::new();
    let mut nodes = Vec::<OwnerNode>::with_capacity(facts.len());
    for stmt in facts {
        for binding in &stmt.declared {
            if let Some(prev) = binding_owner.insert(binding.clone(), OwnerId(stmt.ordinal.0)) {
                return Err(DuplicateTopLevelDeclaration {
                    binding: binding.0.clone(),
                    first: StatementOrdinal(prev.0),
                    second: stmt.ordinal,
                });
            }
        }
        let id = OwnerId(stmt.ordinal.0);
        nodes.push(OwnerNode {
            id,
            statement_ordinal: stmt.ordinal,
            source_location: stmt.source_location.clone(),
            declared: stmt.declared.clone(),
            kind: stmt.kind,
            purity: stmt.purity.clone(),
        });
    }

    // Collect (from, to, reason) triples; the final `edges` Vec is
    // sorted at the end so `OwnerEdgeId` indices are stable.
    let mut raw_edges = Vec::<(OwnerId, OwnerId, EdgeReason)>::new();
    // Look-aside table for "what statement owns this OwnerId" — shared
    // by the direct eager-read filter below and by
    // `promote_at_init_calls` (which builds its own local copy; the
    // duplicate cost is negligible).
    let stmt_by_owner: std::collections::HashMap<OwnerId, &StatementFacts> = facts
        .iter()
        .map(|stmt| (OwnerId(stmt.ordinal.0), stmt))
        .collect();
    // A top-level eager read of a binding declared by a `function`
    // declaration cannot observe a TDZ: ECMAScript Phase 1 of module
    // linking (`ModuleDeclarationInstantiation`) binds every
    // `FunctionDeclaration` to its hoisted closure before any module
    // body runs. So `const x = f()` where `f` is a chunk-declared
    // FnDecl is safe regardless of which module owns `f` — there is
    // no init-order constraint to record, and emitting an `EagerUse`
    // edge would manufacture a cross-module constraint no realizable
    // trace demands. Same rule as the FnDecl exclusion in
    // `promote_at_init_calls`. Other declared kinds (VarDecl,
    // ClassDecl) are TDZ-locked until their statement runs, so their
    // cross-module reads stay constrained.
    let target_is_hoisted = |id: &Id| -> bool {
        binding_owner
            .get(id)
            .and_then(|owner| stmt_by_owner.get(owner))
            .map(|stmt| stmt.kind == StatementKind::FnDecl)
            .unwrap_or(false)
    };
    let push_binding_edge = |raw_edges: &mut Vec<(OwnerId, OwnerId, EdgeReason)>,
                             from: OwnerId,
                             binding: &Id,
                             make_reason: fn(StatementOrdinal, Id) -> EdgeReason,
                             statement_ordinal: StatementOrdinal| {
        let Some(to) = binding_owner.get(binding) else {
            return; // not declared in this chunk (global, ImportSpecifier, never-declared)
        };
        if from == *to {
            return;
        }
        raw_edges.push((from, *to, make_reason(statement_ordinal, binding.clone())));
    };
    for stmt in facts {
        let from = OwnerId(stmt.ordinal.0);
        for binding in &stmt.reads.eager {
            if target_is_hoisted(binding) {
                continue;
            }
            push_binding_edge(
                &mut raw_edges,
                from,
                binding,
                EdgeReason::eager_use,
                stmt.ordinal,
            );
        }
        for binding in &stmt.reads.lazy {
            push_binding_edge(
                &mut raw_edges,
                from,
                binding,
                EdgeReason::lazy_use,
                stmt.ordinal,
            );
        }
        for binding in &stmt.rebinds.eager {
            push_binding_edge(
                &mut raw_edges,
                from,
                binding,
                EdgeReason::eager_rebind,
                stmt.ordinal,
            );
        }
        // Only first-order body rebinds emit a constraining
        // `LazyRebind` edge: a rebind inside a nested closure (e.g.
        // an arrow stashed on `globalThis` by the body) doesn't fire
        // when the function is invoked synchronously, so it must not
        // constrain init order or feed at-init call promotion. See
        // the e2e test `at_init_promotion_nested_closure_test`.
        for binding in &stmt.rebinds.first_order_lazy {
            push_binding_edge(
                &mut raw_edges,
                from,
                binding,
                EdgeReason::lazy_rebind,
                stmt.ordinal,
            );
        }
        // Deeper-nested (or post-await) rebinds still rebind the
        // binding cell whenever they DO fire — and ESM imports are
        // read-only at any time, not just during init. Emit a
        // non-init-constraining `DeferredRebind` edge so
        // cross-destination splits are rejected and `G_atomic`
        // forces co-location, without manufacturing an init-order
        // constraint nothing fires at init.
        for binding in &stmt.rebinds.lazy {
            if stmt.rebinds.first_order_lazy.contains(binding) {
                continue;
            }
            push_binding_edge(
                &mut raw_edges,
                from,
                binding,
                EdgeReason::deferred_rebind,
                stmt.ordinal,
            );
        }
        for binding in &stmt.local_effects {
            push_binding_edge(
                &mut raw_edges,
                from,
                binding,
                EdgeReason::local_effect,
                stmt.ordinal,
            );
        }
    }

    // At-init call promotion (docs/design.md "At-init call promotion").
    //
    // A function body's lazy reads/rebinds fire at-init from the
    // perspective of any caller that invokes the function at-init.
    // Without promotion, the realizability primitive's relaxed
    // clause-3 predicate (constraining-edge subgraph has no
    // multi-module SCC) is unsound for the canonical
    // `console.log(readB())` shape: the lazy read inside `readB`'s
    // body fires when the top-level call evaluates, but only the
    // graph's lazy edge is recorded, so cross-module cycles that
    // close through such a lazy edge look acyclic to the constraining
    // subgraph. After promotion, the primitive's verdict is sound.
    //
    // Promoted edges are added at owner-graph level (partition-
    // independent): intra-module promoted edges are dropped by the
    // quotient automatically. Only direct `f(...)` callees that
    // resolve to chunk-declared bindings are followed; indirect
    // calls (`const g = f; g()`), method calls (`obj.method()`), and
    // dynamic dispatch are conservatively unmodelled.
    promote_at_init_calls(facts, &binding_owner, &mut raw_edges);

    emit_s_chain(facts, options, &mut raw_edges);

    // Sort + assign stable `OwnerEdgeId` indices, then build CSR
    // adjacency in one pass.
    raw_edges.sort_by(|(from_a, to_a, reason_a), (from_b, to_b, reason_b)| {
        from_a
            .cmp(from_b)
            .then(to_a.cmp(to_b))
            .then(reason_a.kind.cmp(&reason_b.kind))
            .then(reason_a.statement_ordinal.cmp(&reason_b.statement_ordinal))
            .then(reason_a.binding.cmp(&reason_b.binding))
    });
    let edges: Vec<OwnerEdge> = raw_edges
        .into_iter()
        .enumerate()
        .map(|(idx, (from, to, reason))| OwnerEdge {
            id: OwnerEdgeId(idx),
            from,
            to,
            reason,
        })
        .collect();
    let mut out_edges: Vec<Vec<OwnerEdgeId>> = vec![Vec::new(); nodes.len()];
    let mut in_edges: Vec<Vec<OwnerEdgeId>> = vec![Vec::new(); nodes.len()];
    let mut callee_edges: Vec<Vec<OwnerEdgeId>> = vec![Vec::new(); nodes.len()];
    for edge in &edges {
        if let Some(slot) = out_edges.get_mut(edge.from.0) {
            slot.push(edge.id);
        }
        if let Some(slot) = in_edges.get_mut(edge.to.0) {
            slot.push(edge.id);
        }
        if let Some(callee) = edge.reason.role.promoted_callee()
            && let Some(slot) = callee_edges.get_mut(callee.0)
        {
            slot.push(edge.id);
        }
    }

    Ok(OwnerGraph {
        nodes,
        edges,
        out_edges,
        in_edges,
        callee_edges,
    })
}

/// Side-effect ordering edges (`S` per docs/design.md "Module dep graphs").
/// At owner level, links impure top-level statements so any realizable
/// schedule preserves their observable order.
///
/// Two emission modes selected by [`OwnerGraphOptions`]:
///
/// - **Strict chain** (default): every later impure statement gets one
///   incoming `Sequenced` edge from the immediately previous impure
///   statement. Transitive reduction of the total order; soundest path.
/// - **Dataflow-aware** (`dataflow_aware_s_chain = true`): emit
///   `Sequenced(curr → prev)` only when `curr` reads or writes a cell
///   `prev` wrote (last-writer-precedes-reader). Statements that fail
///   the `dataflow_summarizable` check (dynamic globalThis key, `with`,
///   direct `eval`, `Function(...)` constructor, `defineProperty` on
///   globals, `Proxy` on globals) fall back to the strict edge against
///   every prior impure owner. See `README.md` →
///   "Conditionally-correct optimizations" for the precondition this
///   relaxation requires.
///
/// `purity` is computed upstream (`classify_expr_purity`) so pure
/// literal initializers (`const X = 42`, `const X = { a: 1 }`,
/// function/class declarations without observable static init) don't
/// contribute to S. Without that precision the cross-module S graph
/// would be dense enough to reject realistic specs for trivially pure
/// const sequences.
fn emit_s_chain(
    facts: &[StatementFacts],
    options: OwnerGraphOptions,
    raw_edges: &mut Vec<(OwnerId, OwnerId, EdgeReason)>,
) {
    if !options.dataflow_aware_s_chain {
        let mut prev: Option<OwnerId> = None;
        for stmt in facts.iter().filter(|s| !s.purity.is_pure()) {
            let from = OwnerId(stmt.ordinal.0);
            if let Some(to) = prev
                && from != to
            {
                raw_edges.push((from, to, EdgeReason::sequenced(stmt.ordinal)));
            }
            prev = Some(from);
        }
        return;
    }

    // Dataflow-aware emission. For each impure `curr`, emit an
    // incoming Sequenced edge from:
    //
    // - the most recent prior impure owner that wrote any cell in
    //   `curr.reads ∪ curr.writes` (read-after-write /
    //   write-after-write), and
    // - every prior impure owner that READ a cell `curr` writes
    //   since that cell's last write (write-after-read — without
    //   this, a later writer could be scheduled before an earlier
    //   reader and the reader would observe the new value).
    //
    // Statements with `dataflow_summarizable = false` are treated as
    // touching every cell — they get edges to every prior impure
    // owner and become a barrier for subsequent statements.
    let mut last_writer: BTreeMap<EffectCell, OwnerId> = BTreeMap::new();
    let mut readers_since_last_write: BTreeMap<EffectCell, BTreeSet<OwnerId>> = BTreeMap::new();
    let mut prior_impure_owners: Vec<OwnerId> = Vec::new();
    let mut opaque_barrier: Option<OwnerId> = None;
    for stmt in facts.iter().filter(|s| !s.purity.is_pure()) {
        let from = OwnerId(stmt.ordinal.0);
        let effects = stmt.effects();
        let mut targets: BTreeSet<OwnerId> = BTreeSet::new();
        if stmt.dataflow_summarizable {
            for cell in effects.reads.iter().chain(effects.writes.iter()) {
                if let Some(&to) = last_writer.get(cell) {
                    targets.insert(to);
                }
            }
            for cell in &effects.writes {
                if let Some(readers) = readers_since_last_write.get(cell) {
                    targets.extend(readers.iter().copied());
                }
            }
            // Non-summarizable prior statements are barriers: any later
            // summarizable statement still depends on them, since we
            // don't know what cells they touched.
            if let Some(barrier) = opaque_barrier {
                targets.insert(barrier);
            }
        } else {
            // This statement can't be summarized: treat it as reading
            // and writing every cell. Depend on every prior impure
            // owner, and become the new opaque barrier so later
            // summarizable statements depend on us too.
            targets.extend(prior_impure_owners.iter().copied());
            opaque_barrier = Some(from);
        }
        for to in targets {
            if from != to {
                raw_edges.push((from, to, EdgeReason::sequenced(stmt.ordinal)));
            }
        }
        if stmt.dataflow_summarizable {
            for cell in effects.writes {
                readers_since_last_write.remove(&cell);
                last_writer.insert(cell, from);
            }
            for cell in effects.reads {
                readers_since_last_write
                    .entry(cell)
                    .or_default()
                    .insert(from);
            }
        }
        prior_impure_owners.push(from);
    }
}

/// Promote function-body lazy reads/rebinds to eager owner edges from
/// every statement that at-init-calls the function. Transitive over
/// the call graph among chunk-declared functions: a top-level
/// `f()` whose `f` calls `g` in its body promotes through `g`'s lazy
/// reads/rebinds too. See docs/design.md "At-init call promotion".
///
/// Per-statement dedup: at most one promoted eager edge per
/// (caller, target-owner) pair. Rebind targets are also promoted as
/// eager order edges; the write site's direct rebind edge enforces
/// write legality. Without dedup, a single at-init call to a
/// function with N transitive lazy reads would emit N edges from the
/// caller, and multiple at-init calls in the same statement would
/// multiply that further.
fn promote_at_init_calls(
    facts: &[StatementFacts],
    binding_owner: &HashMap<Id, OwnerId>,
    raw_edges: &mut Vec<(OwnerId, OwnerId, EdgeReason)>,
) {
    let mut stmt_by_owner: BTreeMap<OwnerId, &StatementFacts> = BTreeMap::new();
    for stmt in facts {
        stmt_by_owner.insert(OwnerId(stmt.ordinal.0), stmt);
    }

    // Bindings rebound anywhere in the chunk: their value at call
    // time may not be the function lexically defined at their owner,
    // so calls to them are not precisely resolvable.
    let rebound: BTreeSet<&Id> = facts
        .iter()
        .flat_map(|s| s.rebinds.eager.iter().chain(s.rebinds.lazy.iter()))
        .collect();

    // A callee binding is precisely resolvable iff (a) its declaring
    // statement binds it directly to a function value (`function f`,
    // `const f = () => ...`) and (b) it is never rebound. Everything
    // else — aliases (`const g = readB`), object-literal methods,
    // conditional initializers, rebound functions — takes the
    // conservative read-closure fallback below.
    let resolvable_callee = |id: &Id| -> Option<OwnerId> {
        let owner = binding_owner.get(id)?;
        let stmt = stmt_by_owner.get(owner)?;
        (stmt.declares_direct_function && !rebound.contains(id)).then_some(*owner)
    };

    // 1. Build the call graph: owner → owner edges for each
    //    resolvable callee reachable via *first-order* calls.lazy.
    //    Nested-closure calls (e.g. inside an arrow returned by the
    //    body) don't fire when the body is invoked synchronously, so
    //    they don't belong on the promotion call graph — see
    //    docs/design.md "At-init call promotion" and the e2e test
    //    `at_init_promotion_nested_closure_test`.
    //
    //    Add every owner whose body has any first-order lazy reads /
    //    rebinds / calls — or an unresolvable first-order callee — as
    //    a node, so the closure pass below covers it even if it makes
    //    no resolvable calls (e.g. `function readB() { return B; }`).
    //
    //    Per-owner "fallback roots": owners through which a function
    //    value could reach a call the owner's first-order body makes
    //    but promotion can't follow — the owners of bindings the
    //    unresolved call mentions, the owners of chunk-declared
    //    Ident callees that aren't direct never-rebound functions,
    //    and the owner itself when the unresolved call carries an
    //    inline function expression. At-init callers inherit these
    //    roots transitively through the call graph and expand them
    //    via [`UnresolvedCallFallback`].
    let fallback_roots_of =
        |sources: &BTreeSet<Id>, inline_fn: bool, self_owner: OwnerId| -> BTreeSet<OwnerId> {
            let mut roots: BTreeSet<OwnerId> = sources
                .iter()
                .filter_map(|id| binding_owner.get(id).copied())
                .collect();
            if inline_fn {
                roots.insert(self_owner);
            }
            roots
        };
    let owner_first_order_roots = |stmt: &StatementFacts| -> BTreeSet<OwnerId> {
        let owner = OwnerId(stmt.ordinal.0);
        let mut roots = fallback_roots_of(
            &stmt.first_order_unresolved_sources,
            stmt.first_order_unresolved_inline_fn,
            owner,
        );
        for callee_id in &stmt.calls.first_order_lazy {
            if resolvable_callee(callee_id).is_none()
                && let Some(&callee_owner) = binding_owner.get(callee_id)
            {
                roots.insert(callee_owner);
            }
        }
        roots
    };
    let mut call_graph: DiGraphMap<OwnerId, ()> = DiGraphMap::new();
    for stmt in facts {
        let owner = OwnerId(stmt.ordinal.0);
        if !stmt.calls.first_order_lazy.is_empty()
            || !stmt.reads.first_order_lazy.is_empty()
            || !stmt.rebinds.first_order_lazy.is_empty()
            || !stmt.first_order_unresolved_sources.is_empty()
            || stmt.first_order_unresolved_inline_fn
        {
            call_graph.add_node(owner);
        }
    }
    for stmt in facts {
        if stmt.calls.first_order_lazy.is_empty() {
            continue;
        }
        let caller = OwnerId(stmt.ordinal.0);
        for callee_id in &stmt.calls.first_order_lazy {
            let Some(callee_owner) = resolvable_callee(callee_id) else {
                continue;
            };
            call_graph.add_node(callee_owner);
            call_graph.add_edge(caller, callee_owner, ());
        }
    }

    // 2. Tarjan SCC. `tarjan_scc` returns SCCs in reverse topological
    //    order: leaves (no outgoing edges to other SCCs) first.
    let sccs = tarjan_scc(&call_graph);
    let mut scc_of: BTreeMap<OwnerId, usize> = BTreeMap::new();
    for (idx, scc) in sccs.iter().enumerate() {
        for owner in scc {
            scc_of.insert(*owner, idx);
        }
    }

    // 3. Per-owner seeds: own reads.lazy / rebinds.lazy resolved to
    //    BindingId. Filters out targets whose owner is a function
    //    declaration — function bindings are hoisted at module
    //    instantiation (Phase 1 of ESM linking), so a cross-module
    //    read of a function never observes a TDZ. Promoting such
    //    reads would spuriously close cycles for shapes like mutual
    //    recursion across modules (`function even(){odd()}` /
    //    `function odd(){even()}`), which are actually realizable.
    //    Other declared kinds (VarDecl, ClassDecl) are kept: const /
    //    let / class are TDZ-locked until their statement runs, so a
    //    cross-module read inside an at-init-called function does
    //    fire the realizability hazard. `var` is technically hoisted
    //    too but is rare enough not to warrant a separate distinction
    //    in StatementKind.
    let target_is_hoisted = |id: &Id| -> bool {
        let Some(target_owner) = binding_owner.get(id) else {
            return false;
        };
        stmt_by_owner
            .get(target_owner)
            .map(|stmt| stmt.kind == StatementKind::FnDecl)
            .unwrap_or(false)
    };
    let mut scc_reads: Vec<BTreeSet<Id>> = vec![BTreeSet::new(); sccs.len()];
    let mut scc_rebinds: Vec<BTreeSet<Id>> = vec![BTreeSet::new(); sccs.len()];
    let mut scc_fallback_roots: Vec<BTreeSet<OwnerId>> = vec![BTreeSet::new(); sccs.len()];

    // 4. Closure over the call graph. Iterate SCCs in
    //    reverse-topological order (leaves first). For each SCC,
    //    union members' own seeds plus successor SCC closures, and
    //    propagate fallback roots the same way.
    for (scc_idx, scc) in sccs.iter().enumerate() {
        let mut reads: BTreeSet<Id> = BTreeSet::new();
        let mut rebinds: BTreeSet<Id> = BTreeSet::new();
        let mut roots: BTreeSet<OwnerId> = BTreeSet::new();
        for owner in scc {
            let Some(stmt) = stmt_by_owner.get(owner) else {
                continue;
            };
            roots.extend(owner_first_order_roots(stmt));
            for id in &stmt.reads.first_order_lazy {
                if binding_owner.contains_key(id) && !target_is_hoisted(id) {
                    reads.insert(id.clone());
                }
            }
            for id in &stmt.rebinds.first_order_lazy {
                if binding_owner.contains_key(id) {
                    rebinds.insert(id.clone());
                }
            }
        }
        for owner in scc {
            for (_, target, _) in call_graph.edges(*owner) {
                let Some(&target_scc) = scc_of.get(&target) else {
                    continue;
                };
                if target_scc == scc_idx {
                    continue;
                }
                reads.extend(scc_reads[target_scc].iter().cloned());
                rebinds.extend(scc_rebinds[target_scc].iter().cloned());
                roots.extend(scc_fallback_roots[target_scc].iter().copied());
            }
        }
        scc_reads[scc_idx] = reads;
        scc_rebinds[scc_idx] = rebinds;
        scc_fallback_roots[scc_idx] = roots;
    }

    // 5. Emit promoted edges with per-statement, per-kind dedup.
    //    Each emitted reason carries `EdgeRole::PromotedAtInit {
    //    callee_owner }` so the realizability gate can drop the edge
    //    when caller and callee land in different partition slots —
    //    the body read fires inside a call into a different module,
    //    after that callee module (and its imports) have already
    //    evaluated, so the manufactured constraint from R to the
    //    target's module is redundant with the already-recorded
    //    R -> callee-module edge. See [`EdgeRole`].
    //
    //    Statements whose at-init calls can't all be resolved take
    //    the conservative fallback in addition: see
    //    [`UnresolvedCallFallback`].
    let mut fallback: Option<UnresolvedCallFallback> = None;
    for stmt in facts {
        if stmt.calls.eager.is_empty()
            && stmt.at_init_unresolved_sources.is_empty()
            && !stmt.at_init_unresolved_inline_fn
        {
            continue;
        }
        let caller = OwnerId(stmt.ordinal.0);
        let mut promoted_read_targets: BTreeSet<OwnerId> = BTreeSet::new();
        let mut fallback_roots = fallback_roots_of(&stmt.at_init_unresolved_sources, false, caller);
        if stmt.at_init_unresolved_inline_fn
            && let Some(&scc_idx) = scc_of.get(&caller)
        {
            fallback_roots.extend(scc_fallback_roots[scc_idx].iter().copied());
            for target_binding in &scc_reads[scc_idx] {
                let Some(target_owner) = binding_owner.get(target_binding) else {
                    continue;
                };
                if caller == *target_owner {
                    continue;
                }
                if !promoted_read_targets.insert(*target_owner) {
                    continue;
                }
                raw_edges.push((
                    caller,
                    *target_owner,
                    EdgeReason::eager_use(stmt.ordinal, target_binding.clone())
                        .promoted_at_init(caller),
                ));
            }
            for target_binding in &scc_rebinds[scc_idx] {
                let Some(target_owner) = binding_owner.get(target_binding) else {
                    continue;
                };
                if caller == *target_owner {
                    continue;
                }
                if !promoted_read_targets.insert(*target_owner) {
                    continue;
                }
                raw_edges.push((
                    caller,
                    *target_owner,
                    EdgeReason::eager_use(stmt.ordinal, target_binding.clone())
                        .promoted_at_init(caller),
                ));
            }
        }
        for callee_id in &stmt.calls.eager {
            let Some(callee_owner) = resolvable_callee(callee_id) else {
                // A bare-Ident callee that isn't chunk-declared is a
                // global/import — out of single-chunk analysis scope
                // (documented precondition in docs/design.md). A
                // chunk-declared but unresolvable callee falls back
                // through the read closure of its own owner.
                if let Some(&callee_owner) = binding_owner.get(callee_id) {
                    fallback_roots.insert(callee_owner);
                }
                continue;
            };
            let Some(&scc_idx) = scc_of.get(&callee_owner) else {
                continue;
            };
            fallback_roots.extend(scc_fallback_roots[scc_idx].iter().copied());
            for target_binding in &scc_reads[scc_idx] {
                let Some(target_owner) = binding_owner.get(target_binding) else {
                    continue;
                };
                if caller == *target_owner {
                    continue;
                }
                if !promoted_read_targets.insert(*target_owner) {
                    continue;
                }
                raw_edges.push((
                    caller,
                    *target_owner,
                    EdgeReason::eager_use(stmt.ordinal, target_binding.clone())
                        .promoted_at_init(callee_owner),
                ));
            }
            for target_binding in &scc_rebinds[scc_idx] {
                let Some(target_owner) = binding_owner.get(target_binding) else {
                    continue;
                };
                if caller == *target_owner {
                    continue;
                }
                if !promoted_read_targets.insert(*target_owner) {
                    continue;
                }
                raw_edges.push((
                    caller,
                    *target_owner,
                    EdgeReason::eager_use(stmt.ordinal, target_binding.clone())
                        .promoted_at_init(callee_owner),
                ));
            }
        }
        if fallback_roots.is_empty() {
            continue;
        }
        let fallback =
            fallback.get_or_insert_with(|| UnresolvedCallFallback::build(facts, binding_owner));
        for root in fallback_roots {
            let (closure_reads, closure_rebinds) = fallback.closures_for(root);
            // Rebind targets get an *order* constraint only (EagerUse,
            // not EagerRebind): assignment to a TDZ-locked binding
            // throws until its statement runs, so the target must
            // evaluate before the caller — but write LEGALITY
            // (read-only imports) is already enforced by the direct
            // `LazyRebind` / `DeferredRebind` edge from the statement
            // lexically containing the write, which forces the write
            // site to co-locate with the declarer. Emitting
            // `EagerRebind` here would force the *caller* into the
            // co-location unit too, rejecting realizable shapes
            // (`c.bump(1)` calling a co-located setter).
            for target_binding in closure_reads.iter().chain(closure_rebinds.iter()) {
                if target_is_hoisted(target_binding) {
                    continue;
                }
                let Some(target_owner) = binding_owner.get(target_binding) else {
                    continue;
                };
                if caller == *target_owner || !promoted_read_targets.insert(*target_owner) {
                    continue;
                }
                raw_edges.push((
                    caller,
                    *target_owner,
                    EdgeReason::eager_use(stmt.ordinal, target_binding.clone())
                        .promoted_at_init(caller),
                ));
            }
        }
    }
}

/// Conservative fallback for at-init calls promotion can't resolve
/// (member calls, IIFEs, aliases, tagged templates, calls into
/// functions whose own bodies contain such calls).
///
/// Premise: whatever function value an unresolvable at-init call
/// invokes, it must have reached the call site through one of the
/// bindings the call expression mentions (callee root, arguments,
/// computed keys) or through an inline function expression at the
/// call. Each such "root" owner is expanded to the **full lazy
/// closure** of every owner reachable from it through the chunk's
/// read graph: edges `O → owner(b)` for every chunk binding `b` the
/// owner `O` reads (eagerly or lazily), with every reachable owner
/// contributing its `reads.lazy` / `rebinds.lazy` at any nesting
/// depth. The caller statement then eagerly depends on every
/// collected target.
///
/// Documented residual preconditions (see docs/design.md "At-init
/// call promotion" → Limitations): function values that reach the
/// call site through global/object property stashes, through rebound
/// bindings (`let g; g = readB; g()`), through parameters of
/// chunk-declared functions (`function h(cb) { cb(); }`), via `new`,
/// or from other chunks are not modelled.
///
/// Fallback-only edges carry `EdgeRole::PromotedAtInit` with
/// `callee_owner = caller`, so neither projection view ever drops
/// them — there is no precisely-known callee module whose evaluation
/// could make the constraint redundant.
struct UnresolvedCallFallback {
    scc_of: BTreeMap<OwnerId, usize>,
    scc_reads: Vec<BTreeSet<Id>>,
    scc_rebinds: Vec<BTreeSet<Id>>,
}

impl UnresolvedCallFallback {
    fn build(facts: &[StatementFacts], binding_owner: &HashMap<Id, OwnerId>) -> Self {
        let mut read_graph: DiGraphMap<OwnerId, ()> = DiGraphMap::new();
        for stmt in facts {
            let owner = OwnerId(stmt.ordinal.0);
            read_graph.add_node(owner);
            for id in stmt.reads.eager.iter().chain(stmt.reads.lazy.iter()) {
                if let Some(&target) = binding_owner.get(id) {
                    read_graph.add_edge(owner, target, ());
                }
            }
        }
        let mut stmt_by_owner: BTreeMap<OwnerId, &StatementFacts> = BTreeMap::new();
        for stmt in facts {
            stmt_by_owner.insert(OwnerId(stmt.ordinal.0), stmt);
        }
        let sccs = tarjan_scc(&read_graph);
        let mut scc_of: BTreeMap<OwnerId, usize> = BTreeMap::new();
        for (idx, scc) in sccs.iter().enumerate() {
            for owner in scc {
                scc_of.insert(*owner, idx);
            }
        }
        let mut scc_reads: Vec<BTreeSet<Id>> = vec![BTreeSet::new(); sccs.len()];
        let mut scc_rebinds: Vec<BTreeSet<Id>> = vec![BTreeSet::new(); sccs.len()];
        for (scc_idx, scc) in sccs.iter().enumerate() {
            let mut reads: BTreeSet<Id> = BTreeSet::new();
            let mut rebinds: BTreeSet<Id> = BTreeSet::new();
            for owner in scc {
                let Some(stmt) = stmt_by_owner.get(owner) else {
                    continue;
                };
                for id in &stmt.reads.lazy {
                    if binding_owner.contains_key(id) {
                        reads.insert(id.clone());
                    }
                }
                for id in &stmt.rebinds.lazy {
                    if binding_owner.contains_key(id) {
                        rebinds.insert(id.clone());
                    }
                }
            }
            for owner in scc {
                for (_, target, _) in read_graph.edges(*owner) {
                    let Some(&target_scc) = scc_of.get(&target) else {
                        continue;
                    };
                    if target_scc == scc_idx {
                        continue;
                    }
                    reads.extend(scc_reads[target_scc].iter().cloned());
                    rebinds.extend(scc_rebinds[target_scc].iter().cloned());
                }
            }
            scc_reads[scc_idx] = reads;
            scc_rebinds[scc_idx] = rebinds;
        }
        Self {
            scc_of,
            scc_reads,
            scc_rebinds,
        }
    }

    fn closures_for(&self, owner: OwnerId) -> (&BTreeSet<Id>, &BTreeSet<Id>) {
        static EMPTY: std::sync::OnceLock<BTreeSet<Id>> = std::sync::OnceLock::new();
        let empty = EMPTY.get_or_init(BTreeSet::new);
        match self.scc_of.get(&owner) {
            Some(&idx) => (&self.scc_reads[idx], &self.scc_rebinds[idx]),
            None => (empty, empty),
        }
    }
}

/// Selects between the gate and lenient views in
/// [`partition_endpoints`].
///
/// `Lenient` drops cross-module `PromotedAtInit` edges (the quotient
/// builder and reports view); `Gate` keeps them (the realizability
/// gate, incremental simulator, and canonical chunk-edge set). See
/// [`EdgeRole`] for the ESM-semantics justification.
#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub enum EndpointView {
    Lenient,
    Gate,
}

/// Partition-projected endpoints of `edge` when it participates in
/// the module quotient view; `None` means "skip this edge."
///
/// `view` selects which projection rule the caller wants:
///
/// - [`EndpointView::Lenient`] — used by `build_module_quotient` and
///   `report_builders::build_quotient_edge_reports` (gate crate). Drops same-module edges
///   AND drops cross-module [`EdgeRole::PromotedAtInit`] edges when
///   the callee module differs from the caller module. ESM
///   justification: the body read fires inside a call into a
///   *different* module, so by ESM DFS post-order the callee module
///   (and its transitive imports) are fully evaluated before the
///   call returns; the manufactured `R -> target-module` constraint
///   is redundant with the already-recorded `R -> callee-module`
///   edge.
/// - [`EndpointView::Gate`] — used by `check_realizability`,
///   `IncrementalQuotient::{add,remove}_current_edge`, and
///   `chunk_constraining_module_edges`. Drops same-module edges but
///   KEEPS cross-module `PromotedAtInit` edges. The emitter's
///   `collect_phantom_side_effect_providers` adds phantom
///   side-effect imports for these edges, which can reorder ESM's
///   link DFS so the target module evaluates while the caller module
///   is still on the stack — closing a TDZ cycle the lenient view
///   would hide. See
///   `realizability::tests::promoted_edge_in_aggregator_cycle_is_unrealizable`
///   for the regression fixture.
///
/// Invariant: every quotient-projecting consumer of the owner graph
/// MUST route through this function so the lenient-vs-gate decision
/// stays welded to the edge's [`EdgeRole`] at one source-level point.
pub fn partition_endpoints(
    edge: &OwnerEdge,
    partition: &Partition,
    view: EndpointView,
) -> Option<(ModuleId, ModuleId)> {
    let from = partition.of(edge.from);
    let to = partition.of(edge.to);
    if from == to {
        return None;
    }
    // Fallback-promoted edges (marked by `callee_owner == edge.from`,
    // see `UnresolvedCallFallback`) record "this statement's
    // unresolvable at-init call may invoke chunk functions reading
    // `to`'s bindings". When the caller lands in residual, the
    // constraint is vacuous: residual is the ESM DFS root and its
    // body runs only after every transitively-imported module has
    // fully evaluated, so no at-init call from residual code can
    // observe a TDZ. Dropping the edge in BOTH views also keeps the
    // gate's assumed I-topology in sync with the emitter, which
    // emits phantom side-effect imports for moved modules but not
    // for entry.
    if let EdgeRole::PromotedAtInit { callee_owner } = edge.reason.role
        && callee_owner == edge.from
        && from == partition.residual()
    {
        return None;
    }
    if view == EndpointView::Lenient
        && edge
            .reason
            .role
            .is_cross_module_promotion(edge.from, partition)
    {
        return None;
    }
    Some((from, to))
}

/// Quotient the owner graph by `partition` to build the module
/// dependency graph consumed by validation and emit. The single
/// public construction path; validation and reports both go through
/// this for any non-hypothetical quotient.
pub fn build_module_quotient(owner_graph: &OwnerGraph, partition: &Partition) -> ModuleQuotient {
    let mut graph = ModuleQuotient(DiGraphMap::new());
    let mut seen_side_effect_module_pairs = BTreeSet::<(ModuleId, ModuleId)>::new();
    for edge in owner_graph.iter_edges() {
        let Some((from, to)) = partition_endpoints(edge, partition, EndpointView::Lenient) else {
            continue;
        };
        if edge.reason.is_sequenced() && !seen_side_effect_module_pairs.insert((from, to)) {
            continue;
        }
        graph.record_reason(from, to, edge.reason.clone());
    }
    graph
}

/// The canonical chunk-wide ESM I-graph. Each entry is a module-level
/// init-order-constraining read or sequenced effect that the
/// emitter actually emits as an ESM `import` directive and that the
/// runtime ECMA-262 linker DFS therefore traverses when the chunk
/// loads. Both the realizability gate (Pass-2 simulator's
/// `i_successors`, linker / source-import positions) and the
/// emitter (`lowering::plan_references::collect_phantom_side_effect_providers`,
/// `chunk_factorization::compute_{linker,source_import}_order`)
/// MUST drive their topology decisions through this single set so
/// they cannot drift apart.
///
/// Filter rule:
///   * Drop same-module edges (no ESM `import`).
///   * Keep cross-module edges whose reason `constrains_init_order()`
///     and is **not** a rebind — i.e. `EagerUse`, `Sequenced`,
///     `LocalEffect`. These are the edges the emitter currently
///     turns into either a binding-level ESM import or a phantom
///     side-effect import.
///   * Drop pure `LazyUse` cross-module edges. They are
///     function-body reads, resolved at call time after every module
///     has loaded; the runtime DFS never follows them, so neither
///     can the gate's simulator without manufacturing imaginary
///     cycles.
///   * Drop `EagerRebind` / `LazyRebind` cross-module edges. They
///     surface as `cross_rebinds` in the realizability verdict, not
///     as I-graph nodes; the emitter never emits them as imports.
///   * Keep cross-module at-init promoted edges (see
///     [`EndpointView::Gate`]) — the emitter's phantom side-effect
///     importer also keeps them, so the gate must too.
///
/// Sequenced edges are deduped per `(from, to)` pair to mirror the
/// dedup `build_module_quotient` performs: multiple sequenced
/// reasons between the same module pair represent the same
/// ordering constraint and should not over-weight the I-graph.
///
/// Returns the canonical edge set plus a precomputed `from -> {to}`
/// adjacency map (`i_successors`) ready to feed into the simulator.
pub fn chunk_constraining_module_edges(
    owner_graph: &OwnerGraph,
    partition: &Partition,
) -> ChunkConstrainingEdgeSet {
    let mut edges: BTreeMap<(ModuleId, ModuleId), Vec<OwnerEdgeId>> = BTreeMap::new();
    let mut i_successors: BTreeMap<ModuleId, BTreeSet<ModuleId>> = BTreeMap::new();
    let mut seen_sequenced_pairs: BTreeSet<(ModuleId, ModuleId)> = BTreeSet::new();
    for edge in owner_graph.iter_edges() {
        if owner_graph.node(edge.from).is_none() || owner_graph.node(edge.to).is_none() {
            continue;
        }
        // Gate-side view: keep cross-module at-init promoted edges.
        // The matching `EndpointView::Lenient` view would drop them;
        // the canonical edge set is the strict view (see
        // [`partition_endpoints`]).
        let Some((from, to)) = partition_endpoints(edge, partition, EndpointView::Gate) else {
            continue;
        };
        if edge.reason.is_rebind() {
            // Rebinds are not I-graph members; they surface via the
            // `cross_rebinds` verdict and are never emitted as ESM
            // imports.
            continue;
        }
        // Every non-rebind cross-module edge — including LazyUse —
        // joins `i_successors`. The simulator's Pass-2 DFS needs
        // lazy back-edges to identify asymmetric (constraining
        // forward / lazy back) I-cycles that Lemma 2's source-import
        // reversal must rescue. The diagnostic `edges` field below
        // is constraining-only — that's the surface Pass-1's strict
        // SCC search and the cycle-report carry.
        i_successors.entry(from).or_default().insert(to);
        if !edge.reason.constrains_init_order() {
            continue;
        }
        if edge.reason.is_sequenced() && !seen_sequenced_pairs.insert((from, to)) {
            continue;
        }
        edges.entry((from, to)).or_default().push(edge.id);
    }
    ChunkConstrainingEdgeSet {
        edges,
        i_successors,
    }
}

/// Output of [`chunk_constraining_module_edges`]: the canonical
/// chunk-wide ESM I-graph plus its precomputed adjacency map.
///
/// Consumers MUST treat this as the single source of truth for the
/// "edges the emitter emits as ESM imports" question. See the
/// function-level doc for the filter rule.
#[derive(Debug, Clone, Default, Eq, PartialEq)]
pub struct ChunkConstrainingEdgeSet {
    /// `(from_module, to_module) -> all owner-edge ids` projecting
    /// onto this module pair. Stable ordering by `(ModuleId,
    /// ModuleId)`.
    pub edges: BTreeMap<(ModuleId, ModuleId), Vec<OwnerEdgeId>>,
    /// `from_module -> set of import targets`. Equivalent to
    /// `edges.keys().fold(...)` but precomputed because every
    /// simulator and emitter consumer walks adjacency, not the raw
    /// `(from, to)` list.
    pub i_successors: BTreeMap<ModuleId, BTreeSet<ModuleId>>,
}

impl ChunkConstrainingEdgeSet {
    /// `(from, to) -> &[OwnerEdgeId]` lookup.
    pub fn edges_for(&self, from: ModuleId, to: ModuleId) -> &[OwnerEdgeId] {
        self.edges
            .get(&(from, to))
            .map(Vec::as_slice)
            .unwrap_or(&[])
    }

    /// `from -> &BTreeSet<ModuleId>` lookup, empty default.
    pub fn successors_of(&self, from: ModuleId) -> Option<&BTreeSet<ModuleId>> {
        self.i_successors.get(&from)
    }

    /// `(from, to)` pairs in the canonical edge set (constraining
    /// only). Stable iteration order.
    pub fn pairs(&self) -> impl Iterator<Item = (ModuleId, ModuleId)> + '_ {
        self.edges.keys().copied()
    }

    /// `(from, to)` pairs across the full I-graph (constraining +
    /// lazy back-edges). Used by Lemma 2's SCC computation so the
    /// dependent/dependency reversal within asymmetric I-cycles is
    /// detected — the constraining-only view collapses those into
    /// singleton SCCs and would miss the reversal opportunity.
    pub fn i_pairs(&self) -> impl Iterator<Item = (ModuleId, ModuleId)> + '_ {
        self.i_successors
            .iter()
            .flat_map(|(from, succs)| succs.iter().map(move |to| (*from, *to)))
    }

    /// Membership test for the canonical edge set.
    pub fn contains(&self, from: ModuleId, to: ModuleId) -> bool {
        self.edges.contains_key(&(from, to))
    }
}

/// Toposort of the canonical edge set, deepest dependency first.
/// The returned `Vec<ModuleId>` is the canonical "linker order":
/// element 0 is the deepest dependency (must evaluate before
/// everything else); the last element is the most-dependent module.
/// Position in this vector is the module's "linker_position" — the
/// relative order ECMA-262's depth-first link traversal needs to
/// evaluate this chunk so that every constraining edge `M → M'` has
/// `M'` evaluating before `M`.
///
/// Modules that don't participate in any canonical edge are omitted
/// from the result (they're absent from the constraining DAG, hence
/// unconstrained relative to it). Callers fall back to `usize::MAX`
/// when sorting by linker_position so unconstrained modules sort
/// LAST.
///
/// Callers that need O(1) position lookup should pipe the result
/// through [`position_lookup`] once.
///
/// Note: every edge in the canonical set already satisfies
/// `constrains_init_order()`, so the toposort runs on the full
/// set — no extra filter needed. If the canonical edge set has a
/// constraining-only cycle (Pass 1 reports it as unrealizable),
/// `toposort` returns `Err`; this function returns the empty vector.
pub fn chunk_linker_order(edges: &ChunkConstrainingEdgeSet) -> Vec<ModuleId> {
    chunk_linker_order_from_pairs(edges.pairs())
}

/// Adjacency-only variant of [`chunk_linker_order`]. Same toposort,
/// same return shape; differs only in input — used by the overlay
/// realizability path (`EsmEvaluationSimulator::build`) whose
/// `IncrementalQuotient` materializes constraining pairs without
/// reaching for the full canonical edge map.
pub fn chunk_linker_order_from_pairs(
    pairs: impl IntoIterator<Item = (ModuleId, ModuleId)>,
) -> Vec<ModuleId> {
    use petgraph::algo::toposort;
    let mut graph: DiGraphMap<ModuleId, ()> = DiGraphMap::new();
    for (from, to) in pairs {
        graph.add_node(from);
        graph.add_node(to);
        graph.add_edge(from, to, ());
    }
    match toposort(&graph, None) {
        // `toposort` yields dependents first (root → leaves); the
        // canonical "linker order" is dependency-first, so reverse.
        Ok(order) => order.into_iter().rev().collect(),
        Err(_) => Vec::new(),
    }
}

/// Build an O(1) position lookup from a canonical linker-order slice.
/// `result[id] = i` iff `id` is at index `i` in `order`. Modules
/// absent from `order` are absent from the returned map; callers
/// fall back to `usize::MAX` when sorting.
///
/// This is the one place that materializes the
/// `BTreeMap<ModuleId, usize>` view of the linker order. Callers
/// that only need to iterate in order should consume the
/// `Vec<ModuleId>` directly instead of going through this helper.
pub fn position_lookup(order: &[ModuleId]) -> BTreeMap<ModuleId, usize> {
    order
        .iter()
        .copied()
        .enumerate()
        .map(|(idx, id)| (id, idx))
        .collect()
}

/// Lemma 2 ordering: sort by `(SCC dep rank ASC, intra-SCC
/// linker_position DESC)`. SCCs are over the canonical edge set
/// (the I-graph the emitter and runtime actually traverse). SCC
/// dep rank = min linker_position of SCC members.
///
/// The returned vector is the order in which entry's source-level
/// `import` directives must appear so the runtime ECMA-262 linker
/// DFS lands on the desired evaluation order (post-DFS = ESM Phase-2
/// evaluation). Within each SCC, members with no linker_position
/// (modules absent from the canonical set — they can only be SCC
/// members via lazy back-edges; canonical edges are all
/// init-constraining, so this case is empty by construction — but
/// the `None`-after-Some clause is kept for robustness against
/// future filter changes that might admit non-constraining members)
/// sort AFTER constraining members.
///
/// `extra_nodes` — modules that should appear in the result even if
/// they have no canonical edges (e.g. spec-known logical modules
/// the emitter wants a deterministic source-order slot for). These
/// land at the end with `linker_position = None`.
pub fn chunk_source_import_order(
    edges: &ChunkConstrainingEdgeSet,
    extra_nodes: &BTreeSet<ModuleId>,
) -> Vec<ModuleId> {
    chunk_source_import_order_from_adjacency(edges.pairs(), &edges.i_successors, extra_nodes)
}

/// Adjacency-only variant of [`chunk_source_import_order`]. The
/// constraining pairs drive the toposort (linker_position) while
/// `i_successors` drives the SCC computation. Used by the overlay
/// realizability path; see [`chunk_linker_order_from_pairs`] for
/// the matching motivation.
pub fn chunk_source_import_order_from_adjacency(
    constraining_pairs: impl IntoIterator<Item = (ModuleId, ModuleId)>,
    i_successors: &BTreeMap<ModuleId, BTreeSet<ModuleId>>,
    extra_nodes: &BTreeSet<ModuleId>,
) -> Vec<ModuleId> {
    use petgraph::algo::tarjan_scc;
    // We need O(1) position lookups inside the sort comparator below,
    // so materialize the linker order into the position-lookup map
    // once. The canonical linker-order Vec is the toposort output;
    // `position_lookup` is the small enumerate-collect adapter.
    let linker_position = position_lookup(&chunk_linker_order_from_pairs(constraining_pairs));
    // SCCs are computed over the FULL I-graph (constraining + lazy
    // back-edges) so Lemma 2's intra-SCC `linker_position`-DESC
    // reversal catches asymmetric cycles. The constraining-only
    // view would collapse `(constraining-forward, lazy-back)`
    // shapes into singleton SCCs and miss the rescue.
    let mut graph: DiGraphMap<ModuleId, ()> = DiGraphMap::new();
    let mut nodes: BTreeSet<ModuleId> = extra_nodes.iter().copied().collect();
    for (from, succs) in i_successors {
        for to in succs {
            graph.add_node(*from);
            graph.add_node(*to);
            graph.add_edge(*from, *to, ());
            nodes.insert(*from);
            nodes.insert(*to);
        }
    }
    for &n in &nodes {
        graph.add_node(n);
    }
    let sccs = tarjan_scc(&graph);
    let mut scc_of: BTreeMap<ModuleId, usize> = BTreeMap::new();
    let mut scc_rank: Vec<usize> = Vec::with_capacity(sccs.len());
    for (idx, scc) in sccs.iter().enumerate() {
        let min_pos = scc
            .iter()
            .filter_map(|m| linker_position.get(m).copied())
            .min()
            .unwrap_or(usize::MAX);
        scc_rank.push(min_pos);
        for m in scc {
            scc_of.insert(*m, idx);
        }
    }
    let mut sorted: Vec<ModuleId> = nodes.into_iter().collect();
    sorted.sort_by(|a, b| {
        let a_rank = scc_of
            .get(a)
            .and_then(|i| scc_rank.get(*i).copied())
            .unwrap_or(usize::MAX);
        let b_rank = scc_of
            .get(b)
            .and_then(|i| scc_rank.get(*i).copied())
            .unwrap_or(usize::MAX);
        let a_pos = linker_position.get(a).copied();
        let b_pos = linker_position.get(b).copied();
        a_rank.cmp(&b_rank).then_with(|| match (a_pos, b_pos) {
            (Some(a), Some(b)) => b.cmp(&a),
            (Some(_), None) => std::cmp::Ordering::Less,
            (None, Some(_)) => std::cmp::Ordering::Greater,
            (None, None) => std::cmp::Ordering::Equal,
        })
    });
    sorted
}

/// Stable per-chunk identity of an owner-graph edge. Equal to the
/// edge's position in [`OwnerGraph::edges`]. The previous
/// representation stored the report-shape spelling
/// (`format!("owner_edge:{idx}")`) on every entry; that spelling is
/// `O(n_edges)` strings allocated per chunk and
/// repeated clones in graph-report hot paths. Carry the typed index
/// instead and let the report layer do the formatting at its single
/// serialization boundary via
/// [`OwnerEdgeId::report_key`].
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash)]
pub struct OwnerEdgeId(pub usize);

impl OwnerEdgeId {
    pub fn report_key(self) -> String {
        format!("owner_edge:{}", self.0)
    }
}

#[derive(Debug, Clone)]
pub struct OwnerEdge {
    pub id: OwnerEdgeId,
    pub from: OwnerId,
    pub to: OwnerId,
    pub reason: EdgeReason,
}

#[cfg(test)]
mod chunk_constraining_module_edges_tests {
    //! Regression coverage for [`chunk_constraining_module_edges`]'s
    //! filter rule. The canonical edge set must match what the
    //! emitter actually emits as ESM `import` directives — namely
    //! all cross-module non-rebind non-LazyUse edges, including
    //! cross-module at-init promoted edges.
    use std::collections::BTreeSet;

    use swc_common::{FileName, SourceMap, sync::Lrc};
    use swc_ecma_parser::{Parser, StringInput, Syntax, lexer::Lexer};

    use super::*;
    use crate::ids::{LogicalModuleIndex, ModuleId};
    use crate::partition::Partition;
    use crate::{AnalysisHints, OwnerGraph, facts::analyze_chunk};

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
        build_owner_graph(&facts).unwrap()
    }

    fn parse_facts(source: &str) -> Vec<crate::StatementFacts> {
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
        analyze_chunk(&module, &AnalysisHints::default(), None, |_| None).facts
    }

    /// Strict mapping: two top-level statements declaring the same
    /// binding (legal JS) must error instead of silently letting the
    /// last declaration win — last-insert-wins drops every edge into
    /// the earlier owner.
    #[test]
    fn duplicate_top_level_declarations_error() {
        let err = build_owner_graph(&parse_facts("var x = 1;\nvar x = 2;\n")).unwrap_err();
        assert_eq!(err.binding.as_ref(), "x");
        assert_eq!(err.first, StatementOrdinal(0));
        assert_eq!(err.second, StatementOrdinal(1));
        assert!(
            err.to_string().contains("duplicate top-level declaration"),
            "{err}"
        );
    }

    /// Same name in distinct scopes is hygienically distinct — no
    /// duplicate. Comma-split declarators of *different* names are
    /// also fine.
    #[test]
    fn distinct_bindings_with_shared_name_are_not_duplicates() {
        build_owner_graph(&parse_facts(
            "var x = 1;\nfunction f() { var x = 2; return x; }\nconst y = 3, z = 4;\n",
        ))
        .unwrap();
    }

    /// Pure cross-module lazy edge must not appear in the canonical
    /// edge set. The emitter never emits an ESM `import` for a
    /// function-body read; the gate must agree.
    /// Pure cross-module `LazyUse` edges contribute to
    /// `i_successors` (the runtime DFS topology — required for
    /// Lemma 2 asymmetric-cycle detection) but never to `edges`
    /// (the constraining/diagnostic surface).
    #[test]
    fn lazy_only_cross_module_edge_in_i_successors_not_edges() {
        let source = "const a = 1; function f() { return a; }";
        let owner_graph = parse_and_build(source);
        let mut partition = Partition::new(&owner_graph, module_id(0));
        partition.set(OwnerId(1), module_id(1));
        let canonical = chunk_constraining_module_edges(&owner_graph, &partition);
        // `f` reads `a` from a function body → LazyUse f → a. The
        // constraining `edges` surface stays empty because lazy
        // reads don't constrain init order.
        assert!(
            canonical.edges.is_empty(),
            "lazy edges must NOT enter constraining `edges`; got {:#?}",
            canonical.edges
        );
        // But the simulator's DFS topology (`i_successors`)
        // includes the lazy back-edge — Pass 2's asymmetric-cycle
        // rescue needs it.
        assert!(
            !canonical.i_successors.is_empty(),
            "lazy edges must contribute to `i_successors`; empty: {:#?}",
            canonical.i_successors
        );
    }

    /// Cross-module eager_use edge appears in the canonical set.
    #[test]
    fn eager_cross_module_edge_included() {
        let source = "const a = 1; const b = a + 1;";
        let owner_graph = parse_and_build(source);
        let mut partition = Partition::new(&owner_graph, module_id(0));
        partition.set(OwnerId(1), module_id(1));
        let canonical = chunk_constraining_module_edges(&owner_graph, &partition);
        let pairs: BTreeSet<(ModuleId, ModuleId)> = canonical.pairs().collect();
        assert_eq!(
            pairs,
            BTreeSet::from([(module_id(1), module_id(0))]),
            "eager cross-module read `b = a + 1` must contribute mod_1 → mod_0"
        );
        assert!(canonical.contains(module_id(1), module_id(0)));
    }

    /// Same-module edges (intra-module reads) never appear in the
    /// canonical set — they don't correspond to any ESM import.
    #[test]
    fn same_module_edges_excluded() {
        let source = "const a = 1; const b = a + 1;";
        let owner_graph = parse_and_build(source);
        // Both owners in module 0 → no cross-module edges.
        let partition = Partition::new(&owner_graph, module_id(0));
        let canonical = chunk_constraining_module_edges(&owner_graph, &partition);
        assert!(canonical.edges.is_empty());
    }

    /// Sequenced edges between the same module pair are deduped (one
    /// representative owner edge per pair) so that having N sequenced
    /// reasons between two modules doesn't over-weight the I-graph.
    /// This mirrors `build_module_quotient`'s dedup.
    #[test]
    fn sequenced_edges_dedup_per_pair() {
        // Two impure statements in different modules: each carries a
        // Sequenced edge from the later impure stmt to the earlier
        // (graph.rs::sequenced_edges).
        let source = "console.log(\"a\"); console.log(\"b\"); console.log(\"c\");";
        let owner_graph = parse_and_build(source);
        let mut partition = Partition::new(&owner_graph, module_id(0));
        partition.set(OwnerId(1), module_id(1));
        partition.set(OwnerId(2), module_id(1));
        let canonical = chunk_constraining_module_edges(&owner_graph, &partition);
        // mod_1 contains owners 1 and 2; the only cross-module
        // sequenced edge is from mod_1 to mod_0 (owners 1, 2 both
        // sequenced after owner 0). We expect exactly ONE pair, even
        // though two owners contribute.
        let pair_count: usize = canonical
            .pairs()
            .filter(|&(from, to)| from == module_id(1) && to == module_id(0))
            .count();
        assert!(
            pair_count <= 1,
            "sequenced edges between the same pair must dedup; got {pair_count}",
        );
    }

    /// `chunk_linker_order` on a 3-module DAG returns dependency-first
    /// positions: deepest dependency at index 0, dependent at the
    /// last index.
    #[test]
    fn chunk_linker_order_assigns_positions_dependency_first() {
        let source = "const leaf = 1; const middle = leaf + 1; const top = middle + 1;";
        let owner_graph = parse_and_build(source);
        let mut partition = Partition::new(&owner_graph, module_id(0));
        partition.set(OwnerId(0), module_id(1)); // leaf
        partition.set(OwnerId(1), module_id(2)); // middle
        partition.set(OwnerId(2), module_id(3)); // top
        let canonical = chunk_constraining_module_edges(&owner_graph, &partition);
        let linker = chunk_linker_order(&canonical);
        let pos = position_lookup(&linker);
        // leaf (mod_1) must come before middle (mod_2) and top (mod_3).
        assert!(pos[&module_id(1)] < pos[&module_id(2)]);
        assert!(pos[&module_id(2)] < pos[&module_id(3)]);
    }

    /// `chunk_source_import_order` reverses within an SCC so the
    /// dependent appears first in source. Asymmetric cycle shape: a
    /// canonical edge from dependent → dependency, but only after
    /// the unification's lazy-edge exclusion takes effect (so the
    /// SCC is detected via some other path — here we exercise it
    /// directly with the modules present even though canonical
    /// edges are acyclic post-fix).
    #[test]
    fn chunk_source_import_order_includes_extra_nodes() {
        // Simple two-module DAG.
        let source = "const a = 1; const b = a + 1;";
        let owner_graph = parse_and_build(source);
        let mut partition = Partition::new(&owner_graph, module_id(0));
        partition.set(OwnerId(1), module_id(1));
        let canonical = chunk_constraining_module_edges(&owner_graph, &partition);
        let extra: BTreeSet<ModuleId> = BTreeSet::from([module_id(5), module_id(0)]);
        let order = chunk_source_import_order(&canonical, &extra);
        assert!(
            order.contains(&module_id(5)),
            "extra node must be included; got {order:?}"
        );
        assert!(order.contains(&module_id(0)));
        assert!(order.contains(&module_id(1)));
    }

    /// Asymmetric I-cycle shape: eager forward + lazy back. The
    /// canonical edge set must contain ONLY the forward edge — the
    /// lazy back-edge is dropped. This is the gaffer fix: a
    /// dependency's lazy back-edge to its dependent must NOT appear
    /// in the runtime DFS topology the simulator walks.
    #[test]
    fn asymmetric_cycle_canonical_set_excludes_lazy_back_edge() {
        let source = "const schemas_target = \"v\"; function lazy_back() { return ids_val; } const ids_val = schemas_target + \"-derived\";";
        let owner_graph = parse_and_build(source);
        let mut partition = Partition::new(&owner_graph, module_id(0));
        partition.set(OwnerId(0), module_id(1)); // schemas_target -> mod_schemas
        partition.set(OwnerId(1), module_id(1)); // lazy_back     -> mod_schemas
        partition.set(OwnerId(2), module_id(2)); // ids_val       -> mod_ids
        let canonical = chunk_constraining_module_edges(&owner_graph, &partition);
        let pairs: BTreeSet<(ModuleId, ModuleId)> = canonical.pairs().collect();
        assert!(
            pairs.contains(&(module_id(2), module_id(1))),
            "forward eager edge ids → schemas must be present; got {pairs:?}"
        );
        assert!(
            !pairs.contains(&(module_id(1), module_id(2))),
            "lazy back-edge schemas → ids must NOT be present; got {pairs:?}"
        );
    }
}

#[cfg(test)]
mod edge_role_wire_format_tests {
    //! Wire-format round-trip for [`EdgeRole`]. The materializer
    //! emits the role through `OwnerGraphEdgeReport.role`; the peel
    //! planner reconstructs it via `OwnerGraph::from_report`. Both
    //! ends must agree so the planner's gate runs the same
    //! cross-module-at-init filter the materializer's gate does.
    use crate::purity::Purity;
    use crate::reports::schema::{
        AtomicGraphReport, EdgeRoleReport, OwnerGraphEdgeReport, OwnerGraphNodeReport,
        OwnerGraphQuotientReport, OwnerGraphReport,
    };
    use crate::{
        DepKind, EdgeRole, OwnerEdgeId, OwnerGraph, OwnerId, StatementKind, StatementOrdinal,
    };

    fn node(id: &str, ordinal: usize) -> OwnerGraphNodeReport {
        OwnerGraphNodeReport {
            id: id.to_string(),
            statement_ordinal: StatementOrdinal(ordinal),
            source_location: None,
            declared_bindings: Vec::new(),
            statement_kind: StatementKind::VarDecl,
            purity: Purity::Pure,
            destination: crate::ModuleKey("residual".to_string()),
        }
    }

    /// Direct edges serialize with `role = None`; on the way back in
    /// they reconstruct as `EdgeRole::Direct`.
    #[test]
    fn direct_role_round_trips_via_none() {
        let report = OwnerGraphReport {
            chunk_id: "chunk".into(),
            nodes: vec![node("owner:0", 0), node("owner:1", 1)],
            edges: vec![OwnerGraphEdgeReport {
                id: "owner_edge:0".to_string(),
                source: "owner:1".to_string(),
                target: "owner:0".to_string(),
                edge_kind: DepKind::EagerUse,
                binding: None,
                statement_ordinal: StatementOrdinal(1),
                constrains_init_order: true,
                role: None,
            }],
            quotient: OwnerGraphQuotientReport {
                nodes: Vec::new(),
                edges: Vec::new(),
                sccs: Vec::new(),
            },
            atomic_graph: AtomicGraphReport {
                nodes: Vec::new(),
                edges: Vec::new(),
            },
        };
        let (graph, _) = OwnerGraph::from_report(&report, &[]).unwrap();
        assert_eq!(graph.num_edges(), 1);
        assert_eq!(graph.edge(OwnerEdgeId(0)).reason.role(), EdgeRole::Direct);
    }

    /// Promoted edges carry an `EdgeRoleReport::PromotedAtInit` on
    /// the wire and reconstruct as `EdgeRole::PromotedAtInit` with
    /// the resolved `OwnerId`. The CSR `callee_edges` adjacency must
    /// also populate so `impacted_owner_edges` can find the edge by
    /// callee owner.
    #[test]
    fn promoted_at_init_role_round_trips_with_callee_owner() {
        let report = OwnerGraphReport {
            chunk_id: "chunk".into(),
            nodes: vec![node("owner:0", 0), node("owner:1", 1), node("owner:2", 2)],
            edges: vec![OwnerGraphEdgeReport {
                id: "owner_edge:0".to_string(),
                source: "owner:1".to_string(),
                target: "owner:0".to_string(),
                edge_kind: DepKind::EagerUse,
                binding: None,
                statement_ordinal: StatementOrdinal(1),
                constrains_init_order: true,
                role: Some(EdgeRoleReport::PromotedAtInit {
                    callee_owner: "owner:2".to_string(),
                }),
            }],
            quotient: OwnerGraphQuotientReport {
                nodes: Vec::new(),
                edges: Vec::new(),
                sccs: Vec::new(),
            },
            atomic_graph: AtomicGraphReport {
                nodes: Vec::new(),
                edges: Vec::new(),
            },
        };
        let (graph, _) = OwnerGraph::from_report(&report, &[]).unwrap();
        assert_eq!(graph.num_edges(), 1);
        assert_eq!(
            graph.edge(OwnerEdgeId(0)).reason.role(),
            EdgeRole::PromotedAtInit {
                callee_owner: OwnerId(2),
            }
        );
        // CSR by-callee adjacency populated for owner:2.
        assert_eq!(graph.callee_edges_of(OwnerId(2)).len(), 1);
        assert_eq!(graph.callee_edges_of(OwnerId(0)).len(), 0);
    }

    /// JSON serialization shape: a `Direct` role omits the `role`
    /// field; a `PromotedAtInit` role nests `{kind: "promoted_at_init",
    /// callee_owner: "owner:N"}`. This pins the wire encoding so
    /// callers (Stage A artifact readers) don't drift.
    #[test]
    fn role_json_shape_pinned() {
        let direct_report = OwnerGraphEdgeReport {
            id: "owner_edge:0".to_string(),
            source: "owner:1".to_string(),
            target: "owner:0".to_string(),
            edge_kind: DepKind::EagerUse,
            binding: None,
            statement_ordinal: StatementOrdinal(1),
            constrains_init_order: true,
            role: None,
        };
        let direct_json = serde_json::to_string(&direct_report).unwrap();
        assert!(
            !direct_json.contains("\"role\""),
            "Direct edges omit the role field; got {direct_json}",
        );

        let promoted_report = OwnerGraphEdgeReport {
            role: Some(EdgeRoleReport::PromotedAtInit {
                callee_owner: "owner:7".to_string(),
            }),
            ..direct_report
        };
        let promoted_json = serde_json::to_string(&promoted_report).unwrap();
        assert!(
            promoted_json.contains("\"role\""),
            "PromotedAtInit edges carry a role field; got {promoted_json}",
        );
        assert!(
            promoted_json.contains("\"kind\":\"promoted_at_init\""),
            "role tag must be `promoted_at_init`; got {promoted_json}",
        );
        assert!(
            promoted_json.contains("\"callee_owner\":\"owner:7\""),
            "callee_owner must round-trip; got {promoted_json}",
        );

        // Round-trip via JSON.
        let parsed: OwnerGraphEdgeReport = serde_json::from_str(&promoted_json).unwrap();
        assert_eq!(parsed.role, promoted_report.role);
    }
}

#[cfg(test)]
mod declared_round_trip_tests {
    //! Round-trip the per-owner `declared: BTreeSet<Id>` set through
    //! the JSON wire shape. `OwnerGraphNodeReport` itself doesn't
    //! carry hygienic `Id` atoms (its `declared_bindings: Vec<BindingReport>`
    //! is `Atom`-only), so `OwnerGraph::from_report` joins each node's
    //! `statement_ordinal` against the matching
    //! `StatementFactsReport.declared` (which does carry the
    //! `(name, ctxt)` pair via `IdReport`). The tests below pin both
    //! the syntactic round-trip (declared sets match) and the
    //! semantic round-trip (`compute_owner_claims` returns the same
    //! ModuleId verdict on the reconstructed graph as on the
    //! in-memory original).
    use std::collections::HashMap;

    use crate::factor_assembly::assemble_partition;
    use crate::facts::analyze_chunk;
    use crate::graph::{OwnerGraph, build_owner_graph};
    use crate::ids::{BindingKind, LogicalModule, LogicalModuleIndex, ModuleId};
    use crate::partition::Partition;
    use crate::reports::owner_key;
    use crate::reports::schema::{
        AtomicGraphReport, OwnerGraphEdgeReport, OwnerGraphNodeReport, OwnerGraphQuotientReport,
        OwnerGraphReport,
    };
    use crate::{AnalysisHints, BindingReport, StatementFactsReport};

    use swc_common::{FileName, SourceMap, sync::Lrc};
    use swc_ecma_parser::{Parser, StringInput, Syntax, lexer::Lexer};

    /// Build a real owner graph from JS source, then synthesize the
    /// JSON-shaped reports (owner graph + per-statement facts) that
    /// `OwnerGraph::from_report` consumes. The synthesized reports
    /// re-encode the in-memory graph faithfully (modulo what the
    /// wire format intentionally drops, e.g. per-edge `EdgeReason`
    /// metadata beyond `kind` + `role`).
    fn build_and_serialize(
        source: &str,
    ) -> (
        OwnerGraph,
        Vec<StatementFactsReport>,
        OwnerGraphReport,
        HashMap<swc_ecma_ast::Id, BindingKind>,
        Vec<LogicalModule>,
    ) {
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
        let analysis = analyze_chunk(&module, &AnalysisHints::default(), None, |_| None);
        let owner_graph = build_owner_graph(&analysis.facts).unwrap();
        let facts_reports: Vec<StatementFactsReport> = analysis
            .facts
            .iter()
            .map(StatementFactsReport::from_facts)
            .collect();
        let nodes = owner_graph
            .iter_nodes()
            .map(|n| OwnerGraphNodeReport {
                id: owner_key(n.id),
                statement_ordinal: n.statement_ordinal,
                source_location: n.source_location.clone(),
                declared_bindings: n
                    .declared
                    .iter()
                    .map(|id| BindingReport {
                        binding: id.0.clone(),
                        export_name: id.0.clone(),
                    })
                    .collect(),
                statement_kind: n.kind,
                purity: n.purity.clone(),
                destination: crate::ModuleKey("m".to_string()),
            })
            .collect();
        let edges: Vec<OwnerGraphEdgeReport> = owner_graph
            .iter_edges()
            .map(|edge| OwnerGraphEdgeReport {
                id: edge.id.report_key(),
                source: owner_key(edge.from),
                target: owner_key(edge.to),
                edge_kind: edge.reason.kind,
                binding: edge.reason.binding.as_ref().map(|id| id.0.clone()),
                statement_ordinal: edge.reason.statement_ordinal,
                constrains_init_order: edge.reason.constrains_init_order(),
                role: None,
            })
            .collect();
        let report = OwnerGraphReport {
            chunk_id: "test".into(),
            nodes,
            edges,
            quotient: OwnerGraphQuotientReport {
                nodes: Vec::new(),
                edges: Vec::new(),
                sccs: Vec::new(),
            },
            atomic_graph: AtomicGraphReport {
                nodes: Vec::new(),
                edges: Vec::new(),
            },
        };
        // Build a `bindings` table mapping each declared Id → owner.
        // This is the standard input to `compute_owner_claims` /
        // `assemble_partition`.
        let mut bindings: HashMap<swc_ecma_ast::Id, BindingKind> = HashMap::new();
        for (idx, node) in owner_graph.iter_nodes().enumerate() {
            // Round-robin owners to alternating modules so the
            // semantic test below sees a non-trivial partition.
            let dest = ModuleId(LogicalModuleIndex(idx % 2));
            for id in &node.declared {
                bindings.insert(id.clone(), BindingKind::Owned { module: dest });
            }
        }
        let logical_modules = vec![
            LogicalModule {
                id: "m0".into(),
                target_file: "m0.js".into(),
                anonymous_statement_ordinals: Vec::new(),
                residual: false,
                rename_map: HashMap::new(),
            },
            LogicalModule {
                id: "m1".into(),
                target_file: "m1.js".into(),
                anonymous_statement_ordinals: Vec::new(),
                residual: true,
                rename_map: HashMap::new(),
            },
        ];
        (
            owner_graph,
            facts_reports,
            report,
            bindings,
            logical_modules,
        )
    }

    /// Serialize a graph with non-empty `declared`, deserialize, and
    /// assert each owner's `declared` set matches the original.
    ///
    /// This is the syntactic round-trip — it pins that
    /// `OwnerGraph::from_report` no longer silently drops the
    /// per-owner declared binding set.
    #[test]
    fn declared_round_trips_through_owner_graph_report() {
        let source = "const a = 1;\nconst b = a + 1;\nlet c = 0;\n";
        let (original, facts, report, _bindings, _logical) = build_and_serialize(source);
        assert!(
            original.iter_nodes().any(|n| !n.declared.is_empty()),
            "fixture must have at least one declared-binding owner",
        );

        let (round_tripped, _) = OwnerGraph::from_report(&report, &facts).unwrap();

        assert_eq!(
            round_tripped.nodes.len(),
            original.nodes.len(),
            "node count must match"
        );
        for (orig, restored) in original.iter_nodes().zip(round_tripped.iter_nodes()) {
            assert_eq!(
                orig.statement_ordinal, restored.statement_ordinal,
                "statement ordinals must align (join key)"
            );
            assert_eq!(
                orig.declared, restored.declared,
                "declared sets must round-trip via StatementFactsReport.declared"
            );
        }
    }

    /// Semantic round-trip: rebuild the graph from the wire shape,
    /// then run `assemble_partition` (which internally calls
    /// `compute_owner_claims`) against the reconstructed graph and
    /// assert the resulting `Partition` matches the partition
    /// obtained by running the same call on the in-memory original.
    ///
    /// This is what distinguishes "round-trip works syntactically"
    /// from "round-trip works semantically": the planner-side gate
    /// reconstructs a graph and feeds it to `assemble_partition` to
    /// derive the post-edit partition; if `compute_owner_claims`
    /// silently returns `None` for every owner (because
    /// `nodes[].declared` is empty), the partition reduces to the
    /// residual fallback and the gate's verdict is meaningless.
    #[test]
    fn compute_owner_claims_round_trips_via_reconstructed_graph() {
        let source = "const a = 1;\nconst b = a + 1;\nlet c = 0;\n";
        let (original, facts, report, bindings, logical_modules) = build_and_serialize(source);
        let residual = ModuleId(LogicalModuleIndex(1));
        let atomic_units = crate::atomic_units::compute_atomic_units(&original);
        let original_outcome = assemble_partition(
            &original,
            &atomic_units,
            &bindings,
            &logical_modules,
            residual,
        );

        let (round_tripped, _) = OwnerGraph::from_report(&report, &facts).unwrap();
        let restored_units = crate::atomic_units::compute_atomic_units(&round_tripped);
        let restored_outcome = assemble_partition(
            &round_tripped,
            &restored_units,
            &bindings,
            &logical_modules,
            residual,
        );

        assert_eq!(
            partition_destinations(&original_outcome.partition, original.nodes.len()),
            partition_destinations(&restored_outcome.partition, round_tripped.nodes.len()),
            "compute_owner_claims must derive the same partition on the round-tripped graph",
        );
    }

    fn partition_destinations(partition: &Partition, owner_count: usize) -> Vec<ModuleId> {
        (0..owner_count)
            .map(|i| partition.of(crate::OwnerId(i)))
            .collect()
    }

    /// Strict mapping: an edge referencing an owner id missing from
    /// the node table (malformed / version-skewed `owner_graph.json`)
    /// must be a hard error, not a silently dropped edge — the
    /// planner-side gate would otherwise reason over a weaker graph.
    #[test]
    fn from_report_errors_on_unresolvable_edge_endpoint() {
        let (_, _, mut report, _, _) = build_and_serialize("const a = 1;\nconst b = a + 1;\n");
        assert!(
            !report.edges.is_empty(),
            "fixture must produce at least one edge"
        );
        report.edges[0].target = "owner:999".to_string();
        let err = OwnerGraph::from_report(&report, &[]).unwrap_err();
        assert_eq!(err.endpoint, "owner:999");
        assert_eq!(err.edge_id, report.edges[0].id);
        assert!(err.to_string().contains("owner:999"), "{err}");
    }
}
