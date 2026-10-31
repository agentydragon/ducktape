use serde::{Deserialize, Serialize};
use source_identity::SourceUnitOrdinal;
use std::collections::BTreeSet;
use swc_atoms::Atom;

use crate::analysis_inputs::{
    CallbackFactoryEvidence, CommutingStatementsRef, ReturnedRecordSummary,
};
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
    /// Whole-program census sites, shared by failed native premise checks.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub native_integrity_witnesses: Vec<NativeMutationWitness>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub native_prototype_mutations: Vec<NativePrototypeMutation>,
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
    SymbolIterator,
    OtherSymbol,
    Unknown,
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
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
pub struct NativeIntegrityCheck {
    pub guard: NativeIntegrityGuard,
    /// References to the affected roots in graph-level native_integrity_witnesses.
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
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
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
}

/// Positive rules participating in a consumed construction certificate.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum InferenceRule {
    NativeIntegrity,
    NativeCallIdentity,
    NativeOwnDataRead,
    EmptyCollectionConstruction,
    LocalClassConstruction,
    PrimitiveSetIteration,
    StablePrimitiveArraySpread,
    StableFunction,
    StableProvider,
    ArgumentNoEscape,
    ReturnedRecord,
    NamespaceWrapper,
    OwnPropertySourceOrigin,
    OwnPropertyShape,
    OwnPropertyPrefixState,
    OwnPropertyNamesProjection,
    OwnPropertyDescriptorProjection,
    DescriptorCopyProvider,
    DescriptorCopyFactory,
    ForeignCallSummary,
}

/// A purpose-specific certificate actually used by synchronous execution.
/// Internal derivations share premise IDs; reports expand only rule names.
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
pub struct InferenceUse {
    pub conclusion: InferenceRule,
    pub derivation_rules: BTreeSet<InferenceRule>,
    pub value_input_refs: BTreeSet<crate::analysis_inputs::ValueInputRef>,
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pub descriptor_copy_author_refs: BTreeSet<crate::analysis_inputs::DescriptorCopyAuthorRef>,
    pub argument_input_refs: BTreeSet<crate::analysis_inputs::ArgumentInputRef>,
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pub argument_input_spec_locations: BTreeSet<String>,
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pub native_operation_input_refs: BTreeSet<crate::analysis_inputs::NativeOperationInputRef>,
}

/// Completeness of the existing synchronous-effect inference, independent of purity.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct SynchronousExecutionReport {
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub inference_uses: Vec<InferenceUse>,
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
    pub complete: bool,
    pub precision_losses: Vec<ExecutionPrecisionLoss>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub callback_factory_uses: Vec<CallbackFactoryUseReport>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub returned_record_uses: Vec<ReturnedRecordUseReport>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub author_host_contract_uses: Vec<ReentryFreeOperationUse>,
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
    pub lexical_statement_ordinal: SourceUnitOrdinal,
    pub source_location: Option<SourceLocation>,
    #[serde(skip)]
    pub(crate) span: swc_common::Span,
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
    /// The callee is not the stable, unexposed hoisted factory function.
    FactoryIdentity,
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

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct ExecutionPrecisionLoss {
    pub reason: ExecutionPrecisionLossReason,
    pub reach: HavocReach,
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
    UnsupportedOperation {
        operation: ExecutionOperation,
    },
    /// `new` of a local class whose construction proof failed. The rejection
    /// names the first failed check, not every check that would fail.
    UnsupportedConstruction {
        rejection: ConstructionRejection,
    },
    RecursiveInvocation,
    InvocationDepthLimit {
        limit: usize,
    },
    InvocationExpansionLimit {
        limit: usize,
    },
    /// `pool_size` counts the callbacks the opaque call could invoke.
    CallbackPoolLimit {
        limit: usize,
        pool_size: usize,
    },
}

/// What the code a precision loss may run can reach, classified per
/// SPEC.md § Synchronous execution diagnostics. A report only: the conservative
/// path promotes the same effects for both.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum HavocReach {
    /// The unmodelled part is known source code: its callables and the
    /// values it receives reach no code the interpreter cannot name.
    Bounded,
    Global {
        cause: GlobalReachCause,
    },
}

/// The first collapse rule that made a reach `global`.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum GlobalReachCause {
    /// The code calls, directly or through the synchronous read closure, a
    /// callee the source does not name: an import, a global, a parameter, an
    /// unknown member, or an opaque body.
    UnknownCallee,
    /// An input reaches a value opaque code may hold or have changed: an
    /// unknown value, a tainted object, an escaped callable, or a global
    /// object alias.
    TaintedInput,
    /// A class the code runs is published, escaped or tainted, so its methods
    /// may run on a borrowed receiver.
    BorrowedReceiver,
    /// Dynamic scope (`eval`, `with`) makes the read closure incomplete.
    DynamicScope,
    /// An input is a foreign deferred record.
    ForeignRecord,
    /// The operation itself is unmodelled; values the interpreter does not
    /// know choose the code it may run.
    UnmodelledOperation,
    /// The callbacks unknown code may invoke: the prefix-wide escape pool.
    EscapedCallbackPool,
}

/// Why a local class construction kept the conservative path.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
#[serde(tag = "check", rename_all = "snake_case")]
pub enum ConstructionRejection {
    SpreadArguments,
    /// The class has no source-checked constructor summary.
    UncheckedConstructor,
    /// An implicit constructor with checked field initializers received arguments.
    ArgumentsToDefaultFields,
    /// The class binding is used other than as a `new` callee.
    ClassUsedOutsideConstruction,
    /// Export reentry can reach the class binding.
    ExportedClass,
    /// A write through an instance of this class was observed.
    ClassMutated,
    /// The class's members escaped to code that can modify its prototype.
    ClassMembersEscaped,
    /// An assigned instance field may name an `Object.prototype` accessor or
    /// non-writable property; the census entries are in
    /// `native_prototype_property_checks`.
    FieldMayExistOnObjectPrototype {
        property: String,
    },
    /// The constructor reads a binding not declared in the chunk.
    UndeclaredConstructorRead,
    /// An argument whose properties the constructor reads is not a fresh,
    /// untainted plain-data object.
    UncheckedPropertySource {
        parameter: usize,
    },
}

impl std::fmt::Display for ExecutionPrecisionLossReason {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::UnknownCallTarget => f.write_str("`unknown_call_target`"),
            Self::UnsupportedOperation { operation } => {
                let operation = serde_json::to_value(operation)
                    .expect("execution operations serialize as strings");
                write!(
                    f,
                    "`unsupported_operation` ({})",
                    operation.as_str().expect("unit variant")
                )
            }
            Self::UnsupportedConstruction { rejection } => {
                let rejection = serde_json::to_value(rejection)
                    .expect("construction rejections serialize as tagged objects");
                write!(
                    f,
                    "`unsupported_construction` ({})",
                    rejection["check"].as_str().expect("tagged by check")
                )
            }
            Self::RecursiveInvocation => f.write_str("`recursive_invocation`"),
            Self::InvocationDepthLimit { limit } => {
                write!(f, "`invocation_depth_limit` (limit {limit})")
            }
            Self::InvocationExpansionLimit { limit } => {
                write!(f, "`invocation_expansion_limit` (limit {limit})")
            }
            Self::CallbackPoolLimit { limit, pool_size } => {
                write!(f, "`callback_pool_limit` (pool {pool_size}, limit {limit})")
            }
        }
    }
}

/// The conservative path that assumed a binding effect instead of deriving
/// it (SPEC.md § Commuting statements). Each path assumes some code may run
/// (or reach the binding) at the statement; none follows an invocation the
/// statement definitely makes.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
#[serde(tag = "path", rename_all = "snake_case")]
pub enum AssumedEffectReason {
    /// The statement's bounded fallback summary: having stopped expanding
    /// callbacks, or handed a returned callback to opaque code, the
    /// interpreter assumes each may run. `loss` opened the summary.
    FallbackSummary { loss: ExecutionPrecisionLossReason },
    /// A callback opaque code may invoke (escaped earlier, exported, or
    /// reachable from the opaque operation's inputs), executed as if it ran.
    OpaqueCallbackInvocation,
    /// The synchronous read/rebind closure of a callback, class or foreign
    /// record exposed to opaque code, promoted without executing it.
    EscapedCallbackClosure,
    /// The lazy body of an accessor owner whose setter a property write or
    /// `Object.assign` may trigger on a receiver of unknown shape.
    PossibleSetter,
    /// The whole lazy body of a callable the interpreter executed but could
    /// not model (`callable_inputs`, `array_iteration`), beyond what that
    /// execution reached.
    WholeBodyPromotion { operation: ExecutionOperation },
    /// The omitted callback effects restored when a private-constructor
    /// result proof's execution failed.
    PrivateConstructorRestore,
    /// Graph build: the read closure of a callee at-init promotion could not
    /// resolve, assumed to run at the calling statement.
    UnresolvedCallClosure,
}

impl std::fmt::Display for AssumedEffectReason {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::FallbackSummary { loss } => write!(f, "the {loss} fallback summary"),
            Self::OpaqueCallbackInvocation => {
                f.write_str("a callback opaque code may invoke (`opaque_callback_invocation`)")
            }
            Self::EscapedCallbackClosure => {
                f.write_str("an escaped callback's closure (`escaped_callback_closure`)")
            }
            Self::PossibleSetter => f.write_str("a possible setter (`possible_setter`)"),
            Self::WholeBodyPromotion { operation } => {
                let operation = serde_json::to_value(operation)
                    .expect("execution operations serialize as strings");
                write!(
                    f,
                    "whole-body promotion (`whole_body_promotion`, {})",
                    operation.as_str().expect("unit variant")
                )
            }
            Self::PrivateConstructorRestore => f.write_str(
                "the failed private-constructor proof's restore (`private_constructor_restore`)",
            ),
            Self::UnresolvedCallClosure => {
                f.write_str("an unresolved at-init call's closure (`unresolved_call_closure`)")
            }
        }
    }
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
    /// Author-conditioned native premises used by a certified local-class
    /// heap construction in this source unit. Empty uses stay omitted.
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
    /// `synchronous_execution.complete` of the source unit; `None` when
    /// synchronous analysis did not run. `false` marks the statements
    /// whose sequencing edges are a conservative flood rather than a
    /// known ordering fact.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub synchronous_execution_complete: Option<bool>,
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
    impure_source_unit_cause(dependency_graph, edge.from)
}

/// The impure-initializer evidence of one source unit; `None` when the
/// source unit is classified pure (a pure endpoint of a sequencing edge,
/// e.g. an array allocation or a pure content reader).
pub fn impure_source_unit_cause(
    dependency_graph: &crate::DependencyGraph,
    source_unit: crate::SourceUnitId,
) -> Option<SequencedSourceUnitCause> {
    let node = dependency_graph.node(source_unit)?;
    let Purity::NotPure { .. } = &node.purity else {
        return None;
    };
    Some(SequencedSourceUnitCause {
        source_unit_id: crate::reports::source_unit_key(source_unit),
        binding_names: node.declared.iter().map(|id| id.0.clone()).collect(),
        source_location: node.source_location.clone(),
        purity: node.purity.clone(),
        synchronous_execution_complete: node
            .synchronous_execution
            .as_ref()
            .map(|execution| execution.complete),
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
