//! Report-only may-accesses composed by canonical runtime transfers.
//!
//! This ledger covers static global-property accesses only. A recorded access
//! is never a certificate of execution, purity, or complete domain coverage.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use source_identity::SourceUnitOrdinal;
use swc_common::Span;

use crate::analysis_inputs::ProofDependencies;
use crate::reports::schema::AssumedEffectReason;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum GlobalAccessKind {
    Read,
    Write,
}

/// A physical source operation, independent of graph IDs and runtime replay.
/// The containing chunk supplies the source-file scope.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub struct GlobalAccessSite {
    pub lexical_owner: SourceUnitOrdinal,
    pub start: u32,
    pub end: u32,
    pub access: GlobalAccessKind,
    pub property: String,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct GlobalAccessEvidence {
    pub dependencies: ProofDependencies,
    pub assumptions: Vec<AssumedEffectReason>,
}

#[derive(Debug, Clone, Serialize)]
pub struct GlobalMayAccess {
    pub site: GlobalAccessSite,
    pub evidence: GlobalAccessEvidence,
}

impl GlobalAccessEvidence {
    fn extend(&mut self, other: &Self) {
        self.dependencies.extend(&other.dependencies);
        for reason in &other.assumptions {
            if !self.assumptions.contains(reason) {
                self.assumptions.push(reason.clone());
            }
        }
    }
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub(crate) struct EvaluationEffects {
    pub global_accesses: BTreeMap<GlobalAccessSite, GlobalAccessEvidence>,
}

impl EvaluationEffects {
    pub(crate) fn record(
        &mut self,
        owner: SourceUnitOrdinal,
        span: Span,
        access: GlobalAccessKind,
        property: &str,
    ) {
        // Synthetic value projections have no physical source operation.
        // They cannot certify an access or domain absence.
        if span.is_dummy() {
            return;
        }
        self.global_accesses
            .entry(GlobalAccessSite {
                lexical_owner: owner,
                start: span.lo.0,
                end: span.hi.0,
                access,
                property: property.to_owned(),
            })
            .or_default();
    }

    pub(crate) fn assume(&mut self, reason: Option<&AssumedEffectReason>) {
        if let Some(reason) = reason {
            for evidence in self.global_accesses.values_mut() {
                if !evidence.assumptions.contains(reason) {
                    evidence.assumptions.push(reason.clone());
                }
            }
        }
    }

    pub(crate) fn depend_on(&mut self, dependencies: &ProofDependencies) {
        for evidence in self.global_accesses.values_mut() {
            evidence.dependencies.extend(dependencies);
        }
    }

    pub(crate) fn extend(&mut self, other: &Self) {
        for (site, evidence) in &other.global_accesses {
            self.global_accesses
                .entry(site.clone())
                .or_default()
                .extend(evidence);
        }
    }
}
