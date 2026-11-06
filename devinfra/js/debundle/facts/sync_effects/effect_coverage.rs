//! Nonterminal receipts from operations executed by the ordered local kernel.

use super::value_model::CallSite;
use crate::analysis_inputs::{NativeIntrinsic, ProofDependencies};
use crate::execution_evidence::{ExecutionPrecisionLoss, SkippedCallReason};
use crate::facts::abstract_domain::builtins::Row;
use crate::facts::semantic_inference::NativePremise;
use crate::reports::saved::SavedExecutionPrecisionLoss;
use crate::reports::schema::BindingEffectAuthorityReport;
use analysis_codec::{ContextOrdinals, SavedBinding};
use anyhow::Result;
use serde::{Deserialize, Serialize};
use source_identity::SourceUnitOrdinal;
use std::borrow::Cow;
use std::collections::BTreeMap;
use swc_common::Span;
use swc_ecma_ast::Id;

#[derive(Debug, Clone, Default, Eq, PartialEq, Serialize, Deserialize)]
pub(super) enum DomainCoverage {
    #[default]
    Unknown,
    Covered(ProofDependencies),
}

impl DomainCoverage {
    fn compose(&mut self, next: &Self) {
        match (&mut *self, next) {
            (Self::Covered(current), Self::Covered(next)) => current.extend(next),
            _ => *self = Self::Unknown,
        }
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
pub(super) enum Completion {
    #[default]
    Unknown,
    Normal,
    Abrupt,
}

/// Exact source root whose ordered execution may seal lexical binding effects.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct RootEffectIdentity {
    pub(super) owner: SourceUnitOrdinal,
    pub(super) source: RootEffectSource,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(super) enum RootEffectSource {
    BindingInitializer { binding: Id, initializer_span: Span },
    ExpressionStatement { expression_span: Span },
}

impl RootEffectIdentity {
    pub(super) fn binding_initializer(&self) -> Option<(&Id, Span)> {
        match &self.source {
            RootEffectSource::BindingInitializer {
                binding,
                initializer_span,
            } => Some((binding, *initializer_span)),
            RootEffectSource::ExpressionStatement { .. } => None,
        }
    }

    pub(super) fn body_identity(&self) -> LocalBodyIdentity {
        match self.source {
            RootEffectSource::BindingInitializer {
                initializer_span, ..
            } => LocalBodyIdentity::RootInitializer {
                owner: self.owner,
                initializer_span,
            },
            RootEffectSource::ExpressionStatement { expression_span } => {
                LocalBodyIdentity::RootExpression {
                    owner: self.owner,
                    expression_span,
                }
            }
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(super) enum LocalBodyIdentity {
    RootInitializer {
        owner: SourceUnitOrdinal,
        initializer_span: Span,
    },
    RootExpression {
        owner: SourceUnitOrdinal,
        expression_span: Span,
    },
    Callable {
        key: u32,
        owner: SourceUnitOrdinal,
    },
    ImportedCallable(Box<ImportedCallableIdentity>),
    Native {
        intrinsic: NativeIntrinsic,
        property: Option<String>,
    },
    NativeConstruction {
        intrinsic: NativeIntrinsic,
    },
}

/// The exact intrinsic selected by an executed callee GetValue. The ordered
/// caller transports this snapshot across operand evaluation to the row transfer.
#[derive(Debug, Clone)]
pub(super) struct NativeCalleeSelection {
    pub(super) owner: SourceUnitOrdinal,
    pub(super) site: CallSite,
    pub(super) row: &'static Row,
    pub(super) premise: NativePremise,
}

/// Kept out of every ordinary local edge's inline layout.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub(super) struct ImportedCallableIdentity {
    pub(super) defining_module: String,
    pub(super) export: String,
    pub(super) local: String,
    pub(super) member: Option<String>,
    pub(super) key: u32,
}

/// One selected direct local invocation. The flat edge set avoids expanding
/// every root-to-leaf path; each edge retains prerequisites merged from every
/// invocation context that selected it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(super) struct LocalCallReceipt {
    pub(super) caller: LocalBodyIdentity,
    pub(super) call_span: Span,
    pub(super) target: LocalBodyIdentity,
    pub(super) dependencies: Box<ProofDependencies>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(super) struct SkippedCallReceipt {
    pub(super) caller: LocalBodyIdentity,
    pub(super) call_span: Span,
    pub(super) region_span: Span,
    pub(super) frontier_span: Span,
    pub(super) reason: SkippedCallReason,
    pub(super) dependencies: Box<ProofDependencies>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct LexicalBindingEffectReceipt {
    pub(super) identity: RootEffectIdentity,
    pub(super) read_dependencies: ProofDependencies,
    pub(super) rebind_dependencies: ProofDependencies,
    pub(super) calls: Vec<LocalCallReceipt>,
    pub(super) skipped_calls: Vec<SkippedCallReceipt>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum BindingEffectCoverage {
    #[default]
    Unclassified,
    LegacyExecution,
    OrderedRoot {
        identity: RootEffectIdentity,
        receipt: Option<Box<LexicalBindingEffectReceipt>>,
    },
    RetainedEffectClosure {
        receipt: Box<LexicalBindingEffectReceipt>,
        dependencies: Box<ProofDependencies>,
        precision_losses: Vec<ExecutionPrecisionLoss>,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum BindingEffectAuthority {
    Unclassified,
    LegacyExecution { complete: bool },
    AuthorSourceOnly,
    OrderedRootUncovered,
    OperationReceipt,
    RetainedEffectClosure,
}

impl BindingEffectCoverage {
    pub(crate) fn snapshot(
        &self,
        ordinals: &ContextOrdinals,
    ) -> Result<SavedBindingEffectCoverage<'_>> {
        Ok(match self {
            Self::Unclassified => SavedBindingEffectCoverage::Unclassified,
            Self::LegacyExecution => SavedBindingEffectCoverage::LegacyExecution,
            Self::OrderedRoot { identity, receipt } => SavedBindingEffectCoverage::OrderedRoot {
                identity: SavedRootEffectIdentity::snapshot(identity, ordinals)?,
                receipt: receipt
                    .as_deref()
                    .map(|receipt| SavedLexicalBindingEffectReceipt::snapshot(receipt, ordinals))
                    .transpose()?
                    .map(Box::new),
            },
            Self::RetainedEffectClosure {
                receipt,
                dependencies,
                precision_losses,
            } => SavedBindingEffectCoverage::RetainedEffectClosure {
                receipt: Box::new(SavedLexicalBindingEffectReceipt::snapshot(
                    receipt, ordinals,
                )?),
                dependencies: dependencies.clone(),
                precision_losses: precision_losses
                    .iter()
                    .map(SavedExecutionPrecisionLoss::snapshot)
                    .collect(),
            },
        })
    }

    /// Actual lexical operation coverage for this exact root. Legacy
    /// completion and author source-only assertions supply no such proof.
    pub(crate) fn operation_dependencies(
        &self,
        owner: SourceUnitOrdinal,
    ) -> Option<ProofDependencies> {
        let (identity, receipt) = match self {
            Self::OrderedRoot {
                identity,
                receipt: Some(receipt),
            } => (identity, receipt),
            Self::RetainedEffectClosure { receipt, .. } => (&receipt.identity, receipt),
            _ => return None,
        };
        if identity.owner != owner || receipt.identity != *identity {
            return None;
        }
        let mut dependencies = receipt.read_dependencies.clone();
        dependencies.extend(&receipt.rebind_dependencies);
        for call in &receipt.calls {
            dependencies.extend(&call.dependencies);
        }
        for call in &receipt.skipped_calls {
            dependencies.extend(&call.dependencies);
        }
        Some(dependencies)
    }

    pub(crate) fn retained_effect_dependencies(
        &self,
        owner: SourceUnitOrdinal,
        losses: &[ExecutionPrecisionLoss],
    ) -> Option<&ProofDependencies> {
        let Self::RetainedEffectClosure {
            receipt,
            dependencies,
            precision_losses,
        } = self
        else {
            return None;
        };
        (receipt.identity.owner == owner
            && !losses.is_empty()
            && losses.len() == precision_losses.len()
            && losses
                .iter()
                .all(|loss| precision_losses.iter().any(|held| same_loss(held, loss))))
        .then_some(dependencies)
    }

    pub(crate) fn authority(
        &self,
        complete: bool,
        author_source_only: bool,
    ) -> BindingEffectAuthority {
        if author_source_only {
            return BindingEffectAuthority::AuthorSourceOnly;
        }
        match self {
            Self::Unclassified => BindingEffectAuthority::Unclassified,
            Self::LegacyExecution => BindingEffectAuthority::LegacyExecution { complete },
            Self::OrderedRoot {
                receipt: Some(_), ..
            } => BindingEffectAuthority::OperationReceipt,
            Self::OrderedRoot { receipt: None, .. } => BindingEffectAuthority::OrderedRootUncovered,
            Self::RetainedEffectClosure { .. } => BindingEffectAuthority::RetainedEffectClosure,
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
                let Self::OrderedRoot {
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
            BindingEffectAuthority::OrderedRootUncovered => {
                let Self::OrderedRoot { identity, .. } = self else {
                    unreachable!("typed authority comes from this root attempt");
                };
                match &identity.source {
                    RootEffectSource::BindingInitializer {
                        binding,
                        initializer_span,
                    } => BindingEffectAuthorityReport::OrderedRootInitializerUncovered {
                        owner: identity.owner,
                        binding: binding.0.to_string(),
                        initializer_span: Some(*initializer_span),
                    },
                    RootEffectSource::ExpressionStatement { expression_span } => {
                        BindingEffectAuthorityReport::OrderedRootExpressionUncovered {
                            owner: identity.owner,
                            expression_span: *expression_span,
                        }
                    }
                }
            }
            BindingEffectAuthority::RetainedEffectClosure => {
                let Self::RetainedEffectClosure {
                    receipt,
                    dependencies,
                    precision_losses,
                } = self
                else {
                    unreachable!("retained authority comes from its complete closure");
                };
                BindingEffectAuthorityReport::RetainedEffectClosure {
                    receipt: Box::new(receipt.to_report()),
                    dependencies: dependencies.as_ref().clone(),
                    precision_losses: precision_losses.clone(),
                }
            }
        }
    }
}

/// Process-independent binding-effect authority retained by staged analysis.
#[derive(Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum SavedBindingEffectCoverage<'a> {
    Unclassified,
    LegacyExecution,
    OrderedRoot {
        identity: SavedRootEffectIdentity,
        receipt: Option<Box<SavedLexicalBindingEffectReceipt>>,
    },
    RetainedEffectClosure {
        receipt: Box<SavedLexicalBindingEffectReceipt>,
        dependencies: Box<ProofDependencies>,
        precision_losses: Vec<SavedExecutionPrecisionLoss<'a>>,
    },
}

impl SavedBindingEffectCoverage<'_> {
    pub(crate) fn restore(self, ordinals: &ContextOrdinals) -> Result<BindingEffectCoverage> {
        Ok(match self {
            Self::Unclassified => BindingEffectCoverage::Unclassified,
            Self::LegacyExecution => BindingEffectCoverage::LegacyExecution,
            Self::OrderedRoot { identity, receipt } => BindingEffectCoverage::OrderedRoot {
                identity: identity.restore(ordinals)?,
                receipt: receipt
                    .map(|receipt| receipt.restore(ordinals))
                    .transpose()?
                    .map(Box::new),
            },
            Self::RetainedEffectClosure {
                receipt,
                dependencies,
                precision_losses,
            } => BindingEffectCoverage::RetainedEffectClosure {
                receipt: Box::new(receipt.restore(ordinals)?),
                dependencies,
                precision_losses: precision_losses
                    .into_iter()
                    .map(SavedExecutionPrecisionLoss::restore)
                    .collect(),
            },
        })
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct SavedRootEffectIdentity {
    owner: SourceUnitOrdinal,
    source: SavedRootEffectSource,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
enum SavedRootEffectSource {
    BindingInitializer {
        binding: SavedBinding,
        initializer_span: Span,
    },
    ExpressionStatement {
        expression_span: Span,
    },
}

impl SavedRootEffectIdentity {
    fn snapshot(identity: &RootEffectIdentity, ordinals: &ContextOrdinals) -> Result<Self> {
        Ok(Self {
            owner: identity.owner,
            source: match &identity.source {
                RootEffectSource::BindingInitializer {
                    binding,
                    initializer_span,
                } => SavedRootEffectSource::BindingInitializer {
                    binding: ordinals.snapshot_binding(binding)?,
                    initializer_span: *initializer_span,
                },
                RootEffectSource::ExpressionStatement { expression_span } => {
                    SavedRootEffectSource::ExpressionStatement {
                        expression_span: *expression_span,
                    }
                }
            },
        })
    }

    fn restore(self, ordinals: &ContextOrdinals) -> Result<RootEffectIdentity> {
        Ok(RootEffectIdentity {
            owner: self.owner,
            source: match self.source {
                SavedRootEffectSource::BindingInitializer {
                    binding,
                    initializer_span,
                } => RootEffectSource::BindingInitializer {
                    binding: ordinals.restore_binding(binding)?,
                    initializer_span,
                },
                SavedRootEffectSource::ExpressionStatement { expression_span } => {
                    RootEffectSource::ExpressionStatement { expression_span }
                }
            },
        })
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct SavedLexicalBindingEffectReceipt {
    identity: SavedRootEffectIdentity,
    read_dependencies: ProofDependencies,
    rebind_dependencies: ProofDependencies,
    calls: Vec<LocalCallReceipt>,
    skipped_calls: Vec<SkippedCallReceipt>,
}

impl SavedLexicalBindingEffectReceipt {
    fn snapshot(receipt: &LexicalBindingEffectReceipt, ordinals: &ContextOrdinals) -> Result<Self> {
        Ok(Self {
            identity: SavedRootEffectIdentity::snapshot(&receipt.identity, ordinals)?,
            read_dependencies: receipt.read_dependencies.clone(),
            rebind_dependencies: receipt.rebind_dependencies.clone(),
            calls: receipt.calls.clone(),
            skipped_calls: receipt.skipped_calls.clone(),
        })
    }

    fn restore(self, ordinals: &ContextOrdinals) -> Result<LexicalBindingEffectReceipt> {
        Ok(LexicalBindingEffectReceipt {
            identity: self.identity.restore(ordinals)?,
            read_dependencies: self.read_dependencies,
            rebind_dependencies: self.rebind_dependencies,
            calls: self.calls,
            skipped_calls: self.skipped_calls,
        })
    }
}

impl LexicalBindingEffectReceipt {
    pub(super) fn to_report(&self) -> crate::reports::schema::LexicalBindingEffectReceipt {
        use crate::reports::schema::{
            LexicalBindingEffectCall, LexicalBindingEffectReceipt, LexicalBindingEffectSkippedCall,
            LexicalEffectRoot,
        };

        LexicalBindingEffectReceipt {
            owner: self.identity.owner,
            root: match &self.identity.source {
                RootEffectSource::BindingInitializer {
                    binding,
                    initializer_span,
                } => LexicalEffectRoot::BindingInitializer {
                    binding: binding.0.to_string(),
                    initializer_span: *initializer_span,
                },
                RootEffectSource::ExpressionStatement { expression_span } => {
                    LexicalEffectRoot::ExpressionStatement {
                        expression_span: *expression_span,
                    }
                }
            },
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
            skipped_calls: self
                .skipped_calls
                .iter()
                .map(|call| LexicalBindingEffectSkippedCall {
                    caller: call.caller.to_report(),
                    call_span: call.call_span,
                    region_span: call.region_span,
                    frontier_span: call.frontier_span,
                    reason: call.reason,
                    dependencies: call.dependencies.as_ref().clone(),
                })
                .collect(),
        }
    }
}

impl LocalBodyIdentity {
    fn to_report(&self) -> crate::reports::schema::LexicalBindingEffectBody {
        use crate::reports::schema::LexicalBindingEffectBody;
        match self {
            Self::RootInitializer {
                owner,
                initializer_span,
            } => LexicalBindingEffectBody::RootInitializer {
                owner: *owner,
                initializer_span: *initializer_span,
            },
            Self::RootExpression {
                owner,
                expression_span,
            } => LexicalBindingEffectBody::RootExpression {
                owner: *owner,
                expression_span: *expression_span,
            },
            Self::Callable { key, owner } => LexicalBindingEffectBody::Callable {
                key: *key,
                owner: *owner,
            },
            Self::ImportedCallable(identity) => LexicalBindingEffectBody::ImportedCallable {
                defining_module: identity.defining_module.clone(),
                export: identity.export.clone(),
                local: identity.local.clone(),
                member: identity.member.clone(),
                key: identity.key,
            },
            Self::Native {
                intrinsic,
                property,
            } => LexicalBindingEffectBody::Native {
                intrinsic: *intrinsic,
                property: property.clone(),
            },
            Self::NativeConstruction { intrinsic } => {
                LexicalBindingEffectBody::NativeConstruction {
                    intrinsic: *intrinsic,
                }
            }
        }
    }
}

/// This describes only the operations of one execution. It never identifies
/// or certifies a whole source owner, and may travel through invocation memos.
#[derive(Debug, Clone, Default, Eq, PartialEq, Serialize, Deserialize)]
pub(crate) struct EffectCoverageFragment {
    initialization: Option<Box<InitializationEffectCoverage>>,
    lexical: Option<Box<LexicalEffectCoverage>>,
    completion: Completion,
    call_observations: Option<Box<CallObservationCoverage>>,
    /// Loss sites whose retained suffix was covered through the same kernel.
    /// This is not an execution-completion or returned-value certificate.
    retained_losses: Option<Box<RetainedLossCoverage>>,
}

#[derive(Debug, Clone, Default, Eq, PartialEq, Serialize, Deserialize)]
struct RetainedLossCoverage {
    sites: Vec<ExecutionPrecisionLoss>,
}

#[derive(Serialize, Deserialize)]
pub(crate) struct SavedEffectCoverageFragment<'a> {
    initialization: Cow<'a, Option<Box<InitializationEffectCoverage>>>,
    lexical: Cow<'a, Option<Box<LexicalEffectCoverage>>>,
    completion: Completion,
    call_observations: Cow<'a, Option<Box<CallObservationCoverage>>>,
    retained_losses: Option<Vec<SavedExecutionPrecisionLoss<'a>>>,
}

impl<'a> SavedEffectCoverageFragment<'a> {
    pub(crate) fn snapshot(fragment: &'a EffectCoverageFragment) -> Self {
        Self {
            initialization: Cow::Borrowed(&fragment.initialization),
            lexical: Cow::Borrowed(&fragment.lexical),
            completion: fragment.completion,
            call_observations: Cow::Borrowed(&fragment.call_observations),
            retained_losses: fragment.retained_losses.as_ref().map(|losses| {
                losses
                    .sites
                    .iter()
                    .map(SavedExecutionPrecisionLoss::snapshot)
                    .collect()
            }),
        }
    }

    pub(crate) fn restore(self) -> EffectCoverageFragment {
        EffectCoverageFragment {
            initialization: self.initialization.into_owned(),
            lexical: self.lexical.into_owned(),
            completion: self.completion,
            call_observations: self.call_observations.into_owned(),
            retained_losses: self.retained_losses.map(|losses| {
                Box::new(RetainedLossCoverage {
                    sites: losses
                        .into_iter()
                        .map(SavedExecutionPrecisionLoss::restore)
                        .collect(),
                })
            }),
        }
    }
}

/// The three existing initialization domains remain independent, but are
/// grouped behind one pointer so every recursive fragment does not carry
/// three inline proof-dependency payloads.
#[derive(Debug, Clone, Default, Eq, PartialEq, Serialize, Deserialize)]
struct InitializationEffectCoverage {
    global_properties: DomainCoverage,
    general_heap: DomainCoverage,
    host_effects: DomainCoverage,
}

/// Kept behind one pointer because `ProofDependencies` contains several
/// inline set headers. This data is absent on the conservative default path,
/// which is common in deep ordinary call execution.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
struct LexicalEffectCoverage {
    reads: DomainCoverage,
    rebinds: DomainCoverage,
}

/// Call-site observations outlive lexical effect authority: composing an
/// unknown effect fragment must not erase already observed source sites.
#[derive(Debug, Clone, Default, Eq, PartialEq, Serialize, Deserialize)]
struct CallObservationCoverage {
    #[serde(
        serialize_with = "serialize_calls",
        deserialize_with = "deserialize_calls"
    )]
    calls: BTreeMap<LocalCallKey, LocalCallReceipt>,
    #[serde(
        serialize_with = "serialize_skipped_calls",
        deserialize_with = "deserialize_skipped_calls"
    )]
    skipped_calls: BTreeMap<LocalSkippedCallKey, SkippedCallReceipt>,
}

fn serialize_calls<S: serde::Serializer>(
    calls: &BTreeMap<LocalCallKey, LocalCallReceipt>,
    serializer: S,
) -> Result<S::Ok, S::Error> {
    calls.values().collect::<Vec<_>>().serialize(serializer)
}

fn deserialize_calls<'de, D: serde::Deserializer<'de>>(
    deserializer: D,
) -> Result<BTreeMap<LocalCallKey, LocalCallReceipt>, D::Error> {
    let mut calls = BTreeMap::<LocalCallKey, LocalCallReceipt>::new();
    for call in Vec::<LocalCallReceipt>::deserialize(deserializer)? {
        calls
            .entry(local_call_key(&call))
            .and_modify(|held| held.dependencies.extend(&call.dependencies))
            .or_insert(call);
    }
    Ok(calls)
}

fn serialize_skipped_calls<S: serde::Serializer>(
    calls: &BTreeMap<LocalSkippedCallKey, SkippedCallReceipt>,
    serializer: S,
) -> Result<S::Ok, S::Error> {
    calls.values().collect::<Vec<_>>().serialize(serializer)
}

fn deserialize_skipped_calls<'de, D: serde::Deserializer<'de>>(
    deserializer: D,
) -> Result<BTreeMap<LocalSkippedCallKey, SkippedCallReceipt>, D::Error> {
    let mut calls = BTreeMap::<LocalSkippedCallKey, SkippedCallReceipt>::new();
    for call in Vec::<SkippedCallReceipt>::deserialize(deserializer)? {
        calls
            .entry(local_skipped_call_key(&call))
            .and_modify(|held| held.dependencies.extend(&call.dependencies))
            .or_insert(call);
    }
    Ok(calls)
}

impl EffectCoverageFragment {
    /// No unaccounted effect in these domains. Concrete cell effects, such as
    /// an initialized declarative read, belong in the caller's ledger.
    /// Source identity and actual binding effects remain separate evidence.
    pub(super) fn no_effects() -> Self {
        Self {
            initialization: Some(Box::new(InitializationEffectCoverage {
                global_properties: DomainCoverage::Covered(ProofDependencies::default()),
                general_heap: DomainCoverage::Covered(ProofDependencies::default()),
                host_effects: DomainCoverage::Covered(ProofDependencies::default()),
            })),
            lexical: Some(Box::new(LexicalEffectCoverage {
                reads: DomainCoverage::Covered(ProofDependencies::default()),
                rebinds: DomainCoverage::Covered(ProofDependencies::default()),
            })),
            completion: Completion::Normal,
            call_observations: None,
            retained_losses: None,
        }
    }

    /// Checked native body with no application callback, accessor, host
    /// operation or exterior write. Callee and operand evaluation remain with
    /// the caller; this receipt grants no lexical binding-effect authority.
    pub(super) fn native_operation(dependencies: &ProofDependencies) -> Self {
        Self {
            initialization: Some(Box::new(InitializationEffectCoverage {
                global_properties: DomainCoverage::Covered(dependencies.clone()),
                general_heap: DomainCoverage::Covered(dependencies.clone()),
                host_effects: DomainCoverage::Covered(dependencies.clone()),
            })),
            // Native operands are outside the bounded lexical migration.
            lexical: None,
            completion: Completion::Normal,
            call_observations: None,
            retained_losses: None,
        }
    }

    /// Actual ordinary native iteration over a proved primitive source.
    /// Its protocol/contents reads and fresh internal allocation are modeled;
    /// this is body coverage, not completion of operand evaluation.
    pub(super) fn native_iterable_protocol(dependencies: &ProofDependencies) -> Self {
        Self::native_operation(dependencies)
    }

    /// Ordered local execution also owns the operand reads and exact lexical
    /// call edge. Preserve that coverage while certifying the native protocol
    /// body with the same dependencies as the ordinary iterable receipt.
    pub(super) fn ordered_native_iterable_protocol(dependencies: &ProofDependencies) -> Self {
        let mut fragment = Self::no_effects();
        fragment.depend_on(dependencies);
        fragment
    }

    /// A top-level module-cell read has no global/heap/host effect, and its
    /// lexical read/rebind behavior is accounted for by the caller.
    pub(super) fn initialized_module_read() -> Self {
        let mut fragment = Self::no_effects();
        fragment
            .lexical
            .as_mut()
            .expect("no-effects fragment carries known lexical domains")
            .reads = DomainCoverage::Covered(ProofDependencies::default());
        fragment
    }

    /// A checked present own-data access has no implicit accessor, prototype,
    /// global or host operation. Its actual property cell must be recorded in
    /// the heap ledger separately; receiver/RHS evaluation composes separately.
    /// Writes additionally require the current-prefix writable witness.
    pub(super) fn modeled_own_data_access() -> Self {
        Self::no_effects()
    }

    /// Proof prerequisites for a successfully consumed own-data selection.
    /// Unknown lexical coverage never becomes positive through dependencies.
    pub(super) fn lexical_selection_dependencies(&self) -> Option<Box<ProofDependencies>> {
        if self.completion != Completion::Normal {
            return None;
        }
        let lexical = self.lexical.as_ref()?;
        let DomainCoverage::Covered(reads) = &lexical.reads else {
            return None;
        };
        let DomainCoverage::Covered(rebinds) = &lexical.rebinds else {
            return None;
        };
        let mut dependencies = Box::<ProofDependencies>::default();
        dependencies.extend(reads);
        dependencies.extend(rebinds);
        Some(dependencies)
    }

    pub(super) fn compose(&mut self, next: &Self) {
        if let Some(losses) = &next.retained_losses {
            for loss in &losses.sites {
                self.retain_loss(loss.clone());
            }
        }
        match (&mut self.initialization, &next.initialization) {
            (Some(current), Some(next)) => {
                current.global_properties.compose(&next.global_properties);
                current.general_heap.compose(&next.general_heap);
                current.host_effects.compose(&next.host_effects);
            }
            _ => self.initialization = None,
        }
        match (&mut self.lexical, &next.lexical) {
            (Some(current), Some(next)) => {
                current.reads.compose(&next.reads);
                current.rebinds.compose(&next.rebinds);
            }
            _ => self.lexical = None,
        }
        if let Some(next_observations) = &next.call_observations {
            let current = self
                .call_observations
                .get_or_insert_with(|| Box::new(CallObservationCoverage::default()));
            for (key, next_call) in &next_observations.calls {
                current
                    .calls
                    .entry(key.clone())
                    .and_modify(|call| call.dependencies.extend(&next_call.dependencies))
                    .or_insert_with(|| next_call.clone());
            }
            for (key, next_call) in &next_observations.skipped_calls {
                current
                    .skipped_calls
                    .entry(key.clone())
                    .and_modify(|call| call.dependencies.extend(&next_call.dependencies))
                    .or_insert_with(|| next_call.clone());
            }
        }
        self.completion = match (self.completion, next.completion) {
            (Completion::Unknown, _) | (_, Completion::Unknown) => Completion::Unknown,
            (Completion::Abrupt, _) | (_, Completion::Abrupt) => Completion::Abrupt,
            (Completion::Normal, Completion::Normal) => Completion::Normal,
        };
    }

    /// Prerequisites of selecting an already covered operation. Conditions
    /// never turn an unknown domain into a covered one.
    pub(super) fn depend_on(&mut self, dependencies: &ProofDependencies) {
        if let Some(initialization) = &mut self.initialization {
            for domain in [
                &mut initialization.global_properties,
                &mut initialization.general_heap,
                &mut initialization.host_effects,
            ] {
                if let DomainCoverage::Covered(current) = domain {
                    current.extend(dependencies);
                }
            }
        }
        if let Some(lexical) = &mut self.lexical {
            for domain in [&mut lexical.reads, &mut lexical.rebinds] {
                if let DomainCoverage::Covered(current) = domain {
                    current.extend(dependencies);
                }
            }
        }
        if let Some(observations) = &mut self.call_observations {
            for call in observations.calls.values_mut() {
                call.dependencies.extend(dependencies);
            }
            for call in observations.skipped_calls.values_mut() {
                call.dependencies.extend(dependencies);
            }
        }
    }

    /// Add one selected call edge without expanding child paths.
    pub(super) fn selected_local_call(&mut self, call: LocalCallReceipt) {
        let observations = self
            .call_observations
            .get_or_insert_with(|| Box::new(CallObservationCoverage::default()));
        observations
            .calls
            .entry(local_call_key(&call))
            .and_modify(|existing| existing.dependencies.extend(&call.dependencies))
            .or_insert(call);
    }

    pub(super) fn skipped_local_call(&mut self, call: SkippedCallReceipt) {
        let observations = self
            .call_observations
            .get_or_insert_with(|| Box::new(CallObservationCoverage::default()));
        observations
            .skipped_calls
            .entry(local_skipped_call_key(&call))
            .and_modify(|existing| existing.dependencies.extend(&call.dependencies))
            .or_insert(call);
    }

    /// Whether this exact source site has either invocation or skip evidence.
    pub(super) fn has_local_call(&self, caller: &LocalBodyIdentity, call_span: Span) -> bool {
        let Some(observations) = &self.call_observations else {
            return false;
        };
        observations
            .calls
            .values()
            .any(|call| call.caller == *caller && call.call_span == call_span)
            || observations
                .skipped_calls
                .values()
                .any(|call| call.caller == *caller && call.call_span == call_span)
    }

    /// Exact union of invoked and skipped sites for one source body.
    pub(super) fn has_call_inventory(
        &self,
        caller: &LocalBodyIdentity,
        expected: &std::collections::BTreeSet<(u32, u32)>,
    ) -> bool {
        let Some(observations) = &self.call_observations else {
            return expected.is_empty();
        };
        let observed: std::collections::BTreeSet<_> = observations
            .calls
            .values()
            .filter(|call| call.caller == *caller)
            .map(|call| (call.call_span.lo.0, call.call_span.hi.0))
            .chain(
                observations
                    .skipped_calls
                    .values()
                    .filter(|call| call.caller == *caller)
                    .map(|call| (call.call_span.lo.0, call.call_span.hi.0)),
            )
            .collect();
        &observed == expected
    }

    pub(super) fn lexical_effect_dependencies(
        &self,
    ) -> Option<(ProofDependencies, ProofDependencies)> {
        if self.completion != Completion::Normal {
            return None;
        }
        let lexical = self.lexical.as_ref()?;
        let DomainCoverage::Covered(read_dependencies) = &lexical.reads else {
            return None;
        };
        let DomainCoverage::Covered(rebind_dependencies) = &lexical.rebinds else {
            return None;
        };
        Some((read_dependencies.clone(), rebind_dependencies.clone()))
    }

    pub(super) fn closed_effect_dependencies(&self) -> Option<ProofDependencies> {
        let mut dependencies = self.normal_dependencies()?;
        let (reads, rebinds) = self.lexical_effect_dependencies()?;
        dependencies.extend(&reads);
        dependencies.extend(&rebinds);
        if let Some(observations) = &self.call_observations {
            for call in observations.calls.values() {
                dependencies.extend(&call.dependencies);
            }
            for call in observations.skipped_calls.values() {
                dependencies.extend(&call.dependencies);
            }
        }
        Some(dependencies)
    }

    /// Dependencies for consuming a fully covered abrupt alternative.
    /// The caller must independently know this is an actual known throw, not
    /// a return or mixed/unknown completion.
    pub(super) fn known_abrupt_dependencies(&self) -> Option<ProofDependencies> {
        if self.completion != Completion::Abrupt {
            return None;
        }
        let initialization = self.initialization.as_ref()?;
        let lexical = self.lexical.as_ref()?;
        let mut dependencies = ProofDependencies::default();
        for domain in [
            &initialization.global_properties,
            &initialization.general_heap,
            &initialization.host_effects,
            &lexical.reads,
            &lexical.rebinds,
        ] {
            let DomainCoverage::Covered(domain_dependencies) = domain else {
                return None;
            };
            dependencies.extend(domain_dependencies);
        }
        if let Some(observations) = &self.call_observations {
            for call in observations.calls.values() {
                dependencies.extend(&call.dependencies);
            }
            for call in observations.skipped_calls.values() {
                dependencies.extend(&call.dependencies);
            }
        }
        Some(dependencies)
    }

    /// Turn a fully covered abrupt fragment normal after its known throw is
    /// consumed by the matching handler. Call only for a known-throw path.
    pub(super) fn handle_known_throw(&mut self) -> bool {
        if self.known_abrupt_dependencies().is_none() {
            return false;
        }
        self.completion = Completion::Normal;
        true
    }

    pub(super) fn has_closed_effects(&self) -> bool {
        let (Some(initialization), Some(lexical)) = (&self.initialization, &self.lexical) else {
            return false;
        };
        self.completion == Completion::Normal
            && [
                &initialization.global_properties,
                &initialization.general_heap,
                &initialization.host_effects,
                &lexical.reads,
                &lexical.rebinds,
            ]
            .into_iter()
            .all(|domain| matches!(domain, DomainCoverage::Covered(_)))
    }

    pub(super) fn retain_loss(&mut self, loss: ExecutionPrecisionLoss) {
        let losses = self.retained_losses.get_or_insert_with(Box::default);
        if !losses.sites.iter().any(|held| same_loss(held, &loss)) {
            losses.sites.push(loss);
        }
    }

    pub(super) fn covers_retained_losses(&self, losses: &[ExecutionPrecisionLoss]) -> bool {
        !losses.is_empty()
            && self.has_closed_effects()
            && self.retained_losses.as_ref().is_some_and(|covered| {
                losses
                    .iter()
                    .all(|loss| covered.sites.iter().any(|held| same_loss(held, loss)))
            })
    }

    pub(super) fn seal_lexical_binding_effects(
        &self,
        identity: RootEffectIdentity,
        expected_root_calls: &std::collections::BTreeSet<(u32, u32)>,
    ) -> Option<LexicalBindingEffectReceipt> {
        let (read_dependencies, rebind_dependencies) = self.lexical_effect_dependencies()?;
        let root_body = identity.body_identity();
        if !self.has_call_inventory(&root_body, expected_root_calls) {
            return None;
        }
        let observations = self.call_observations.as_deref();

        // A receipt can contain only edges reached while executing this root.
        // Check the flattened caller/target relation iteratively, without
        // reconstructing exponentially many invocation paths or repeatedly
        // scanning every edge for each newly reached body.
        let mut adjacency = std::collections::BTreeMap::<
            LocalBodyKey,
            std::collections::BTreeSet<LocalBodyKey>,
        >::new();
        for call in observations
            .into_iter()
            .flat_map(|calls| calls.calls.values())
        {
            adjacency
                .entry(local_body_key(&call.caller))
                .or_default()
                .insert(local_body_key(&call.target));
        }
        let root_key = local_body_key(&root_body);
        let mut reachable_bodies = std::collections::BTreeSet::from([root_key.clone()]);
        let mut pending = vec![root_key];
        while let Some(body) = pending.pop() {
            if let Some(targets) = adjacency.get(&body) {
                for target in targets {
                    if reachable_bodies.insert(target.clone()) {
                        pending.push(target.clone());
                    }
                }
            }
        }
        if observations.into_iter().any(|observations| {
            observations
                .calls
                .values()
                .any(|call| !reachable_bodies.contains(&local_body_key(&call.caller)))
                || observations
                    .skipped_calls
                    .values()
                    .any(|call| !reachable_bodies.contains(&local_body_key(&call.caller)))
        }) {
            return None;
        }
        Some(LexicalBindingEffectReceipt {
            identity,
            read_dependencies,
            rebind_dependencies,
            calls: observations
                .into_iter()
                .flat_map(|observations| observations.calls.values().cloned())
                .collect(),
            skipped_calls: observations
                .into_iter()
                .flat_map(|observations| observations.skipped_calls.values().cloned())
                .collect(),
        })
    }

    pub(super) fn abrupt(&mut self) {
        self.completion = Completion::Abrupt;
    }

    pub(super) fn uncertain_completion(&mut self) {
        self.completion = Completion::Unknown;
    }

    pub(super) fn normal_dependencies(&self) -> Option<ProofDependencies> {
        if self.completion != Completion::Normal {
            return None;
        }
        let initialization = self.initialization.as_ref()?;
        let mut dependencies = ProofDependencies::default();
        for domain in [
            &initialization.global_properties,
            &initialization.general_heap,
            &initialization.host_effects,
        ] {
            let DomainCoverage::Covered(domain_dependencies) = domain else {
                return None;
            };
            dependencies.extend(domain_dependencies);
        }
        Some(dependencies)
    }
}

fn same_loss(first: &ExecutionPrecisionLoss, later: &ExecutionPrecisionLoss) -> bool {
    first.statement_ordinal == later.statement_ordinal
        && first.span == later.span
        && first.reason == later.reason
        && first.reach == later.reach
}

/// Compact ordered identity for the edge map and iterative connectivity check.
/// Spans are represented by their exact byte coordinates because SWC `Span`
/// itself does not implement `Ord`.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
enum LocalBodyKey {
    RootInitializer {
        owner: usize,
        lo: u32,
        hi: u32,
    },
    RootExpression {
        owner: usize,
        lo: u32,
        hi: u32,
    },
    Callable {
        key: u32,
        owner: usize,
    },
    ImportedCallable(Box<ImportedCallableIdentity>),
    Native {
        intrinsic: NativeIntrinsic,
        property: Option<String>,
    },
    NativeConstruction {
        intrinsic: NativeIntrinsic,
    },
}

/// Exact identity for a flat local call edge. Span coordinates are used
/// because SWC `Span` itself does not implement `Ord`.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
struct LocalCallKey {
    caller: LocalBodyKey,
    call_lo: u32,
    call_hi: u32,
    target: LocalBodyKey,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
struct LocalSkippedCallKey {
    caller: LocalBodyKey,
    call_lo: u32,
    call_hi: u32,
    region_lo: u32,
    region_hi: u32,
    frontier_lo: u32,
    frontier_hi: u32,
    reason: SkippedCallReason,
}

fn local_body_key(body: &LocalBodyIdentity) -> LocalBodyKey {
    match body {
        LocalBodyIdentity::RootInitializer {
            owner,
            initializer_span,
        } => LocalBodyKey::RootInitializer {
            owner: owner.0,
            lo: initializer_span.lo.0,
            hi: initializer_span.hi.0,
        },
        LocalBodyIdentity::RootExpression {
            owner,
            expression_span,
        } => LocalBodyKey::RootExpression {
            owner: owner.0,
            lo: expression_span.lo.0,
            hi: expression_span.hi.0,
        },
        LocalBodyIdentity::Callable { key, owner } => LocalBodyKey::Callable {
            key: *key,
            owner: owner.0,
        },
        LocalBodyIdentity::ImportedCallable(identity) => {
            LocalBodyKey::ImportedCallable(identity.clone())
        }
        LocalBodyIdentity::Native {
            intrinsic,
            property,
        } => LocalBodyKey::Native {
            intrinsic: *intrinsic,
            property: property.clone(),
        },
        LocalBodyIdentity::NativeConstruction { intrinsic } => LocalBodyKey::NativeConstruction {
            intrinsic: *intrinsic,
        },
    }
}

fn local_call_key(call: &LocalCallReceipt) -> LocalCallKey {
    LocalCallKey {
        caller: local_body_key(&call.caller),
        call_lo: call.call_span.lo.0,
        call_hi: call.call_span.hi.0,
        target: local_body_key(&call.target),
    }
}

fn local_skipped_call_key(call: &SkippedCallReceipt) -> LocalSkippedCallKey {
    LocalSkippedCallKey {
        caller: local_body_key(&call.caller),
        call_lo: call.call_span.lo.0,
        call_hi: call.call_span.hi.0,
        region_lo: call.region_span.lo.0,
        region_hi: call.region_span.hi.0,
        frontier_lo: call.frontier_span.lo.0,
        frontier_hi: call.frontier_span.hi.0,
        reason: call.reason,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::analysis_inputs::{ProgramPremiseKey, ValueInputRef};
    use swc_common::BytePos;

    fn test_callable(key: u32, owner: usize) -> LocalBodyIdentity {
        LocalBodyIdentity::Callable {
            key,
            owner: SourceUnitOrdinal(owner),
        }
    }

    fn test_call(
        caller: LocalBodyIdentity,
        lo: u32,
        hi: u32,
        target: LocalBodyIdentity,
        dependencies: ProofDependencies,
    ) -> LocalCallReceipt {
        LocalCallReceipt {
            caller,
            call_span: Span::new(BytePos(lo), BytePos(hi)),
            target,
            dependencies: Box::new(dependencies),
        }
    }

    fn test_skipped_call(
        caller: LocalBodyIdentity,
        lo: u32,
        hi: u32,
        region: (u32, u32),
        frontier: (u32, u32),
        reason: SkippedCallReason,
        dependencies: ProofDependencies,
    ) -> SkippedCallReceipt {
        SkippedCallReceipt {
            caller,
            call_span: Span::new(BytePos(lo), BytePos(hi)),
            region_span: Span::new(BytePos(region.0), BytePos(region.1)),
            frontier_span: Span::new(BytePos(frontier.0), BytePos(frontier.1)),
            reason,
            dependencies: Box::new(dependencies),
        }
    }

    fn captured_value_input(input_index: usize) -> ProofDependencies {
        ProofDependencies {
            value_inputs: std::collections::BTreeSet::from([ValueInputRef {
                defining_chunk: "static/provider".to_string(),
                input_index,
            }]),
            ..ProofDependencies::default()
        }
    }

    #[test]
    fn default_binding_effect_authority_is_unknown() {
        assert_eq!(
            BindingEffectCoverage::default().authority(true, false),
            BindingEffectAuthority::Unclassified,
            "programmatic default remains fail-closed"
        );
    }

    #[test]
    fn domains_compose_independently_and_unknown_is_absorbing() {
        let mut first = EffectCoverageFragment::no_effects();
        let initialization = first.initialization.as_mut().unwrap();
        let DomainCoverage::Covered(dependencies) = &mut initialization.global_properties else {
            unreachable!();
        };
        dependencies
            .program_premises
            .insert(ProgramPremiseKey::IntrinsicsNeverWritten);
        let mut second = EffectCoverageFragment::no_effects();
        second.initialization.as_mut().unwrap().general_heap = DomainCoverage::Unknown;
        first.compose(&second);
        assert!(
            matches!(&first.initialization.as_ref().unwrap().global_properties, DomainCoverage::Covered(dependencies)
            if dependencies.program_premises.contains(&ProgramPremiseKey::IntrinsicsNeverWritten))
        );
        assert!(matches!(
            &first.initialization.as_ref().unwrap().general_heap,
            DomainCoverage::Unknown
        ));
        assert!(matches!(
            &first.initialization.as_ref().unwrap().host_effects,
            DomainCoverage::Covered(_)
        ));
        assert!(first.normal_dependencies().is_none());
        first.compose(&EffectCoverageFragment::no_effects());
        assert!(matches!(
            &first.initialization.as_ref().unwrap().general_heap,
            DomainCoverage::Unknown
        ));
    }

    #[test]
    fn prerequisites_cannot_create_a_positive_receipt() {
        let mut dependencies = ProofDependencies::default();
        dependencies
            .program_premises
            .insert(ProgramPremiseKey::IntrinsicsNeverWritten);
        let mut unknown = EffectCoverageFragment::default();
        unknown.depend_on(&dependencies);
        assert!(unknown.normal_dependencies().is_none());
        assert!(unknown.initialization.is_none());
        let mut covered = EffectCoverageFragment::no_effects();
        covered.depend_on(&dependencies);
        assert_eq!(covered.normal_dependencies(), Some(dependencies));
    }

    #[test]
    fn default_and_abrupt_fragments_cannot_seal_normal_initialization() {
        assert!(
            EffectCoverageFragment::default()
                .normal_dependencies()
                .is_none()
        );
        let mut fragment = EffectCoverageFragment::no_effects();
        assert!(fragment.normal_dependencies().is_some());
        fragment.abrupt();
        fragment.compose(&EffectCoverageFragment::no_effects());
        assert!(fragment.normal_dependencies().is_none());
    }

    #[test]
    fn repeated_diamond_composition_unions_call_dependencies_and_unknown_absorbs() {
        let root = LocalBodyIdentity::RootInitializer {
            owner: SourceUnitOrdinal(0),
            initializer_span: Span::new(BytePos(1), BytePos(100)),
        };
        let branch = test_callable(1, 0);
        let leaf = test_callable(2, 0);

        // Two root call sites reach the same branch body. Each execution then
        // contributes the same branch-to-leaf source edge under distinct
        // captured-input prerequisites.
        let mut first_path = EffectCoverageFragment::no_effects();
        first_path.selected_local_call(test_call(
            root.clone(),
            10,
            15,
            branch.clone(),
            captured_value_input(1),
        ));
        first_path.selected_local_call(test_call(
            branch.clone(),
            40,
            45,
            leaf.clone(),
            captured_value_input(1),
        ));

        let mut second_path = EffectCoverageFragment::no_effects();
        second_path.selected_local_call(test_call(
            root,
            20,
            25,
            branch.clone(),
            captured_value_input(2),
        ));
        second_path.selected_local_call(test_call(
            branch.clone(),
            40,
            45,
            leaf,
            captured_value_input(2),
        ));

        let mut composed = first_path.clone();
        composed.compose(&second_path);
        composed.compose(&first_path);
        let calls = &composed.call_observations.as_ref().unwrap().calls;
        assert_eq!(calls.len(), 3, "each exact source edge is retained once");
        let branch_to_leaf = calls
            .values()
            .find(|call| call.caller == branch && call.call_span.lo.0 == 40)
            .expect("shared branch-to-leaf edge");
        assert_eq!(
            branch_to_leaf.dependencies.value_inputs,
            std::collections::BTreeSet::from([
                ValueInputRef {
                    defining_chunk: "static/provider".to_string(),
                    input_index: 1,
                },
                ValueInputRef {
                    defining_chunk: "static/provider".to_string(),
                    input_index: 2,
                },
            ]),
            "prerequisites from both contexts are retained"
        );

        composed.compose(&EffectCoverageFragment::default());
        assert!(
            composed.lexical.is_none(),
            "unknown lexical coverage absorbs"
        );
        assert_eq!(
            composed.call_observations.as_ref().unwrap().calls.len(),
            3,
            "unknown effect coverage does not erase exact site observations"
        );
        assert!(composed.lexical_effect_dependencies().is_none());
    }

    #[test]
    fn call_edge_identity_distinguishes_caller_span_and_target() {
        let caller = test_callable(1, 3);
        let target = test_callable(5, 6);
        let key = local_call_key(&test_call(
            caller.clone(),
            10,
            15,
            target.clone(),
            ProofDependencies::default(),
        ));

        for distinct in [
            test_call(
                caller.clone(),
                11,
                15,
                target.clone(),
                ProofDependencies::default(),
            ),
            test_call(
                test_callable(1, 4),
                10,
                15,
                target.clone(),
                ProofDependencies::default(),
            ),
            test_call(
                test_callable(2, 3),
                10,
                15,
                target.clone(),
                ProofDependencies::default(),
            ),
            test_call(
                caller.clone(),
                10,
                15,
                test_callable(5, 7),
                ProofDependencies::default(),
            ),
            test_call(
                caller,
                10,
                15,
                test_callable(8, 6),
                ProofDependencies::default(),
            ),
        ] {
            assert_ne!(
                key,
                local_call_key(&distinct),
                "a differing caller identity, span, or target keeps a separate edge"
            );
        }
    }

    #[test]
    fn serialized_fragments_preserve_module_qualified_call_identity_and_coverage() {
        let mut fragment = EffectCoverageFragment::no_effects();
        let caller = test_callable(7, 2);
        for module in ["first", "second"] {
            fragment.selected_local_call(test_call(
                caller.clone(),
                10,
                15,
                LocalBodyIdentity::ImportedCallable(Box::new(ImportedCallableIdentity {
                    defining_module: module.into(),
                    export: "make".into(),
                    local: "make".into(),
                    member: None,
                    key: 1,
                })),
                ProofDependencies::default(),
            ));
        }
        let bytes = serde_json::to_vec(&fragment)
            .expect("non-string runtime edge keys serialize as receipts");
        let decoded: EffectCoverageFragment = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(decoded, fragment);
        assert!(decoded.has_closed_effects());
        assert_eq!(decoded.call_observations.as_ref().unwrap().calls.len(), 2);
        let mut unknown = EffectCoverageFragment::default();
        unknown.compose(&decoded);
        let bytes = serde_json::to_vec(&unknown).unwrap();
        let decoded: EffectCoverageFragment = serde_json::from_slice(&bytes).unwrap();
        assert!(
            !decoded.has_closed_effects(),
            "artifact transport must not promote unknown domains"
        );
    }

    #[test]
    fn call_inventory_unions_invoked_and_skipped_site_evidence_exactly() {
        let caller = test_callable(7, 2);
        let mut fragment = EffectCoverageFragment::no_effects();
        fragment.selected_local_call(test_call(
            caller.clone(),
            10,
            15,
            test_callable(8, 2),
            ProofDependencies::default(),
        ));
        fragment.skipped_local_call(test_skipped_call(
            caller.clone(),
            20,
            25,
            (1, 100),
            (10, 15),
            SkippedCallReason::KnownThrow,
            ProofDependencies::default(),
        ));

        let expected = std::collections::BTreeSet::from([(10, 15), (20, 25)]);
        assert!(fragment.has_call_inventory(&caller, &expected));
        assert!(fragment.has_local_call(&caller, Span::new(BytePos(10), BytePos(15))));
        assert!(fragment.has_local_call(&caller, Span::new(BytePos(20), BytePos(25))));
        assert!(!fragment.has_local_call(&caller, Span::new(BytePos(30), BytePos(35))));
        assert!(
            !fragment.has_call_inventory(&caller, &std::collections::BTreeSet::from([(10, 15)]))
        );
        assert!(!fragment.has_call_inventory(
            &caller,
            &std::collections::BTreeSet::from([(10, 15), (20, 25), (30, 35)])
        ));
        assert!(!fragment.has_call_inventory(&test_callable(7, 3), &expected));
    }

    #[test]
    fn skipped_call_composition_merges_only_equal_frontier_evidence() {
        let caller = test_callable(7, 2);
        let mut first = EffectCoverageFragment::no_effects();
        first.skipped_local_call(test_skipped_call(
            caller.clone(),
            20,
            25,
            (1, 100),
            (10, 15),
            SkippedCallReason::KnownThrow,
            captured_value_input(1),
        ));
        let mut second = EffectCoverageFragment::no_effects();
        second.skipped_local_call(test_skipped_call(
            caller.clone(),
            20,
            25,
            (1, 100),
            (10, 15),
            SkippedCallReason::KnownThrow,
            captured_value_input(2),
        ));
        second.skipped_local_call(test_skipped_call(
            caller.clone(),
            20,
            25,
            (1, 100),
            (12, 17),
            SkippedCallReason::KnownThrow,
            captured_value_input(3),
        ));

        first.compose(&second);
        let skipped_calls = &first.call_observations.as_ref().unwrap().skipped_calls;
        assert_eq!(skipped_calls.len(), 2);
        let same_frontier = skipped_calls
            .values()
            .find(|call| call.frontier_span.lo.0 == 10)
            .unwrap();
        assert_eq!(
            same_frontier.dependencies.value_inputs,
            std::collections::BTreeSet::from([
                ValueInputRef {
                    defining_chunk: "static/provider".to_string(),
                    input_index: 1,
                },
                ValueInputRef {
                    defining_chunk: "static/provider".to_string(),
                    input_index: 2,
                },
            ])
        );
        assert!(first.has_call_inventory(&caller, &std::collections::BTreeSet::from([(20, 25)])));
    }

    #[test]
    fn known_throw_dependencies_are_required_before_completion_is_consumed() {
        let mut abrupt = EffectCoverageFragment::no_effects();
        abrupt.abrupt();
        assert_eq!(
            abrupt.known_abrupt_dependencies(),
            Some(ProofDependencies::default())
        );
        assert!(abrupt.handle_known_throw());
        assert_eq!(abrupt.completion, Completion::Normal);

        let mut unknown = EffectCoverageFragment::no_effects();
        unknown.uncertain_completion();
        assert!(unknown.known_abrupt_dependencies().is_none());
        assert!(!unknown.handle_known_throw());
        assert_eq!(unknown.completion, Completion::Unknown);

        let mut uncovered = EffectCoverageFragment::no_effects();
        uncovered.abrupt();
        uncovered.initialization.as_mut().unwrap().host_effects = DomainCoverage::Unknown;
        assert!(uncovered.known_abrupt_dependencies().is_none());
        assert!(!uncovered.handle_known_throw());
        assert_eq!(uncovered.completion, Completion::Abrupt);
    }

    #[test]
    fn skipped_call_receipts_survive_saved_fragment_and_root_receipt_roundtrips() {
        let caller = test_callable(7, 2);
        let skipped_call = test_skipped_call(
            caller.clone(),
            20,
            25,
            (1, 100),
            (10, 15),
            SkippedCallReason::HandlerNotEntered,
            captured_value_input(4),
        );
        let mut fragment = EffectCoverageFragment::no_effects();
        fragment.skipped_local_call(skipped_call.clone());

        let saved = SavedEffectCoverageFragment::snapshot(&fragment);
        let bytes = serde_json::to_vec(&saved).unwrap();
        let saved: SavedEffectCoverageFragment<'_> = serde_json::from_slice(&bytes).unwrap();
        let restored = saved.restore();
        assert_eq!(restored, fragment);
        assert!(restored.has_local_call(&caller, skipped_call.call_span));

        let receipt = LexicalBindingEffectReceipt {
            identity: RootEffectIdentity {
                owner: SourceUnitOrdinal(2),
                source: RootEffectSource::ExpressionStatement {
                    expression_span: Span::new(BytePos(1), BytePos(100)),
                },
            },
            read_dependencies: ProofDependencies::default(),
            rebind_dependencies: ProofDependencies::default(),
            calls: Vec::new(),
            skipped_calls: vec![skipped_call.clone()],
        };
        let ordinals = ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>());
        let saved = SavedLexicalBindingEffectReceipt::snapshot(&receipt, &ordinals).unwrap();
        let bytes = serde_json::to_vec(&saved).unwrap();
        let saved: SavedLexicalBindingEffectReceipt = serde_json::from_slice(&bytes).unwrap();
        let restored = saved.restore(&ordinals).unwrap();
        assert_eq!(restored, receipt);
        let report = restored.to_report();
        assert_eq!(report.skipped_calls.len(), 1);
        assert_eq!(
            report.skipped_calls[0].reason,
            SkippedCallReason::HandlerNotEntered
        );
        assert_eq!(report.skipped_calls[0].call_span, skipped_call.call_span);
        assert_eq!(report.skipped_calls[0].caller, caller.to_report());
    }
}
