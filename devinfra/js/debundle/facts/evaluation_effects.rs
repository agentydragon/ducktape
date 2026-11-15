//! Report-only may-accesses composed by canonical runtime transfers.
//!
//! This ledger covers static global-property accesses only. A recorded access
//! is never a certificate of execution, purity, or complete domain coverage.

use std::collections::BTreeMap;

use anyhow::{Result, bail, ensure};

use serde::{Deserialize, Serialize};
use source_identity::SourceUnitOrdinal;
use swc_common::Span;

use crate::analysis_inputs::ProofDependencies;
use crate::execution_evidence::AssumedEffectReason;
use crate::saved_proto::{ProtoConvert, proto, required};

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

impl GlobalAccessEvidence {
    fn record_assumption(&mut self, reason: &AssumedEffectReason) {
        if let Some(existing) = self
            .assumptions
            .iter_mut()
            .find(|existing| existing.same_report_category(reason))
        {
            if reason.is_canonical_predecessor(existing) {
                *existing = reason.clone();
            }
            return;
        }
        self.assumptions.push(reason.clone());
    }
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
            self.record_assumption(reason);
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
                evidence.record_assumption(reason);
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

impl ProtoConvert<proto::EvaluationEffects> for EvaluationEffects {
    fn to_proto(&self) -> Result<proto::EvaluationEffects> {
        Ok(proto::EvaluationEffects {
            global_accesses: self
                .global_accesses
                .iter()
                .map(|(site, evidence)| {
                    Ok(proto::GlobalAccess {
                        site: Some(site.to_proto()?),
                        evidence: Some(evidence.to_proto()?),
                    })
                })
                .collect::<Result<_>>()?,
        })
    }
    fn from_proto(value: proto::EvaluationEffects) -> Result<Self> {
        let mut global_accesses = BTreeMap::new();
        for entry in value.global_accesses {
            let site = GlobalAccessSite::from_proto(required(entry.site, "GlobalAccess.site")?)?;
            let evidence = GlobalAccessEvidence::from_proto(required(
                entry.evidence,
                "GlobalAccess.evidence",
            )?)?;
            ensure!(
                global_accesses.insert(site, evidence).is_none(),
                "duplicate saved global-access site"
            );
        }
        Ok(Self { global_accesses })
    }
}
impl ProtoConvert<proto::GlobalAccessEvidence> for GlobalAccessEvidence {
    fn to_proto(&self) -> Result<proto::GlobalAccessEvidence> {
        Ok(proto::GlobalAccessEvidence {
            dependencies: Some(self.dependencies.to_proto()?),
            assumptions: self.assumptions.to_proto()?,
        })
    }
    fn from_proto(value: proto::GlobalAccessEvidence) -> Result<Self> {
        Ok(Self {
            dependencies: ProtoConvert::from_proto(required(
                value.dependencies,
                "GlobalAccessEvidence.dependencies",
            )?)?,
            assumptions: ProtoConvert::from_proto(value.assumptions)?,
        })
    }
}
impl ProtoConvert<proto::GlobalAccessSite> for GlobalAccessSite {
    fn to_proto(&self) -> Result<proto::GlobalAccessSite> {
        Ok(proto::GlobalAccessSite {
            lexical_owner: Some(self.lexical_owner.to_proto()?),
            start: Some(self.start),
            end: Some(self.end),
            access: Some(self.access.to_proto()?),
            property: Some(self.property.clone()),
        })
    }
    fn from_proto(value: proto::GlobalAccessSite) -> Result<Self> {
        Ok(Self {
            lexical_owner: ProtoConvert::from_proto(required(
                value.lexical_owner,
                "GlobalAccessSite.lexical_owner",
            )?)?,
            start: required(value.start, "GlobalAccessSite.start")?,
            end: required(value.end, "GlobalAccessSite.end")?,
            access: ProtoConvert::from_proto(required(value.access, "GlobalAccessSite.access")?)?,
            property: required(value.property, "GlobalAccessSite.property")?,
        })
    }
}
impl ProtoConvert<i32> for GlobalAccessKind {
    fn to_proto(&self) -> Result<i32> {
        Ok(match self {
            Self::Read => proto::GlobalAccessKind::Read as i32,
            Self::Write => proto::GlobalAccessKind::Write as i32,
        })
    }
    fn from_proto(value: i32) -> Result<Self> {
        Ok(match proto::GlobalAccessKind::try_from(value)? {
            proto::GlobalAccessKind::Read => Self::Read,
            proto::GlobalAccessKind::Write => Self::Write,
            proto::GlobalAccessKind::Unspecified => {
                bail!("unspecified saved-analysis global access kind")
            }
        })
    }
}

#[cfg(test)]
mod protobuf_tests {
    use super::*;
    use crate::analysis_inputs::ValueInputRef;
    use crate::execution_evidence::{PossibleGetterLookupSite, PossibleGetterPromotion};
    use swc_common::BytePos;

    #[test]
    fn saved_accesses_retain_source_sites_proof_dependencies_and_getter_witnesses() {
        let mut effects = EvaluationEffects::default();
        effects.record(
            SourceUnitOrdinal(3),
            Span::new(BytePos(17), BytePos(29)),
            GlobalAccessKind::Write,
            "property",
        );
        let dependencies = ProofDependencies {
            value_inputs: [ValueInputRef {
                defining_chunk: "provider".into(),
                input_index: 2,
            }]
            .into(),
            ..ProofDependencies::default()
        };
        effects.depend_on(&dependencies);
        let reason = AssumedEffectReason::PossibleGetter {
            lookup: PossibleGetterLookupSite {
                lexical_owner: SourceUnitOrdinal(1),
                span_start: 5,
                span_end: 13,
            },
            promotion: PossibleGetterPromotion::LazyOwner {
                source_owner: SourceUnitOrdinal(4),
            },
        };
        effects.assume(Some(&reason));
        let bytes = analysis_codec::encode_protobuf(&effects.to_proto().unwrap()).unwrap();
        let restored =
            EvaluationEffects::from_proto(analysis_codec::decode_protobuf(&bytes).unwrap())
                .unwrap();
        let (site, evidence) = restored.global_accesses.first_key_value().unwrap();
        assert_eq!(
            *site,
            GlobalAccessSite {
                lexical_owner: SourceUnitOrdinal(3),
                start: 17,
                end: 29,
                access: GlobalAccessKind::Write,
                property: "property".into()
            }
        );
        assert_eq!(evidence.dependencies, dependencies);
        assert_eq!(evidence.assumptions, [reason]);
    }

    #[test]
    fn saved_accesses_reject_missing_fields_duplicate_sites_and_invalid_kinds() {
        let mut effects = EvaluationEffects::default();
        effects.record(
            SourceUnitOrdinal(0),
            Span::new(BytePos(1), BytePos(2)),
            GlobalAccessKind::Read,
            "property",
        );
        let wire = effects.to_proto().unwrap();
        for kind in [0, i32::MAX] {
            let mut malformed = wire.clone();
            malformed.global_accesses[0].site.as_mut().unwrap().access = Some(kind);
            assert!(EvaluationEffects::from_proto(malformed).is_err());
        }
        let mut malformed = wire.clone();
        malformed.global_accesses[0]
            .evidence
            .as_mut()
            .unwrap()
            .dependencies = None;
        assert!(EvaluationEffects::from_proto(malformed).is_err());
        let mut malformed = wire.clone();
        let accesses = &mut malformed.global_accesses;
        accesses.push(accesses[0].clone());
        assert!(EvaluationEffects::from_proto(malformed).is_err());
        let mut missing_site = wire.clone();
        missing_site.global_accesses[0].site = None;
        assert!(EvaluationEffects::from_proto(missing_site).is_err());
        let mut missing_evidence = wire;
        missing_evidence.global_accesses[0].evidence = None;
        assert!(EvaluationEffects::from_proto(missing_evidence).is_err());
        let empty = EvaluationEffects::default().to_proto().unwrap();
        assert!(
            EvaluationEffects::from_proto(empty)
                .unwrap()
                .global_accesses
                .is_empty()
        );
    }

    #[test]
    fn saved_accesses_encode_in_canonical_site_order() {
        let mut effects = EvaluationEffects::default();
        for owner in [2, 0, 1] {
            effects.record(
                SourceUnitOrdinal(owner),
                Span::new(BytePos(1), BytePos(2)),
                GlobalAccessKind::Read,
                "property",
            );
        }
        let mut wire = effects.to_proto().unwrap();
        let canonical = analysis_codec::encode_protobuf(&wire).unwrap();
        wire.global_accesses.reverse();
        let restored = EvaluationEffects::from_proto(wire).unwrap();
        assert_eq!(
            analysis_codec::encode_protobuf(&restored.to_proto().unwrap()).unwrap(),
            canonical
        );
    }
}
