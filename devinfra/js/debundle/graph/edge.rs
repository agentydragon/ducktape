use std::collections::BTreeSet;
use std::fmt;

use serde::{Deserialize, Serialize};
use source_identity::SourceUnitOrdinal;
use swc_atoms::Atom;
use swc_ecma_ast::Id;

use crate::ModuleId;
use crate::analysis_inputs::CommutingStatementsRef;
use crate::facts::{DataflowOpacityCause, EffectCell};
use crate::reports::schema::ExecutionPrecisionLossReason;

use super::build::PremiseVerdict;
use super::dependency_graph::SourceUnitId;

/// How an edge was emitted. Determines whether the gate / quotient /
/// reports consumers project the edge through the lenient (drop
/// cross-module promotions) or strict (keep them for soundness) view.
///
/// Variants:
/// - `Direct` — the edge was emitted by direct binding lookup in
///   `build_dependency_graph_with` (eager/lazy reads, rebinds, sequenced
///   side effects, local effects). The quotient drops same-module
///   edges (`from == to`) but never drops a `Direct` edge by role.
/// - `PromotedAtInit { callee_source_unit }` — `promote_at_init_calls`
///   manufactured the edge by lifting a function-body lazy read/rebind
///   into a caller's eager read. The lenient view (quotient, reports)
///   drops these when `assignment.of(callee_source_unit) != assignment.of(from)`
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
    PromotedAtInit { callee_source_unit: SourceUnitId },
}

impl EdgeRole {
    /// `true` if this is a `PromotedAtInit` role and the callee source unit
    /// lives in a different module than the caller per `module_of`.
    /// The lenient projection view (quotient, reports) drops such
    /// edges; the gate view keeps them.
    pub fn is_cross_module_promotion(
        self,
        from: SourceUnitId,
        module_of: impl Fn(SourceUnitId) -> ModuleId,
    ) -> bool {
        match self {
            EdgeRole::Direct => false,
            EdgeRole::PromotedAtInit { callee_source_unit } => {
                module_of(callee_source_unit) != module_of(from)
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
/// - `HoistedUse` retains a direct eager read's import topology when its
///   function declaration is already initialized during module instantiation.
///   The read imposes no evaluation-order constraint on that binding.
/// - `EagerRebind` / `LazyRebind` describe rebinding writes. A
///   cross-destination write is rejected outright because ESM imports
///   are read-only in the importing module; same-destination writes
///   are represented only at source unit level and don't become module
///   imports.
/// - `CoDeclaration` is emitted in both directions between statements
///   declaring the same `var` binding. A split therefore forms a forbidden
///   constraining cycle; these edges express placement equality.
/// - `CoLocation` keeps a spread's primitive-array producers with its
///   consumer so the original same-declaration source interval is retained.
/// - `Sequenced` contributes to `S` and constrains
///   realizability because source-order side effects require a
///   topological order.
/// - `LocalEffect` is a trusted target-local mutation (for example
///   a TypeScript `__decorate` helper application) that must
///   co-locate with the target source unit but should not impose global
///   side-effect ordering on unrelated source units.
///
/// `role` (see [`EdgeRole`]) selects which projection rule the
/// quotient/gate/reports consumers apply to the edge.
///
/// A `Sequenced` reason an author `commuting_statements` premise accepted is
/// marked inert (`inerted_by`): the edge stays in the graph as the record of
/// what source analysis found, but [`EdgeReason::constrains_init_order`] is
/// `false`, so no gate, quotient, linker-order or atomic-group consumer
/// treats it as a constraint. Only `Sequenced` reasons are ever inerted
/// (`graph/build.rs::apply_commuting_statements`).
#[derive(Debug, Clone)]
pub struct EdgeReason {
    pub kind: DepKind,
    pub statement_ordinal: SourceUnitOrdinal,
    pub binding: Option<Id>,
    pub(crate) role: EdgeRole,
    /// Present on every `Sequenced` edge built from facts: the emission
    /// rule that produced it and how it classified both endpoints.
    /// Boxed so the far more numerous binding edges stay small.
    sequenced_cause: Option<Box<SequencedCause>>,
    pub(crate) inerted_by: Option<CommutingStatementsRef>,
    /// How a `commuting_statements` premise over this edge's endpoints would
    /// treat it (`graph/build.rs::premise_verdict`); `None` for the kinds a
    /// premise never considers. Boxed like `sequenced_cause`.
    pub(crate) premise_verdict: Option<Box<PremiseVerdict>>,
}

/// Why a `Sequenced` edge `(from, to)` exists: the emission rule in
/// `build_dependency_graph_with` that fired and the effect
/// classification of the two source units it compared. Cycle reports
/// surface it so a spec author can see why the engine refuses to
/// commute the pair (`gate::CycleEdge::explanation`).
///
/// `from` is the later source unit; the edge reads "`from` must observe
/// `to`'s work as complete". Where several rules would emit the same
/// `(from, to)` pair, the first emitted cause is retained.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
#[serde(tag = "rule", rename_all = "snake_case")]
pub enum SequencedCause {
    /// Strict S-chain (`dataflow_aware_s_chain: false`): `from` is the
    /// impure top-level statement immediately after `to` in source
    /// order. Both endpoints are `Purity::NotPure`; the reasons live on
    /// the source-unit nodes.
    StrictChain,
    /// Consecutive at-init accesses (initializer, eager read or eager
    /// rebind) of a binding declared at several `var` sites. The cell is
    /// hoisted, so the accesses keep source order even when every access
    /// is pure.
    /// `source` / `target` are the provenance of `from`'s / `to`'s access.
    SharedVarAccess {
        binding: Atom,
        source: EffectProvenance,
        target: EffectProvenance,
    },
    /// At-init binding timeline (`promote_at_init_calls`): `to` and
    /// `from` are a writer and an observer (or two writers) of `binding`
    /// that must keep source order. `source` / `target` are the provenance
    /// of `from`'s / `to`'s effect named by `order`.
    BindingTimeline {
        binding: Atom,
        order: BindingTimelineOrder,
        source: EffectProvenance,
        target: EffectProvenance,
    },
    /// A synchronous callee invoked by `to` observed `binding` before
    /// `from`, the statement declaring it, ran; the pair keeps source
    /// order so the TDZ / initial-`undefined` observation is preserved.
    /// `observation` is the provenance of `to`'s observation; the
    /// declaration side is syntax.
    UninitializedObservation {
        binding: Atom,
        observation: EffectProvenance,
    },
    /// Cell timeline (dataflow-aware S-chain and array/Set content
    /// order): `conflicts` names every reason the timeline made `from`
    /// depend on `to`, given how each endpoint was classified.
    CellOrder {
        from: CellEffectClass,
        to: CellEffectClass,
        conflicts: BTreeSet<CellConflict>,
    },
}

/// Whether the engine derived one endpoint's binding effect behind a
/// binding-ordering edge or only assumed it (SPEC.md § Commuting statements).
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum EffectProvenance {
    /// Direct syntax or precise synchronous execution.
    Observed,
    /// Contributed only by the statement's fallback summary, which assumes
    /// every callback it stopped expanding may run; `reason` is the
    /// precision loss that opened that summary.
    Fallback {
        reason: ExecutionPrecisionLossReason,
    },
}

impl EffectProvenance {
    pub(crate) fn from_fallback(reason: Option<&ExecutionPrecisionLossReason>) -> Self {
        reason.map_or(Self::Observed, |reason| Self::Fallback {
            reason: reason.clone(),
        })
    }

    /// Observed wins: an effect also derived precisely is a fact.
    pub(crate) fn join(&mut self, other: &Self) {
        if *other == Self::Observed {
            *self = Self::Observed;
        }
    }
}

impl fmt::Display for EffectProvenance {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Observed => f.write_str("observed"),
            Self::Fallback { reason } => write!(f, "assumed by the {reason} fallback"),
        }
    }
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BindingTimelineOrder {
    /// `to` and `from` both write the binding; later writer last.
    WriterAfterWriter,
    /// `from` observes the binding after `to`, its nearest preceding writer.
    ReaderAfterWriter,
    /// `from` writes the binding after `to`, a preceding observer, read it.
    WriterAfterReader,
}

/// How the cell timeline classified one source unit's effects.
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum CellEffectClass {
    /// Exact `(reads, writes)` cell summary (`StatementEffectSummary`).
    Summarized,
    /// Not dataflow-summarizable (`causes` is the statement's
    /// `dataflow_opacity`): treated as touching every cell, so it
    /// depends on every prior impure source unit and is the barrier
    /// every later summarized source unit depends on.
    OpaqueBarrier {
        causes: BTreeSet<DataflowOpacityCause>,
    },
    /// Allocates array/Set cells; seeds their content timelines without
    /// depending on earlier heap effects.
    HeapAllocation,
    /// Opaque array/Set content operation: depends on the live content
    /// frontier and becomes the frontier for later observations.
    /// `causes` are the disjuncts of `emit_heap_content_order`'s
    /// opacity test that held.
    HeapFrontier { causes: BTreeSet<HeapBarrierCause> },
}

/// One disjunct of the heap-content opacity test in
/// `emit_heap_content_order` (docs/design.md "Emission modes").
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum HeapBarrierCause {
    /// The array-content collector recorded an opaque at-init effect:
    /// member write, mutating method call, escaped array, key
    /// enumeration, optional call or tagged template.
    ArrayContentEffects,
    /// Impure and not dataflow-summarizable (unproven call/`new`,
    /// member write through a binding, dynamic-dispatch shape).
    NonSummarizableImpure,
    /// Synchronous execution analysis reported incomplete effects
    /// (`synchronous_execution.complete = false`).
    IncompleteSynchronousExecution,
    /// An at-init callee is not a never-rebound direct chunk function,
    /// so its body's heap effects cannot be promoted.
    UnresolvedCallee,
    /// A resolved at-init callee's body is itself heap-opaque.
    CalleeHeapEffects,
}

/// One dependence the cell timeline found between `from` (later) and
/// `to` (earlier). Read/write vocabulary follows docs/design.md
/// "Emission modes".
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum CellConflict {
    /// `from` reads `cell`; `to` was its last writer.
    ReadAfterWrite { cell: EffectCellReport },
    /// `from` writes `cell`; `to` was its last writer.
    WriteAfterWrite { cell: EffectCellReport },
    /// `from` writes `cell`; `to` read it since its last write.
    WriteAfterRead { cell: EffectCellReport },
    /// `from` reads or writes `cell`; `to` allocated it.
    UseAfterAllocation { cell: EffectCellReport },
    /// `to` is the opaque barrier in force when `from` runs.
    AfterBarrier,
    /// `from` is an opaque barrier; `to` is a prior impure source unit.
    BarrierAfterPrior,
    /// `from` is an opaque heap operation; `to` allocated, wrote or read
    /// `cell` on the live content frontier.
    FrontierAfterCell { cell: EffectCellReport },
}

/// Serializable projection of [`EffectCell`]: binding cells carry the
/// name only (the hygiene context is dropped, as everywhere on the
/// wire; see docs/wire_format.md).
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum EffectCellReport {
    Binding { binding: Atom },
    GlobalProp { key: String },
    ArrayContents { origin: u32 },
    SetContents { origin: u32 },
}

impl From<&EffectCell> for EffectCellReport {
    fn from(cell: &EffectCell) -> Self {
        match cell {
            EffectCell::Binding(id) => Self::Binding {
                binding: id.0.clone(),
            },
            EffectCell::GlobalProp(key) => Self::GlobalProp { key: key.clone() },
            EffectCell::ArrayContents(origin) => Self::ArrayContents { origin: *origin },
            EffectCell::SetContents(origin) => Self::SetContents { origin: *origin },
        }
    }
}

impl fmt::Display for EffectCellReport {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Binding { binding } => write!(f, "binding `{binding}`"),
            Self::GlobalProp { key } => write!(f, "global property `{key}`"),
            Self::ArrayContents { origin } => write!(f, "array contents #{origin}"),
            Self::SetContents { origin } => write!(f, "Set contents #{origin}"),
        }
    }
}

impl fmt::Display for CellEffectClass {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Summarized => f.write_str("summarized cells"),
            Self::OpaqueBarrier { causes } => {
                f.write_str("opaque barrier (")?;
                for (i, cause) in causes.iter().enumerate() {
                    if i > 0 {
                        f.write_str(", ")?;
                    }
                    f.write_str(match cause {
                        DataflowOpacityCause::MemberWriteOrAlias => "member write or alias",
                        DataflowOpacityCause::DynamicCellWrites => "dynamic cell writes",
                        DataflowOpacityCause::GlobalObjectAliasEscape => {
                            "global-object alias escape"
                        }
                        DataflowOpacityCause::OpaqueAtInitCall => "unproven at-init call",
                    })?;
                }
                f.write_str(")")
            }
            Self::HeapAllocation => f.write_str("heap allocation"),
            Self::HeapFrontier { causes } => {
                f.write_str("opaque heap barrier (")?;
                for (i, cause) in causes.iter().enumerate() {
                    if i > 0 {
                        f.write_str(", ")?;
                    }
                    f.write_str(cause.label())?;
                }
                f.write_str(")")
            }
        }
    }
}

impl HeapBarrierCause {
    fn label(self) -> &'static str {
        match self {
            Self::ArrayContentEffects => "array content effects",
            Self::NonSummarizableImpure => "impure and not summarizable",
            Self::IncompleteSynchronousExecution => "incomplete synchronous execution",
            Self::UnresolvedCallee => "unresolved at-init callee",
            Self::CalleeHeapEffects => "heap-opaque callee body",
        }
    }
}

impl fmt::Display for CellConflict {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::ReadAfterWrite { cell } => write!(f, "reads {cell} last written by target"),
            Self::WriteAfterWrite { cell } => write!(f, "writes {cell} last written by target"),
            Self::WriteAfterRead { cell } => write!(f, "writes {cell} read by target"),
            Self::UseAfterAllocation { cell } => write!(f, "uses {cell} allocated by target"),
            Self::AfterBarrier => f.write_str("target is the opaque barrier in force"),
            Self::BarrierAfterPrior => f.write_str("source is an opaque barrier after target"),
            Self::FrontierAfterCell { cell } => write!(f, "heap barrier after target's {cell}"),
        }
    }
}

/// Suffix naming each endpoint effect that is only assumed; nothing when
/// both are observed.
fn write_assumed(
    f: &mut fmt::Formatter<'_>,
    source: &EffectProvenance,
    target: &EffectProvenance,
) -> fmt::Result {
    for (side, provenance) in [("source", source), ("target", target)] {
        if let EffectProvenance::Fallback { .. } = provenance {
            write!(f, " ({side} effect {provenance})")?;
        }
    }
    Ok(())
}

impl fmt::Display for SequencedCause {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::StrictChain => {
                f.write_str("strict S-chain: source is the next impure statement after target")
            }
            Self::SharedVarAccess {
                binding,
                source,
                target,
            } => {
                write!(
                    f,
                    "consecutive at-init accesses of shared `var` `{binding}`"
                )?;
                write_assumed(f, source, target)
            }
            Self::BindingTimeline {
                binding,
                order,
                source,
                target,
            } => {
                let order = match order {
                    BindingTimelineOrder::WriterAfterWriter => "writes after target wrote",
                    BindingTimelineOrder::ReaderAfterWriter => "observes after target wrote",
                    BindingTimelineOrder::WriterAfterReader => "writes after target observed",
                };
                write!(f, "at-init timeline of `{binding}`: source {order}")?;
                write_assumed(f, source, target)
            }
            Self::UninitializedObservation {
                binding,
                observation,
            } => {
                write!(
                    f,
                    "target's synchronous callee observed `{binding}` before source declared it"
                )?;
                write_assumed(f, &EffectProvenance::Observed, observation)
            }
            Self::CellOrder {
                from,
                to,
                conflicts,
            } => {
                write!(f, "cell order: source is {from}, target is {to}; ")?;
                for (i, conflict) in conflicts.iter().enumerate() {
                    if i > 0 {
                        f.write_str("; ")?;
                    }
                    write!(f, "{conflict}")?;
                }
                Ok(())
            }
        }
    }
}

impl EdgeReason {
    pub(crate) fn eager_use(so: SourceUnitOrdinal, b: Id) -> Self {
        Self {
            kind: DepKind::EagerUse,
            statement_ordinal: so,
            binding: Some(b),
            role: EdgeRole::Direct,
            sequenced_cause: None,
            inerted_by: None,
            premise_verdict: None,
        }
    }
    pub(crate) fn lazy_use(so: SourceUnitOrdinal, b: Id) -> Self {
        Self {
            kind: DepKind::LazyUse,
            statement_ordinal: so,
            binding: Some(b),
            role: EdgeRole::Direct,
            sequenced_cause: None,
            inerted_by: None,
            premise_verdict: None,
        }
    }
    pub(crate) fn hoisted_use(so: SourceUnitOrdinal, b: Id) -> Self {
        Self {
            kind: DepKind::HoistedUse,
            statement_ordinal: so,
            binding: Some(b),
            role: EdgeRole::Direct,
            sequenced_cause: None,
            inerted_by: None,
            premise_verdict: None,
        }
    }
    pub(crate) fn eager_rebind(so: SourceUnitOrdinal, b: Id) -> Self {
        Self {
            kind: DepKind::EagerRebind,
            statement_ordinal: so,
            binding: Some(b),
            role: EdgeRole::Direct,
            sequenced_cause: None,
            inerted_by: None,
            premise_verdict: None,
        }
    }
    pub(crate) fn lazy_rebind(so: SourceUnitOrdinal, b: Id) -> Self {
        Self {
            kind: DepKind::LazyRebind,
            statement_ordinal: so,
            binding: Some(b),
            role: EdgeRole::Direct,
            sequenced_cause: None,
            inerted_by: None,
            premise_verdict: None,
        }
    }
    pub(crate) fn deferred_rebind(so: SourceUnitOrdinal, b: Id) -> Self {
        Self {
            kind: DepKind::DeferredRebind,
            statement_ordinal: so,
            binding: Some(b),
            role: EdgeRole::Direct,
            sequenced_cause: None,
            inerted_by: None,
            premise_verdict: None,
        }
    }
    pub(crate) fn co_declaration(so: SourceUnitOrdinal, b: Id) -> Self {
        Self {
            kind: DepKind::CoDeclaration,
            statement_ordinal: so,
            binding: Some(b),
            role: EdgeRole::Direct,
            sequenced_cause: None,
            inerted_by: None,
            premise_verdict: None,
        }
    }
    pub(crate) fn co_location(so: SourceUnitOrdinal, b: Id) -> Self {
        Self {
            kind: DepKind::CoLocation,
            statement_ordinal: so,
            binding: Some(b),
            role: EdgeRole::Direct,
            sequenced_cause: None,
            inerted_by: None,
            premise_verdict: None,
        }
    }
    pub(crate) fn sequenced(so: SourceUnitOrdinal, cause: SequencedCause) -> Self {
        Self {
            kind: DepKind::Sequenced,
            statement_ordinal: so,
            binding: None,
            role: EdgeRole::Direct,
            sequenced_cause: Some(Box::new(cause)),
            inerted_by: None,
            premise_verdict: None,
        }
    }
    pub(crate) fn local_effect(so: SourceUnitOrdinal, b: Id) -> Self {
        Self {
            kind: DepKind::LocalEffect,
            statement_ordinal: so,
            binding: Some(b),
            role: EdgeRole::Direct,
            sequenced_cause: None,
            inerted_by: None,
            premise_verdict: None,
        }
    }

    /// Promote this reason to `EdgeRole::PromotedAtInit`. Used by
    /// `promote_at_init_calls`; downstream gate/lenient projection
    /// helpers consult `role` to decide whether to drop the edge when
    /// caller and callee land in different assignment slots.
    pub(crate) fn promoted_at_init(mut self, callee_source_unit: SourceUnitId) -> Self {
        self.role = EdgeRole::PromotedAtInit { callee_source_unit };
        self
    }

    /// Construct a synthetic edge reason from raw fields. Used by
    /// `DependencyGraph::from_report` and similar JSON-recovery paths that
    /// don't carry an `Id` atom for the binding. The realizability
    /// gate (`check_realizability`) consults only `kind` and the role
    /// — every `is_*` and `constrains_init_order` predicate delegates
    /// to `kind` — so a synthetic reason without a binding is
    /// sufficient for the gate. Source-of-truth construction from
    /// `StatementFacts` still goes through the kind-specific helpers
    /// above.
    pub fn synthetic(kind: DepKind, statement_ordinal: SourceUnitOrdinal, role: EdgeRole) -> Self {
        Self {
            kind,
            statement_ordinal,
            binding: None,
            role,
            sequenced_cause: None,
            inerted_by: None,
            premise_verdict: None,
        }
    }

    /// Reattach the retained cause of a `Sequenced` edge read back from
    /// `dependency_graph.json` (`DependencyEdgeReport::sequenced_cause`).
    pub fn with_sequenced_cause(mut self, cause: Option<SequencedCause>) -> Self {
        self.sequenced_cause = cause.map(Box::new);
        self
    }

    /// Mark the reason inert under `premise`, or reattach a marker read
    /// back from `dependency_graph.json` (`DependencyEdgeReport::commuting_premise`).
    pub fn with_commuting_premise(mut self, premise: Option<CommutingStatementsRef>) -> Self {
        self.inerted_by = premise;
        self
    }

    /// Retained emission cause of a `Sequenced` edge; `None` for every
    /// other kind and for synthetic edges whose report lacked it.
    pub fn sequenced_cause(&self) -> Option<&SequencedCause> {
        self.sequenced_cause.as_deref()
    }

    /// The `commuting_statements` entry that made this edge inert.
    pub fn commuting_premise(&self) -> Option<&CommutingStatementsRef> {
        self.inerted_by.as_ref()
    }

    /// How a `commuting_statements` premise over this edge's endpoints would
    /// treat it; `None` for kinds a premise never considers and for
    /// synthetic edges.
    pub fn premise_verdict(&self) -> Option<&PremiseVerdict> {
        self.premise_verdict.as_deref()
    }

    /// The role this reason was emitted with. See [`EdgeRole`].
    pub fn role(&self) -> EdgeRole {
        self.role
    }

    pub fn kind(&self) -> DepKind {
        self.kind
    }
    pub fn binding(&self) -> Option<&Id> {
        self.binding.as_ref()
    }
    pub fn statement_ordinal(&self) -> SourceUnitOrdinal {
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
    /// Every kind except `LazyUse`, `HoistedUse` and `DeferredRebind` constrains
    /// realizability, unless an author `commuting_statements` premise made
    /// the edge inert. `DeferredRebind` writes never fire at module
    /// init (the write site is nested ≥2 closures deep or past an
    /// await), so they impose no init-order constraint — but they
    /// still participate in cross-destination-rebind rejection and
    /// `G_atomic` co-location, because ESM imports are read-only at
    /// ANY time, not just during init.
    pub fn constrains_init_order(&self) -> bool {
        self.inerted_by.is_none()
            && !matches!(
                self.kind,
                DepKind::LazyUse | DepKind::HoistedUse | DepKind::DeferredRebind
            )
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
    strum::EnumString,
    strum::EnumMessage,
)]
#[cfg_attr(test, derive(strum::EnumIter))]
#[serde(rename_all = "snake_case")]
#[strum(serialize_all = "snake_case")]
pub enum DepKind {
    #[strum(message = "at-init")]
    EagerUse,
    #[strum(message = "lazy")]
    LazyUse,
    #[strum(message = "hoisted read")]
    HoistedUse,
    #[strum(message = "at-init rebind")]
    EagerRebind,
    #[strum(message = "lazy rebind")]
    LazyRebind,
    /// Rebinding write that only fires after module init (nested ≥2
    /// closures deep, or past an `await` in an async body). Rejected
    /// across destinations like the other rebinds (ESM imports are
    /// read-only whenever the write fires), but excluded from
    /// init-order constraints and the I-graph.
    #[strum(message = "deferred rebind")]
    DeferredRebind,
    #[strum(message = "side-effect")]
    Sequenced,
    /// Two statements declare the same module-scoped var binding. Emitted in
    /// both directions to require one destination without rewriting the AST.
    #[strum(message = "shared var declaration")]
    CoDeclaration,
    /// A source-prefix proof needs the producer and consumer in one module.
    #[strum(message = "stable array spread co-location")]
    CoLocation,
    #[strum(message = "local-effect")]
    LocalEffect,
}

impl DepKind {
    /// Short human-readable label used in cycle and gate diagnostics.
    pub fn diagnostic_label(self) -> &'static str {
        strum::EnumMessage::get_message(&self)
            .expect("every dependency kind has a diagnostic message")
    }
}

/// Stable per-chunk identity of a dependency graph edge. Equal to the
/// edge's position in [`DependencyGraph::edges`]. The previous
/// representation stored the report-shape spelling
/// (`format!("dependency_edge:{idx}")`) on every entry; that spelling is
/// `O(n_edges)` strings allocated per chunk and
/// repeated clones in graph-report hot paths. Carry the typed index
/// instead and let the report layer do the formatting at its single
/// serialization boundary via
/// [`DependencyEdgeId::report_key`].
///
/// [`DependencyGraph::edges`]: super::dependency_graph::DependencyGraph
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Hash)]
pub struct DependencyEdgeId(pub usize);

impl DependencyEdgeId {
    pub fn report_key(self) -> String {
        format!("dependency_edge:{}", self.0)
    }
}

#[derive(Debug, Clone)]
pub struct DependencyEdge {
    pub id: DependencyEdgeId,
    pub from: SourceUnitId,
    pub to: SourceUnitId,
    pub reason: EdgeReason,
}

#[cfg(test)]
mod tests {
    use super::DepKind;
    use strum::IntoEnumIterator;

    #[test]
    fn every_dependency_kind_has_a_diagnostic_message() {
        for kind in DepKind::iter() {
            assert!(!kind.diagnostic_label().is_empty());
        }
        // Diagnostic messages must not replace the machine-readable spelling.
        assert_eq!(DepKind::EagerUse.to_string(), "eager_use");
        assert_eq!(DepKind::EagerUse.diagnostic_label(), "at-init");
    }
}
