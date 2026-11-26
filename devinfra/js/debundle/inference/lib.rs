//! Shared proof judgments and iterative inference over finite fact domains.

use analysis_vocabulary::inputs::ProofDependencies;
use std::collections::BTreeSet;
use std::sync::LazyLock;

static EMPTY_PROOF_DEPENDENCIES: LazyLock<ProofDependencies> =
    LazyLock::new(ProofDependencies::default);

mod iterative;
pub use iterative::{
    CertificateContext, CompletedTransfer, FactChange, FactContext, GuardedContext, IterativeFacts,
    IterativeInference, IterativeRules, SealedFacts,
};

/// A proven fact and the direct premises that certify it.
///
/// Premises are stored as keys rather than nested judgments. This keeps a
/// shared proof graph compact; explanations can follow the keys through the
/// completed fact store.
#[derive(Debug, Clone, Eq, PartialEq)]
pub struct Judgment<Key, Fact> {
    value: Fact,
    // Source-derived facts have no author dependencies. Keep their empty
    // metadata shared rather than embedding every dependency set in each row.
    dependencies: Option<Box<ProofDependencies>>,
    // Value-only closures retain no explanatory graph. Empty premise sets
    // must not occupy a BTreeSet in every dense source-fact row either.
    #[allow(
        clippy::box_collection,
        reason = "keep absent premise metadata to one word per fact"
    )]
    premises: Option<Box<BTreeSet<Key>>>,
}

impl<Key, Fact> Judgment<Key, Fact> {
    /// Returns the fact certified by this judgment.
    pub fn value(&self) -> &Fact {
        &self.value
    }

    /// Returns the union of captured-value dependencies used by the proof.
    pub fn dependencies(&self) -> &ProofDependencies {
        self.dependencies
            .as_deref()
            .unwrap_or(&EMPTY_PROOF_DEPENDENCIES)
    }

    /// Returns the direct inference premises used by the proof.
    pub fn premises(&self) -> impl Iterator<Item = &Key> {
        self.premises.iter().flat_map(|premises| premises.iter())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn source_fact_metadata_stays_compact() {
        // Dense value-flow stores can contain millions of source-only rows.
        // Empty proof categories must not each occupy a BTreeSet in every row.
        assert!(std::mem::size_of::<Judgment<u32, u32>>() <= 3 * std::mem::size_of::<usize>());
    }
}
