//! Saved purity verdicts. Runtime `Arc`s become references into a postorder
//! table; the ordinary Serde representation includes proof metadata and spans
//! without consulting the report serializer or ambient serialization state.

use std::collections::HashMap;
use std::marker::PhantomData;
use std::sync::Arc;

use crate::analysis_inputs::ProofDependencies;
use crate::execution_evidence::SourceLocation;
use crate::purity::{Purity, PurityReason, PurityRule};
use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use swc_common::Span;

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
#[serde(transparent)]
pub struct PurityNodeId(usize);

#[derive(Serialize, Deserialize)]
pub struct SavedPurityGraph {
    nodes: Vec<SavedPurity>,
}

#[derive(Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum SavedPurity {
    Pure {
        dependencies: Box<ProofDependencies>,
    },
    NotPure {
        reasons: Vec<SavedReason>,
    },
}

#[derive(Serialize, Deserialize)]
pub struct SavedReason {
    rule: PurityRule,
    source_location: Option<SourceLocation>,
    detail: Option<String>,
    author_guidance: Option<String>,
    cause_ref: Option<String>,
    cause: Option<PurityNodeId>,
    span: Span,
}

/// Snapshot state belongs to the conversion, never to the saved data.
#[derive(Default)]
pub struct PuritySnapshot<'a> {
    nodes: Vec<SavedPurity>,
    identities: HashMap<*const Purity, PurityNodeId>,
    // Keep every pointer key alive until the snapshot is finished.
    roots: PhantomData<&'a Purity>,
}

impl<'a> PuritySnapshot<'a> {
    pub fn snapshot(&mut self, purity: &'a Arc<Purity>) -> PurityNodeId {
        let identity = Arc::as_ptr(purity);
        if let Some(id) = self.identities.get(&identity) {
            return *id;
        }
        let saved = self.snapshot_value(purity);
        let id = PurityNodeId(self.nodes.len());
        self.nodes.push(saved);
        self.identities.insert(identity, id);
        id
    }

    /// A statement owns its verdict by value; only its cause edges are shared.
    pub fn snapshot_value(&mut self, purity: &'a Purity) -> SavedPurity {
        match purity {
            Purity::Pure { dependencies } => SavedPurity::Pure {
                dependencies: dependencies.clone(),
            },
            Purity::NotPure { reasons } => SavedPurity::NotPure {
                reasons: reasons
                    .iter()
                    .map(|reason| SavedReason {
                        rule: reason.rule,
                        source_location: reason.source_location.clone(),
                        detail: reason.detail.clone(),
                        author_guidance: reason.author_guidance.clone(),
                        cause_ref: reason.cause_ref.clone(),
                        cause: reason.cause.as_ref().map(|cause| self.snapshot(cause)),
                        span: reason.span,
                    })
                    .collect(),
            },
        }
    }

    pub fn finish(self) -> SavedPurityGraph {
        SavedPurityGraph { nodes: self.nodes }
    }
}

impl SavedPurityGraph {
    pub fn restore(self) -> Result<RestoredPurities> {
        let mut nodes = RestoredPurities(Vec::with_capacity(self.nodes.len()));
        for saved in self.nodes {
            let purity = nodes.restore_value(saved)?;
            nodes.0.push(Arc::new(purity));
        }
        Ok(nodes)
    }
}

pub struct RestoredPurities(Vec<Arc<Purity>>);

impl RestoredPurities {
    pub fn restore_value(&self, saved: SavedPurity) -> Result<Purity> {
        Ok(match saved {
            SavedPurity::Pure { dependencies } => Purity::Pure { dependencies },
            SavedPurity::NotPure { reasons } => Purity::NotPure {
                reasons: reasons
                    .into_iter()
                    .map(|reason| {
                        Ok(PurityReason {
                            rule: reason.rule,
                            source_location: reason.source_location,
                            detail: reason.detail,
                            author_guidance: reason.author_guidance,
                            cause_ref: reason.cause_ref,
                            cause: reason.cause.map(|id| self.get(id)).transpose()?,
                            span: reason.span,
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
    fn ordinary_serde_preserves_shared_causes_and_proof_metadata() {
        let dependencies = ProofDependencies {
            value_inputs: [ValueInputRef {
                defining_chunk: "provider".to_string(),
                input_index: 2,
            }]
            .into(),
            ..ProofDependencies::default()
        };
        let cause = Arc::new(Purity::Pure {
            dependencies: Box::new(dependencies.clone()),
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
        let root_id = snapshot.snapshot(&root);
        assert_eq!(snapshot.snapshot(&root).0, root_id.0);
        let cause_id = snapshot.snapshot(&cause);
        let saved = snapshot.finish();
        assert_eq!(saved.nodes.len(), 2);
        // Both conversions use ordinary Serde with explicit saved data.
        let bytes = rmp_serde::to_vec_named(&saved).expect("ordinary encode");
        let saved: SavedPurityGraph = rmp_serde::from_slice(&bytes).expect("ordinary decode");
        let restored = saved.restore().expect("valid graph");
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
        assert_eq!(
            cause.as_ref(),
            &Purity::Pure {
                dependencies: Box::new(dependencies)
            }
        );
    }

    #[test]
    fn rejects_invalid_and_cyclic_cause_references() {
        for reference in [0, 1, usize::MAX] {
            let graph = SavedPurityGraph {
                nodes: vec![SavedPurity::NotPure {
                    reasons: vec![SavedReason {
                        rule: PurityRule::UnknownCall,
                        source_location: None,
                        detail: None,
                        author_guidance: None,
                        cause_ref: None,
                        cause: Some(PurityNodeId(reference)),
                        span: DUMMY_SP,
                    }],
                }],
            };
            assert!(graph.restore().is_err());
        }
    }
}
