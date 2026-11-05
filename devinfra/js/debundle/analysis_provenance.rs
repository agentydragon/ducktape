//! Typed provenance for source-derived analysis conclusions.
//!
//! These records describe which inference rules a consumer used and which
//! author inputs the derivation depends on. They are explanatory provenance;
//! trusted `ProofDependencies` remain the authority for inferred facts.

use std::collections::BTreeSet;

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
    ConditionalForeignBody,
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
}
