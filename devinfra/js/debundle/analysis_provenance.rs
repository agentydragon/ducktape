//! Typed provenance for source-derived analysis conclusions.
//!
//! These records describe which inference rules a consumer used and which
//! author inputs the derivation depends on. They are explanatory provenance;
//! trusted `ProofDependencies` remain the authority for inferred facts.

use std::collections::BTreeSet;

use crate::saved_proto::{ProtoConvert, decode_set, proto, required};
use anyhow::{Result, bail};
use serde::{Deserialize, Serialize};

use crate::analysis_inputs::{
    ArgumentInputRef, DescriptorCopyAuthorRef, NativeOperationInputRef, ValueInputRef,
};

/// Positive rules participating in a consumed construction certificate.
#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum InferenceRule {
    NativeIntegrity,
    NativeCallIdentity,
    NativeOwnDataRead,
    EmptyCollectionConstruction,
    LocalClassConstruction,
    PrimitiveSetIteration,
    StablePrimitiveArraySpread,
    ImportedPrimitiveArraySpread,
    StableFunction,
    StableProvider,
    ArgumentNoEscape,
    ReturnedRecord,
    NamespaceWrapper,
    OwnPropertySourceOrigin,
    OwnPropertyShape,
    OwnPropertyPrefixState,
    OwnPropertyNamesProjection,
    OwnPropertyDescriptorProjection,
    DescriptorCopyProvider,
    DescriptorCopyFactory,
    ForeignCallSummary,
}

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

impl ProtoConvert<i32> for InferenceRule {
    fn to_proto(&self) -> Result<i32> {
        Ok(match self {
            Self::NativeIntegrity => proto::InferenceRule::NativeIntegrity as i32,
            Self::NativeCallIdentity => proto::InferenceRule::NativeCallIdentity as i32,
            Self::NativeOwnDataRead => proto::InferenceRule::NativeOwnDataRead as i32,
            Self::EmptyCollectionConstruction => {
                proto::InferenceRule::EmptyCollectionConstruction as i32
            }
            Self::LocalClassConstruction => proto::InferenceRule::LocalClassConstruction as i32,
            Self::PrimitiveSetIteration => proto::InferenceRule::PrimitiveSetIteration as i32,
            Self::StablePrimitiveArraySpread => {
                proto::InferenceRule::StablePrimitiveArraySpread as i32
            }
            Self::ImportedPrimitiveArraySpread => {
                proto::InferenceRule::ImportedPrimitiveArraySpread as i32
            }
            Self::StableFunction => proto::InferenceRule::StableFunction as i32,
            Self::StableProvider => proto::InferenceRule::StableProvider as i32,
            Self::ArgumentNoEscape => proto::InferenceRule::ArgumentNoEscape as i32,
            Self::ReturnedRecord => proto::InferenceRule::ReturnedRecord as i32,
            Self::NamespaceWrapper => proto::InferenceRule::NamespaceWrapper as i32,
            Self::OwnPropertySourceOrigin => proto::InferenceRule::OwnPropertySourceOrigin as i32,
            Self::OwnPropertyShape => proto::InferenceRule::OwnPropertyShape as i32,
            Self::OwnPropertyPrefixState => proto::InferenceRule::OwnPropertyPrefixState as i32,
            Self::OwnPropertyNamesProjection => {
                proto::InferenceRule::OwnPropertyNamesProjection as i32
            }
            Self::OwnPropertyDescriptorProjection => {
                proto::InferenceRule::OwnPropertyDescriptorProjection as i32
            }
            Self::DescriptorCopyProvider => proto::InferenceRule::DescriptorCopyProvider as i32,
            Self::DescriptorCopyFactory => proto::InferenceRule::DescriptorCopyFactory as i32,
            Self::ForeignCallSummary => proto::InferenceRule::ForeignCallSummary as i32,
        })
    }
    fn from_proto(value: i32) -> Result<Self> {
        Ok(match proto::InferenceRule::try_from(value)? {
            proto::InferenceRule::NativeIntegrity => Self::NativeIntegrity,
            proto::InferenceRule::NativeCallIdentity => Self::NativeCallIdentity,
            proto::InferenceRule::NativeOwnDataRead => Self::NativeOwnDataRead,
            proto::InferenceRule::EmptyCollectionConstruction => Self::EmptyCollectionConstruction,
            proto::InferenceRule::LocalClassConstruction => Self::LocalClassConstruction,
            proto::InferenceRule::PrimitiveSetIteration => Self::PrimitiveSetIteration,
            proto::InferenceRule::StablePrimitiveArraySpread => Self::StablePrimitiveArraySpread,
            proto::InferenceRule::ImportedPrimitiveArraySpread => {
                Self::ImportedPrimitiveArraySpread
            }
            proto::InferenceRule::StableFunction => Self::StableFunction,
            proto::InferenceRule::StableProvider => Self::StableProvider,
            proto::InferenceRule::ArgumentNoEscape => Self::ArgumentNoEscape,
            proto::InferenceRule::ReturnedRecord => Self::ReturnedRecord,
            proto::InferenceRule::NamespaceWrapper => Self::NamespaceWrapper,
            proto::InferenceRule::OwnPropertySourceOrigin => Self::OwnPropertySourceOrigin,
            proto::InferenceRule::OwnPropertyShape => Self::OwnPropertyShape,
            proto::InferenceRule::OwnPropertyPrefixState => Self::OwnPropertyPrefixState,
            proto::InferenceRule::OwnPropertyNamesProjection => Self::OwnPropertyNamesProjection,
            proto::InferenceRule::OwnPropertyDescriptorProjection => {
                Self::OwnPropertyDescriptorProjection
            }
            proto::InferenceRule::DescriptorCopyProvider => Self::DescriptorCopyProvider,
            proto::InferenceRule::DescriptorCopyFactory => Self::DescriptorCopyFactory,
            proto::InferenceRule::ForeignCallSummary => Self::ForeignCallSummary,
            proto::InferenceRule::Unspecified => bail!("unspecified saved-analysis inference rule"),
        })
    }
}
impl ProtoConvert<proto::InferenceUse> for InferenceUse {
    fn to_proto(&self) -> Result<proto::InferenceUse> {
        Ok(proto::InferenceUse {
            conclusion: Some(self.conclusion.to_proto()?),
            derivation_rules: self
                .derivation_rules
                .iter()
                .map(ProtoConvert::to_proto)
                .collect::<Result<_>>()?,
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
        Ok(Self {
            conclusion: ProtoConvert::from_proto(required(
                value.conclusion,
                "InferenceUse.conclusion",
            )?)?,
            derivation_rules: decode_set(value.derivation_rules)?,
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
