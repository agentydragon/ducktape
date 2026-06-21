use serde::{Deserialize, Serialize};
use swc_ecma_ast::Id;

use crate::StatementOrdinal;
use crate::partition::Partition;

use super::owner_graph::OwnerId;

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

#[derive(
    Debug,
    Clone,
    Copy,
    Eq,
    PartialEq,
    Ord,
    PartialOrd,
    Hash,
    Serialize,
    Deserialize,
    strum::IntoStaticStr,
    strum::Display,
)]
#[serde(rename_all = "snake_case")]
#[strum(serialize_all = "snake_case")]
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

/// Stable per-chunk identity of an owner-graph edge. Equal to the
/// edge's position in [`OwnerGraph::edges`]. The previous
/// representation stored the report-shape spelling
/// (`format!("owner_edge:{idx}")`) on every entry; that spelling is
/// `O(n_edges)` strings allocated per chunk and
/// repeated clones in graph-report hot paths. Carry the typed index
/// instead and let the report layer do the formatting at its single
/// serialization boundary via
/// [`OwnerEdgeId::report_key`].
///
/// [`OwnerGraph::edges`]: super::owner_graph::OwnerGraph
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
