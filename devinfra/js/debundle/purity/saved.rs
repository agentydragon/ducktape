//! Protobuf purity verdicts. Runtime identities become references into a
//! postorder table; causes retain their sharing and artifact-only spans.

use std::collections::HashMap;
use std::marker::PhantomData;
use std::sync::Arc;

use analysis_vocabulary::execution_evidence::SourceLocation;
use analysis_vocabulary::purity::{Purity, PurityReason, PurityRule};
use anyhow::{Context, Result, ensure};
use prost::Message;
use saved_purity_proto::debundle::saved_purity as proto;
use serde::{Deserialize, Serialize};
use swc_common::{BytePos, Span};

#[path = "saved_dependencies.rs"]
mod dependencies;

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
#[serde(transparent)]
pub struct PurityNodeId(u32);

/// Snapshot state belongs to the conversion, never to the saved data.
#[derive(Default)]
pub struct PuritySnapshot<'a> {
    nodes: Vec<proto::Purity>,
    identities: HashMap<*const Purity, PurityNodeId>,
    // Keep every pointer key alive until the snapshot is finished.
    roots: PhantomData<&'a Purity>,
}

impl<'a> PuritySnapshot<'a> {
    pub fn snapshot(&mut self, purity: &'a Arc<Purity>) -> PurityNodeId {
        self.snapshot_value(purity)
    }

    pub fn snapshot_value(&mut self, purity: &'a Purity) -> PurityNodeId {
        let identity = purity as *const Purity;
        if let Some(id) = self.identities.get(&identity) {
            return *id;
        }
        use proto::purity::Verdict;
        let verdict = match purity {
            Purity::Pure { dependencies } => Verdict::Pure(dependencies::encode(dependencies)),
            Purity::NotPure { reasons } => Verdict::NotPure(proto::Reasons {
                reasons: reasons
                    .iter()
                    .map(|reason| proto::Reason {
                        rule: Some(reason.rule as i32),
                        source_location: reason.source_location.as_ref().map(|location| {
                            proto::SourceLocation {
                                source_path: location.source_path.clone(),
                                start_line: location.start_line as u64,
                                end_line: location.end_line as u64,
                                start_column: location.start_column.map(|column| column as u64),
                            }
                        }),
                        detail: reason.detail.clone(),
                        author_guidance: reason.author_guidance.clone(),
                        cause_ref: reason.cause_ref.clone(),
                        cause: reason.cause.as_ref().map(|cause| self.snapshot(cause).0),
                        span: Some(proto::Span {
                            start: reason.span.lo.0,
                            end: reason.span.hi.0,
                        }),
                    })
                    .collect(),
            }),
        };
        let id = PurityNodeId(
            self.nodes
                .len()
                .try_into()
                .expect("fewer than 2^32 verdicts"),
        );
        self.nodes.push(proto::Purity {
            verdict: Some(verdict),
        });
        self.identities.insert(identity, id);
        id
    }

    pub fn encode(self) -> Vec<u8> {
        proto::PurityGraph { nodes: self.nodes }.encode_to_vec()
    }
}

pub struct RestoredPurities(Vec<Arc<Purity>>);

impl RestoredPurities {
    pub fn decode(bytes: &[u8]) -> Result<Self> {
        let graph = proto::PurityGraph::decode(bytes).context("decode saved purity protobuf")?;
        let mut nodes = Self(Vec::with_capacity(graph.nodes.len()));
        for saved in graph.nodes {
            let purity = nodes.restore_node(saved)?;
            nodes.0.push(Arc::new(purity));
        }
        Ok(nodes)
    }

    /// Statements own their verdicts by value; cloned cause edges stay shared.
    pub fn restore_value(&self, id: PurityNodeId) -> Result<Purity> {
        Ok(self.get(id)?.as_ref().clone())
    }

    fn restore_node(&self, saved: proto::Purity) -> Result<Purity> {
        use proto::purity::Verdict;
        Ok(
            match saved.verdict.context("missing saved purity verdict")? {
                Verdict::Pure(dependencies) => Purity::Pure {
                    dependencies: dependencies::decode(dependencies)?,
                },
                Verdict::NotPure(reasons) => Purity::NotPure {
                    reasons: reasons
                        .reasons
                        .into_iter()
                        .map(|reason| {
                            let span = reason.span.context("missing saved purity span")?;
                            ensure!(span.start <= span.end, "invalid saved purity span");
                            Ok(PurityReason {
                                rule: PurityRule::try_from(
                                    reason.rule.context("missing saved purity rule")?,
                                )?,
                                source_location: reason
                                    .source_location
                                    .map(|location| -> Result<_> {
                                        Ok(SourceLocation {
                                            source_path: location.source_path,
                                            start_line: location.start_line.try_into()?,
                                            end_line: location.end_line.try_into()?,
                                            start_column: location
                                                .start_column
                                                .map(usize::try_from)
                                                .transpose()?,
                                        })
                                    })
                                    .transpose()?,
                                detail: reason.detail,
                                author_guidance: reason.author_guidance,
                                cause_ref: reason.cause_ref,
                                cause: reason
                                    .cause
                                    .map(|id| self.get(PurityNodeId(id)))
                                    .transpose()?,
                                span: Span::new(BytePos(span.start), BytePos(span.end)),
                            })
                        })
                        .collect::<Result<_>>()?,
                },
            },
        )
    }

    pub fn get(&self, id: PurityNodeId) -> Result<Arc<Purity>> {
        self.0.get(id.0 as usize).cloned().with_context(|| {
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
    use analysis_vocabulary::inputs::{
        ArgumentInputRef, DescriptorCopyAuthorRef, HostBehaviorKey,
        ImportedPrimitiveArraySpreadInputRef, InitializerCallEffectInputRef,
        NativeOperationInputRef, NoAccessorInstallRef, ProgramPremiseKey, ProofDependencies,
        ValueInputRef,
    };
    use swc_common::{BytePos, DUMMY_SP};

    #[test]
    fn protobuf_preserves_shared_causes_and_proof_metadata() {
        let dependencies = ProofDependencies {
            value_inputs: [ValueInputRef {
                defining_chunk: "provider".to_string(),
                input_index: 2,
            }]
            .into(),
            native_operation_inputs: [NativeOperationInputRef {
                defining_chunk: "provider".into(),
                input_index: 3,
            }]
            .into(),
            initializer_call_effect_inputs: [InitializerCallEffectInputRef {
                defining_chunk: "consumer".into(),
                input_index: 4,
            }]
            .into(),
            imported_primitive_array_spread_inputs: [ImportedPrimitiveArraySpreadInputRef {
                defining_chunk: "consumer".into(),
                input_index: 5,
            }]
            .into(),
            descriptor_copy_authors: [DescriptorCopyAuthorRef {
                defining_module: "provider".into(),
                export: "copy".into(),
                spec_location: "descriptor_copy".into(),
            }]
            .into(),
            argument_inputs: [ArgumentInputRef {
                defining_chunk: "consumer".into(),
                input_index: 6,
            }]
            .into(),
            host_behavior: [
                HostBehaviorKey::LoaderResultForEvaluatingModule,
                HostBehaviorKey::SynchronousLoadRunsAppCallbacks,
                HostBehaviorKey::SynchronousLoadDrainsMicrotasks,
                HostBehaviorKey::PredefinedGlobals,
                HostBehaviorKey::ReentryFreeBuiltinModule(spec::NodeBuiltinModule::Fs),
                HostBehaviorKey::ReentryFreeBunModule(spec::BunModule::BunFfi),
                HostBehaviorKey::ReentryFreeEmbeddedLoader(spec::EmbeddedLoader::Text),
                HostBehaviorKey::ReentryFreeOperation(spec::ReentryFreeOperation::PromiseResolve),
            ]
            .into(),
            program_premises: [
                ProgramPremiseKey::ProcessPrototypeChainAccessors,
                ProgramPremiseKey::NonLiteralLoaderCalls,
                ProgramPremiseKey::IntrinsicsNeverWritten,
            ]
            .into(),
            no_accessor_install_callables: [NoAccessorInstallRef {
                defining_chunk: "provider".into(),
                input_index: 7,
            }]
            .into(),
        };
        let cause = Arc::new(Purity::Pure {
            dependencies: dependencies.clone(),
        });
        let root = Arc::new(Purity::NotPure {
            reasons: vec![PurityReason {
                rule: PurityRule::ImpureFunctionCall,
                source_location: Some(SourceLocation {
                    source_path: "provider.js".into(),
                    start_line: 2,
                    end_line: 3,
                    start_column: Some(0),
                }),
                detail: Some(String::new()),
                author_guidance: Some("inspect callee".into()),
                cause_ref: Some("cause_0".into()),
                cause: Some(cause.clone()),
                span: Span::new(BytePos(5), BytePos(13)),
            }],
        });
        let mut snapshot = PuritySnapshot::default();
        let root_id = snapshot.snapshot(&root);
        assert_eq!(snapshot.snapshot(&root).0, root_id.0);
        let cause_id = snapshot.snapshot(&cause);
        let expected_report = serde_json::to_value(root.as_ref()).expect("report");
        let bytes = snapshot.encode();
        let restored = RestoredPurities::decode(&bytes).expect("valid graph");
        let root = restored.get(root_id).expect("root");
        assert_eq!(
            serde_json::to_value(root.as_ref()).expect("report"),
            expected_report
        );
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
        let Purity::NotPure { reasons } = restored.restore_value(root_id).expect("owned verdict")
        else {
            panic!("expected owned verdict");
        };
        assert!(Arc::ptr_eq(
            reasons[0].cause.as_ref().expect("shared cause"),
            &cause
        ));
    }

    #[test]
    fn rejects_invalid_and_cyclic_cause_references() {
        for reference in [0, 1, u32::MAX] {
            let graph = proto::PurityGraph {
                nodes: vec![proto::Purity {
                    verdict: Some(proto::purity::Verdict::NotPure(proto::Reasons {
                        reasons: vec![proto::Reason {
                            rule: Some(PurityRule::UnknownCall as i32),
                            cause: Some(reference),
                            span: Some(proto::Span::default()),
                            ..proto::Reason::default()
                        }],
                    })),
                }],
            };
            assert!(RestoredPurities::decode(&graph.encode_to_vec()).is_err());
        }
    }

    #[test]
    fn rejects_missing_verdict_metadata() {
        let reason = proto::Reason {
            rule: Some(PurityRule::AssignOrUpdate as i32),
            span: Some(proto::Span::default()),
            ..proto::Reason::default()
        };
        let mut missing_rule = reason.clone();
        missing_rule.rule = None;
        let mut missing_span = reason;
        missing_span.span = None;
        for verdict in [
            None,
            Some(proto::purity::Verdict::NotPure(proto::Reasons {
                reasons: vec![missing_rule],
            })),
            Some(proto::purity::Verdict::NotPure(proto::Reasons {
                reasons: vec![missing_span],
            })),
        ] {
            let graph = proto::PurityGraph {
                nodes: vec![proto::Purity { verdict }],
            };
            assert!(RestoredPurities::decode(&graph.encode_to_vec()).is_err());
        }
    }
}
