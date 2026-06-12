//! Spec-derived guidance for the analysis layer.
//!
//! `AnalysisHints` bundles the spec-shaped data the analyzer needs from
//! upstream (declared purity, declared-pure constructors, declared-pure
//! members, known local-effect helpers, local-effect policy). It lives
//! outside `facts/` so the module path matches the data shape: hints are
//! spec inputs to the facts pass, not facts themselves.

use std::collections::{BTreeMap, BTreeSet};

use crate::purity::Purity;

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub enum KnownEffect {
    TypescriptDecorateHelper,
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
    /// that field's doc (`spec::OwnerGraphOptions`) for the soundness
    /// precondition the spec author accepts.
    LocalPropertyWrites,
}

#[derive(Debug, Clone, Default)]
pub struct AnalysisHints {
    pub declared_pure: BTreeSet<String>,
    pub declared_pure_new: BTreeSet<String>,
    /// Author-declared pure member properties — keyed by binding name,
    /// value is the set of property names whose `<binding>.<prop>(args)`
    /// calls the spec author asserts are pure. The classifier consults
    /// this to admit `<recv>.<prop>(args)` as pure when `recv` is the
    /// keyed binding and `<prop>` is in the value set.
    /// See AGENTS.md "Declared purity".
    pub declared_pure_members: BTreeMap<String, BTreeSet<String>>,
    /// Member names on a binding whose calls may receive callback-like
    /// arguments but do not synchronously invoke them. The call itself is
    /// still impure/ordered unless another hint says otherwise; this only
    /// narrows at-init call promotion by not treating inline functions,
    /// object literals containing functions, or first-order argument
    /// callbacks as synchronously reachable fallback roots.
    pub no_sync_callback_members: BTreeMap<String, BTreeSet<String>>,
    pub known_effects: BTreeMap<String, KnownEffect>,
    pub local_effect_policy: LocalEffectPolicy,
    /// Author-trusted chunk-level opt-in that lets conservative-but-present
    /// syntactic dataflow summaries drive S-chain emission instead of
    /// falling back to opaque barriers. Set from
    /// `OwnerGraphOptions::trusted_dataflow_summaries`.
    pub trusted_dataflow_summaries: bool,
    /// Cross-module purity verdicts for this chunk's imported function
    /// bindings, keyed by local binding name. Produced by the program-level
    /// oracle (`crate::cross_module_purity`); empty in strictly per-chunk
    /// paths, where imported callees stay `unknown_call`.
    pub imported_purities: BTreeMap<String, Purity>,
    /// Local binding names of this chunk that import an author-asserted
    /// fluent export (`chunk_export_purity.<chunk>.fluent_exports`,
    /// projected by `crate::cross_module_purity` onto importer locals).
    /// The classifier treats them as deep-purity roots: member reads /
    /// calls on them AND on values derived from them are pure. See
    /// `ChunkCodeGraph::fluent_bindings`.
    pub fluent_bindings: BTreeSet<String>,
}

impl AnalysisHints {
    pub fn from_declared_pure(declared_pure: &BTreeSet<String>) -> Self {
        Self {
            declared_pure: declared_pure.clone(),
            declared_pure_new: BTreeSet::new(),
            declared_pure_members: BTreeMap::new(),
            no_sync_callback_members: BTreeMap::new(),
            known_effects: BTreeMap::new(),
            local_effect_policy: LocalEffectPolicy::KnownEffectsOnly,
            trusted_dataflow_summaries: false,
            imported_purities: BTreeMap::new(),
            fluent_bindings: BTreeSet::new(),
        }
    }
}
