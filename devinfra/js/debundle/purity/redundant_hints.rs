use super::*;

/// One author-declared `purity: pure` hint the analyzer determines
/// would be inferred automatically — the binding's body classifies
/// `Pure` (or the binding admits as `PlainData`) even with the hint
/// itself removed from `declared_pure`. Surfaced as a chunk-level
/// warning so spec authors can delete the load-free hint and keep
/// only the genuinely-load-bearing ones (vendor-shape impurity
/// overrides, etc.).
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct RedundantPurityHint {
    pub binding_name: String,
    pub reason: RedundantPurityReason,
    pub proof_basis: RedundantPurityProofBasis,
}

pub use crate::saved_proto::proto::RedundantPurityProofBasis;

pub trait RedundantHintExplanation {
    fn explanation(self) -> &'static str;
}

impl RedundantHintExplanation for RedundantPurityProofBasis {
    /// Human explanation of the independent-purity proof used by the hint warning.
    fn explanation(self) -> &'static str {
        match self {
            Self::WithoutPurityHints => {
                "This proof uses no `purity: pure` hints. Remove the hint and validate the spec."
            }
            Self::WithOtherPurityHints => {
                "This proof requires other `purity: pure` hints. Remove hints individually and revalidate after each edit."
            }
        }
    }
}

pub use crate::saved_proto::proto::RedundantPurityReason;

impl RedundantHintExplanation for RedundantPurityReason {
    /// Human explanation of why the binding is inferred without its hint.
    fn explanation(self) -> &'static str {
        match self {
            Self::InferredPureFunction => {
                "pure (the function body classifies Pure by recursive analysis)"
            }
            Self::InferredPlainDataBinding => {
                "PlainData (chunk-local const/let plain literal with no chunk-wide writes through the binding)"
            }
        }
    }
}

/// One redundant `pure_members: [<prop>]` entry — the
/// `<binding>.<prop>(args)` call would already classify pure
/// without the spec hint (e.g. the receiver is `Array` and the
/// property is `isArray`, whose builtin row admits any operands).
/// Surfaced so spec authors can prune the redundant entry.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct RedundantPureMemberHint {
    pub binding_name: String,
    pub property: String,
    pub reason: RedundantPureMemberReason,
}

pub use crate::saved_proto::proto::RedundantPureMemberReason;

impl RedundantHintExplanation for RedundantPureMemberReason {
    /// Human explanation of why the member hint is redundant.
    fn explanation(self) -> &'static str {
        match self {
            Self::WhitelistedStaticCall => "pure via its builtin summary row",
        }
    }
}

/// Probe each hint with that hint disabled. A shared hint-free probe distinguishes
/// independent inference from inference requiring other `purity: pure` hints.
/// These diagnostic probes exclude imported verdicts and other author contracts;
/// they do not change statement classification. Removing hints still requires
/// validating the resulting spec; individually redundant hints need not be jointly
/// removable.
pub(crate) fn detect_redundant_purity_hints(
    body: &[TopLevelItemView<'_>],
    shadowed: &BTreeSet<&'static str>,
    declared_pure: &BTreeSet<String>,
) -> Vec<RedundantPurityHint> {
    let mut out = Vec::new();
    let mut hint_free = None;
    for name in declared_pure {
        let mut without = declared_pure.clone();
        without.remove(name);
        let probe = ChunkCodeGraph::build(body, shadowed, &without);
        let reason = redundant_reason(&probe, name);
        if let Some(reason) = reason {
            let proof_basis = if without.is_empty()
                || redundant_reason(
                    hint_free.get_or_insert_with(|| {
                        ChunkCodeGraph::build(body, shadowed, &BTreeSet::new())
                    }),
                    name,
                )
                .is_some()
            {
                RedundantPurityProofBasis::WithoutPurityHints
            } else {
                RedundantPurityProofBasis::WithOtherPurityHints
            };
            out.push(RedundantPurityHint {
                binding_name: name.clone(),
                reason,
                proof_basis,
            });
        }
    }
    out
}

fn redundant_reason(graph: &ChunkCodeGraph, name: &str) -> Option<RedundantPurityReason> {
    match graph.bindings.get(name) {
        Some(ChunkBinding::Function { purity }) if purity.is_pure() => {
            Some(RedundantPurityReason::InferredPureFunction)
        }
        Some(ChunkBinding::PlainData) => Some(RedundantPurityReason::InferredPlainDataBinding),
        _ => None,
    }
}

/// Walk `declared_pure_members` and flag entries the analyzer would
/// classify pure without the hint: a member of an unshadowed intrinsic
/// whose builtin row admits any operands, so an entry like
/// `pure_members: [isArray]` on a binding named `Array` is a no-op.
///
/// Rows that constrain their operands (`Object.keys` needs a plain-data
/// argument) are not reported: the hint covers every call shape, while the
/// row covers only some, and proving every callsite's shape is out of scope
/// under the "report only confirmed-redundant" contract.
pub(crate) fn detect_redundant_pure_member_hints(
    declared_pure_members: &BTreeMap<String, BTreeSet<String>>,
    shadowed: &BTreeSet<&'static str>,
) -> Vec<RedundantPureMemberHint> {
    let mut out = Vec::new();
    for (binding, props) in declared_pure_members {
        if shadowed.contains(binding.as_str()) {
            continue;
        }
        for prop in props {
            if builtins::lookup(binding, Some(prop), Invocation::Call)
                .is_some_and(admits_any_operands)
            {
                out.push(RedundantPureMemberHint {
                    binding_name: binding.clone(),
                    property: prop.clone(),
                    reason: RedundantPureMemberReason::WhitelistedStaticCall,
                });
            }
        }
    }
    out
}

use crate::saved_proto::{ProtoConvert, proto, required};

impl ProtoConvert<proto::RedundantPurityHint> for RedundantPurityHint {
    fn to_proto(&self) -> anyhow::Result<proto::RedundantPurityHint> {
        Ok(proto::RedundantPurityHint {
            binding_name: Some(self.binding_name.clone()),
            reason: Some(self.reason as i32),
            proof_basis: Some(self.proof_basis as i32),
        })
    }

    fn from_proto(value: proto::RedundantPurityHint) -> anyhow::Result<Self> {
        Ok(Self {
            binding_name: required(value.binding_name, "RedundantPurityHint.binding_name")?,
            reason: RedundantPurityReason::try_from(required(
                value.reason,
                "RedundantPurityHint.reason",
            )?)?,
            proof_basis: RedundantPurityProofBasis::try_from(required(
                value.proof_basis,
                "RedundantPurityHint.proof_basis",
            )?)?,
        })
    }
}

impl ProtoConvert<proto::RedundantPureMemberHint> for RedundantPureMemberHint {
    fn to_proto(&self) -> anyhow::Result<proto::RedundantPureMemberHint> {
        Ok(proto::RedundantPureMemberHint {
            binding_name: Some(self.binding_name.clone()),
            property: Some(self.property.clone()),
            reason: Some(self.reason as i32),
        })
    }

    fn from_proto(value: proto::RedundantPureMemberHint) -> anyhow::Result<Self> {
        Ok(Self {
            binding_name: required(value.binding_name, "RedundantPureMemberHint.binding_name")?,
            property: required(value.property, "RedundantPureMemberHint.property")?,
            reason: RedundantPureMemberReason::try_from(required(
                value.reason,
                "RedundantPureMemberHint.reason",
            )?)?,
        })
    }
}
