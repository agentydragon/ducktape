//! Typed findings of `debundle spec lint` (SPEC.md § Spec lint).
//!
//! Each finding names one author annotation the engine may already infer.
//! `inferable` is a claim that withholding the annotation leaves the gate
//! verdict and the emitted order unchanged; anything a probe cannot decide is
//! `inconclusive`. Findings are suggestions: removal stays a reviewed spec edit.

use serde::{Deserialize, Serialize};
use spec::{AdmissionCheck, ResolvedMemberSelector};

use crate::graph::{CommutingStatementsUnused, UnusedCommutingEndpoint, UnusedPremiseReason};
use crate::purity::{RedundantPurityHint, RedundantPurityProofBasis, RedundantPurityReason};

/// A lint rule. Every finding is a warning; no rule fails a spec.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LintRule {
    /// An author annotation the engine may already infer.
    InferableAnnotation,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LintFinding {
    pub rule: LintRule,
    pub chunk: String,
    pub annotation: LintedAnnotation,
    pub verdict: InferenceVerdict,
}

/// The annotation a finding is about, by authored identity. The `kind` tag is
/// the one vocabulary of annotation kinds.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum LintedAnnotation {
    /// A `purity: pure` binding annotation.
    PurityHint { owner: PurityHintOwner },
    /// One `chunk_analysis_options.<chunk>.admission_overrides` check.
    AdmissionOverride {
        spec_location: String,
        check: AdmissionCheck,
    },
    /// One `chunk_analysis_options.<chunk>.commuting_statements` entry.
    CommutingStatements {
        spec_location: String,
        a: Box<UnusedCommutingEndpoint>,
        b: Box<UnusedCommutingEndpoint>,
    },
}

/// Where a `purity: pure` hint was authored.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "owner", rename_all = "snake_case")]
pub enum PurityHintOwner {
    /// A logical-module member annotation.
    LogicalMember(ResolvedMemberSelector),
    /// An annotation no logical-module member resolves to (a
    /// `chunk_renames` annotation): only the input binding is known.
    InputBinding { binding: String },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "verdict", rename_all = "snake_case")]
pub enum InferenceVerdict {
    /// Withholding the annotation leaves the dependency graph, and so the gate
    /// verdict and emitted order, unchanged.
    Inferable { reason: InferableReason },
    /// A probe suggests the annotation is unnecessary but does not establish
    /// that the verdict and emitted order are unchanged.
    Inconclusive { reason: InconclusiveReason },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum InferableReason {
    /// No `sequenced` edge joins the entry's endpoints.
    NoSequencedEdge,
    /// Every `sequenced` edge between the endpoints orders two summarized
    /// effects on disjoint cells.
    SummarizedEndpoints,
    /// The chunk has no violation of the overridden check.
    NoAdmissionViolation,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "probe", rename_all = "snake_case")]
pub enum InconclusiveReason {
    /// The binding classifies without the hint, but other analysis consumers
    /// of the hint were not rerun.
    PurityClassification {
        classification: RedundantPurityReason,
        proof_basis: RedundantPurityProofBasis,
    },
}

impl LintFinding {
    pub fn unused_commuting_statements(chunk: &str, unused: &CommutingStatementsUnused) -> Self {
        Self {
            rule: LintRule::InferableAnnotation,
            chunk: chunk.to_owned(),
            annotation: LintedAnnotation::CommutingStatements {
                spec_location: unused.spec_location.clone(),
                a: Box::new(unused.a.clone()),
                b: Box::new(unused.b.clone()),
            },
            verdict: InferenceVerdict::Inferable {
                reason: match unused.reason {
                    UnusedPremiseReason::NoSequencedEdge => InferableReason::NoSequencedEdge,
                    UnusedPremiseReason::SummarizedEndpoints => {
                        InferableReason::SummarizedEndpoints
                    }
                },
            },
        }
    }

    pub fn redundant_admission_override(chunk: &str, check: AdmissionCheck) -> Self {
        Self {
            rule: LintRule::InferableAnnotation,
            chunk: chunk.to_owned(),
            annotation: LintedAnnotation::AdmissionOverride {
                spec_location: format!("chunk_analysis_options.{chunk}.admission_overrides"),
                check,
            },
            verdict: InferenceVerdict::Inferable {
                reason: InferableReason::NoAdmissionViolation,
            },
        }
    }

    pub fn redundant_purity_hint(
        chunk: &str,
        hint: &RedundantPurityHint,
        owner: PurityHintOwner,
    ) -> Self {
        Self {
            rule: LintRule::InferableAnnotation,
            chunk: chunk.to_owned(),
            annotation: LintedAnnotation::PurityHint { owner },
            verdict: InferenceVerdict::Inconclusive {
                reason: InconclusiveReason::PurityClassification {
                    classification: hint.reason,
                    proof_basis: hint.proof_basis,
                },
            },
        }
    }
}
