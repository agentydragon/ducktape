use serde::{Deserialize, Serialize};
use source_identity::SourceUnitOrdinal;
use std::collections::BTreeSet;
use std::fmt;
use swc_atoms::Atom;

use crate::execution_evidence::{ConstructionRejection, ExecutionPrecisionLoss, SourceLocation};
use crate::facts::EffectCell;
use crate::purity::Purity;

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
    pub span: swc_common::Span,
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
    pub fn may_be_string(&self, name: &str) -> bool {
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
    pub guard_span: swc_common::Span,
    #[serde(skip)]
    pub wrapper_call_span: swc_common::Span,
    #[serde(skip)]
    pub callback_span: swc_common::Span,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize, Deserialize, strum::Display)]
#[serde(rename_all = "snake_case")]
#[strum(serialize_all = "snake_case")]
pub enum NativeMutationOperation {
    PropertyAssignment,
    BindingAssignment,
    Update,
    Delete,
    MutationCall,
}

#[derive(
    Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize, strum::Display,
)]
#[serde(rename_all = "snake_case")]
#[strum(serialize_all = "snake_case")]
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
    pub span: swc_common::Span,
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
    #[serde(skip_serializing_if = "Option::is_none")]
    pub initializer_external_effects: Option<InitializerExternalEffectsUse>,
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

/// Consumed external-state author contract; this is not an execution receipt.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct InitializerExternalEffectsUse {
    pub binding: String,
    pub effects: spec::InitializerExternalEffects,
}

/// One exact call site that consumed a native-operation author premise.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct NativeOperationInputUse {
    pub evidence: crate::analysis_inputs::NativeOperationInputEvidence,
    pub source_location: Option<SourceLocation>,
    #[serde(skip)]
    pub span: swc_common::Span,
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
    pub span: swc_common::Span,
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
    pub span: swc_common::Span,
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
    /// `Object.assign` onto a target the analysis does not track.
    ObjectAssignCopy,
    /// The receiver of a local method that reads `this`, which the summary
    /// does not bind.
    MethodReceiver,
    /// The unknown instance of a refused local class construction.
    RefusedConstructionInstance,
    /// The receiver of a member read or write that may run an accessor
    /// reading `this`.
    AccessorReceiver,
    /// What an escaped class's construction stored on an instance or the
    /// class.
    ClassReach,
    /// A store on a local class or its prototype.
    ClassSurfaceMemberWrite,
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
    pub span: swc_common::Span,
}

/// Serializable projection of [`EffectCell`]: binding cells carry the
/// name only (the hygiene context is dropped, as everywhere on the
/// wire; see docs/wire_format.md).
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize, strum::Display)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum EffectCellReport {
    #[strum(to_string = "binding `{binding}`")]
    Binding { binding: Atom },
    #[strum(to_string = "global property `{key}`")]
    GlobalProp { key: String },
    #[strum(to_string = "array contents #{origin}")]
    ArrayContents { origin: u32 },
    #[strum(to_string = "Set contents #{origin}")]
    SetContents { origin: u32 },
    #[strum(to_string = "object shape #{origin}")]
    ObjectShape { origin: u32 },
    #[strum(to_string = "own property `{property}` of allocation #{origin}")]
    OwnProperty { origin: u32, property: String },
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
            EffectCell::ObjectShape(origin) => Self::ObjectShape { origin: *origin },
            EffectCell::OwnProperty { origin, property } => Self::OwnProperty {
                origin: *origin,
                property: property.clone(),
            },
        }
    }
}
