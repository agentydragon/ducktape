//! Typed provenance for source-derived analysis conclusions.
//!
//! These records describe which inference rules a consumer used and which
//! author inputs the derivation depends on. They are explanatory provenance;
//! trusted `ProofDependencies` remain the authority for inferred facts.

use std::collections::BTreeSet;

use crate::saved_proto::{ProtoConvert, decode_set, proto, required};
use anyhow::{Result, ensure};
use serde::{Deserialize, Serialize};

use crate::analysis_inputs::{
    ArgumentInputRef, DescriptorCopyAuthorRef, NativeOperationInputRef, ValueInputRef,
};

/// Positive rules participating in a consumed construction certificate.
pub use crate::saved_proto::proto::InferenceRule;

/// Provenance actually consumed by one source-derived inference.
///
/// Internal derivations share typed premise identities; presentation layers
/// may expand those identities into authored locations for human readers.
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
pub struct InferenceUse {
    pub conclusion: InferenceRule,
    pub derivation_rules: BTreeSet<InferenceRule>,
    pub value_input_refs: BTreeSet<ValueInputRef>,
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pub descriptor_copy_author_refs: BTreeSet<DescriptorCopyAuthorRef>,
    pub argument_input_refs: BTreeSet<ArgumentInputRef>,
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pub native_operation_input_refs: BTreeSet<NativeOperationInputRef>,
    /// Exact author lifetime assertions for imported primitive-array spreads.
    #[serde(default, skip_serializing_if = "BTreeSet::is_empty")]
    pub imported_primitive_array_spread_input_refs:
        BTreeSet<crate::analysis_inputs::ImportedPrimitiveArraySpreadInputRef>,
}

impl ProtoConvert<proto::InferenceUse> for InferenceUse {
    fn to_proto(&self) -> Result<proto::InferenceUse> {
        Ok(proto::InferenceUse {
            conclusion: Some(self.conclusion as i32),
            derivation_rules: self
                .derivation_rules
                .iter()
                .map(|rule| *rule as i32)
                .collect(),
            value_input_refs: self
                .value_input_refs
                .iter()
                .map(ProtoConvert::to_proto)
                .collect::<Result<_>>()?,
            descriptor_copy_author_refs: self
                .descriptor_copy_author_refs
                .iter()
                .map(ProtoConvert::to_proto)
                .collect::<Result<_>>()?,
            argument_input_refs: self
                .argument_input_refs
                .iter()
                .map(ProtoConvert::to_proto)
                .collect::<Result<_>>()?,
            native_operation_input_refs: self
                .native_operation_input_refs
                .iter()
                .map(ProtoConvert::to_proto)
                .collect::<Result<_>>()?,
            imported_primitive_array_spread_input_refs: self
                .imported_primitive_array_spread_input_refs
                .iter()
                .map(ProtoConvert::to_proto)
                .collect::<Result<_>>()?,
        })
    }
    fn from_proto(value: proto::InferenceUse) -> Result<Self> {
        let conclusion =
            InferenceRule::try_from(required(value.conclusion, "InferenceUse.conclusion")?)?;
        let mut derivation_rules = BTreeSet::new();
        for rule in value.derivation_rules {
            let rule = InferenceRule::try_from(rule)?;
            ensure!(derivation_rules.insert(rule), "duplicate inference rule");
        }
        Ok(Self {
            conclusion,
            derivation_rules,
            value_input_refs: decode_set(value.value_input_refs)?,
            descriptor_copy_author_refs: decode_set(value.descriptor_copy_author_refs)?,
            argument_input_refs: decode_set(value.argument_input_refs)?,
            native_operation_input_refs: decode_set(value.native_operation_input_refs)?,
            imported_primitive_array_spread_input_refs: decode_set(
                value.imported_primitive_array_spread_input_refs,
            )?,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn zero_valued_saved_enums_keep_json_names() {
        use crate::analysis_inputs::{ForeignInvocation, ProgramPremiseKey};
        use crate::execution_evidence::{
            ExecutionOperation, GlobalReachCause, OrderedCallAdmissionRefusal,
            OrderedFieldReadRefusal,
        };
        use crate::loader_closure::LoaderClosureFailureKind;

        let cases = [
            (
                serde_json::to_value(ExecutionOperation::MembershipProtocol).unwrap(),
                "membership_protocol",
            ),
            (
                serde_json::to_value(GlobalReachCause::UnknownCallee).unwrap(),
                "unknown_callee",
            ),
            (
                serde_json::to_value(OrderedFieldReadRefusal::ReceiverNotSingleton).unwrap(),
                "receiver_not_singleton",
            ),
            (
                serde_json::to_value(OrderedCallAdmissionRefusal::UnknownTarget).unwrap(),
                "unknown_target",
            ),
            (
                serde_json::to_value(InferenceRule::NativeIntegrity).unwrap(),
                "native_integrity",
            ),
            (
                serde_json::to_value(ForeignInvocation::Call).unwrap(),
                "call",
            ),
            (
                serde_json::to_value(ProgramPremiseKey::ProcessPrototypeChainAccessors).unwrap(),
                "process_prototype_chain_accessors",
            ),
            (
                serde_json::to_value(LoaderClosureFailureKind::UndeclaredHostBehavior).unwrap(),
                "undeclared_host_behavior",
            ),
        ];
        for (value, expected) in cases {
            assert_eq!(value, expected);
        }
    }

    #[test]
    fn inference_rule_zero_keeps_presence_and_duplicate_checks() {
        let inference = InferenceUse {
            conclusion: InferenceRule::NativeIntegrity,
            derivation_rules: BTreeSet::from([InferenceRule::NativeIntegrity]),
            value_input_refs: BTreeSet::new(),
            descriptor_copy_author_refs: BTreeSet::new(),
            argument_input_refs: BTreeSet::new(),
            native_operation_input_refs: BTreeSet::new(),
            imported_primitive_array_spread_input_refs: BTreeSet::new(),
        };
        let wire = inference.to_proto().unwrap();
        assert_eq!(wire.conclusion, Some(0));
        assert_eq!(wire.derivation_rules, vec![0]);
        assert_eq!(InferenceUse::from_proto(wire.clone()).unwrap(), inference);

        let mut missing = wire.clone();
        missing.conclusion = None;
        assert!(InferenceUse::from_proto(missing).is_err());

        let mut duplicate = wire;
        duplicate.derivation_rules.push(0);
        assert!(InferenceUse::from_proto(duplicate).is_err());
    }
}
