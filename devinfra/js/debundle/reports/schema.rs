use analysis_model::DepKind;
use serde::{Deserialize, Serialize};
use source_identity::{SourceUnitId, SourceUnitOrdinal};
use std::collections::{BTreeSet, HashMap, VecDeque};
use std::fmt;
use swc_atoms::Atom;

use crate::analysis_inputs::{
    CallbackFactoryEvidence, CommutingStatementsRef, ReturnedRecordSummary,
};
use crate::assignment_assembly::AtomicGroupConflict;
use crate::atomic_groups::{
    AtomicGroupPathOrientation, AtomicGroupPathProjection, atomic_constraint_arcs,
};
use crate::execution_evidence::SourceLocation;
use crate::execution_evidence::{ConstructionRejection, ExecutionPrecisionLoss};
use crate::graph::{DependencyEdge, DependencyEdgeId, EdgeRole, EffectCellReport};
use crate::ids::ModuleId;
use crate::purity::Purity;
use crate::{SourceUnitClass, StatementKind};

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

/// One census witness `program_premises.intrinsics_never_written` overrides.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct NeverWrittenOverride {
    /// Index into `native_integrity_witnesses`.
    pub witness: usize,
    pub chunk_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_location: Option<SourceLocation>,
    /// The named families whose slots this witness would reach.
    pub families: BTreeSet<crate::analysis_inputs::NativeIntrinsic>,
    /// Why it reaches them.
    pub reach: NeverWrittenReach,
}

/// How an overridden witness reaches a named family.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NeverWrittenReach {
    /// The receiver may be any native object.
    UnknownReceiver,
    /// A write to the global object, whose unknown key may name a binding or
    /// which couples every prototype slot.
    GlobalObject,
    /// A write the census resolves to the family without an exact path.
    Family,
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

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReturnedRecordUseKind {
    CallTiming,
    ReturnCapability,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct ReturnedRecordUseReport {
    pub summary_ref: String,
    pub kind: ReturnedRecordUseKind,
    pub lexical_statement_ordinal: SourceUnitOrdinal,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_location: Option<SourceLocation>,
    /// Author-conditioned native-integrity inputs used by the defining
    /// returned-record certificate.
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pub native_integrity_value_inputs: BTreeSet<crate::analysis_inputs::ValueInputRef>,
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

/// A conservative source write census entry, including writes in deferred bodies.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct NativeMutationWitness {
    pub chunk_id: String,
    pub statement_ordinal: SourceUnitOrdinal,
    pub source_location: Option<SourceLocation>,
    pub operation: NativeMutationOperation,
    /// Identity before unknown-global descendants widen to GlobalObject.
    pub receiver_roots: BTreeSet<crate::analysis_inputs::NativeIntrinsic>,
    pub through_local_alias: bool,
    /// The global-object property the receiver is read from, by spelling:
    /// `location` for `window.location.href = url`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub receiver_global: Option<String>,
    pub affected_roots: BTreeSet<crate::analysis_inputs::NativeIntrinsic>,
    /// Target paths and keys retained before family-wide integrity widening.
    /// Unknown paths/keys remain visible but are not source-known slot writes.
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pub target_slots: BTreeSet<NativeWriteSlot>,
    /// Native identities needed before a computed key can be trusted as exact.
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pub key_dependencies: BTreeSet<crate::analysis_inputs::NativeIntrinsic>,
    /// Native premises invalidating the property-key refinement, if any.
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pub invalidated_key_dependencies: BTreeSet<crate::analysis_inputs::NativeIntrinsic>,
    /// Author-conditioned captured-value facts consumed by this receiver
    /// refinement, including refinements whose resulting affected roots are empty.
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pub value_input_refs: BTreeSet<crate::analysis_inputs::ValueInputRef>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub value_input_evidence: Vec<crate::analysis_inputs::ValueInputEvidence>,
    /// The original write remains visible when source argument evidence proves
    /// its containing branch unreachable in every covered activation.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub unreachable_branch: Option<NativeUnreachableBranch>,
    #[serde(skip)]
    pub(crate) span: swc_common::Span,
}

/// A source-visible receiver path. Constructor/prototype paths identify the
/// binding used by source; consumers still need native-identity evidence before
/// treating that binding as the original intrinsic object.
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NativeReceiverPath {
    GlobalObject,
    Constructor(crate::analysis_inputs::NativeIntrinsic),
    Prototype(crate::analysis_inputs::NativeIntrinsic),
    Descendant(crate::analysis_inputs::NativeIntrinsic),
    ArrayIteratorPrototype,
    SetIteratorPrototype,
    Unknown,
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NativePropertyKey {
    String(String),
    /// Any string starting with `prefix` and ending with `suffix`.
    Affixed {
        prefix: String,
        suffix: String,
    },
    SymbolIterator,
    OtherSymbol,
    Unknown,
}

impl NativePropertyKey {
    /// Whether a key in this domain may be the string `name`.
    pub(crate) fn may_be_string(&self, name: &str) -> bool {
        match self {
            Self::String(key) => key == name,
            Self::Affixed { prefix, suffix } => {
                name.len() >= prefix.len() + suffix.len()
                    && name.starts_with(prefix.as_str())
                    && name.ends_with(suffix.as_str())
            }
            Self::SymbolIterator | Self::OtherSymbol => false,
            Self::Unknown => true,
        }
    }
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
pub struct NativeWriteSlot {
    pub receiver: NativeReceiverPath,
    pub key: NativePropertyKey,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct NativeUnreachableBranch {
    pub arguments: crate::analysis_inputs::CallbackObjectArguments,
    pub imported_local: String,
    pub wrapper_call_location: Option<SourceLocation>,
    pub callback_location: Option<SourceLocation>,
    pub guard_location: Option<SourceLocation>,
    #[serde(skip)]
    pub(crate) guard_span: swc_common::Span,
    #[serde(skip)]
    pub(crate) wrapper_call_span: swc_common::Span,
    #[serde(skip)]
    pub(crate) callback_span: swc_common::Span,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NativeMutationOperation {
    PropertyAssignment,
    BindingAssignment,
    Update,
    Delete,
    MutationCall,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NativeIntegrityGuard {
    OrdinaryObjectPrototype,
    ArrayIntrinsics,
    HostPromiseResolve,
    NativeCallIdentity,
    SetIterable,
    LocalClassConstruction,
    ForeignCallSummary,
    /// `Object.fromEntries` over an Array `map` whose callbacks are retained.
    MappedFromEntries,
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
pub struct NativeIntegrityCheck {
    pub guard: NativeIntegrityGuard,
    /// Every slot the guard required, as `%Array.prototype%`; absent from
    /// graphs written before it was recorded.
    #[serde(default)]
    pub slots: BTreeSet<String>,
    /// References to the affected roots in `program_graph.json.gz`'s native_integrity_witnesses.
    pub affected_roots: BTreeSet<crate::analysis_inputs::NativeIntrinsic>,
}

/// A source operation that can affect properties of the intrinsic Object prototype.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct NativePrototypeMutation {
    pub chunk_id: String,
    pub statement_ordinal: SourceUnitOrdinal,
    pub source_location: Option<SourceLocation>,
    pub kind: NativePrototypeMutationKind,
    /// None denotes any property; an empty set denotes only Symbol keys.
    pub properties: Option<BTreeSet<String>>,
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pub key_dependencies: BTreeSet<crate::analysis_inputs::NativeIntrinsic>,
    #[serde(skip)]
    pub(crate) span: swc_common::Span,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NativePrototypeMutationKind {
    Write,
    Delete,
    Reflective,
    Escape,
    DynamicScope,
}

/// An evaluated property-specific absence obligation, including its source blockers.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct NativePrototypePropertyCheck {
    pub property: String,
    pub intact: bool,
    pub blocking_mutation_count: usize,
}

/// Diagnostic effects activated before a modeled native premise. This does
/// not certify the premise or impose an emitted-module ordering constraint.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize, Hash)]
#[serde(rename_all = "snake_case")]
pub enum NativeProgramPointKind {
    Evaluate,
    Invoke,
    Call,
    OwnProperty,
    Heap,
    Callbacks,
    Model,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct NativeProgramPointStep {
    pub kind: NativeProgramPointKind,
    pub ordinal: usize,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct NativePrefixObservation {
    pub guard: NativeIntegrityGuard,
    /// Runtime fact path within the source unit; the array position orders
    /// repeated guards at the same transfer point.
    pub program_point_path: Vec<NativeProgramPointStep>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub property: Option<String>,
    pub prior_affected_roots: BTreeSet<crate::analysis_inputs::NativeIntrinsic>,
    pub prior_blocking_prototype_mutations: usize,
    pub uncertain_execution: bool,
}

/// A successful native guard whose conclusion depends on one or more
/// author-conditioned receiver refinements in the whole-program census.
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
pub struct NativeIntegrityUse {
    pub guard: NativeIntegrityGuard,
    pub value_input_refs: BTreeSet<crate::analysis_inputs::ValueInputRef>,
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pub no_accessor_install_refs: BTreeSet<crate::analysis_inputs::NoAccessorInstallRef>,
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pub program_premises: BTreeSet<crate::analysis_inputs::ProgramPremiseKey>,
}

/// Human-readable projection of inference provenance for report output.
///
/// Analysis owns the typed evidence. The report builder enriches it with
/// rendered author-input locations while preserving the stable report shape.
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
pub struct InferenceUseReport {
    pub conclusion: crate::analysis_provenance::InferenceRule,
    pub derivation_rules: BTreeSet<crate::analysis_provenance::InferenceRule>,
    pub value_input_refs: BTreeSet<crate::analysis_inputs::ValueInputRef>,
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pub descriptor_copy_author_refs: BTreeSet<crate::analysis_inputs::DescriptorCopyAuthorRef>,
    pub argument_input_refs: BTreeSet<crate::analysis_inputs::ArgumentInputRef>,
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pub argument_input_spec_locations: BTreeSet<String>,
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pub native_operation_input_refs: BTreeSet<crate::analysis_inputs::NativeOperationInputRef>,
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pub imported_primitive_array_spread_input_refs:
        BTreeSet<crate::analysis_inputs::ImportedPrimitiveArraySpreadInputRef>,
}

impl From<&crate::analysis_provenance::InferenceUse> for InferenceUseReport {
    fn from(inference: &crate::analysis_provenance::InferenceUse) -> Self {
        Self {
            conclusion: inference.conclusion,
            derivation_rules: inference.derivation_rules.clone(),
            value_input_refs: inference.value_input_refs.clone(),
            descriptor_copy_author_refs: inference.descriptor_copy_author_refs.clone(),
            argument_input_refs: inference.argument_input_refs.clone(),
            argument_input_spec_locations: BTreeSet::new(),
            native_operation_input_refs: inference.native_operation_input_refs.clone(),
            imported_primitive_array_spread_input_refs: inference
                .imported_primitive_array_spread_input_refs
                .clone(),
        }
    }
}

/// A classifier-pure owner becoming impure at completion is an inference bug.
/// The containing source-unit node supplies the exact source/chunk/site.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct PurityCompletionDisagreement {
    pub prepared: Purity,
    pub completed: Purity,
}

/// Count and stable bounded sample of unique canonical effect cells.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct HeapCellSetReport {
    pub count: usize,
    pub sample: Vec<EffectCellReport>,
}

/// Heap effects in one summary. `opaque` is the summary's existing coarse
/// unknown-frontier bit; it does not identify the operation that set it.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct HeapContentSummaryReport {
    pub opaque: bool,
    pub allocations: HeapCellSetReport,
    pub reads: HeapCellSetReport,
    pub writes: HeapCellSetReport,
}

/// Heap accesses recorded outside an active `AssumedEffectReason` path at the
/// recording or merge point. Other premises, including program and native-input
/// premises, may still have been consumed.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct NonAssumedHeapAccessReport {
    pub reads: HeapCellSetReport,
    pub writes: HeapCellSetReport,
}

/// Existing own and composed heap summaries projected for bounded diagnosis.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct HeapFrontierEvidence {
    pub own_at_init: HeapContentSummaryReport,
    pub composed_synchronous_calls: HeapContentSummaryReport,
    pub non_assumed_heap_accesses: NonAssumedHeapAccessReport,
}

impl fmt::Display for HeapCellSetReport {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{} [", self.count)?;
        for (index, cell) in self.sample.iter().enumerate() {
            if index > 0 {
                f.write_str(", ")?;
            }
            write!(f, "{cell}")?;
        }
        let omitted = self.count.saturating_sub(self.sample.len());
        if omitted > 0 {
            if !self.sample.is_empty() {
                f.write_str(", ")?;
            }
            write!(f, "{omitted} omitted")?;
        }
        f.write_str("]")
    }
}

impl fmt::Display for HeapFrontierEvidence {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "own-at-init opaque={} allocations={} reads={} writes={}; \
             composed synchronous calls opaque={} allocations={} reads={} writes={}; \
             non-assumed reads={} writes={}",
            self.own_at_init.opaque,
            self.own_at_init.allocations,
            self.own_at_init.reads,
            self.own_at_init.writes,
            self.composed_synchronous_calls.opaque,
            self.composed_synchronous_calls.allocations,
            self.composed_synchronous_calls.reads,
            self.composed_synchronous_calls.writes,
            self.non_assumed_heap_accesses.reads,
            self.non_assumed_heap_accesses.writes,
        )
    }
}

/// Completeness of the existing synchronous-effect inference, independent of purity.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct SynchronousExecutionReport {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub initializer_binding_effects: Option<Box<InitializerBindingEffectsUse>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub purity_disagreement: Option<PurityCompletionDisagreement>,
    /// Which authority the graph uses to decide whether to skip syntactic
    /// at-init call fallback. This report is diagnostic output only.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub binding_effect_authority: Option<BindingEffectAuthorityReport>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub inference_uses: Vec<InferenceUseReport>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub local_record_factory_uses: Vec<LocalRecordFactoryUse>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub local_record_factory_rejections: Vec<LocalRecordFactoryRejection>,
    /// Evaluated premises which failed; other eligibility conditions may also fail.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub native_integrity_checks: Vec<NativeIntegrityCheck>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub native_prototype_property_checks: Vec<NativePrototypePropertyCheck>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub native_prefix_observations: Vec<NativePrefixObservation>,
    /// Successful native guards that consumed author-conditioned census facts.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub native_integrity_uses: Vec<NativeIntegrityUse>,
    /// Exact native-operation sites admitted by a source-selected author
    /// premise. This is author evidence, not a native-integrity inference.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub native_operation_input_uses: Vec<NativeOperationInputUse>,
    /// Exact imported-array spreads with the authored lifetime assertion
    /// beside independently source-resolved import/export and literal facts.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub imported_primitive_array_spread_input_uses:
        Vec<crate::analysis_inputs::ImportedPrimitiveArraySpreadInputUse>,
    pub complete: bool,
    pub precision_losses: Vec<ExecutionPrecisionLoss>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub callback_factory_uses: Vec<CallbackFactoryUseReport>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub returned_record_uses: Vec<ReturnedRecordUseReport>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub author_host_contract_uses: Vec<ReentryFreeOperationUse>,
    /// Bounded attribution of the existing own and composed heap summaries.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub heap_frontier: Option<HeapFrontierEvidence>,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
#[serde(tag = "authority", rename_all = "snake_case")]
pub enum BindingEffectAuthorityReport {
    /// No operation or transitional authority is present in these facts.
    /// Programmatically created, unclassified owners retain fallback; a saved
    /// analysis artifact missing its required authority field is rejected.
    Unclassified,
    /// Temporary compatibility path for owners outside ordered root-body
    /// execution. `complete` is not an operation receipt.
    LegacyExecution { complete: bool },
    /// The exact source-only initializer assertion was applied.
    AuthorSourceOnly,
    /// Ordered root execution started, but did not seal typed lexical proof.
    OrderedRootInitializerUncovered {
        owner: SourceUnitOrdinal,
        binding: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        initializer_span: Option<swc_common::Span>,
    },
    /// Typed operation evidence sealed for the exact ordered root owner/body.
    OperationReceipt {
        receipt: Box<LexicalBindingEffectReceipt>,
    },
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct LexicalBindingEffectReceipt {
    pub owner: SourceUnitOrdinal,
    pub binding: String,
    pub initializer_span: Option<swc_common::Span>,
    pub read_dependencies: crate::analysis_inputs::ProofDependencies,
    pub rebind_dependencies: crate::analysis_inputs::ProofDependencies,
    /// Flat exact call edges retain each consuming call-site, caller body,
    /// resolved target body, and merged prerequisites, including memo-reused
    /// invocations. The analyzer checks connectivity from the root body.
    pub calls: Vec<LexicalBindingEffectCall>,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum LexicalBindingEffectBody {
    RootInitializer {
        owner: SourceUnitOrdinal,
        initializer_span: swc_common::Span,
    },
    Callable {
        key: u32,
        owner: SourceUnitOrdinal,
    },
    /// A local class's base-constructor body run by a checked construction.
    Constructor {
        class: u32,
        owner: SourceUnitOrdinal,
    },
    Native {
        intrinsic: crate::analysis_inputs::NativeIntrinsic,
        property: Option<String>,
    },
    /// An imported callable replaced by its derived foreign call summary.
    Imported {
        defining_module: String,
        export: String,
    },
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct LexicalBindingEffectCall {
    pub caller: LexicalBindingEffectBody,
    pub call_span: swc_common::Span,
    pub target: LexicalBindingEffectBody,
    pub dependencies: crate::analysis_inputs::ProofDependencies,
}

/// An exact initializer's author-supplied lexical effect bound. The enclosing
/// source unit supplies its location; this does not certify other effect domains.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
#[serde(tag = "outcome", rename_all = "snake_case")]
pub enum InitializerBindingEffectsUse {
    Applied {
        binding: String,
        narrowed_reads: usize,
        narrowed_rebinds: usize,
        narrowed_uninitialized_reads: usize,
    },
    Contradicted {
        binding: String,
        reads: BTreeSet<String>,
        rebinds: BTreeSet<String>,
    },
}

/// One exact call site that consumed a native-operation author premise.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct NativeOperationInputUse {
    pub evidence: crate::analysis_inputs::NativeOperationInputEvidence,
    pub source_location: Option<SourceLocation>,
    #[serde(skip)]
    pub(crate) span: swc_common::Span,
}

/// A consumed author assertion on a local unary record-retaining factory.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct LocalRecordFactoryUse {
    pub factory_binding: String,
    /// Named prototype premises supplied by the author, not inferred integrity.
    pub prototype_properties: Vec<String>,
    pub lexical_statement_ordinal: SourceUnitOrdinal,
    pub source_location: Option<SourceLocation>,
    #[serde(skip)]
    pub(crate) span: swc_common::Span,
}

/// A direct call of an annotated record factory whose argument is outside the
/// admitted fresh-record shapes, so the author premise was not consulted.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct LocalRecordFactoryRejection {
    pub factory_binding: String,
    pub reason: RecordFactoryRejectionReason,
    /// Outermost record property holding the refused value, when there is one.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub property: Option<String>,
    /// The returned allocation's failed constructor check, for `constructor_proof`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub construction: Option<ConstructionRejection>,
    /// First owner observed inserting this factory function into the escaped
    /// callback set through `publish_values`; absent when no such insertion was recorded.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub first_callback_publication_owner: Option<SourceUnitOrdinal>,
    /// The first actual insertion of the returned class's members into the
    /// escaped-callback set, when the constructor proof was refused because
    /// those members escaped. Diagnostic context only; it is not proof
    /// authority.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub first_class_members_publication: Option<FirstClassMembersPublication>,
    pub lexical_statement_ordinal: SourceUnitOrdinal,
    pub source_location: Option<SourceLocation>,
    #[serde(skip)]
    pub(crate) span: swc_common::Span,
}

/// The first operation that inserted `ClassMembers(key)` into the escaped
/// callback set. The top-level owner and source span are optional when the
/// insertion path had no source-site context.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct FirstClassMembersPublication {
    pub writer: ClassMembersPublicationWriter,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub top_level_owner: Option<SourceUnitOrdinal>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub span_start: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub span_end: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_location: Option<SourceLocation>,
    /// For bounded invocation fallback, one deterministic retained-input path
    /// to the first newly published class capability. This is diagnostic
    /// context only; it does not establish that the seed caused publication.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub bounded_fallback_input_path: Option<BoundedInvocationFallbackInputPath>,
    /// Span of the opaque invocation that entered a bounded fallback, when
    /// known. The location is the active top-level owner context, which may
    /// differ from a nested callable's lexical source location. This context
    /// is distinct from the writer's own span above.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub bounded_fallback_callsite: Option<BoundedInvocationFallbackCallSite>,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct BoundedInvocationFallbackCallSite {
    pub top_level_owner: Option<SourceUnitOrdinal>,
    pub span_start: u32,
    pub span_end: u32,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_location: Option<SourceLocation>,
}

/// A retained-input path followed by the bounded callback-pool fallback.
/// Unknown is explicit when the shared callback walk found a class capability
/// but could not preserve a rooted path for it.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum BoundedInvocationFallbackInputPath {
    Retained {
        seed: BoundedInvocationFallbackSeed,
        edges: Vec<BoundedInvocationFallbackPathEdge>,
    },
    Unknown {
        reason: BoundedInvocationFallbackUnknownPathReason,
    },
}

/// One of the exact root groups supplied to the bounded fallback's existing
/// callback-value union. If a value occurs in several groups, the witness
/// follows the first matching group in argument, captured-binding, escaped
/// pool, export-callback order.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum BoundedInvocationFallbackSeed {
    /// Position in an abstract input-argument vector, not proof of a runtime
    /// caller argument. Pool fallback uses original supplied inputs rather
    /// than its synthetic flattened callback-set argument.
    Argument {
        position: usize,
    },
    CapturedBinding {
        binding: String,
    },
    EscapedCallbackPool,
    AvailableExportBinding {
        binding: String,
    },
    AvailableExportCallbacks,
}

/// A typed retained-reference edge. Array-element edges require an actual
/// array allocation and a canonical index property; ordinary object-backed
/// array-literal storage remains an object-property edge. Collection indices
/// and runtime allocation identities are omitted where the abstract value is
/// a set.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum BoundedInvocationFallbackPathEdge {
    ObjectProperty { property: String },
    UnknownObjectProperty,
    ArrayElement { index: u32 },
    UnknownArrayProperty,
    AppliedGetter { property: Option<String> },
    AppliedSetter { property: Option<String> },
    ClosureHomeObject,
    ClosureSnapshot { binding: String },
    ClosureCell { binding: String },
    InstanceRetainedValue,
    DeferredCallbackRetainedValue,
    AuditedResultRetainedValue,
    DescriptorMapRetainedValue,
    EscapedCallbackPoolExpansion,
    ClassMembersCapability,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BoundedInvocationFallbackUnknownPathReason {
    SeedUnavailable,
    RetainedReferencePathUnavailable,
}

/// The operation that first exposed class members to the opaque callback pool.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ClassMembersPublicationWriter {
    GlobalOrIntrinsicMemberWrite,
    InstanceMemberWrite,
    OwnDescriptorMapMemberWrite,
    UntrackedReceiverMemberWrite,
    PublishedObjectMemberWrite,
    MemberChainStore,
    PossibleCallbackRetention,
    BoundedInvocationFallback,
    NoReentryCallRetention,
    LocalOperationFallback,
    ThrowValuePublication,
    /// A store to a local the source-summary lane reads as unknown.
    UntrackedLocalStore,
    /// A generator call: whoever later resumes the generator object runs
    /// the body.
    GeneratorCreation,
}

/// Why an annotated record-factory call kept the conservative path. The
/// first group refuses the argument's shape; the rest are checked-constructor
/// proof conditions outside the record.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RecordFactoryRejectionReason {
    /// The call does not pass exactly one direct object literal.
    NotObjectLiteral,
    /// An object spread whose source is not a proven fresh plain-data record.
    Spread,
    ComputedKey,
    /// A getter or setter, which a constructor property read could invoke.
    Accessor,
    /// A `__proto__` key, which sets the prototype instead of a field.
    PrototypeKey,
    DuplicateKey,
    /// An array hole or array spread.
    ArrayShape,
    /// A value whose evaluation can run code (calls, assignments, sequences, ...).
    UnsupportedValue,
    /// A binding read not proven to hold a primitive or a function at the call.
    UnprovenBinding,
    /// A member read not proven to be a closed enum's own primitive member.
    UnprovenMemberRead,
    /// A template substitution not proven primitive, so `ToString` could run code.
    TemplateSubstitution,
    /// Synchronous execution was already incomplete before this statement.
    IncompleteExecution,
    /// The result binding is exported, with importer reentry not ruled out.
    ExportedResult,
    /// The call is not the sole initializer of one declared binding, or the
    /// statement also writes properties or exports values.
    StatementShape,
    /// The statement evaluates other calls or unproven conversions.
    ArgumentEffects,
    /// The callee is not a direct binding read.
    FactoryCalleeShape,
    /// The callee binding has no hoisted source function.
    FactoryNotHoisted,
    /// The result binding is not a declared, definitely initialized binding.
    InitializerBinding,
    /// The factory binding is exported.
    FactoryExported,
    /// The factory function was published to the global escaped-callback set.
    FactoryEscaped,
    /// Whole-chunk flow does not contain only the hoisted factory function.
    FactoryFlowIdentity,
    /// The current prefix does not contain only the hoisted factory function.
    FactoryPrefixIdentity,
    /// The factory body is not one return of checked local allocations.
    FactoryBody,
    /// A constructed class failed the checked local constructor proof.
    ConstructorProof,
}

/// A successful timing refinement that consumed a `reentry_free_operations`
/// entry (SPEC.md § Host behavior).
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct ReentryFreeOperationUse {
    pub operation: spec::ReentryFreeOperation,
    pub source_location: Option<SourceLocation>,
    #[serde(skip)]
    pub(crate) span: swc_common::Span,
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
    pub sequenced_cause: Option<crate::graph::SequencedCause>,
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
    pub premise_verdict: Option<crate::graph::PremiseVerdict>,
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
                callee_source_unit: crate::reports::source_unit_key(callee_source_unit),
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
            source: crate::reports::source_unit_key(edge.from),
            target: crate::reports::source_unit_key(edge.to),
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
                source_unit: crate::reports::source_unit_key(first.source_unit),
                module: module_path(first.module),
            },
            AtomicGroupWitnessClaim {
                source_unit: crate::reports::source_unit_key(second.source_unit),
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
        from_source_unit: crate::reports::source_unit_key(from),
        to_source_unit: crate::reports::source_unit_key(to),
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
    let heap_frontier = node
        .synchronous_execution
        .as_ref()
        .and_then(|execution| execution.heap_frontier.clone());
    if matches!(&node.purity, Purity::Pure { .. }) && heap_frontier.is_none() {
        return None;
    }
    Some(SequencedSourceUnitCause {
        source_unit_id: crate::reports::source_unit_key(source_unit),
        binding_names: node.declared.iter().map(|id| id.0.clone()).collect(),
        source_location: node.source_location.clone(),
        purity: node.purity.clone(),
        synchronous_execution_complete: node
            .synchronous_execution
            .as_ref()
            .map(|execution| execution.complete),
        heap_frontier,
    })
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
