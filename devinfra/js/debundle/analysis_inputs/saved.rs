//! Explicit saved projections for imported capabilities and native census data.

use std::borrow::Cow;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use analysis_codec::{ContextOrdinals, SavedModuleCell};
use anyhow::{Result, bail, ensure};
use serde::{Deserialize, Serialize};
use source_identity::ModuleCellId;

use super::{
    CallableSummary, ConditionalForeignBody, ForeignBodyEffects, HostBehaviorKey,
    ModuleCellAccesses, NativeIntegrity, NativeIntrinsic, PrimitiveCellInvariant,
    ProofDependencies, ValueInputEvidence, ValueInputRef,
};
use crate::facts::sync_effects::SavedEffectCoverageFragment;
use crate::reports::saved::{SavedNativeMutationWitness, SavedNativePrototypeMutation};
use crate::reports::schema::NeverWrittenOverride;

/// A callable summary whose trusted proof dependencies are explicit artifact
/// data while remaining absent from its ordinary report-facing Serde shape.
#[derive(Serialize, Deserialize)]
pub struct SavedCallableSummary<'a> {
    summary: Cow<'a, CallableSummary>,
    /// Required field, nullable value: missing must differ from explicit None.
    #[serde(deserialize_with = "Deserialize::deserialize")]
    namespace_wrapper_dependencies: Option<ProofDependencies>,
    /// Required field, nullable value: missing must differ from explicit None.
    #[serde(deserialize_with = "Deserialize::deserialize")]
    foreign_call_dependencies: Option<ProofDependencies>,
    #[serde(deserialize_with = "Deserialize::deserialize")]
    foreign_call_body_effects: Option<SavedForeignBodyEffects<'a>>,
    #[serde(deserialize_with = "Deserialize::deserialize")]
    foreign_call_initialized_class: Option<ProofDependencies>,
    foreign_member_evidence: BTreeMap<String, SavedForeignMemberEvidence<'a>>,
    conditional_bodies: Vec<SavedConditionalForeignBody<'a>>,
}

#[derive(Serialize, Deserialize)]
struct SavedForeignBodyEffects<'a> {
    callable_key: u32,
    effects: SavedEffectCoverageFragment<'a>,
}

impl<'a> SavedForeignBodyEffects<'a> {
    fn snapshot(body: &'a ForeignBodyEffects) -> Self {
        Self {
            callable_key: body.callable_key,
            effects: SavedEffectCoverageFragment::snapshot(&body.effects),
        }
    }

    fn restore(self) -> ForeignBodyEffects {
        ForeignBodyEffects {
            callable_key: self.callable_key,
            effects: self.effects.restore(),
        }
    }
}

#[derive(Serialize, Deserialize)]
struct SavedForeignMemberEvidence<'a> {
    dependencies: ProofDependencies,
    #[serde(deserialize_with = "Deserialize::deserialize")]
    body_effects: Option<SavedForeignBodyEffects<'a>>,
    #[serde(deserialize_with = "Deserialize::deserialize")]
    initialized_class: Option<ProofDependencies>,
}

#[derive(Serialize, Deserialize)]
struct SavedConditionalForeignBody<'a> {
    defining_module: String,
    export: String,
    local: String,
    callable_key: u32,
    effects: SavedEffectCoverageFragment<'a>,
    module_cells: SavedModuleCellAccesses,
}

#[derive(Serialize, Deserialize)]
struct SavedModuleCellAccesses {
    reads: Vec<SavedModuleCell>,
    writes: Vec<SavedModuleCell>,
    invariants: Vec<SavedModuleCellInvariant>,
}

#[derive(Clone, Serialize, Deserialize)]
struct SavedModuleCellInvariant {
    cell: SavedModuleCell,
    invariant: PrimitiveCellInvariant,
}

impl SavedModuleCellAccesses {
    fn snapshot(accesses: &ModuleCellAccesses, ordinals: &ContextOrdinals) -> Result<Self> {
        Ok(Self {
            reads: accesses
                .reads
                .iter()
                .map(|cell| ordinals.snapshot_module_cell(*cell))
                .collect::<Result<_>>()?,
            writes: accesses
                .writes
                .iter()
                .map(|cell| ordinals.snapshot_module_cell(*cell))
                .collect::<Result<_>>()?,
            invariants: accesses
                .invariants
                .iter()
                .map(|(cell, invariant)| {
                    Ok(SavedModuleCellInvariant {
                        cell: ordinals.snapshot_module_cell(*cell)?,
                        invariant: invariant.clone(),
                    })
                })
                .collect::<Result<_>>()?,
        })
    }

    fn restore(self, ordinals: &ContextOrdinals) -> Result<ModuleCellAccesses> {
        let mut reads = BTreeSet::new();
        for saved in self.reads {
            let cell = ordinals.restore_module_cell(saved)?;
            ensure!(
                reads.insert(cell),
                "duplicate saved module-cell read {cell:?}"
            );
        }
        let mut writes = BTreeSet::new();
        for saved in self.writes {
            let cell = ordinals.restore_module_cell(saved)?;
            ensure!(
                writes.insert(cell),
                "duplicate saved module-cell write {cell:?}"
            );
        }
        let mut invariants = BTreeMap::new();
        for saved in self.invariants {
            let cell = ordinals.restore_module_cell(saved.cell)?;
            if invariants.insert(cell, saved.invariant).is_some() {
                bail!("duplicate saved module-cell invariant {cell:?}");
            }
        }
        Ok(ModuleCellAccesses {
            reads,
            writes,
            invariants,
        })
    }
}

impl<'a> SavedConditionalForeignBody<'a> {
    fn snapshot(body: &'a ConditionalForeignBody, ordinals: &ContextOrdinals) -> Result<Self> {
        Ok(Self {
            defining_module: body.defining_module.clone(),
            export: body.export.clone(),
            local: body.local.clone(),
            callable_key: body.callable_key,
            effects: SavedEffectCoverageFragment::snapshot(&body.effects),
            module_cells: SavedModuleCellAccesses::snapshot(&body.module_cells, ordinals)?,
        })
    }

    fn restore(self, body: &mut ConditionalForeignBody, ordinals: &ContextOrdinals) -> Result<()> {
        ensure!(
            body.defining_module == self.defining_module
                && body.export == self.export
                && body.local == self.local
                && body.callable_key == self.callable_key,
            "saved conditional-body evidence does not match its callable summary"
        );
        body.effects = self.effects.restore();
        body.module_cells = self.module_cells.restore(ordinals)?;
        Ok(())
    }
}

impl<'a> SavedCallableSummary<'a> {
    pub fn snapshot(summary: &'a CallableSummary, ordinals: &ContextOrdinals) -> Result<Self> {
        Ok(Self {
            summary: Cow::Borrowed(summary),
            namespace_wrapper_dependencies: summary
                .namespace_wrapper
                .as_ref()
                .map(|wrapper| wrapper.dependencies.clone()),
            foreign_call_dependencies: summary
                .foreign_call
                .as_ref()
                .map(|foreign_call| foreign_call.dependencies.clone()),
            foreign_call_body_effects: summary
                .foreign_call
                .as_ref()
                .and_then(|call| call.body_effects.as_deref())
                .map(SavedForeignBodyEffects::snapshot),
            foreign_call_initialized_class: summary
                .foreign_call
                .as_ref()
                .and_then(|call| call.initialized_class.clone()),
            foreign_member_evidence: summary
                .foreign_members
                .iter()
                .map(|(name, call)| {
                    (
                        name.clone(),
                        SavedForeignMemberEvidence {
                            dependencies: call.dependencies.clone(),
                            initialized_class: call.initialized_class.clone(),
                            body_effects: call
                                .body_effects
                                .as_deref()
                                .map(SavedForeignBodyEffects::snapshot),
                        },
                    )
                })
                .collect(),
            conditional_bodies: summary
                .conditional_bodies
                .iter()
                .map(|body| SavedConditionalForeignBody::snapshot(body, ordinals))
                .collect::<Result<_>>()?,
        })
    }

    pub fn restore(self, ordinals: &ContextOrdinals) -> Result<CallableSummary> {
        let mut summary = self.summary.into_owned();
        ensure!(
            summary.namespace_wrapper.is_some() == self.namespace_wrapper_dependencies.is_some(),
            "saved namespace-wrapper dependency does not match its callable summary"
        );
        ensure!(
            summary.foreign_call.is_some() == self.foreign_call_dependencies.is_some(),
            "saved foreign-call dependency does not match its callable summary"
        );
        ensure!(
            summary.foreign_call.is_some()
                || (self.foreign_call_body_effects.is_none()
                    && self.foreign_call_initialized_class.is_none()),
            "saved foreign-call body has no callable summary"
        );
        ensure!(
            summary
                .foreign_members
                .keys()
                .eq(self.foreign_member_evidence.keys()),
            "saved foreign-member evidence does not match its callable summaries"
        );
        ensure!(
            summary.conditional_bodies.len() == self.conditional_bodies.len(),
            "saved conditional bodies do not match their callable summaries"
        );
        if let (Some(wrapper), Some(dependencies)) = (
            summary.namespace_wrapper.as_mut(),
            self.namespace_wrapper_dependencies,
        ) {
            wrapper.dependencies = dependencies;
        }
        if let (Some(foreign_call), Some(dependencies)) = (
            summary.foreign_call.as_mut(),
            self.foreign_call_dependencies,
        ) {
            foreign_call.dependencies = dependencies;
            foreign_call.initialized_class = self.foreign_call_initialized_class;
            foreign_call.body_effects = self
                .foreign_call_body_effects
                .map(|body| Box::new(body.restore()));
        }
        for (name, evidence) in self.foreign_member_evidence {
            let member = summary
                .foreign_members
                .get_mut(&name)
                .expect("member keys verified");
            member.dependencies = evidence.dependencies;
            member.initialized_class = evidence.initialized_class;
            member.body_effects = evidence.body_effects.map(|body| Box::new(body.restore()));
        }
        for (body, saved) in summary
            .conditional_bodies
            .iter_mut()
            .zip(self.conditional_bodies)
        {
            saved.restore(body, ordinals)?;
        }
        Ok(summary)
    }
}

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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::analysis_inputs::{
        ArgumentInputRef, BindingCellDeclarationIdentity, BindingCellStoreEvidence,
        BindingStoreKind, CallableSummary, ConditionalForeignBody, ForeignCallSummary,
        ForeignInvocation, ForeignReturn, ModuleCellAccesses, NamespaceWrapperSummary,
        RecordInputShape, ValueInputRef,
    };
    use crate::analysis_provenance::{InferenceRule, InferenceUse};
    use analysis_codec::{ContextOrdinals, SavedChunk};
    use source_identity::{ChunkId, ChunkTable, ModuleCellId, SourceUnitOrdinal};
    use std::collections::BTreeSet;
    use swc_common::DUMMY_SP;

    #[test]
    fn callable_dependencies_round_trip_outside_codec_and_stay_out_of_report_json() {
        let chunks = ChunkTable::default();
        let ordinals = ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>(), &chunks);
        let namespace_dependencies = ProofDependencies {
            value_inputs: BTreeSet::from([ValueInputRef {
                defining_chunk: "provider".to_string(),
                input_index: 2,
            }]),
            ..ProofDependencies::default()
        };
        let foreign_dependencies = ProofDependencies {
            value_inputs: BTreeSet::from([ValueInputRef {
                defining_chunk: "provider".to_string(),
                input_index: 4,
            }]),
            ..ProofDependencies::default()
        };
        let inference = InferenceUse {
            conclusion: InferenceRule::NamespaceWrapper,
            derivation_rules: BTreeSet::from([InferenceRule::NamespaceWrapper]),
            value_input_refs: BTreeSet::new(),
            descriptor_copy_author_refs: BTreeSet::new(),
            argument_input_refs: BTreeSet::<ArgumentInputRef>::new(),
            native_operation_input_refs: BTreeSet::new(),
        };
        let mut summary = CallableSummary::namespace_wrapper(NamespaceWrapperSummary {
            defining_module: "provider".to_string(),
            export: "wrap".to_string(),
            local: "wrap".to_string(),
            source_location: None,
            inference,
            dependencies: namespace_dependencies,
        });
        summary.foreign_call = Some(ForeignCallSummary {
            defining_module: "provider".to_string(),
            export: "invoke".to_string(),
            local: "invoke".to_string(),
            member: None,
            source_location: None,
            invocation: ForeignInvocation::Construct,
            returns: BTreeSet::from([ForeignReturn::FreshAllocation]),
            retained_arguments: BTreeSet::new(),
            retained_provider_captures: BTreeSet::new(),
            native_operations: BTreeSet::new(),
            replacement_package: None,
            dependencies: foreign_dependencies.clone(),
            initialized_class: Some(foreign_dependencies),
            body_effects: None,
        });

        let report = serde_json::to_value(&summary).expect("callable report serializes");
        assert!(report["namespace_wrapper"].get("dependencies").is_none());
        assert!(report["foreign_call"].get("dependencies").is_none());
        assert!(report["foreign_call"].get("initialized_class").is_none());

        let saved = SavedCallableSummary::snapshot(&summary, &ordinals)
            .expect("callable summary snapshots");
        let bytes = rmp_serde::to_vec_named(&saved).expect("ordinary saved encode");
        let saved: SavedCallableSummary<'_> =
            rmp_serde::from_slice(&bytes).expect("ordinary saved decode");
        assert_eq!(
            saved.restore(&ordinals).expect("valid callable summary"),
            summary
        );

        let mut saved_json = serde_json::to_value(
            SavedCallableSummary::snapshot(&summary, &ordinals).expect("callable snapshots"),
        )
        .expect("saved callable serializes as ordinary Serde");
        saved_json
            .as_object_mut()
            .expect("saved callable is a map")
            .remove("namespace_wrapper_dependencies");
        assert!(serde_json::from_value::<SavedCallableSummary<'static>>(saved_json).is_err());

        let mut missing_initialization =
            serde_json::to_value(SavedCallableSummary::snapshot(&summary, &ordinals).unwrap())
                .unwrap();
        missing_initialization
            .as_object_mut()
            .unwrap()
            .remove("foreign_call_initialized_class");
        assert!(
            serde_json::from_value::<SavedCallableSummary<'static>>(missing_initialization)
                .is_err()
        );

        let mut mismatched_json =
            serde_json::to_value(SavedCallableSummary::snapshot(&summary, &ordinals).unwrap())
                .unwrap();
        mismatched_json
            .as_object_mut()
            .expect("saved callable is a map")
            .insert(
                "namespace_wrapper_dependencies".to_string(),
                serde_json::Value::Null,
            );
        let decoded: SavedCallableSummary<'static> =
            serde_json::from_value(mismatched_json).expect("explicit None dependencies decode");
        assert!(decoded.restore(&ordinals).is_err());

        let absent_capability = CallableSummary::default();
        let saved_json = serde_json::to_value(
            SavedCallableSummary::snapshot(&absent_capability, &ordinals).unwrap(),
        )
        .expect("saved callable serializes with explicit absent dependencies");
        let decoded: SavedCallableSummary<'static> =
            serde_json::from_value(saved_json).expect("explicit None dependencies decode");
        assert_eq!(
            decoded
                .restore(&ordinals)
                .expect("absent capabilities match"),
            absent_capability
        );
    }

    #[test]
    fn conditional_module_cell_accesses_use_checked_saved_addresses() {
        let mut writer_chunks = ChunkTable::default();
        let writer_provider = writer_chunks.intern("provider".to_owned());
        let writer_other = writer_chunks.intern("other".to_owned());
        let provider_cell = ModuleCellId {
            chunk: writer_provider,
            declaration_origin: 11,
        };
        let other_cell = ModuleCellId {
            chunk: writer_other,
            declaration_origin: 23,
        };
        let invariant = |owner, post_init_kinds| PrimitiveCellInvariant {
            declaration: BindingCellDeclarationIdentity {
                owner: SourceUnitOrdinal(owner),
                activation_callable: None,
                binding_span: DUMMY_SP,
                initializer_span: Some(DUMMY_SP),
            },
            post_init_kinds,
            stores: vec![BindingCellStoreEvidence {
                owner: SourceUnitOrdinal(owner),
                activation_callable: Some(7),
                span: DUMMY_SP,
                kind: BindingStoreKind::Assignment,
            }],
        };
        let provider_invariant = invariant(3, analysis_model::PrimKinds::STRING);
        let other_invariant = invariant(5, analysis_model::PrimKinds::NUMBER);
        let mut summary = CallableSummary::default();
        summary.conditional_bodies.push(ConditionalForeignBody {
            defining_module: "provider".to_owned(),
            export: "wrap".to_owned(),
            local: "wrap".to_owned(),
            callable_key: 7,
            input: RecordInputShape {
                properties: Default::default(),
                creation_order: Vec::new(),
            },
            reads: BTreeSet::new(),
            native_operations: BTreeSet::new(),
            effects: Default::default(),
            module_cells: ModuleCellAccesses {
                reads: BTreeSet::from([provider_cell]),
                writes: BTreeSet::from([other_cell]),
                invariants: BTreeMap::from([
                    (provider_cell, provider_invariant.clone()),
                    (other_cell, other_invariant.clone()),
                ]),
            },
            dependencies: ProofDependencies::default(),
            replacement_package: None,
        });

        let writer_ordinals =
            ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>(), &writer_chunks);
        let saved = SavedCallableSummary::snapshot(&summary, &writer_ordinals)
            .expect("module-cell sidecar snapshots");
        let saved_json = serde_json::to_value(&saved).expect("saved callable serializes");
        assert_eq!(
            saved_json["conditional_bodies"][0]["module_cells"]["reads"][0]["chunk"],
            "provider"
        );
        let mut missing = saved_json.clone();
        missing
            .as_object_mut()
            .expect("saved callable is a map")
            .remove("conditional_bodies");
        assert!(serde_json::from_value::<SavedCallableSummary<'static>>(missing).is_err());

        let bytes = rmp_serde::to_vec_named(&saved).expect("saved callable encodes");
        let decoded: SavedCallableSummary<'static> =
            rmp_serde::from_slice(&bytes).expect("saved callable decodes");
        let mut reader_chunks = ChunkTable::default();
        let reader_other = reader_chunks.intern("other".to_owned());
        let reader_provider = reader_chunks.intern("provider".to_owned());
        assert_ne!(writer_provider, reader_provider);
        assert_ne!(writer_other, reader_other);
        let reader_ordinals =
            ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>(), &reader_chunks);
        let restored = decoded
            .restore(&reader_ordinals)
            .expect("checked cell names restore");
        let mut expected = summary.clone();
        expected.conditional_bodies[0].module_cells = ModuleCellAccesses {
            reads: BTreeSet::from([ModuleCellId {
                chunk: reader_provider,
                declaration_origin: 11,
            }]),
            writes: BTreeSet::from([ModuleCellId {
                chunk: reader_other,
                declaration_origin: 23,
            }]),
            invariants: BTreeMap::from([
                (
                    ModuleCellId {
                        chunk: reader_provider,
                        declaration_origin: 11,
                    },
                    provider_invariant.clone(),
                ),
                (
                    ModuleCellId {
                        chunk: reader_other,
                        declaration_origin: 23,
                    },
                    other_invariant,
                ),
            ]),
        };
        assert_eq!(restored, expected);

        let mut unknown = SavedCallableSummary::snapshot(&summary, &writer_ordinals)
            .expect("module-cell sidecar snapshots");
        unknown.conditional_bodies[0].module_cells.reads[0] = SavedModuleCell {
            chunk: SavedChunk("missing".to_owned()),
            declaration_origin: 11,
        };
        assert!(unknown.restore(&reader_ordinals).is_err());

        let mut duplicate = SavedCallableSummary::snapshot(&summary, &writer_ordinals)
            .expect("module-cell sidecar snapshots");
        let duplicate_proof = duplicate.conditional_bodies[0].module_cells.invariants[0].clone();
        duplicate.conditional_bodies[0]
            .module_cells
            .invariants
            .push(duplicate_proof);
        assert!(duplicate.restore(&reader_ordinals).is_err());

        let mut unknown_id = summary.clone();
        unknown_id.conditional_bodies[0]
            .module_cells
            .reads
            .insert(ModuleCellId {
                chunk: ChunkId(writer_chunks.len()),
                declaration_origin: 99,
            });
        assert!(SavedCallableSummary::snapshot(&unknown_id, &writer_ordinals).is_err());
    }
}
