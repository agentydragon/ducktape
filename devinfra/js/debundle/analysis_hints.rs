//! Spec-derived guidance for the analysis layer.
//!
//! `AnalysisHints` bundles the spec-shaped data the analyzer needs from
//! upstream (declared purity, declared-pure constructors, declared-pure
//! members, known local-effect helpers, local-effect policy). It lives
//! outside `facts/` so the module path matches the data shape: hints are
//! spec inputs to the facts pass, not facts themselves.

use std::collections::{BTreeMap, BTreeSet};

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub enum KnownEffect {
    TypescriptDecorateHelper,
}

#[derive(Debug, Clone, Copy, Default, Eq, PartialEq)]
pub enum LocalEffectPolicy {
    #[default]
    KnownEffectsOnly,
    VendorPrune,
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
    pub known_effects: BTreeMap<String, KnownEffect>,
    pub local_effect_policy: LocalEffectPolicy,
}

impl AnalysisHints {
    pub fn from_declared_pure(declared_pure: &BTreeSet<String>) -> Self {
        Self {
            declared_pure: declared_pure.clone(),
            declared_pure_new: BTreeSet::new(),
            declared_pure_members: BTreeMap::new(),
            known_effects: BTreeMap::new(),
            local_effect_policy: LocalEffectPolicy::KnownEffectsOnly,
        }
    }
}
