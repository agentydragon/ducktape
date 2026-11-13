//! Typed findings of `debundle spec lint` (SPEC.md § Spec lint).
//!
//! Each finding names one author annotation the engine may already infer.
//! `inferable` is a claim that withholding the annotation leaves the gate
//! verdict and the emitted order unchanged; anything a probe cannot decide is
//! `inconclusive`. Findings are suggestions: removal stays a reviewed spec edit.

use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;

use spec::{AdmissionCheck, ReentryFreeOperation, ResolvedMemberSelector};

use crate::analysis_inputs::{HostBehaviorKey, ProgramPremiseKey};
use crate::graph::{CommutingStatementsUnused, UnusedCommutingEndpoint, UnusedPremiseReason};
use crate::purity::{RedundantPurityHint, RedundantPurityProofBasis, RedundantPurityReason};

/// A lint rule. Every finding is a warning; no rule fails a spec.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LintRule {
    /// An author annotation the engine may already infer.
    InferableAnnotation,
}

/// One chunk's `spec_lint.json`: its findings, and the program-wide
/// annotations its analysis consumed, which `spec lint` joins across chunks.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChunkSpecLint {
    pub findings: Vec<LintFinding>,
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pub consumed_reentry_free_operations: BTreeSet<ReentryFreeOperation>,
    /// Loader keys the program's export-reentry loader closures consumed.
    #[serde(default)]
    pub loader_key_uses: LoaderKeyUses,
}

/// What the program's loader closures consumed (docs/premise_provenance.md).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "uses", rename_all = "snake_case")]
pub enum LoaderKeyUses {
    /// Every closure that decided a policy recorded the keys it consumed.
    Recorded {
        host_keys: BTreeSet<HostBehaviorKey>,
        premise_keys: BTreeSet<ProgramPremiseKey>,
    },
    /// A policy rests on a closure whose only failure is reach-back
    /// (`reentry_through`): its keys suppressed other failures without being
    /// recorded, so no key can be judged unused.
    Unattributed,
}

impl Default for LoaderKeyUses {
    fn default() -> Self {
        Self::Recorded {
            host_keys: BTreeSet::new(),
            premise_keys: BTreeSet::new(),
        }
    }
}

impl LoaderKeyUses {
    pub fn join(self, other: Self) -> Self {
        match (self, other) {
            (
                Self::Recorded {
                    mut host_keys,
                    mut premise_keys,
                },
                Self::Recorded {
                    host_keys: other_host,
                    premise_keys: other_premise,
                },
            ) => {
                host_keys.extend(other_host);
                premise_keys.extend(other_premise);
                Self::Recorded {
                    host_keys,
                    premise_keys,
                }
            }
            _ => Self::Unattributed,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LintFinding {
    pub rule: LintRule,
    /// The chunk a chunk-scoped annotation belongs to; `None` for a
    /// program-wide one (`host_behavior`).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub chunk: Option<String>,
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
    /// `chunk_analysis_options.<chunk>.trusted_dataflow_summaries`.
    TrustedDataflowSummaries { spec_location: String },
    /// One `host_behavior.reentry_free_operations` entry.
    ReentryFreeOperation { operation: ReentryFreeOperation },
    /// A `host_behavior` loader key: `loader_result_for_evaluating_module`,
    /// `synchronous_load_runs_app_callbacks`, `synchronous_load_drains_microtasks`
    /// or one `reentry_free_embedded_loaders` entry.
    LoaderHostBehavior { key: HostBehaviorKey },
    /// A `program_premises` key the loader closure reads
    /// (`non_literal_loader_calls`).
    LoaderProgramPremise { key: ProgramPremiseKey },
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
    /// A `chunk_renames.<chunk>.annotations` entry, by its readable name.
    ChunkRename { name: String },
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

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, strum::Display)]
#[serde(rename_all = "snake_case")]
pub enum InferableReason {
    /// No `sequenced` edge joins the entry's endpoints.
    #[strum(to_string = "no sequenced edge joins the endpoints")]
    NoSequencedEdge,
    /// Every `sequenced` edge between the endpoints orders two summarized
    /// effects on disjoint cells.
    #[strum(to_string = "the endpoints' summarized effects touch disjoint cells")]
    SummarizedEndpoints,
    /// The chunk has no violation of the overridden check.
    #[strum(to_string = "the chunk has no violation to override")]
    NoAdmissionViolation,
    /// `spec lint --probe-analysis` reran the program with this one
    /// annotation withheld: it was accepted with identical reports (dependency
    /// graphs, atomic groups, precision losses, module contents).
    #[strum(to_string = "withholding it alone leaves every report unchanged")]
    WithheldRunUnchanged,
    /// No analysis consumed the annotation: every read of it records the use,
    /// and none was recorded (docs/premise_provenance.md).
    #[strum(to_string = "no analysis consumed it")]
    NeverConsumed,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "probe", rename_all = "snake_case")]
pub enum InconclusiveReason {
    /// The binding classifies without the hint, but other analysis consumers
    /// of the hint were not rerun.
    PurityClassification {
        classification: RedundantPurityReason,
        proof_basis: RedundantPurityProofBasis,
    },
    /// The baseline run of `--probe-analysis` was not accepted, so no
    /// withheld run can be compared with it.
    BaselineNotAccepted { error: String },
    /// The withheld run could not run (an invalid edited spec, an I/O error).
    ProbeFailed { error: String },
    /// `--max-probes` was reached before this annotation was probed.
    NotProbed,
    /// Some analysis recorded a use. The fact it supported may have another
    /// derivation; `--probe-analysis` decides.
    Consumed,
    /// A program-wide annotation, but some chunk was not linted, so its uses
    /// are unknown.
    UnlintedChunks,
}

impl LintFinding {
    /// A program-wide annotation (no `chunk`).
    pub fn program_wide(annotation: LintedAnnotation, verdict: InferenceVerdict) -> Self {
        Self {
            rule: LintRule::InferableAnnotation,
            chunk: None,
            annotation,
            verdict,
        }
    }

    /// A `host_behavior.reentry_free_operations` entry, from its recorded uses.
    pub fn reentry_free_operation(
        operation: ReentryFreeOperation,
        verdict: InferenceVerdict,
    ) -> Self {
        Self {
            rule: LintRule::InferableAnnotation,
            chunk: None,
            annotation: LintedAnnotation::ReentryFreeOperation { operation },
            verdict,
        }
    }

    /// `trusted_dataflow_summaries` on `chunk`, from whether any statement's
    /// havoc was discharged by it (`StatementFacts::trusted_summary_discharged`).
    pub fn trusted_dataflow_summaries(chunk: &str, consumed: bool) -> Self {
        Self {
            rule: LintRule::InferableAnnotation,
            chunk: Some(chunk.to_owned()),
            annotation: LintedAnnotation::TrustedDataflowSummaries {
                spec_location: format!("chunk_analysis_options.{chunk}.trusted_dataflow_summaries"),
            },
            verdict: if consumed {
                InferenceVerdict::Inconclusive {
                    reason: InconclusiveReason::Consumed,
                }
            } else {
                InferenceVerdict::Inferable {
                    reason: InferableReason::NeverConsumed,
                }
            },
        }
    }

    pub fn unused_commuting_statements(chunk: &str, unused: &CommutingStatementsUnused) -> Self {
        Self {
            rule: LintRule::InferableAnnotation,
            chunk: Some(chunk.to_owned()),
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
            chunk: Some(chunk.to_owned()),
            annotation: LintedAnnotation::AdmissionOverride {
                spec_location: format!("chunk_analysis_options.{chunk}.admission_overrides"),
                check,
            },
            verdict: InferenceVerdict::Inferable {
                reason: InferableReason::NoAdmissionViolation,
            },
        }
    }

    /// A `purity: pure` hint whose name no statement of its chunk reads, so
    /// no classifier arm or import read consulted it.
    pub fn unconsulted_purity_hint(chunk: &str, owner: PurityHintOwner) -> Self {
        Self {
            rule: LintRule::InferableAnnotation,
            chunk: Some(chunk.to_owned()),
            annotation: LintedAnnotation::PurityHint { owner },
            verdict: InferenceVerdict::Inferable {
                reason: InferableReason::NeverConsumed,
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
            chunk: Some(chunk.to_owned()),
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
