//! Shared proof judgments and iterative inference over finite fact domains.

use crate::analysis_inputs::ProofDependencies;
use std::collections::BTreeSet;

mod iterative;
pub(crate) use iterative::{
    CertificateContext, FactContext, GuardedContext, IterativeFacts, IterativeInference,
    IterativeRules,
};

/// A proven fact and the direct premises that certify it.
///
/// Premises are stored as keys rather than nested judgments. This keeps a
/// shared proof graph compact; explanations can follow the keys through the
/// completed fact store.
#[derive(Debug, Clone, Eq, PartialEq)]
pub(crate) struct Judgment<Key, Fact> {
    value: Fact,
    dependencies: ProofDependencies,
    premises: BTreeSet<Key>,
}

impl<Key, Fact> Judgment<Key, Fact> {
    /// Returns the fact certified by this judgment.
    pub fn value(&self) -> &Fact {
        &self.value
    }

    /// Returns the union of captured-value dependencies used by the proof.
    pub fn dependencies(&self) -> &ProofDependencies {
        &self.dependencies
    }

    /// Returns the direct inference premises used by the proof.
    pub fn premises(&self) -> &BTreeSet<Key> {
        &self.premises
    }
}
