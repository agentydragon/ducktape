//! Wire reports of the dependency graph, its module quotient and atomic
//! groups. Fact-level evidence types live in `analysis_vocabulary::report_schema`.

use analysis_model::DepKind;
use serde::{Deserialize, Serialize};
use source_identity::{SourceUnitId, SourceUnitOrdinal};
use std::collections::{BTreeSet, HashMap, VecDeque};
use swc_atoms::Atom;

use analysis::StatementKind;
use analysis::ids::ModuleId;
use analysis_vocabulary::execution_evidence::SourceLocation;
use analysis_vocabulary::inputs::{
    CallbackFactoryEvidence, CommutingStatementsRef, ReturnedRecordSummary,
};
use analysis_vocabulary::purity::Purity;
use analysis_vocabulary::report_schema::{
    HeapFrontierEvidence, NativeIntegrityUse, NativeMutationWitness, NativePrototypeMutation,
    NeverWrittenOverride, SynchronousExecutionReport,
};

use crate::assignment_assembly::AtomicGroupConflict;
use crate::atomic_groups::{
    AtomicGroupPathOrientation, AtomicGroupPathProjection, atomic_constraint_arcs,
};
use crate::{DependencyEdge, DependencyEdgeId, EdgeRole, SourceUnitClass};

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
    /// Concrete nonentry module priority used by this materialization.
    pub import_order: Vec<spec::ModulePath>,
    /// Normalized ownership subset used to produce this selected graph. The
    /// matching program provenance contains the exact input-file fingerprint.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ownership_subset: Option<Vec<graph_provenance::SelectedOwnershipModule>>,
    /// The run's [`ProgramGraphReport`], relative to this file's directory.
    /// Resolve with [`graph_provenance::program_graph_path`].
    pub program_graph: std::path::PathBuf,
    pub nodes: Vec<SourceUnitReport>,
    /// Shared callee verdicts referenced by `purity.reasons[].cause_ref`.
    /// Each cause is emitted once, even when many source units call it.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub purity_causes: Vec<PurityCauseReport>,
    /// Imported callable summaries whose callback-factory capability was used.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub imported_callable_summaries: Vec<ImportedCallableSummaryReport>,
    pub edges: Vec<DependencyEdgeReport>,
    /// Author `commuting_statements` premises the graph build applied and
    /// what each one bought (SPEC.md § Commuting statements).
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub commuting_statements: Vec<CommutingStatementsReport>,
    #[serde(rename = "module_graph")]
    pub quotient: ModuleQuotientReport,
    pub atomic_graph: AtomicGraphReport,
}

/// `program_graph.json.gz`: the program-wide sections every chunk's
/// `dependency_graph.json.gz` shares, written once per run beside the chunk
/// report directories.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProgramGraphReport {
    /// Exact inputs consumed by the run that produced the graphs. Older
    /// reports remain readable as historical snapshots, but cannot validate edits.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub provenance: Option<graph_provenance::GraphProvenance>,
    /// Whole-program census sites, shared by failed native premise checks.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub native_integrity_witnesses: Vec<NativeMutationWitness>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub native_prototype_mutations: Vec<NativePrototypeMutation>,
    /// Census witnesses `program_premises.intrinsics_never_written` overrides:
    /// the author's audit list.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub intrinsics_never_written_overrides: Vec<NeverWrittenOverride>,
}
/// One applied `commuting_statements` premise: the pair's source units
/// (`unit:N` keys into `nodes`) and the `sequenced` edges (`edges[].id`) it
/// made inert. An applied premise inerts at least one edge.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct CommutingStatementsReport {
    pub reference: CommutingStatementsRef,
    pub spec_location: String,
    pub a: String,
    pub b: String,
    pub inerted_edge_ids: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub returned_record: Option<ReturnedRecordSummary>,
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_location: Option<SourceLocation>,
    pub receiver_binding: Atom,
    #[serde(default, skip_serializing_if = "Option::is_none")]
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
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SourceUnitReport {
    pub id: String,
    pub statement_ordinal: SourceUnitOrdinal,
    #[serde(default, skip_serializing_if = "Option::is_none")]
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
    /// Native premises used by proved heap construction in this source unit.
    /// Empty uses stay omitted.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub heap_native_integrity_uses: Vec<NativeIntegrityUse>,
    /// Reasons retained by synchronous inference, rather than reconstructed from purity.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub synchronous_execution: Option<SynchronousExecutionReport>,
    /// The module this source unit is assigned to, as an interned
    /// [`ModuleKey`]. Resolve to a path / residual flag via the module
    /// table (`quotient.nodes`).
    pub destination: ModuleKey,
}

impl SourceUnitReport {
    pub fn sequenced_endpoint_cause(&self) -> Option<SequencedSourceUnitCause> {
        SequencedSourceUnitCause::from_evidence(
            &self.purity,
            self.synchronous_execution.as_ref(),
            self.source_location.as_ref(),
            || self.id.clone(),
            self.declared_bindings
                .iter()
                .map(|binding| binding.binding.clone()),
        )
    }

    pub fn class(&self) -> SourceUnitClass {
        SourceUnitClass::from_declared_bindings(&self.declared_bindings)
    }
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct DependencyEdgeReport {
    pub id: String,
    pub source: String,
    pub target: String,
    pub edge_kind: DepKind,
    #[serde(default, skip_serializing_if = "Option::is_none")]
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
    /// Retained emission cause of a `Sequenced` edge
    /// (`EdgeReason::sequenced_cause`); absent on every other kind and
    /// on graphs written before the cause was retained.
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub sequenced_cause: Option<crate::SequencedCause>,
    /// The author `commuting_statements` entry that made this edge inert
    /// (`constrains_init_order` is then `false`). The edge stays in the
    /// report as the record of what source analysis found.
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub commuting_premise: Option<CommutingStatementsRef>,
    /// How a `commuting_statements` premise over this edge's endpoints would
    /// treat it (`EdgeReason::premise_verdict`): present on `sequenced` and
    /// `co_declaration` edges of graphs written since the verdict was
    /// retained, absent on every other kind. `spec propose-commutations`
    /// reads it so a drafted pair is classified exactly as the gate will.
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub premise_verdict: Option<crate::PremiseVerdict>,
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
        by_id: &std::collections::HashMap<String, SourceUnitId>,
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

    fn from_role(role: EdgeRole) -> Option<Self> {
        match role {
            EdgeRole::Direct => None,
            EdgeRole::PromotedAtInit { callee_source_unit } => Some(Self::PromotedAtInit {
                callee_source_unit: source_unit_key(callee_source_unit),
            }),
        }
    }
}

impl DependencyEdgeReport {
    /// Project one graph edge onto the canonical wire representation used by
    /// `dependency_graph.json.gz` and atomic-group conflict witnesses.
    pub fn from_edge(edge: &DependencyEdge) -> Self {
        Self {
            id: edge.id.report_key(),
            source: source_unit_key(edge.from),
            target: source_unit_key(edge.to),
            edge_kind: edge.reason.kind,
            binding: edge.reason.binding.as_ref().map(|id| id.0.clone()),
            statement_ordinal: edge.reason.statement_ordinal,
            constrains_init_order: edge.reason.constrains_init_order(),
            role: EdgeRoleReport::from_role(edge.reason.role()),
            sequenced_cause: edge.reason.sequenced_cause().cloned(),
            commuting_premise: edge.reason.commuting_premise().cloned(),
            premise_verdict: edge.reason.premise_verdict().cloned(),
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
/// in-memory core: this shape covers every quotient SCC, including isolated
/// modules. It records graph structure, not a realizability verdict: a cycle
/// containing initialization constraints can still have a valid ESM schedule.
/// It uses
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
    pub module_edge_ids: Vec<String>,
    pub constraining_module_edge_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SequencedSourceUnitCause {
    pub source_unit_id: String,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub binding_names: Vec<Atom>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_location: Option<SourceLocation>,
    pub purity: Purity,
    /// `synchronous_execution.complete` of the source unit; `None` when
    /// synchronous analysis did not run. `false` marks the statements
    /// whose sequencing edges are a conservative flood rather than a
    /// known ordering fact.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub synchronous_execution_complete: Option<bool>,
    /// Existing own/composed heap evidence when non-empty. Pure source units
    /// can have endpoint context when this evidence is available; its absence
    /// does not prove that the source unit has no heap effects.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub heap_frontier: Option<HeapFrontierEvidence>,
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
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

/// A deterministic pair of differently assigned claims connected both ways
/// through the atomic group's constraining projection.
#[derive(Debug, Clone, Serialize)]
pub struct AtomicGroupConflictWitness {
    pub claim_pair: [AtomicGroupWitnessClaim; 2],
    pub paths: [AtomicGroupWitnessPath; 2],
}

#[derive(Debug, Clone, Serialize)]
pub struct AtomicGroupWitnessClaim {
    pub source_unit: String,
    pub module: spec::ModulePath,
}

/// One shortest path direction between the selected claim pair. `steps` keeps
/// the first twelve edges; `total_step_count` and `truncated` describe the
/// complete path when its detail exceeds that bound.
#[derive(Debug, Clone, Serialize)]
pub struct AtomicGroupWitnessPath {
    pub from_source_unit: String,
    pub to_source_unit: String,
    pub total_step_count: usize,
    pub truncated: bool,
    pub steps: Vec<AtomicGroupWitnessStep>,
}

#[derive(Debug, Clone, Serialize)]
pub struct AtomicGroupWitnessStep {
    /// Full canonical edge evidence, joinable to the dependency graph export.
    pub edge: DependencyEdgeReport,
    /// The relation this edge contributes to `G_atomic`.
    pub projection: AtomicGroupPathProjection,
    /// Whether the path follows or traverses against the original edge.
    pub orientation: AtomicGroupPathOrientation,
}

/// Wire shape for `atomic_group_conflicts.json` (one entry per
/// conflicting atomic group). Projects the in-memory
/// [`AtomicGroupConflict`] onto the shared entity-key formats: owners
/// as `"unit:N"` strings (joining `dependency_graph.json.gz`'s `nodes[].id`)
/// and modules as canonical [`spec::ModulePath`]s (joining the module
/// table's `path`). The raw `SourceUnitId` / `ModuleId` indices never hit
/// the wire.
#[derive(Debug, Clone, Serialize)]
pub struct AtomicGroupConflictReport {
    /// Members as `"unit:N"` keys, sorted by source-unit ID.
    pub members: Vec<String>,
    pub claims: Vec<ConflictingClaimReport>,
    pub witness: AtomicGroupConflictWitness,
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

const ATOMIC_GROUP_WITNESS_STEP_LIMIT: usize = 12;

#[derive(Debug, Clone, Copy)]
struct AtomicGroupPathParent {
    previous: SourceUnitId,
    edge_id: DependencyEdgeId,
    projection: AtomicGroupPathProjection,
    orientation: AtomicGroupPathOrientation,
}

fn atomic_group_conflict_witness(
    conflict: &AtomicGroupConflict,
    module_path: &dyn Fn(ModuleId) -> spec::ModulePath,
    dependency_graph: &crate::DependencyGraph,
    edge_report: &dyn Fn(DependencyEdgeId) -> DependencyEdgeReport,
) -> AtomicGroupConflictWitness {
    let first = conflict
        .claims
        .first()
        .expect("an atomic conflict has at least one claimed source unit");
    let second = conflict
        .claims
        .iter()
        .find(|claim| claim.module != first.module)
        .expect("an atomic conflict has claims in distinct modules");
    let members = conflict.members.iter().copied().collect::<BTreeSet<_>>();
    AtomicGroupConflictWitness {
        claim_pair: [
            AtomicGroupWitnessClaim {
                source_unit: source_unit_key(first.source_unit),
                module: module_path(first.module),
            },
            AtomicGroupWitnessClaim {
                source_unit: source_unit_key(second.source_unit),
                module: module_path(second.module),
            },
        ],
        paths: [
            atomic_group_witness_path(
                dependency_graph,
                &members,
                first.source_unit,
                second.source_unit,
                edge_report,
            ),
            atomic_group_witness_path(
                dependency_graph,
                &members,
                second.source_unit,
                first.source_unit,
                edge_report,
            ),
        ],
    }
}

fn atomic_group_witness_path(
    dependency_graph: &crate::DependencyGraph,
    members: &BTreeSet<SourceUnitId>,
    from: SourceUnitId,
    to: SourceUnitId,
    edge_report: &dyn Fn(DependencyEdgeId) -> DependencyEdgeReport,
) -> AtomicGroupWitnessPath {
    let mut parents =
        HashMap::<SourceUnitId, Option<AtomicGroupPathParent>>::with_capacity(members.len());
    parents.insert(from, None);
    let mut queue = VecDeque::from([from]);
    while let Some(current) = queue.pop_front() {
        if current == to {
            break;
        }
        for edge_id in dependency_graph
            .out_edges_of(current)
            .iter()
            .chain(dependency_graph.in_edges_of(current))
        {
            let edge = dependency_graph.edge(*edge_id);
            for arc in atomic_constraint_arcs(edge) {
                if arc.from != current
                    || !members.contains(&arc.to)
                    || parents.contains_key(&arc.to)
                {
                    continue;
                }
                parents.insert(
                    arc.to,
                    Some(AtomicGroupPathParent {
                        previous: current,
                        edge_id: edge.id,
                        projection: arc.projection,
                        orientation: arc.orientation,
                    }),
                );
                queue.push_back(arc.to);
            }
        }
    }
    assert!(
        parents.contains_key(&to),
        "atomic-group membership and witness projection must agree"
    );

    let mut path = Vec::new();
    let mut current = to;
    while current != from {
        let parent = parents[&current].expect("every non-start path node has a predecessor");
        path.push((parent.edge_id, parent.projection, parent.orientation));
        current = parent.previous;
    }
    path.reverse();
    let total_step_count = path.len();
    let truncated = total_step_count > ATOMIC_GROUP_WITNESS_STEP_LIMIT;
    let steps = path
        .into_iter()
        .take(ATOMIC_GROUP_WITNESS_STEP_LIMIT)
        .map(
            |(edge_id, projection, orientation)| AtomicGroupWitnessStep {
                edge: edge_report(edge_id),
                projection,
                orientation,
            },
        )
        .collect();
    AtomicGroupWitnessPath {
        from_source_unit: source_unit_key(from),
        to_source_unit: source_unit_key(to),
        total_step_count,
        truncated,
        steps,
    }
}

pub fn sequenced_source_unit_causes(
    dependency_graph: &crate::DependencyGraph,
    members: &[SourceUnitId],
) -> Vec<SequencedSourceUnitCause> {
    let members: std::collections::BTreeSet<_> = members.iter().copied().collect();
    members
        .into_iter()
        .filter_map(|source_unit| {
            dependency_graph
                .out_edges_of(source_unit)
                .iter()
                .map(|id| dependency_graph.edge(*id))
                .find(|edge| {
                    edge.reason.kind == DepKind::Sequenced && edge.reason.constrains_init_order()
                })
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
    impure_source_unit_cause(dependency_graph, edge.from)
}

/// Context for one sequencing endpoint: every impure source unit, or a pure
/// source unit with available non-empty heap evidence. Pure endpoints with no
/// such evidence are omitted; that absence does not prove zero heap effects.
pub fn impure_source_unit_cause(
    dependency_graph: &crate::DependencyGraph,
    source_unit: SourceUnitId,
) -> Option<SequencedSourceUnitCause> {
    let node = dependency_graph.node(source_unit)?;
    SequencedSourceUnitCause::from_evidence(
        &node.purity,
        node.synchronous_execution.as_ref(),
        node.source_location.as_ref(),
        || source_unit_key(source_unit),
        node.declared.iter().map(|id| id.0.clone()),
    )
}

impl SequencedSourceUnitCause {
    fn from_evidence(
        purity: &Purity,
        execution: Option<&SynchronousExecutionReport>,
        source_location: Option<&SourceLocation>,
        source_unit_id: impl FnOnce() -> String,
        binding_names: impl Iterator<Item = Atom>,
    ) -> Option<Self> {
        let heap_frontier = execution.and_then(|execution| execution.heap_frontier.clone());
        if matches!(purity, Purity::Pure { .. }) && heap_frontier.is_none() {
            return None;
        }
        Some(Self {
            source_unit_id: source_unit_id(),
            binding_names: binding_names.collect(),
            source_location: source_location.cloned(),
            purity: purity.clone(),
            synchronous_execution_complete: execution.map(|execution| execution.complete),
            heap_frontier,
        })
    }
}

impl AtomicGroupConflictReport {
    pub fn from_conflicts(
        conflicts: &[AtomicGroupConflict],
        module_path: &dyn Fn(ModuleId) -> spec::ModulePath,
        dependency_graph: &crate::DependencyGraph,
        // Graph-query inputs retain canonical display labels separately from
        // topology; do not invent hygienic binding IDs to reconstruct them.
        edge_report: &dyn Fn(DependencyEdgeId) -> DependencyEdgeReport,
    ) -> Vec<Self> {
        conflicts
            .iter()
            .map(|conflict| Self {
                members: conflict
                    .members
                    .iter()
                    .copied()
                    .map(source_unit_key)
                    .collect(),
                claims: conflict
                    .claims
                    .iter()
                    .map(|claim| ConflictingClaimReport {
                        source_unit: source_unit_key(claim.source_unit),
                        binding_names: claim.binding_names.clone(),
                        module: module_path(claim.module),
                    })
                    .collect(),
                witness: atomic_group_conflict_witness(
                    conflict,
                    module_path,
                    dependency_graph,
                    edge_report,
                ),
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

/// The shared "modules in the SCC + the edges in the SCC" shape — the
/// one core carried, in different encodings, by every type describing
/// an unrealizable / cyclic module-quotient SCC:
///
/// - `gate::SccDiagnosis` — the in-memory primitive:
///   this core plus an `SccRejection` decoration. Carries it verbatim.
/// - `gate::CycleReport` — the validator's rendered
///   projection: module names stringified to `ModulePath`, plus FAS
///   `cut` / `lazy_closure` decorations.
/// - [`schema::QuotientSccReport`] — the wire projection: wire-stable
///   string ids in place of typed [`ModuleId`] / [`DependencyEdgeId`],
///   covering every dep-graph SCC (not only the unrealizable ones).
///
/// The rendered/wire projections re-encode these two fields with their
/// own id spelling and decorations, so they point here rather than
/// re-describing the shape. [`crate::assignment_assembly::AtomicGroupConflict`]
/// is the atom-level sibling — same "unrealizable, here's why" framing
/// but a different domain (source units in an atomic group, not modules in an
/// SCC), so it does not reuse this core.
#[derive(Debug, Clone, Eq, PartialEq)]
pub struct SccCore {
    /// Modules participating in the cycle.
    pub modules: BTreeSet<ModuleId>,
    /// Constraining cross-module dependency-edge evidence backing the SCC,
    /// in stable [`DependencyEdgeId`] order.
    pub constraining_dependency_edges: Vec<DependencyEdgeId>,
}

pub fn source_unit_key(id: SourceUnitId) -> String {
    format!("unit:{}", id.0)
}

pub fn module_key(id: ModuleId) -> ModuleKey {
    let idx = id.0;
    ModuleKey(format!("logical:{idx}"))
}

pub fn atomic_group_key(idx: usize) -> String {
    format!("atomic:{idx}")
}
