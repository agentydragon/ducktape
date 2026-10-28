use serde::{Deserialize, Serialize};
use source_identity::SourceUnitOrdinal;
use std::collections::BTreeSet;
use swc_atoms::Atom;

use crate::analysis_inputs::CallbackFactoryEvidence;
use crate::assignment_assembly::AtomicGroupConflict;
use crate::ids::ModuleId;
use crate::purity::Purity;
use crate::{DepKind, SourceUnitClass, StatementKind};

#[derive(Debug, Clone, Ord, PartialOrd, Eq, PartialEq, Serialize, Deserialize)]
pub struct SourceLocation {
    pub source_path: String,
    pub start_line: usize,
    pub end_line: usize,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub start_column: Option<usize>,
}

impl SourceLocation {
    /// Expand this location's line range to include `other`.
    pub fn expand_to(&mut self, other: &SourceLocation) {
        if other.start_line < self.start_line {
            self.start_line = other.start_line;
            self.start_column = other.start_column;
        } else if other.start_line == self.start_line {
            self.start_column = match (self.start_column, other.start_column) {
                (Some(a), Some(b)) => Some(a.min(b)),
                (None, value) | (value, None) => value,
            };
        }
        self.end_line = self.end_line.max(other.end_line);
    }
}

/// Accumulates the minimum start-line and maximum end-line across a
/// collection of `SourceLocation`s. Used to compute the
/// `source_line_range` field of `AtomicGroupReport` and similar.
pub struct LineRange {
    start: usize,
    end: usize,
    size_estimate: usize,
    found: bool,
}

impl Default for LineRange {
    fn default() -> Self {
        Self::new()
    }
}

impl LineRange {
    pub fn new() -> Self {
        Self {
            start: usize::MAX,
            end: 0,
            size_estimate: 0,
            found: false,
        }
    }

    pub fn expand(&mut self, location: &SourceLocation) {
        self.found = true;
        self.start = self.start.min(location.start_line);
        self.end = self.end.max(location.end_line);
        self.size_estimate += location.end_line + 1 - location.start_line;
    }

    pub fn size_estimate(&self) -> usize {
        self.size_estimate
    }

    pub fn into_array(self) -> Option<[usize; 2]> {
        self.found.then_some([self.start, self.end])
    }
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
pub struct BindingReport {
    pub binding: Atom,
    pub export_name: Atom,
}

/// Node-link JSON side output for downstream graph analysis.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DependencyGraphReport {
    pub chunk_id: String,
    /// Exact inputs consumed by the run that produced this graph. Older
    /// reports remain readable as historical snapshots, but cannot validate edits.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub provenance: Option<graph_provenance::GraphProvenance>,
    pub nodes: Vec<SourceUnitReport>,
    /// Shared callee verdicts referenced by `purity.reasons[].cause_ref`.
    /// Each cause is emitted once, even when many source units call it.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub purity_causes: Vec<PurityCauseReport>,
    /// Imported callable summaries whose callback-factory capability was used.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub imported_callable_summaries: Vec<ImportedCallableSummaryReport>,
    pub edges: Vec<DependencyEdgeReport>,
    #[serde(rename = "module_graph")]
    pub quotient: ModuleQuotientReport,
    pub atomic_graph: AtomicGraphReport,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PurityCauseReport {
    pub id: String,
    pub purity: Purity,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct ImportedCallableSummaryReport {
    pub id: String,
    pub imported_local: String,
    pub callback_factory_evidence: Vec<CallbackFactoryEvidence>,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CallbackFactoryUseKind {
    /// The checked call suppressed opaque callback promotion.
    CallTiming,
    /// The checked return shape retained callbacks in a deferred result.
    ReturnShape,
    /// The private initializer result was omitted as a reachability root.
    ClosedResultRoot,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct CallbackFactoryUseReport {
    pub summary_ref: String,
    pub kind: CallbackFactoryUseKind,
    pub lexical_statement_ordinal: SourceUnitOrdinal,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_location: Option<SourceLocation>,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct CallbackFactoryBarrierReport {
    pub contract: CallbackFactoryContractReport,
    pub rejection: CallbackFactoryRejectionReport,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct CallbackFactoryContractReport {
    pub namespace_binding: Atom,
    pub member: String,
    pub argument_count: usize,
    pub callback_arguments: BTreeSet<usize>,
    /// Direct callback roots resolved through the value-flow model. Omitted
    /// when those argument values cannot be linked without inference.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub callback_source_units: Option<Vec<CallbackFactoryCallbackSourceReport>>,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct CallbackFactoryCallbackSourceReport {
    /// `unit:N` graph reference; pair with `statement_ordinal` only through
    /// the stored graph crosswalk.
    pub source_unit: String,
    pub statement_ordinal: SourceUnitOrdinal,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
#[serde(tag = "code", rename_all = "snake_case")]
pub enum CallbackFactoryRejectionReport {
    NamespaceMemberWrite {
        witness: CallbackFactoryWriteWitnessReport,
    },
    NamespaceDynamicPropertyWrite {
        witness: CallbackFactoryWriteWitnessReport,
    },
    NamespaceCallArgument {
        witness: CallbackFactoryCallArgumentWitnessReport,
    },
    UnsupportedGuard,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct CallbackFactoryCallArgumentWitnessReport {
    /// Exact graph source unit containing this ordinary top-level call.
    pub source_unit: String,
    pub statement_ordinal: SourceUnitOrdinal,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_location: Option<SourceLocation>,
    pub namespace_binding: Atom,
    /// SWC callsite byte positions in the analyzed source program.
    pub call_span_start: u32,
    pub call_span_end: u32,
    pub argument_index: usize,
    /// True only for an UnknownCallTarget loss at this exact owner and span.
    pub opaque_call: bool,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct CallbackFactoryWriteWitnessReport {
    /// `unit:N` graph reference. Resolve its parsed statement through
    /// `statement_ordinal`; the two numeric coordinates are not interchangeable.
    pub source_unit: String,
    pub statement_ordinal: SourceUnitOrdinal,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_location: Option<SourceLocation>,
    pub receiver_binding: Atom,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub property: Option<String>,
}

impl DependencyGraphReport {
    /// Resolve a [`ModuleKey`] to its module-table entry. The table
    /// (`quotient.nodes`) is the single source of truth for a module's
    /// path + destination kind; consumers look up here rather than reading
    /// a duplicated field off the reference.
    pub fn module(&self, key: &ModuleKey) -> Option<&ModuleEntry> {
        self.quotient.nodes.iter().find(|entry| &entry.key == key)
    }

    /// Whether `key` receives source units without an explicit module claim.
    /// This includes the physical entry destination and planned outputs with
    /// the `Unassigned` policy role. Unknown keys do not match.
    pub fn is_unassigned_or_entry(&self, key: &ModuleKey) -> bool {
        self.module(key).is_some_and(|entry| {
            matches!(
                entry.kind,
                ModuleDestinationKind::Entry | ModuleDestinationKind::Unassigned
            )
        })
    }
}

/// Completeness of the existing synchronous-effect inference, independent of purity.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct SynchronousExecutionReport {
    pub complete: bool,
    pub precision_losses: Vec<ExecutionPrecisionLoss>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub callback_factory_uses: Vec<CallbackFactoryUseReport>,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct ExecutionPrecisionLoss {
    pub reason: ExecutionPrecisionLossReason,
    /// Lexical source unit containing the operation or truncated callable.
    pub statement_ordinal: SourceUnitOrdinal,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_location: Option<SourceLocation>,
    #[serde(skip)]
    pub(crate) span: swc_common::Span,
}

impl ExecutionPrecisionLoss {
    pub fn is_unknown_call_target_at(
        &self,
        statement_ordinal: SourceUnitOrdinal,
        span: swc_common::Span,
    ) -> bool {
        self.reason == ExecutionPrecisionLossReason::UnknownCallTarget
            && self.statement_ordinal == statement_ordinal
            && self.span == span
    }
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
#[serde(tag = "code", rename_all = "snake_case")]
pub enum ExecutionPrecisionLossReason {
    UnknownCallTarget,
    UnsupportedOperation { operation: ExecutionOperation },
    RecursiveInvocation,
    InvocationDepthLimit { limit: usize },
    InvocationExpansionLimit { limit: usize },
    CallbackPoolLimit { limit: usize },
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ExecutionOperation {
    MembershipProtocol,
    InstanceOfProtocol,
    DestructuringProtocol,
    MemberRead,
    ObjectSpread,
    ArraySpread,
    ImplicitCoercion,
    PropertyWrite,
    ReturnedCallback,
    SuperclassConstruction,
    ObjectAssign,
    Construction,
    OpaqueExecution,
    GeneratorParameters,
    CallableInputs,
    ArrayIteration,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SourceUnitReport {
    pub id: String,
    pub statement_ordinal: SourceUnitOrdinal,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_location: Option<SourceLocation>,
    pub declared_bindings: Vec<BindingReport>,
    pub statement_kind: StatementKind,
    /// At-init purity classification, with structured reasons on
    /// any non-`Pure` verdict. A consumer wanting a boolean reads
    /// `purity.kind == "pure"`.
    pub purity: Purity,
    /// Matched namespace callback-factory contracts rejected at top-level
    /// calls in this source unit. Empty for accepted calls and unsupported
    /// call families; rejected unsupported guards are reported explicitly.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub callback_factory_barriers: Vec<CallbackFactoryBarrierReport>,
    /// Reasons retained by synchronous inference, rather than reconstructed from purity.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub synchronous_execution: Option<SynchronousExecutionReport>,
    /// The module this source unit is assigned to, as an interned
    /// [`ModuleKey`]. Resolve to a path / residual flag via the module
    /// table (`quotient.nodes`).
    pub destination: ModuleKey,
}

impl SourceUnitReport {
    pub fn class(&self) -> SourceUnitClass {
        SourceUnitClass::from_declared_bindings(&self.declared_bindings)
    }

    /// Inclusive line span of the source unit's statement; 0 without a
    /// source location.
    pub fn line_count(&self) -> usize {
        self.source_location.as_ref().map_or(0, |loc| {
            loc.end_line
                .saturating_sub(loc.start_line)
                .saturating_add(1)
        })
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DependencyEdgeReport {
    pub id: String,
    pub source: String,
    pub target: String,
    pub edge_kind: DepKind,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub binding: Option<Atom>,
    pub statement_ordinal: SourceUnitOrdinal,
    pub constrains_init_order: bool,
    /// Role the edge was emitted with. Mirrors `EdgeReason::role`
    /// (see `crate::EdgeRole`) through the wire format so the peel
    /// planner's `DependencyGraph::from_report` reapplies the same
    /// cross-module at-init promotion filter the materializer's gate
    /// does. `None` is shorthand for `EdgeRole::Direct` (omitted when
    /// serializing to keep direct edges compact).
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub role: Option<EdgeRoleReport>,
}

/// Wire-format projection of [`crate::EdgeRole`]. The typed variant
/// avoids the previous `at_init_callee_unit: Option<String>`
/// side-channel by routing the same data through a tagged enum, so
/// adding new edge roles is a single-source-of-truth change.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum EdgeRoleReport {
    /// SourceUnit id (e.g. `"unit:42"`) of the at-init callee whose body
    /// produced this edge.
    PromotedAtInit { callee_source_unit: String },
}

impl EdgeRoleReport {
    /// Resolve back to a [`crate::EdgeRole`] using a `report-id ->
    /// SourceUnitId` lookup table. Unknown source-unit IDs fall back to
    /// `EdgeRole::Direct` (the lenient view treats the edge as a
    /// normal direct edge) — same shape as the pre-refactor
    /// `at_init_callee_source_unit.and_then(by_id.get)` fallback.
    pub fn resolve(
        &self,
        by_id: &std::collections::HashMap<String, crate::SourceUnitId>,
    ) -> crate::EdgeRole {
        match self {
            EdgeRoleReport::PromotedAtInit { callee_source_unit } => {
                match by_id.get(callee_source_unit) {
                    Some(&source_unit) => crate::EdgeRole::PromotedAtInit {
                        callee_source_unit: source_unit,
                    },
                    None => crate::EdgeRole::Direct,
                }
            }
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModuleQuotientReport {
    /// The module table: one [`ModuleEntry`] per planned output plus the
    /// entry destination. It is the single source of truth for each
    /// destination's path and kind; every other reference is
    /// a [`ModuleKey`] into this list.
    pub nodes: Vec<ModuleEntry>,
    pub edges: Vec<QuotientEdgeReport>,
    pub sccs: Vec<QuotientSccReport>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct QuotientEdgeReport {
    pub id: String,
    pub source: ModuleKey,
    pub target: ModuleKey,
    pub edge_kinds: Vec<DepKind>,
    pub constrains_init_order: bool,
}

/// Wire-format projection of the shared [`super::SccCore`] shape
/// ("modules in the SCC + edges in the SCC"). Deviation from that
/// in-memory core: this shape covers **every** SCC the dep graph turns
/// up (single-module non-self-loops are filtered out at the builder),
/// with `realizable` distinguishing the offending ones, and uses
/// wire-stable string ids (`module_key`, `quotient_edge:N`) instead of
/// typed `ModuleId` / `DependencyEdgeId`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct QuotientSccReport {
    pub id: String,
    /// Modules in this SCC, as interned [`ModuleKey`]s. Resolve to
    /// paths via the module table; the former parallel `labels` field
    /// (the same modules spelled as paths) is gone.
    pub modules: Vec<ModuleKey>,
    pub is_cycle: bool,
    pub realizable: bool,
    pub module_edge_ids: Vec<String>,
    pub constraining_module_edge_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SequencedSourceUnitCause {
    pub source_unit_id: String,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub binding_names: Vec<Atom>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_location: Option<SourceLocation>,
    pub purity: Purity,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct AtomicGraphReport {
    pub nodes: Vec<AtomicGroupReport>,
    pub edges: Vec<AtomicGroupEdgeReport>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct AtomicGroupReport {
    pub id: String,
    pub source_unit_ids: Vec<String>,
    pub members: Vec<BindingReport>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub anonymous_statement_source_unit_ids: Vec<String>,
    /// Distinct modules the group's source units are assigned to, as interned
    /// [`ModuleKey`]s. Resolve to paths/residual via the module table.
    pub destinations: Vec<ModuleKey>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub causes: Vec<DepKind>,
    pub size_lines_estimate: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_line_range: Option<[usize; 2]>,
    pub ordinal_span: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct AtomicGroupEdgeReport {
    pub id: String,
    pub source: String,
    pub target: String,
    pub edge_kinds: Vec<DepKind>,
    pub dependency_edge_ids: Vec<String>,
    pub constrains_init_order: bool,
}

/// Wire shape for `atomic_group_conflicts.json` (one entry per
/// conflicting atomic group). Projects the in-memory
/// [`AtomicGroupConflict`] onto the shared entity-key formats: owners
/// as `"unit:N"` strings (joining `dependency_graph.json`'s `nodes[].id`)
/// and modules as canonical [`spec::ModulePath`]s (joining the module
/// table's `path`). The raw `SourceUnitId` / `ModuleId` indices never hit
/// the wire.
#[derive(Debug, Clone, Serialize)]
pub struct AtomicGroupConflictReport {
    /// Members as `"unit:N"` keys, sorted by source-unit ID.
    pub members: Vec<String>,
    pub claims: Vec<ConflictingClaimReport>,
    pub causes: Vec<DepKind>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub sequenced_source_units: Vec<SequencedSourceUnitCause>,
}

/// One claim row in [`AtomicGroupConflictReport`].
#[derive(Debug, Clone, Serialize)]
pub struct ConflictingClaimReport {
    /// Claiming source unit as an `"unit:N"` key.
    pub source_unit: String,
    pub binding_names: Vec<Atom>,
    /// Claimed destination, by canonical [`spec::ModulePath`].
    pub module: spec::ModulePath,
}

pub fn sequenced_source_unit_causes(
    dependency_graph: &crate::DependencyGraph,
    members: &[crate::SourceUnitId],
) -> Vec<SequencedSourceUnitCause> {
    let members: std::collections::BTreeSet<_> = members.iter().copied().collect();
    members
        .into_iter()
        .filter_map(|source_unit| {
            dependency_graph
                .out_edges_of(source_unit)
                .iter()
                .map(|id| dependency_graph.edge(*id))
                .find(|edge| edge.reason.kind == DepKind::Sequenced)
                .and_then(|edge| sequenced_edge_cause(dependency_graph, edge))
        })
        .collect()
}

/// Describe a sequencing edge's impure source directly. Callers that already
/// have its provenance need no graph-wide search to rediscover that edge.
pub fn sequenced_edge_cause(
    dependency_graph: &crate::DependencyGraph,
    edge: &crate::DependencyEdge,
) -> Option<SequencedSourceUnitCause> {
    if edge.reason.kind != DepKind::Sequenced {
        return None;
    }
    let node = dependency_graph.node(edge.from)?;
    let Purity::NotPure { .. } = &node.purity else {
        return None;
    };
    Some(SequencedSourceUnitCause {
        source_unit_id: crate::reports::source_unit_key(edge.from),
        binding_names: node.declared.iter().map(|id| id.0.clone()).collect(),
        source_location: node.source_location.clone(),
        purity: node.purity.clone(),
    })
}

impl AtomicGroupConflictReport {
    pub fn from_conflicts(
        conflicts: &[AtomicGroupConflict],
        module_path: &dyn Fn(ModuleId) -> spec::ModulePath,
        dependency_graph: &crate::DependencyGraph,
    ) -> Vec<Self> {
        conflicts
            .iter()
            .map(|conflict| Self {
                members: conflict
                    .members
                    .iter()
                    .copied()
                    .map(crate::reports::source_unit_key)
                    .collect(),
                claims: conflict
                    .claims
                    .iter()
                    .map(|claim| ConflictingClaimReport {
                        source_unit: crate::reports::source_unit_key(claim.source_unit),
                        binding_names: claim.binding_names.clone(),
                        module: module_path(claim.module),
                    })
                    .collect(),
                causes: conflict.causes.iter().copied().collect(),
                sequenced_source_units: sequenced_source_unit_causes(
                    dependency_graph,
                    &conflict.members,
                ),
            })
            .collect()
    }
}

/// Interned reference to a planned output or entry destination.
///
/// This is the **one** encoding of destination identity on the dependency-graph
/// wire. Everything that points at a destination — a source unit's
/// `destination`, a quotient edge's endpoints, an SCC's members, an
/// atomic group's `destinations` — carries a `ModuleKey`, never a
/// path or a parallel label. The key resolves to a [`ModuleEntry`] in
/// the module table (`ModuleQuotientReport::nodes`), which is the
/// sole place a destination's `path` and `residual` policy flag are stored.
///
/// The key is `module_key(ModuleId)` — `"logical:N"` — so it stays
/// compact and stable; the human-readable path lives once, in the
/// table entry.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct ModuleKey(pub String);

impl ModuleKey {
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl std::fmt::Display for ModuleKey {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

/// One entry in the destination table (`ModuleQuotientReport::nodes`).
/// The single source of truth mapping a [`ModuleKey`] to its canonical
/// path and destination kind. No other wire field duplicates them.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ModuleEntry {
    pub key: ModuleKey,
    /// Canonical report identity (see [`spec::ModulePath`]). For planned
    /// outputs this is the module path; for `Entry` it is the historic
    /// `anon_residual_sentinel` report label, not the emitted entry filename.
    pub path: spec::ModulePath,
    /// The physical entry destination or the policy role of a planned output.
    /// Authoritative; the authored `residual/` path namespace is independent.
    pub kind: ModuleDestinationKind,
}

/// Disjoint destinations in the module quotient's wire table.
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ModuleDestinationKind {
    Entry,
    Specified,
    Unassigned,
}
