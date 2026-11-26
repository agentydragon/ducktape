//! Binding-effect coverage of a top-level source unit: which execution sealed
//! its lexical binding effects, the receipts proving it, and their saved form.
//! Produced by the synchronous interpreter, stored in statement facts.

use analysis_codec::{ContextOrdinals, SavedBinding};
use analysis_vocabulary::inputs::{NativeIntrinsic, ProofDependencies};
use analysis_vocabulary::report_schema::BindingEffectAuthorityReport;
use anyhow::Result;
use serde::{Deserialize, Serialize};
use source_identity::SourceUnitOrdinal;
use swc_common::Span;
use swc_ecma_ast::Id;

/// Exact identity for the one root initializer whose ordered execution may
/// seal lexical binding-effect coverage.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RootInitializerIdentity {
    pub owner: SourceUnitOrdinal,
    pub binding: Id,
    pub initializer_span: Option<Span>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LocalBodyIdentity {
    RootInitializer {
        owner: SourceUnitOrdinal,
        initializer_span: Span,
    },
    Callable {
        key: u32,
        owner: SourceUnitOrdinal,
    },
    /// The lowered base-constructor body (instance fields, then constructor
    /// statements) of the class defined at `class`.
    Constructor {
        class: u32,
        owner: SourceUnitOrdinal,
    },
    Native {
        intrinsic: NativeIntrinsic,
        property: Option<String>,
    },
    /// An imported callable whose call the importer replaced by its derived
    /// foreign call summary.
    Imported {
        defining_module: String,
        export: String,
    },
}

/// One selected direct local invocation. The flat edge set avoids expanding
/// every root-to-leaf path; each edge retains prerequisites merged from every
/// invocation context that selected it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LocalCallReceipt {
    pub caller: LocalBodyIdentity,
    pub call_span: Span,
    pub target: LocalBodyIdentity,
    pub dependencies: Box<ProofDependencies>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LexicalBindingEffectReceipt {
    pub identity: RootInitializerIdentity,
    pub read_dependencies: ProofDependencies,
    pub rebind_dependencies: ProofDependencies,
    pub calls: Vec<LocalCallReceipt>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BindingEffectCoverage {
    #[default]
    Unclassified,
    LegacyExecution,
    OrderedRootInitializer {
        identity: RootInitializerIdentity,
        receipt: Option<Box<LexicalBindingEffectReceipt>>,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BindingEffectAuthority {
    Unclassified,
    LegacyExecution { complete: bool },
    AuthorSourceOnly,
    OrderedRootInitializerUncovered,
    OperationReceipt,
}

impl BindingEffectCoverage {
    pub(crate) fn snapshot(
        &self,
        ordinals: &ContextOrdinals,
    ) -> Result<SavedBindingEffectCoverage> {
        Ok(match self {
            Self::Unclassified => SavedBindingEffectCoverage::Unclassified,
            Self::LegacyExecution => SavedBindingEffectCoverage::LegacyExecution,
            Self::OrderedRootInitializer { identity, receipt } => {
                SavedBindingEffectCoverage::OrderedRootInitializer {
                    identity: SavedRootInitializerIdentity::snapshot(identity, ordinals)?,
                    receipt: receipt
                        .as_deref()
                        .map(|receipt| {
                            SavedLexicalBindingEffectReceipt::snapshot(receipt, ordinals)
                        })
                        .transpose()?
                        .map(Box::new),
                }
            }
        })
    }

    pub fn authority(&self, complete: bool, author_source_only: bool) -> BindingEffectAuthority {
        if author_source_only {
            return BindingEffectAuthority::AuthorSourceOnly;
        }
        match self {
            Self::Unclassified => BindingEffectAuthority::Unclassified,
            Self::LegacyExecution => BindingEffectAuthority::LegacyExecution { complete },
            Self::OrderedRootInitializer {
                receipt: Some(_), ..
            } => BindingEffectAuthority::OperationReceipt,
            Self::OrderedRootInitializer { receipt: None, .. } => {
                BindingEffectAuthority::OrderedRootInitializerUncovered
            }
        }
    }

    pub(crate) fn authority_report(
        &self,
        complete: bool,
        author_source_only: bool,
    ) -> BindingEffectAuthorityReport {
        match self.authority(complete, author_source_only) {
            BindingEffectAuthority::Unclassified => BindingEffectAuthorityReport::Unclassified,
            BindingEffectAuthority::AuthorSourceOnly => {
                BindingEffectAuthorityReport::AuthorSourceOnly
            }
            BindingEffectAuthority::LegacyExecution { complete } => {
                BindingEffectAuthorityReport::LegacyExecution { complete }
            }
            BindingEffectAuthority::OperationReceipt => {
                let Self::OrderedRootInitializer {
                    receipt: Some(receipt),
                    ..
                } = self
                else {
                    unreachable!("typed authority comes from this receipt");
                };
                BindingEffectAuthorityReport::OperationReceipt {
                    receipt: Box::new(receipt.as_ref().to_report()),
                }
            }
            BindingEffectAuthority::OrderedRootInitializerUncovered => {
                let Self::OrderedRootInitializer { identity, .. } = self else {
                    unreachable!("typed authority comes from this root attempt");
                };
                BindingEffectAuthorityReport::OrderedRootInitializerUncovered {
                    owner: identity.owner,
                    binding: identity.binding.0.to_string(),
                    initializer_span: identity.initializer_span,
                }
            }
        }
    }
}

/// Process-independent binding-effect authority retained by staged analysis.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum SavedBindingEffectCoverage {
    Unclassified,
    LegacyExecution,
    OrderedRootInitializer {
        identity: SavedRootInitializerIdentity,
        receipt: Option<Box<SavedLexicalBindingEffectReceipt>>,
    },
}

impl SavedBindingEffectCoverage {
    pub(crate) fn restore(self, ordinals: &ContextOrdinals) -> Result<BindingEffectCoverage> {
        Ok(match self {
            Self::Unclassified => BindingEffectCoverage::Unclassified,
            Self::LegacyExecution => BindingEffectCoverage::LegacyExecution,
            Self::OrderedRootInitializer { identity, receipt } => {
                BindingEffectCoverage::OrderedRootInitializer {
                    identity: identity.restore(ordinals)?,
                    receipt: receipt
                        .map(|receipt| receipt.restore(ordinals))
                        .transpose()?
                        .map(Box::new),
                }
            }
        })
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct SavedRootInitializerIdentity {
    owner: SourceUnitOrdinal,
    binding: SavedBinding,
    initializer_span: Option<Span>,
}

impl SavedRootInitializerIdentity {
    fn snapshot(identity: &RootInitializerIdentity, ordinals: &ContextOrdinals) -> Result<Self> {
        Ok(Self {
            owner: identity.owner,
            binding: ordinals.snapshot_binding(&identity.binding)?,
            initializer_span: identity.initializer_span,
        })
    }

    fn restore(self, ordinals: &ContextOrdinals) -> Result<RootInitializerIdentity> {
        Ok(RootInitializerIdentity {
            owner: self.owner,
            binding: ordinals.restore_binding(self.binding)?,
            initializer_span: self.initializer_span,
        })
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct SavedLexicalBindingEffectReceipt {
    identity: SavedRootInitializerIdentity,
    read_dependencies: ProofDependencies,
    rebind_dependencies: ProofDependencies,
    calls: Vec<LocalCallReceipt>,
}

impl SavedLexicalBindingEffectReceipt {
    fn snapshot(receipt: &LexicalBindingEffectReceipt, ordinals: &ContextOrdinals) -> Result<Self> {
        Ok(Self {
            identity: SavedRootInitializerIdentity::snapshot(&receipt.identity, ordinals)?,
            read_dependencies: receipt.read_dependencies.clone(),
            rebind_dependencies: receipt.rebind_dependencies.clone(),
            calls: receipt.calls.clone(),
        })
    }

    fn restore(self, ordinals: &ContextOrdinals) -> Result<LexicalBindingEffectReceipt> {
        Ok(LexicalBindingEffectReceipt {
            identity: self.identity.restore(ordinals)?,
            read_dependencies: self.read_dependencies,
            rebind_dependencies: self.rebind_dependencies,
            calls: self.calls,
        })
    }
}

impl LexicalBindingEffectReceipt {
    pub(super) fn to_report(
        &self,
    ) -> analysis_vocabulary::report_schema::LexicalBindingEffectReceipt {
        use analysis_vocabulary::report_schema::{
            LexicalBindingEffectCall, LexicalBindingEffectReceipt,
        };

        LexicalBindingEffectReceipt {
            owner: self.identity.owner,
            binding: self.identity.binding.0.to_string(),
            initializer_span: self.identity.initializer_span,
            read_dependencies: self.read_dependencies.clone(),
            rebind_dependencies: self.rebind_dependencies.clone(),
            calls: self
                .calls
                .iter()
                .map(|call| LexicalBindingEffectCall {
                    caller: call.caller.to_report(),
                    call_span: call.call_span,
                    target: call.target.to_report(),
                    dependencies: call.dependencies.as_ref().clone(),
                })
                .collect(),
        }
    }
}

impl LocalBodyIdentity {
    fn to_report(&self) -> analysis_vocabulary::report_schema::LexicalBindingEffectBody {
        use analysis_vocabulary::report_schema::LexicalBindingEffectBody;

        match self {
            Self::RootInitializer {
                owner,
                initializer_span,
            } => LexicalBindingEffectBody::RootInitializer {
                owner: *owner,
                initializer_span: *initializer_span,
            },
            Self::Callable { key, owner } => LexicalBindingEffectBody::Callable {
                key: *key,
                owner: *owner,
            },
            Self::Constructor { class, owner } => LexicalBindingEffectBody::Constructor {
                class: *class,
                owner: *owner,
            },
            Self::Native {
                intrinsic,
                property,
            } => LexicalBindingEffectBody::Native {
                intrinsic: *intrinsic,
                property: property.clone(),
            },
            Self::Imported {
                defining_module,
                export,
            } => LexicalBindingEffectBody::Imported {
                defining_module: defining_module.clone(),
                export: export.clone(),
            },
        }
    }
}
