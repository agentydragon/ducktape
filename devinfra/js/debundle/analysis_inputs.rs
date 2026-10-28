//! Explicit author contracts, imported summaries, and analysis policy.
//!
//! These are inputs to fact extraction, not inferred facts. Imported summaries
//! can themselves depend on author contracts in their defining chunks.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use serde::{Deserialize, Serialize};

use crate::purity::Purity;
use crate::reports::schema::SourceLocation;
use spec::BuiltinGlobalRoot;

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
}

#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct DeferredRecordMember {
    pub name: String,
    pub source_location: Option<SourceLocation>,
    /// Includes the fresh state binding as well as exterior binding names.
    /// These are provider-local provenance, never importer binding IDs.
    pub captures: BTreeSet<String>,
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

/// Capabilities of one bare imported callable, keyed by the importer's local
/// binding name. More than one proof can support the same capability.
#[derive(Debug, Clone, Default, Eq, PartialEq, Serialize)]
pub struct CallableSummary {
    callback_factory_evidence: BTreeSet<CallbackFactoryEvidence>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub returned_record: Option<ReturnedRecordSummary>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub callback_object_arguments: Option<CallbackObjectArguments>,
}

impl CallableSummary {
    pub fn callback_factory(evidence: CallbackFactoryEvidence) -> Self {
        Self {
            callback_factory_evidence: BTreeSet::from([evidence]),
            returned_record: None,
            callback_object_arguments: None,
        }
    }

    pub fn returned_record(summary: ReturnedRecordSummary) -> Self {
        Self {
            returned_record: Some(summary),
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

/// Source-derived whole-program native integrity. This is an inferred premise,
/// never an author assertion or a serialized spec option.
#[derive(Debug, Clone, Default)]
pub struct NativeIntegrity {
    pub mutated: BTreeSet<NativeIntrinsic>,
    pub prototype_mutations: Arc<Vec<crate::reports::schema::NativePrototypeMutation>>,
    pub witnesses: Arc<Vec<crate::reports::schema::NativeMutationWitness>>,
    pub certificates: Arc<Vec<crate::reports::schema::NativeReceiverCertificate>>,
}

#[derive(Debug, Clone, Default)]
pub struct AnalysisAssumptions {
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
    pub assumptions: AnalysisAssumptions,
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
