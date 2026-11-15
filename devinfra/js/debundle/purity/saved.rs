//! Runtime purity causes are saved as a postorder protobuf graph.

use std::collections::HashMap;
use std::marker::PhantomData;
use std::sync::Arc;

use crate::analysis_inputs::ProofDependencies;
use crate::execution_evidence::SourceLocation;
use crate::purity::{Purity, PurityReason, PurityRule};
use crate::saved_proto::{ProtoConvert, proto, required};
use anyhow::{Context, Result};
use swc_common::Span;

#[derive(Debug, Clone, Copy)]
pub struct PurityNodeId(usize);

impl ProtoConvert<proto::PurityNodeId> for PurityNodeId {
    fn to_proto(&self) -> Result<proto::PurityNodeId> {
        Ok(proto::PurityNodeId {
            index: Some(self.0.to_proto()?),
        })
    }

    fn from_proto(value: proto::PurityNodeId) -> Result<Self> {
        Ok(Self(usize::from_proto(required(
            value.index,
            "PurityNodeId.index",
        )?)?))
    }
}

/// Snapshot state belongs to the conversion, never to the saved data.
#[derive(Default)]
pub struct PuritySnapshot<'a> {
    nodes: Vec<proto::SavedPurity>,
    identities: HashMap<*const Purity, PurityNodeId>,
    // Keep every pointer key alive until the snapshot is finished.
    roots: PhantomData<&'a Purity>,
}

impl<'a> PuritySnapshot<'a> {
    pub fn snapshot(&mut self, purity: &'a Arc<Purity>) -> Result<PurityNodeId> {
        let identity = Arc::as_ptr(purity);
        if let Some(id) = self.identities.get(&identity) {
            return Ok(*id);
        }
        let saved = self.snapshot_value(purity)?;
        let id = PurityNodeId(self.nodes.len());
        self.nodes.push(saved);
        self.identities.insert(identity, id);
        Ok(id)
    }

    /// A statement owns its verdict by value; only its cause edges are shared.
    pub fn snapshot_value(&mut self, purity: &'a Purity) -> Result<proto::SavedPurity> {
        use proto::saved_purity::{NotPure, Value};
        Ok(proto::SavedPurity {
            value: Some(match purity {
                Purity::Pure { dependencies } => Value::Pure(dependencies.to_proto()?),
                Purity::NotPure { reasons } => Value::NotPure(NotPure {
                    reasons: reasons
                        .iter()
                        .map(|reason| {
                            Ok(proto::SavedReason {
                                rule: reason.rule.to_proto()?,
                                source_location: reason
                                    .source_location
                                    .as_ref()
                                    .map(ProtoConvert::to_proto)
                                    .transpose()?,
                                detail: reason.detail.clone(),
                                author_guidance: reason.author_guidance.clone(),
                                cause_ref: reason.cause_ref.clone(),
                                cause_node: reason
                                    .cause
                                    .as_ref()
                                    .map(|cause| -> Result<_> {
                                        let id = self.snapshot(cause)?;
                                        Ok(u64::try_from(id.0)?)
                                    })
                                    .transpose()?,
                                span: Some(reason.span.to_proto()?),
                            })
                        })
                        .collect::<Result<_>>()?,
                }),
            }),
        })
    }

    pub fn finish(self) -> proto::SavedPurityGraph {
        proto::SavedPurityGraph { nodes: self.nodes }
    }
}

pub struct RestoredPurities(Vec<Arc<Purity>>);

impl RestoredPurities {
    pub fn restore(graph: proto::SavedPurityGraph) -> Result<Self> {
        let mut nodes = Self(Vec::with_capacity(graph.nodes.len()));
        for saved in graph.nodes {
            let purity = nodes.restore_value(saved)?;
            nodes.0.push(Arc::new(purity));
        }
        Ok(nodes)
    }

    pub fn restore_value(&self, saved: proto::SavedPurity) -> Result<Purity> {
        use proto::saved_purity::Value;
        Ok(match required(saved.value, "SavedPurity.value")? {
            Value::Pure(dependencies) => Purity::Pure {
                dependencies: ProofDependencies::from_proto(dependencies)?,
            },
            Value::NotPure(not_pure) => Purity::NotPure {
                reasons: not_pure
                    .reasons
                    .into_iter()
                    .map(|reason| {
                        Ok(PurityReason {
                            rule: PurityRule::from_proto(reason.rule)?,
                            source_location: reason
                                .source_location
                                .map(SourceLocation::from_proto)
                                .transpose()?,
                            detail: reason.detail,
                            author_guidance: reason.author_guidance,
                            cause_ref: reason.cause_ref,
                            cause: reason
                                .cause_node
                                .map(|id| self.get(PurityNodeId(usize::try_from(id)?)))
                                .transpose()?,
                            span: Span::from_proto(required(reason.span, "SavedReason.span")?)?,
                        })
                    })
                    .collect::<Result<_>>()?,
            },
        })
    }

    pub fn get(&self, id: PurityNodeId) -> Result<Arc<Purity>> {
        self.0.get(id.0).cloned().with_context(|| {
            format!(
                "invalid saved purity reference {}: expected an earlier node",
                id.0
            )
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::analysis_inputs::ValueInputRef;
    use swc_common::{BytePos, DUMMY_SP};

    #[test]
    fn protobuf_preserves_shared_causes_and_proof_metadata() {
        let dependencies = ProofDependencies {
            value_inputs: [ValueInputRef {
                defining_chunk: "provider".to_string(),
                input_index: 2,
            }]
            .into(),
            ..ProofDependencies::default()
        };
        let cause = Arc::new(Purity::Pure {
            dependencies: dependencies.clone(),
        });
        let root = Arc::new(Purity::NotPure {
            reasons: vec![PurityReason {
                rule: PurityRule::ImpureFunctionCall,
                source_location: None,
                detail: Some("callee".to_string()),
                author_guidance: None,
                cause_ref: None,
                cause: Some(cause.clone()),
                span: Span::new(BytePos(5), BytePos(13)),
            }],
        });
        let mut snapshot = PuritySnapshot::default();
        let root_id = snapshot.snapshot(&root).expect("snapshot root");
        assert_eq!(
            snapshot.snapshot(&root).expect("snapshot root").0,
            root_id.0
        );
        let cause_id = snapshot.snapshot(&cause).expect("snapshot cause");
        let saved = snapshot.finish();
        assert_eq!(saved.nodes.len(), 2);
        let bytes = analysis_codec::encode_protobuf(&saved).expect("protobuf encode");
        let wire: proto::SavedPurityGraph =
            analysis_codec::decode_protobuf(&bytes).expect("protobuf decode");
        let restored = RestoredPurities::restore(wire).expect("valid graph");
        let root = restored.get(root_id).expect("root");
        let Purity::NotPure { reasons } = root.as_ref() else {
            panic!("expected reason");
        };
        assert_ne!(reasons[0].span, DUMMY_SP);
        assert_eq!(reasons[0].span, Span::new(BytePos(5), BytePos(13)));
        let cause = restored.get(cause_id).expect("cause");
        assert!(Arc::ptr_eq(
            reasons[0].cause.as_ref().expect("cause"),
            &cause
        ));
        assert_eq!(cause.as_ref(), &Purity::Pure { dependencies });
    }

    #[test]
    fn rejects_missing_variants_and_invalid_proof_dependencies() {
        use proto::saved_purity::Value;
        assert!(RestoredPurities::restore(proto::SavedPurityGraph { nodes: vec![] }).is_ok());
        let mut incomplete_dependencies = ProofDependencies::default().to_proto().unwrap();
        incomplete_dependencies.program_premises.push(0);
        for value in [None, Some(Value::Pure(incomplete_dependencies))] {
            let graph = proto::SavedPurityGraph {
                nodes: vec![proto::SavedPurity { value }],
            };
            assert!(RestoredPurities::restore(graph).is_err());
        }
    }

    #[test]
    fn rejects_invalid_and_cyclic_cause_references() {
        for reference in [0, 1, usize::MAX] {
            let graph = proto::SavedPurityGraph {
                nodes: vec![proto::SavedPurity {
                    value: Some(proto::saved_purity::Value::NotPure(
                        proto::saved_purity::NotPure {
                            reasons: vec![proto::SavedReason {
                                rule: PurityRule::UnknownCall.to_proto().unwrap(),
                                source_location: None,
                                detail: None,
                                author_guidance: None,
                                cause_ref: None,
                                cause_node: Some(u64::try_from(reference).unwrap()),
                                span: Some(DUMMY_SP.to_proto().unwrap()),
                            }],
                        },
                    )),
                }],
            };
            assert!(RestoredPurities::restore(graph).is_err());
        }
    }

    #[test]
    fn rejects_unknown_reason_rules_and_missing_spans() {
        for (rule, span) in [
            (0, Some(DUMMY_SP.to_proto().unwrap())),
            (i32::MAX, Some(DUMMY_SP.to_proto().unwrap())),
            (PurityRule::UnknownCall.to_proto().unwrap(), None),
        ] {
            let graph = proto::SavedPurityGraph {
                nodes: vec![proto::SavedPurity {
                    value: Some(proto::saved_purity::Value::NotPure(
                        proto::saved_purity::NotPure {
                            reasons: vec![proto::SavedReason {
                                rule,
                                source_location: None,
                                detail: None,
                                author_guidance: None,
                                cause_ref: None,
                                cause_node: None,
                                span,
                            }],
                        },
                    )),
                }],
            };
            assert!(RestoredPurities::restore(graph).is_err());
        }
    }
}
