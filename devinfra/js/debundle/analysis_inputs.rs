//! Explicit author contracts, imported summaries, and analysis policy.
//!
//! These are inputs to fact extraction, not inferred facts. Imported summaries
//! can themselves depend on author contracts in their defining chunks.

use std::collections::{BTreeMap, BTreeSet};

use crate::purity::Purity;
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

#[derive(Debug, Clone, Default)]
pub struct AnalysisAssumptions {
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
    /// Imported callback factories with the return-only callback-flow contract.
    /// Independent of call purity; used only for checked fresh-callback calls.
    pub callback_factories: BTreeSet<String>,
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
    pub assumptions: AnalysisAssumptions,
    pub local_effect_policy: LocalEffectPolicy,
    /// Cross-module purity verdicts for this chunk's imported function
    /// bindings, keyed by local binding name. Produced by the program-level
    /// oracle (`crate::cross_module_purity`); empty in strictly per-chunk
    /// paths, where imported callees stay `unknown_call`.
    pub imported_purities: BTreeMap<String, std::sync::Arc<Purity>>,
}
