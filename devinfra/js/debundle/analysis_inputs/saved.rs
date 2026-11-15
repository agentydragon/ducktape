//! Explicit saved projections for imported capabilities and native census data.

use std::borrow::Cow;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use anyhow::Result;
use serde::{Deserialize, Serialize};

use super::{HostBehaviorKey, NativeIntegrity, NativeIntrinsic, ValueInputEvidence, ValueInputRef};
use crate::reports::saved::{SavedNativeMutationWitness, SavedNativePrototypeMutation};
use crate::reports::schema::NeverWrittenOverride;

/// Native census data with source-site evidence selected explicitly.
#[derive(Serialize, Deserialize)]
pub struct SavedNativeIntegrity<'a> {
    mutated: Cow<'a, BTreeSet<NativeIntrinsic>>,
    prototype_mutations: Vec<SavedNativePrototypeMutation<'a>>,
    witnesses: Vec<SavedNativeMutationWitness<'a>>,
    value_input_evidence: Cow<'a, BTreeMap<ValueInputRef, ValueInputEvidence>>,
    consumed_value_inputs: Cow<'a, BTreeSet<ValueInputRef>>,
    consumed_host_behavior: Cow<'a, BTreeSet<HostBehaviorKey>>,
    never_written: Cow<'a, BTreeSet<NativeIntrinsic>>,
    never_written_overrides: Cow<'a, [NeverWrittenOverride]>,
}

impl<'a> SavedNativeIntegrity<'a> {
    pub fn snapshot(integrity: &'a NativeIntegrity) -> Self {
        Self {
            mutated: Cow::Borrowed(&integrity.mutated),
            prototype_mutations: integrity
                .prototype_mutations
                .iter()
                .map(SavedNativePrototypeMutation::snapshot)
                .collect(),
            witnesses: integrity
                .witnesses
                .iter()
                .map(SavedNativeMutationWitness::snapshot)
                .collect(),
            value_input_evidence: Cow::Borrowed(&integrity.value_input_evidence),
            consumed_value_inputs: Cow::Borrowed(&integrity.consumed_value_inputs),
            consumed_host_behavior: Cow::Borrowed(&integrity.consumed_host_behavior),
            never_written: Cow::Borrowed(&integrity.never_written),
            never_written_overrides: Cow::Borrowed(integrity.never_written_overrides.as_slice()),
        }
    }

    pub fn restore(self) -> Result<NativeIntegrity> {
        Ok(NativeIntegrity {
            mutated: self.mutated.into_owned(),
            prototype_mutations: Arc::new(
                self.prototype_mutations
                    .into_iter()
                    .map(SavedNativePrototypeMutation::restore)
                    .collect(),
            ),
            witnesses: Arc::new(
                self.witnesses
                    .into_iter()
                    .map(SavedNativeMutationWitness::restore)
                    .collect::<Result<_>>()?,
            ),
            value_input_evidence: self.value_input_evidence.into_owned(),
            consumed_value_inputs: self.consumed_value_inputs.into_owned(),
            consumed_host_behavior: self.consumed_host_behavior.into_owned(),
            never_written: self.never_written.into_owned(),
            never_written_overrides: self.never_written_overrides.into_owned(),
        })
    }
}
