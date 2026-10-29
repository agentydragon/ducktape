//! Explicit author contracts, imported summaries, and analysis policy.
//!
//! These are inputs to fact extraction, not inferred facts. Imported summaries
//! can themselves depend on author contracts in their defining chunks.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use serde::{Deserialize, Serialize};
use source_identity::SourceUnitOrdinal;
use swc_common::Span;
use swc_ecma_ast::Id;

use crate::purity::Purity;
use crate::reports::schema::SourceLocation;
use spec::{BuiltinGlobalRoot, CapturedValueKind, SourceMatchClaim};

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub enum KnownEffect {
    TypescriptDecorateHelper,
    TypescriptMetadataDecorateHelper,
    TypescriptMetadataDecorateHelperWithMemberDecorators,
    TypescriptMetadataDecorateHelperThreeArgs,
}

#[derive(Debug, Clone, Copy, Default, Eq, PartialEq)]
pub enum LocalEffectPolicy {
    #[default]
    KnownEffectsOnly,
    VendorPrune,
    /// Recognize whole-statement local property writes —
    /// `X.prop = <pure>;` where `X` is a chunk-top declared binding —
    /// as a local effect on `X` instead of a globally-ordered side
    /// effect. Opt-in via
    /// `chunk_analysis_options.<chunk>.local_property_effects`; see
    /// that field's doc (`spec::DependencyGraphOptions`) for the soundness
    /// precondition the spec author accepts.
    LocalPropertyWrites,
}

/// Evidence for a bare imported callable's return-only callback-factory behavior.
/// An assertion is an author contract; an inferred wrapper depends on the
/// defining chunk's checked source shape.
#[derive(Debug, Clone, Ord, PartialOrd, Eq, PartialEq, Serialize, Deserialize)]
#[serde(tag = "origin", rename_all = "snake_case")]
pub enum CallbackFactoryEvidence {
    AuthorAssertion {
        defining_module: String,
        export: String,
    },
    InferredImmutableWrapper {
        defining_module: String,
        export: String,
        local: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        source_location: Option<SourceLocation>,
    },
}

/// A source-derived successful-return capability. Deferred bodies may have
/// arbitrary effects and exterior captures; consumers must activate the foreign
/// capability pool when this record is read, invoked, or exposed.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct ReturnedRecordSummary {
    pub defining_module: String,
    pub export: String,
    pub local: String,
    pub source_location: Option<SourceLocation>,
    pub members: Vec<DeferredRecordMember>,
    /// Composed source/native proof, including its captured-input dependencies.
    pub inference: crate::reports::schema::InferenceUse,
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct DeferredRecordMember {
    pub name: String,
    pub source_location: Option<SourceLocation>,
    /// Includes the fresh state binding as well as exterior binding names.
    /// These are provider-local provenance, never importer binding IDs.
    pub captures: BTreeSet<String>,
}

/// Source-checked wrapping of one ordinary input. The returned capability
/// retains the input graph; no purity, fresh identity or no-throw claim follows.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct NamespaceWrapperSummary {
    pub defining_module: String,
    pub export: String,
    pub local: String,
    pub source_location: Option<SourceLocation>,
    pub inference: crate::reports::schema::InferenceUse,
    /// Trusted provider judgment; deliberately absent from report JSON.
    #[serde(skip)]
    pub(crate) dependencies: ProofDependencies,
}

/// Source-only wrapper proof: direct callback uses receive these ordinary
/// allocated arguments without explicitly returning or publishing the callback.
/// Consumer entry coverage additionally requires an anonymous inline callback
/// unable to obtain its own identity. Callback-supplied values may still escape.
/// Timing-only author assertions never supply this capability.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct CallbackObjectArguments {
    pub wrapper: CallbackFactoryEvidence,
    pub object_arguments: BTreeSet<usize>,
}

/// Audited provider descriptor-copy behavior. The importer must separately
/// prove an exact fresh literal at a single-execution call site.
#[derive(Debug, Clone, Eq, PartialEq, Serialize)]
pub struct DescriptorCopyFactorySummary {
    pub defining_module: String,
    pub export: String,
    pub author_location: String,
    pub signatures: Vec<spec::DescriptorCopyFactorySignature>,
}

/// Capabilities of one bare imported callable, keyed by the importer's local
/// binding name. More than one proof can support the same capability.
#[derive(Debug, Clone, Default, Eq, PartialEq, Serialize)]
pub struct CallableSummary {
    callback_factory_evidence: BTreeSet<CallbackFactoryEvidence>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub returned_record: Option<ReturnedRecordSummary>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub namespace_wrapper: Option<NamespaceWrapperSummary>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub callback_object_arguments: Option<CallbackObjectArguments>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub descriptor_copy_factory: Option<DescriptorCopyFactorySummary>,
}

impl CallableSummary {
    pub fn callback_factory(evidence: CallbackFactoryEvidence) -> Self {
        Self {
            callback_factory_evidence: BTreeSet::from([evidence]),
            returned_record: None,
            namespace_wrapper: None,
            callback_object_arguments: None,
            descriptor_copy_factory: None,
        }
    }

    pub fn returned_record(summary: ReturnedRecordSummary) -> Self {
        Self {
            returned_record: Some(summary),
            ..Self::default()
        }
    }

    pub fn namespace_wrapper(summary: NamespaceWrapperSummary) -> Self {
        Self {
            namespace_wrapper: Some(summary),
            ..Self::default()
        }
    }

    pub fn descriptor_copy_factory(summary: DescriptorCopyFactorySummary) -> Self {
        Self {
            descriptor_copy_factory: Some(summary),
            ..Self::default()
        }
    }

    pub fn supports_callback_factory(&self) -> bool {
        !self.callback_factory_evidence.is_empty()
    }

    pub fn callback_factory_evidence(&self) -> &BTreeSet<CallbackFactoryEvidence> {
        &self.callback_factory_evidence
    }

    pub fn merge(&mut self, other: Self) {
        if let Some(factory) = other.descriptor_copy_factory {
            assert!(
                self.descriptor_copy_factory
                    .as_ref()
                    .is_none_or(|old| old == &factory)
            );
            self.descriptor_copy_factory = Some(factory);
        }
        if let Some(wrapper) = other.namespace_wrapper {
            assert!(
                self.namespace_wrapper
                    .as_ref()
                    .is_none_or(|previous| previous == &wrapper)
            );
            self.namespace_wrapper = Some(wrapper);
        }
        if let Some(arguments) = other.callback_object_arguments {
            assert!(
                self.callback_object_arguments
                    .as_ref()
                    .is_none_or(|old| old == &arguments)
            );
            self.callback_object_arguments = Some(arguments);
        }
        if let Some(record) = other.returned_record {
            assert!(
                self.returned_record
                    .as_ref()
                    .is_none_or(|previous| previous == &record)
            );
            self.returned_record = Some(record);
        }
        self.callback_factory_evidence
            .extend(other.callback_factory_evidence);
    }
}

/// Intrinsic families whose visible writes constrain inferred native operations.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NativeIntrinsic {
    GlobalObject,
    /// A global slot may hold any native value; descendants retain this root.
    UnknownGlobalValue,
    Error,
    FinalizationRegistry,
    Function,
    Object,
    Array,
    Math,
    RegExp,
    Symbol,
    Promise,
    Map,
    Set,
    WeakMap,
    WeakSet,
}

impl NativeIntrinsic {
    pub(crate) fn for_global_binding(property: &str) -> Option<Self> {
        match property {
            "Error" => Some(Self::Error),
            "Function" => Some(Self::Function),
            "Object" => Some(Self::Object),
            "Array" => Some(Self::Array),
            "Math" => Some(Self::Math),
            "RegExp" => Some(Self::RegExp),
            "Symbol" => Some(Self::Symbol),
            "Promise" => Some(Self::Promise),
            "Map" => Some(Self::Map),
            "Set" => Some(Self::Set),
            "WeakMap" => Some(Self::WeakMap),
            "WeakSet" => Some(Self::WeakSet),
            "FinalizationRegistry" => Some(Self::FinalizationRegistry),
            "globalThis" | "window" | "self" | "frames" | "top" => Some(Self::GlobalObject),
            _ => None,
        }
    }
}

/// Stable reference to one exact author input in one provider chunk.
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
pub struct ValueInputRef {
    pub defining_chunk: String,
    pub input_index: usize,
}

/// Exact authored provider contract consumed by a semantic certificate.
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
pub struct DescriptorCopyAuthorRef {
    pub defining_module: String,
    pub export: String,
    pub spec_location: String,
}

/// Additional captured-value assertions required by an inferred proof.
/// Structural source evidence stays on the proof's existing source identity.
/// Empty dependencies add no captured-value premise; the analysis's baseline
/// native and host assumptions still apply independently.
#[derive(Debug, Clone, Default, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
pub struct ProofDependencies {
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pub value_inputs: BTreeSet<ValueInputRef>,
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pub native_operation_inputs: BTreeSet<NativeOperationInputRef>,
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pub descriptor_copy_authors: BTreeSet<DescriptorCopyAuthorRef>,
}

impl ProofDependencies {
    pub fn is_empty(&self) -> bool {
        self.value_inputs.is_empty()
            && self.native_operation_inputs.is_empty()
            && self.descriptor_copy_authors.is_empty()
    }

    pub fn extend(&mut self, other: &Self) {
        self.value_inputs.extend(other.value_inputs.iter().cloned());
        self.native_operation_inputs
            .extend(other.native_operation_inputs.iter().cloned());
        self.descriptor_copy_authors
            .extend(other.descriptor_copy_authors.iter().cloned());
    }
}

/// Evidence retained for an author-conditioned value refinement. The physical
/// input path and digest identify the bytes consumed by the parser; they do not
/// establish the external runtime premise recorded by the author.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct ValueInputEvidence {
    pub reference: ValueInputRef,
    pub spec_location: String,
    pub source_match: SourceMatchClaim,
    pub value_kind: CapturedValueKind,
    pub source_path: String,
    pub parser_input_file: String,
    pub audited_source_sha256: String,
    pub selected_binding: String,
    pub declaration_location: Option<SourceLocation>,
    pub initializer_location: Option<SourceLocation>,
    pub expected_export: Option<String>,
    pub note: Option<String>,
}

/// Stable reference to one exact native author assertion. The index is in
/// native-operation inputs followed by native-call-identity inputs for that
/// defining chunk, so the two fields cannot collide.
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
pub struct NativeOperationInputRef {
    pub defining_chunk: String,
    pub input_index: usize,
}

/// Shared evidence discriminator for the two distinct authored native-input
/// fields. Their spec enums cannot express each other's cases.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum NativeInputKind {
    Operation(spec::NativeOperationKind),
    CallIdentity(spec::NativeCallIdentityKind),
}

/// Provenance retained for an author-certified native operation or call site.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct NativeOperationInputEvidence {
    pub reference: NativeOperationInputRef,
    pub spec_location: String,
    pub source_match: SourceMatchClaim,
    pub operation: NativeInputKind,
    pub source_path: String,
    pub parser_input_file: String,
    pub audited_source_sha256: String,
    pub selected_binding: String,
    pub declaration_location: Option<SourceLocation>,
    pub initializer_location: Option<SourceLocation>,
    pub note: Option<String>,
}

/// Resolved author input for one canonical source binding. Spans and `Id` are
/// session-local; serialized evidence uses the stable source locations above.
#[derive(Debug, Clone)]
pub struct ResolvedBindingValueInput {
    pub evidence: ValueInputEvidence,
    pub binding: Id,
    pub declaration_span: Span,
    pub initializer_span: Span,
    pub initializer_ordinal: SourceUnitOrdinal,
}

/// A value input after the canonical selector engine has selected one exact
/// top-level source unit. The prepared execution index resolves this name to
/// its canonical SWC `Id` and validates all declaration/write facts.
#[derive(Debug, Clone)]
pub struct SelectedBindingValueInput {
    pub evidence: ValueInputEvidence,
    pub selected_binding: String,
    /// Original parsed module-body item selected by `selector_resolve`. The
    /// canonical execution index maps this span to its split source unit and
    /// validates the declaration and complete write summary.
    pub selected_statement_span: Span,
}

/// A native-operation input after the canonical selector engine has selected
/// one exact top-level source unit. The execution index validates the
/// initializer call and resolves it to its precise call span.
#[derive(Debug, Clone)]
pub struct SelectedNativeOperationInput {
    pub evidence: NativeOperationInputEvidence,
    pub selected_binding: String,
    pub selected_statement_span: Span,
}

/// Resolved author input for one exact native operation call. Spans and `Id`
/// are session-local; serialized evidence uses stable source provenance.
#[derive(Debug, Clone)]
pub struct ResolvedNativeOperationInput {
    pub evidence: NativeOperationInputEvidence,
    pub binding: Id,
    /// Original top-level statement selected by source_match, before the
    /// analysis view splits a multi-declarator declaration into source units.
    pub selected_statement_span: Span,
    pub declaration_span: Span,
    pub initializer_span: Span,
    pub call_span: Span,
    pub initializer_ordinal: SourceUnitOrdinal,
    /// Exact operand of an admitted Set spread, if this is a spread operation.
    pub spread_source: Option<Id>,
    pub spread_source_span: Option<Span>,
    pub spread_token_span: Option<Span>,
}

/// Source- and explicit-output-certified Set spread. This is independent,
/// one-way evidence for the shared value-flow solve, not a result inferred from
/// that solve or from the eventual dependency graph.
#[derive(Debug, Clone)]
pub struct StableSetIterableUse {
    pub source_span: Span,
    pub spread_token_span: Span,
    /// Explicit plan owner validated again against final emission placement.
    pub destination: usize,
    pub interval_bindings: Vec<Id>,
    pub dependencies: ProofDependencies,
}

/// Resolved whole-program native integrity, derived from source facts and any
/// explicitly recorded author input dependencies. This is never a serialized
/// spec assertion.
#[derive(Debug, Clone, Default)]
pub struct NativeIntegrity {
    pub mutated: BTreeSet<NativeIntrinsic>,
    pub prototype_mutations: Arc<Vec<crate::reports::schema::NativePrototypeMutation>>,
    pub witnesses: Arc<Vec<crate::reports::schema::NativeMutationWitness>>,
    pub certificates: Arc<Vec<crate::reports::schema::NativeReceiverCertificate>>,
    /// Validated inputs by stable reference. Consumers report a dependency only
    /// when a receiver refinement was actually used.
    pub value_input_evidence: BTreeMap<ValueInputRef, ValueInputEvidence>,
    pub consumed_value_inputs: BTreeSet<ValueInputRef>,
}

#[derive(Debug, Clone, Default)]
pub struct AnalysisAssumptions {
    /// Author prototype premises for source-checked local unary record factories.
    pub retained_record_factories: BTreeSet<String>,
    /// Source-resolved author inputs for captured provider bindings. These are
    /// keyed by canonical IDs from the prepared entry AST and are distinct from
    /// inferred source facts and all purity/callback contracts.
    pub captured_value_inputs: Vec<ResolvedBindingValueInput>,
    /// Source-resolved author premises for exact native operation call sites.
    /// They are not native integrity guards or inferred identity facts.
    pub native_operation_inputs: Vec<ResolvedNativeOperationInput>,
    /// Version-scoped author assertion for exact host operations.
    pub host_no_reentry: Option<spec::HostNoReentry>,
    /// Exact author-certified browser-global slots whose stored capabilities
    /// cannot be consumed before this chunk finishes synchronous init.
    pub deferred_global_publications: BTreeMap<BuiltinGlobalRoot, BTreeSet<String>>,
    /// Exact root/key host-membership probes with an independent nonreentry audit.
    pub non_reentrant_global_membership: BTreeMap<BuiltinGlobalRoot, BTreeSet<String>>,
    pub declared_pure: BTreeSet<String>,
    pub declared_pure_new: BTreeSet<String>,
    /// Author-trusted purity of one selected top-level variable initializer.
    /// This never participates in call or constructor classification.
    pub declared_pure_initializers: BTreeSet<String>,
    /// Author-declared pure member properties — keyed by binding name,
    /// value is the set of property names whose `<binding>.<prop>(args)`
    /// calls the spec author asserts are pure. The classifier consults
    /// this to admit `<recv>.<prop>(args)` as pure when `recv` is the
    /// keyed binding and `<prop>` is in the value set.
    /// See <docs/purity_soundness.md> "Declared purity".
    pub declared_pure_members: BTreeMap<String, BTreeSet<String>>,
    /// Member names on a binding whose calls may receive callback-like
    /// arguments but do not synchronously invoke them. The call itself is
    /// still impure/ordered unless another hint says otherwise; this only
    /// narrows at-init call promotion by not treating inline functions,
    /// object literals containing functions, or first-order argument
    /// callbacks as synchronously reachable fallback roots.
    pub no_sync_callback_members: BTreeMap<String, BTreeSet<String>>,
    pub known_effects: BTreeMap<String, KnownEffect>,
    /// Author-trusted chunk-level opt-in that lets conservative-but-present
    /// syntactic dataflow summaries drive S-chain emission instead of
    /// falling back to opaque barriers. Set from
    /// `DependencyGraphOptions::trusted_dataflow_summaries`.
    pub trusted_dataflow_summaries: bool,
    /// Imported factories audited only for a single syntactic inline callback.
    /// Retained results passed as arguments remain conservative.
    pub inline_callback_factories: BTreeSet<String>,
    /// Static members of imported namespace bindings with an audited
    /// return-only callback contract, independent of member-call purity.
    pub callback_factory_members: BTreeMap<String, BTreeSet<String>>,
    pub retained_callback_factories: BTreeMap<String, Vec<spec::RetainedCallbackSignature>>,
    pub retained_callback_factory_members:
        BTreeMap<String, BTreeMap<String, Vec<spec::RetainedCallbackSignature>>>,
    pub retained_callback_constructors: BTreeMap<String, Vec<spec::RetainedCallbackSignature>>,
    /// Direct imported function calls with audited no-reentry timing for
    /// checked primitive arguments. Does not imply purity or known return.
    pub no_reentry_exports: BTreeSet<String>,
    /// Audited callback-bearing calls with no synchronous reentry; supplied
    /// callbacks become global roots after the call, independent of purity.
    pub no_reentry_callback_exports: BTreeMap<String, Vec<spec::RetainedCallbackSignature>>,
    /// Exact source-selected top-level calls with the same callback reachability
    /// contract, keyed by the parsed call span `(lo, hi)`. This is a local
    /// timing premise, not a purity or known-result assertion.
    pub no_reentry_statement_calls: BTreeMap<(u32, u32), spec::RetainedCallbackSignature>,
    /// Imported namespace members whose audited direct calls cannot reenter
    /// caller/importer/host code for primitive arguments. No purity claim.
    pub no_reentry_members: BTreeMap<String, BTreeSet<String>>,
    /// Separate author assertion for direct calls with a fresh inert object
    /// record argument. Primitive-only assertions do not imply this contract.
    pub no_reentry_record_members: BTreeMap<String, BTreeSet<String>>,
    /// Stable lookups running no caller/importer/host code, whose capabilities cannot expose their namespace
    /// or replace audited factories except through supplied caller inputs.
    pub namespace_independent_members: BTreeMap<String, BTreeSet<String>>,
    /// Local binding names of this chunk that import an author-asserted
    /// fluent export (`chunk_export_purity.<chunk>.fluent_exports`,
    /// projected by `crate::cross_module_purity` onto importer locals).
    /// The classifier treats them as deep-purity roots: member reads /
    /// calls on them AND on values derived from them are pure. See
    /// `ChunkCodeGraph::fluent_bindings`.
    pub fluent_bindings: BTreeSet<String>,
}

/// Inputs to semantic analysis. Author assertions stay separate from imported
/// call summaries and the caller-selected local-effect policy.
#[derive(Debug, Clone, Default)]
pub struct AnalysisInputs {
    pub native_integrity: Option<NativeIntegrity>,
    /// Source- and explicit-placement-certified spread uses, keyed by the
    /// exact constructor call start. Their dependencies retain the author
    /// native-operation inputs used to derive each certificate.
    pub stable_set_spreads: BTreeMap<u32, StableSetIterableUse>,
    /// Identity of this chunk within a supplied whole-program native census.
    /// Used only to separate local activation from already-present mutations.
    pub current_chunk_id: Option<String>,
    pub assumptions: AnalysisAssumptions,
    /// Source-match results awaiting canonical binding/write-index validation
    /// in the whole-program prepared analysis.
    pub selected_value_inputs: Vec<SelectedBindingValueInput>,
    /// Source-match results awaiting canonical call-shape validation in the
    /// whole-program prepared analysis.
    pub selected_native_operation_inputs: Vec<SelectedNativeOperationInput>,
    pub local_effect_policy: LocalEffectPolicy,
    /// Cross-module purity verdicts for this chunk's imported function
    /// bindings, keyed by local binding name. Produced by the program-level
    /// oracle (`crate::cross_module_purity`); empty in strictly per-chunk
    /// paths, where imported callees stay `unknown_call`.
    pub imported_purities: BTreeMap<String, std::sync::Arc<Purity>>,
    /// Bare imported callable capabilities, retaining the evidence that
    /// established each one. These may depend on defining-chunk author
    /// contracts; they are never local author assumptions.
    pub imported_callables: BTreeMap<String, CallableSummary>,
    /// Inferred direct ESM import bindings whose possible values are primitive.
    /// Used only to suppress ToPrimitive callback promotion, never eager reads.
    pub imported_primitives: BTreeSet<String>,
}

impl AnalysisInputs {
    pub fn merge_imported_callables(&mut self, incoming: BTreeMap<String, CallableSummary>) {
        for (local, summary) in incoming {
            self.imported_callables
                .entry(local)
                .or_default()
                .merge(summary);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn imported_callable_merge_retains_independent_evidence() {
        let asserted = CallbackFactoryEvidence::AuthorAssertion {
            defining_module: "lib".into(),
            export: "wrap".into(),
        };
        let inferred = CallbackFactoryEvidence::InferredImmutableWrapper {
            defining_module: "lib".into(),
            export: "wrap".into(),
            local: "wrap".into(),
            source_location: None,
        };
        let mut inputs = AnalysisInputs::default();
        inputs.merge_imported_callables(BTreeMap::from([(
            "localWrap".into(),
            CallableSummary::callback_factory(asserted.clone()),
        )]));
        inputs.merge_imported_callables(BTreeMap::from([(
            "localWrap".into(),
            CallableSummary::callback_factory(inferred.clone()),
        )]));
        assert_eq!(
            inputs.imported_callables["localWrap"].callback_factory_evidence(),
            &BTreeSet::from([asserted, inferred]),
        );
    }
}
