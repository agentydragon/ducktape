//! Direct protobuf snapshots of source-bound cross-module analysis.

use std::collections::{BTreeMap, BTreeSet};

use analysis::analysis_inputs::{
    InferredSetConstruction, InferredSetOperation, ResolvedArgumentInput,
    ResolvedBindingValueInput, ResolvedImportedPrimitiveArraySpreadInput,
    ResolvedInitializerCallEffectInput, ResolvedNativeOperationInput, StableSetIterableUse,
};
use analysis::purity::saved::{PurityNodeId, PuritySnapshot, RestoredPurities};
use analysis::saved_proto::{ProtoConvert, proto, required};
use analysis_codec::{ContextOrdinals, SavedBinding};
use anyhow::{Result, ensure};
use program_summary_proto::debundle::program_summary as wire;
use swc_ecma_ast::Id;

use super::program_summary::ProgramSummary;

fn read<T, P>(value: Option<P>, field: &str) -> Result<T>
where
    T: ProtoConvert<P>,
{
    T::from_proto(required(value, field)?)
}

fn snapshot_binding(binding: &Id, ordinals: &ContextOrdinals) -> Result<proto::SavedBinding> {
    ordinals.snapshot_binding(binding)?.to_proto()
}

fn restore_binding(
    binding: Option<proto::SavedBinding>,
    field: &str,
    ordinals: &ContextOrdinals,
) -> Result<Id> {
    ordinals.restore_binding(read(binding, field)?)
}

fn snapshot_map<'a, K: Ord + Clone, T: 'a, P>(
    values: &'a BTreeMap<K, T>,
    mut snapshot: impl FnMut(&'a T) -> Result<P>,
) -> Result<BTreeMap<K, P>> {
    values
        .iter()
        .map(|(key, value)| Ok((key.clone(), snapshot(value)?)))
        .collect()
}

fn restore_map<K: Ord, T, P>(
    values: BTreeMap<K, P>,
    mut restore: impl FnMut(P) -> Result<T>,
) -> Result<BTreeMap<K, T>> {
    values
        .into_iter()
        .map(|(key, value)| Ok((key, restore(value)?)))
        .collect()
}

fn snapshot_names(values: &BTreeSet<String>) -> Result<wire::BindingNames> {
    Ok(wire::BindingNames {
        bindings: values.iter().cloned().collect(),
    })
}

fn restore_names(values: Vec<String>) -> Result<BTreeSet<String>> {
    let mut names = BTreeSet::new();
    for name in values {
        ensure!(names.insert(name), "duplicate program-summary name");
    }
    Ok(names)
}

fn snapshot_members(values: &BTreeMap<String, BTreeSet<String>>) -> Result<wire::ImportedMembers> {
    Ok(wire::ImportedMembers {
        bindings: snapshot_map(values, |members| {
            Ok(wire::MemberNames {
                members: members.iter().cloned().collect(),
            })
        })?,
    })
}

fn restore_members(value: wire::ImportedMembers) -> Result<BTreeMap<String, BTreeSet<String>>> {
    restore_map(value.bindings, |value| restore_names(value.members))
}

fn snapshot_signatures(
    values: &[spec::RetainedCallbackSignature],
) -> Result<wire::RetainedCallbackSignatures> {
    Ok(wire::RetainedCallbackSignatures {
        signatures: values
            .iter()
            .map(ProtoConvert::to_proto)
            .collect::<Result<_>>()?,
    })
}

fn restore_signatures(
    value: wire::RetainedCallbackSignatures,
) -> Result<Vec<spec::RetainedCallbackSignature>> {
    value
        .signatures
        .into_iter()
        .map(ProtoConvert::from_proto)
        .collect()
}

fn snapshot_callbacks(
    values: &BTreeMap<String, Vec<spec::RetainedCallbackSignature>>,
) -> Result<wire::RetainedCallbackBindings> {
    Ok(wire::RetainedCallbackBindings {
        bindings: snapshot_map(values, |values| snapshot_signatures(values))?,
    })
}

fn restore_callbacks(
    value: wire::RetainedCallbackBindings,
) -> Result<BTreeMap<String, Vec<spec::RetainedCallbackSignature>>> {
    restore_map(value.bindings, restore_signatures)
}

pub(super) fn snapshot<'a>(
    summary: &'a ProgramSummary,
    ordinals: &ContextOrdinals,
    purities: &mut PuritySnapshot<'a>,
) -> Result<wire::ProgramSummary> {
    Ok(wire::ProgramSummary {
        bindings: snapshot_map(&summary.bindings, |bindings| {
            Ok(wire::ImportedBindingPurities {
                bindings: snapshot_map(bindings, |purity| purities.snapshot(purity)?.to_proto())?,
            })
        })?,
        values: snapshot_map(&summary.values, |values| {
            Ok(wire::ImportedValues {
                bindings: snapshot_map(values, ProtoConvert::to_proto)?,
            })
        })?,
        primitive_reads: snapshot_map(&summary.primitive_reads, |reads| {
            Ok(wire::ImportedPrimitiveReads {
                bindings: snapshot_map(reads, ProtoConvert::to_proto)?,
            })
        })?,
        members: snapshot_map(&summary.members, snapshot_members)?,
        fluent: snapshot_map(&summary.fluent, snapshot_names)?,
        constructors: snapshot_map(&summary.constructors, snapshot_names)?,
        imported_callables: snapshot_map(&summary.imported_callables, |callables| {
            Ok(wire::ImportedCallables {
                bindings: snapshot_map(callables, ProtoConvert::to_proto)?,
            })
        })?,
        native_integrity: summary
            .native_integrity
            .as_ref()
            .map(ProtoConvert::to_proto)
            .transpose()?,
        bundle_globals: Some(summary.bundle_globals.to_proto()?),
        value_inputs: snapshot_map(&summary.value_inputs, |inputs| {
            Ok(wire::ValueInputSites {
                inputs: inputs
                    .iter()
                    .map(|input| snapshot_value_input(input, ordinals))
                    .collect::<Result<_>>()?,
            })
        })?,
        native_operation_inputs: snapshot_map(&summary.native_operation_inputs, |inputs| {
            Ok(wire::NativeOperationInputSites {
                inputs: inputs
                    .iter()
                    .map(|input| snapshot_native_operation_input(input, ordinals))
                    .collect::<Result<_>>()?,
            })
        })?,
        initializer_call_effect_inputs: snapshot_map(
            &summary.initializer_call_effect_inputs,
            |inputs| {
                Ok(wire::InitializerCallEffectInputSites {
                    inputs: inputs
                        .iter()
                        .map(|input| snapshot_initializer_call_effect_input(input, ordinals))
                        .collect::<Result<_>>()?,
                })
            },
        )?,
        imported_primitive_array_spread_inputs: snapshot_map(
            &summary.imported_primitive_array_spread_inputs,
            |inputs| {
                Ok(wire::ImportedPrimitiveArraySpreadInputSites {
                    inputs: inputs
                        .iter()
                        .map(snapshot_array_spread_input)
                        .collect::<Result<_>>()?,
                })
            },
        )?,
        inferred_set_constructions: snapshot_map(&summary.inferred_set_constructions, |sites| {
            Ok(wire::InferredSetSites {
                sites: snapshot_map(sites, |value| snapshot_set_construction(value, ordinals))?,
            })
        })?,
        argument_inputs: snapshot_map(&summary.argument_inputs, |inputs| {
            Ok(wire::ArgumentInputSites {
                inputs: inputs
                    .iter()
                    .map(|input| snapshot_argument_input(input, ordinals))
                    .collect::<Result<_>>()?,
            })
        })?,
        stable_imports: snapshot_map(&summary.stable_imports, snapshot_names)?,
        inline_callback_factories: snapshot_map(
            &summary.inline_callback_factories,
            snapshot_names,
        )?,
        callback_factory_members: snapshot_map(
            &summary.callback_factory_members,
            snapshot_members,
        )?,
        retained_callback_factories: snapshot_map(
            &summary.retained_callback_factories,
            snapshot_callbacks,
        )?,
        retained_callback_factory_members: snapshot_map(
            &summary.retained_callback_factory_members,
            |bindings| {
                Ok(wire::RetainedCallbackObjects {
                    bindings: snapshot_map(bindings, |members| {
                        Ok(wire::RetainedCallbackMembers {
                            members: snapshot_map(members, |values| snapshot_signatures(values))?,
                        })
                    })?,
                })
            },
        )?,
        retained_callback_constructors: snapshot_map(
            &summary.retained_callback_constructors,
            snapshot_callbacks,
        )?,
        no_reentry_exports: snapshot_map(&summary.no_reentry_exports, snapshot_names)?,
        no_reentry_callback_exports: snapshot_map(
            &summary.no_reentry_callback_exports,
            snapshot_callbacks,
        )?,
        no_reentry_members: snapshot_map(&summary.no_reentry_members, snapshot_members)?,
        no_reentry_record_members: snapshot_map(
            &summary.no_reentry_record_members,
            snapshot_members,
        )?,
        namespace_independent_members: snapshot_map(
            &summary.namespace_independent_members,
            snapshot_members,
        )?,
    })
}

pub(super) fn restore(
    value: wire::ProgramSummary,
    ordinals: &ContextOrdinals,
    purities: &RestoredPurities,
) -> Result<ProgramSummary> {
    Ok(ProgramSummary {
        bindings: restore_map(value.bindings, |bindings| {
            restore_map(bindings.bindings, |id| {
                purities.get(PurityNodeId::from_proto(id)?)
            })
        })?,
        values: restore_map(value.values, |values| {
            restore_map(values.bindings, ProtoConvert::from_proto)
        })?,
        primitive_reads: restore_map(value.primitive_reads, |reads| {
            restore_map(reads.bindings, ProtoConvert::from_proto)
        })?,
        members: restore_map(value.members, restore_members)?,
        fluent: restore_map(value.fluent, |value| restore_names(value.bindings))?,
        constructors: restore_map(value.constructors, |value| restore_names(value.bindings))?,
        imported_callables: restore_map(value.imported_callables, |callables| {
            restore_map(callables.bindings, ProtoConvert::from_proto)
        })?,
        native_integrity: value
            .native_integrity
            .map(ProtoConvert::from_proto)
            .transpose()?,
        bundle_globals: read(value.bundle_globals, "ProgramSummary.bundle_globals")?,
        value_inputs: restore_map(value.value_inputs, |value| {
            value
                .inputs
                .into_iter()
                .map(|input| restore_value_input(input, ordinals))
                .collect()
        })?,
        native_operation_inputs: restore_map(value.native_operation_inputs, |value| {
            value
                .inputs
                .into_iter()
                .map(|input| restore_native_operation_input(input, ordinals))
                .collect()
        })?,
        initializer_call_effect_inputs: restore_map(
            value.initializer_call_effect_inputs,
            |value| {
                value
                    .inputs
                    .into_iter()
                    .map(|input| restore_initializer_call_effect_input(input, ordinals))
                    .collect()
            },
        )?,
        imported_primitive_array_spread_inputs: restore_map(
            value.imported_primitive_array_spread_inputs,
            |value| {
                value
                    .inputs
                    .into_iter()
                    .map(restore_array_spread_input)
                    .collect()
            },
        )?,
        inferred_set_constructions: restore_map(value.inferred_set_constructions, |value| {
            restore_map(value.sites, |value| {
                restore_set_construction(value, ordinals)
            })
        })?,
        argument_inputs: restore_map(value.argument_inputs, |value| {
            value
                .inputs
                .into_iter()
                .map(|input| restore_argument_input(input, ordinals))
                .collect()
        })?,
        stable_imports: restore_map(value.stable_imports, |value| restore_names(value.bindings))?,
        inline_callback_factories: restore_map(value.inline_callback_factories, |value| {
            restore_names(value.bindings)
        })?,
        callback_factory_members: restore_map(value.callback_factory_members, restore_members)?,
        retained_callback_factories: restore_map(
            value.retained_callback_factories,
            restore_callbacks,
        )?,
        retained_callback_factory_members: restore_map(
            value.retained_callback_factory_members,
            |value| {
                restore_map(value.bindings, |value| {
                    restore_map(value.members, restore_signatures)
                })
            },
        )?,
        retained_callback_constructors: restore_map(
            value.retained_callback_constructors,
            restore_callbacks,
        )?,
        no_reentry_exports: restore_map(value.no_reentry_exports, |value| {
            restore_names(value.bindings)
        })?,
        no_reentry_callback_exports: restore_map(
            value.no_reentry_callback_exports,
            restore_callbacks,
        )?,
        no_reentry_members: restore_map(value.no_reentry_members, restore_members)?,
        no_reentry_record_members: restore_map(value.no_reentry_record_members, restore_members)?,
        namespace_independent_members: restore_map(
            value.namespace_independent_members,
            restore_members,
        )?,
    })
}

fn snapshot_value_input(
    input: &ResolvedBindingValueInput,
    ordinals: &ContextOrdinals,
) -> Result<wire::ValueInput> {
    Ok(wire::ValueInput {
        evidence: Some(input.evidence.to_proto()?),
        binding: Some(snapshot_binding(&input.binding, ordinals)?),
        declaration_span: Some(input.declaration_span.to_proto()?),
        initializer_span: Some(input.initializer_span.to_proto()?),
        initializer_ordinal: Some(input.initializer_ordinal.to_proto()?),
    })
}

fn restore_value_input(
    value: wire::ValueInput,
    ordinals: &ContextOrdinals,
) -> Result<ResolvedBindingValueInput> {
    Ok(ResolvedBindingValueInput {
        evidence: read(value.evidence, "ValueInput.evidence")?,
        binding: restore_binding(value.binding, "ValueInput.binding", ordinals)?,
        declaration_span: read(value.declaration_span, "ValueInput.declaration_span")?,
        initializer_span: read(value.initializer_span, "ValueInput.initializer_span")?,
        initializer_ordinal: read(value.initializer_ordinal, "ValueInput.initializer_ordinal")?,
    })
}

fn snapshot_argument_input(
    input: &ResolvedArgumentInput,
    ordinals: &ContextOrdinals,
) -> Result<wire::ArgumentInput> {
    Ok(wire::ArgumentInput {
        evidence: Some(input.evidence.to_proto()?),
        binding: Some(snapshot_binding(&input.binding, ordinals)?),
        function_start: Some(input.function_start),
    })
}

fn restore_argument_input(
    value: wire::ArgumentInput,
    ordinals: &ContextOrdinals,
) -> Result<ResolvedArgumentInput> {
    Ok(ResolvedArgumentInput {
        evidence: read(value.evidence, "ArgumentInput.evidence")?,
        binding: restore_binding(value.binding, "ArgumentInput.binding", ordinals)?,
        function_start: required(value.function_start, "ArgumentInput.function_start")?,
    })
}

fn snapshot_initializer_call_effect_input(
    input: &ResolvedInitializerCallEffectInput,
    ordinals: &ContextOrdinals,
) -> Result<wire::InitializerCallEffectInput> {
    Ok(wire::InitializerCallEffectInput {
        reference: Some(input.reference.to_proto()?),
        binding: Some(snapshot_binding(&input.binding, ordinals)?),
        callee: Some(snapshot_binding(&input.callee, ordinals)?),
        call_span: Some(input.call_span.to_proto()?),
        source_sha256: Some(input.source_sha256.clone()),
        provider_chunk: Some(input.provider_chunk.clone()),
        provider_export: Some(input.provider_export.clone()),
        provider_sha256: Some(input.provider_sha256.clone()),
        returns_opaque_function_from_const_primitive: Some(
            input.returns_opaque_function_from_const_primitive,
        ),
        effective_replacement: input
            .effective_replacement
            .as_ref()
            .map(ProtoConvert::to_proto)
            .transpose()?,
    })
}

fn restore_initializer_call_effect_input(
    value: wire::InitializerCallEffectInput,
    ordinals: &ContextOrdinals,
) -> Result<ResolvedInitializerCallEffectInput> {
    Ok(ResolvedInitializerCallEffectInput {
        reference: read(value.reference, "InitializerCallEffectInput.reference")?,
        binding: restore_binding(
            value.binding,
            "InitializerCallEffectInput.binding",
            ordinals,
        )?,
        callee: restore_binding(value.callee, "InitializerCallEffectInput.callee", ordinals)?,
        call_span: read(value.call_span, "InitializerCallEffectInput.call_span")?,
        source_sha256: required(
            value.source_sha256,
            "InitializerCallEffectInput.source_sha256",
        )?,
        provider_chunk: required(
            value.provider_chunk,
            "InitializerCallEffectInput.provider_chunk",
        )?,
        provider_export: required(
            value.provider_export,
            "InitializerCallEffectInput.provider_export",
        )?,
        provider_sha256: required(
            value.provider_sha256,
            "InitializerCallEffectInput.provider_sha256",
        )?,
        returns_opaque_function_from_const_primitive: required(
            value.returns_opaque_function_from_const_primitive,
            "InitializerCallEffectInput.returns_opaque_function_from_const_primitive",
        )?,
        effective_replacement: value
            .effective_replacement
            .map(ProtoConvert::from_proto)
            .transpose()?,
    })
}

fn snapshot_native_operation_input(
    input: &ResolvedNativeOperationInput,
    ordinals: &ContextOrdinals,
) -> Result<wire::NativeOperationInput> {
    Ok(wire::NativeOperationInput {
        evidence: Some(input.evidence.to_proto()?),
        binding: Some(snapshot_binding(&input.binding, ordinals)?),
        selected_statement_span: Some(input.selected_statement_span.to_proto()?),
        declaration_span: Some(input.declaration_span.to_proto()?),
        initializer_span: Some(input.initializer_span.to_proto()?),
        call_span: Some(input.call_span.to_proto()?),
        initializer_ordinal: Some(input.initializer_ordinal.to_proto()?),
    })
}

fn restore_native_operation_input(
    value: wire::NativeOperationInput,
    ordinals: &ContextOrdinals,
) -> Result<ResolvedNativeOperationInput> {
    Ok(ResolvedNativeOperationInput {
        evidence: read(value.evidence, "NativeOperationInput.evidence")?,
        binding: restore_binding(value.binding, "NativeOperationInput.binding", ordinals)?,
        selected_statement_span: read(
            value.selected_statement_span,
            "NativeOperationInput.selected_statement_span",
        )?,
        declaration_span: read(
            value.declaration_span,
            "NativeOperationInput.declaration_span",
        )?,
        initializer_span: read(
            value.initializer_span,
            "NativeOperationInput.initializer_span",
        )?,
        call_span: read(value.call_span, "NativeOperationInput.call_span")?,
        initializer_ordinal: read(
            value.initializer_ordinal,
            "NativeOperationInput.initializer_ordinal",
        )?,
    })
}

fn snapshot_array_spread_input(
    input: &ResolvedImportedPrimitiveArraySpreadInput,
) -> Result<wire::ImportedPrimitiveArraySpreadInput> {
    Ok(wire::ImportedPrimitiveArraySpreadInput {
        evidence: Some(input.evidence.to_proto()?),
        consumer_initializer_span: Some(input.consumer_initializer_span.to_proto()?),
        spread_token_spans: input
            .spread_token_spans
            .iter()
            .map(ProtoConvert::to_proto)
            .collect::<Result<_>>()?,
    })
}

fn restore_array_spread_input(
    value: wire::ImportedPrimitiveArraySpreadInput,
) -> Result<ResolvedImportedPrimitiveArraySpreadInput> {
    Ok(ResolvedImportedPrimitiveArraySpreadInput {
        evidence: read(value.evidence, "ImportedPrimitiveArraySpreadInput.evidence")?,
        consumer_initializer_span: read(
            value.consumer_initializer_span,
            "ImportedPrimitiveArraySpreadInput.consumer_initializer_span",
        )?,
        spread_token_spans: value
            .spread_token_spans
            .into_iter()
            .map(swc_common::Span::from_proto)
            .collect::<Result<_>>()?,
    })
}

fn snapshot_stable_set_use(
    value: &StableSetIterableUse,
    ordinals: &ContextOrdinals,
) -> Result<wire::StableSetIterableUse> {
    Ok(wire::StableSetIterableUse {
        source_span: Some(value.source_span.to_proto()?),
        spread_token_span: Some(value.spread_token_span.to_proto()?),
        interval_bindings: value
            .interval_bindings
            .iter()
            .map(|binding| snapshot_binding(binding, ordinals))
            .collect::<Result<_>>()?,
        dependencies: Some(value.dependencies.to_proto()?),
    })
}

fn restore_stable_set_use(
    value: wire::StableSetIterableUse,
    ordinals: &ContextOrdinals,
) -> Result<StableSetIterableUse> {
    Ok(StableSetIterableUse {
        source_span: read(value.source_span, "StableSetIterableUse.source_span")?,
        spread_token_span: read(
            value.spread_token_span,
            "StableSetIterableUse.spread_token_span",
        )?,
        interval_bindings: value
            .interval_bindings
            .into_iter()
            .map(|binding| ordinals.restore_binding(SavedBinding::from_proto(binding)?))
            .collect::<Result<_>>()?,
        dependencies: read(value.dependencies, "StableSetIterableUse.dependencies")?,
    })
}

fn snapshot_set_construction(
    value: &InferredSetConstruction,
    ordinals: &ContextOrdinals,
) -> Result<wire::InferredSetConstruction> {
    let operation = match &value.operation {
        InferredSetOperation::PrimitiveArray => {
            wire::inferred_set_operation::Value::PrimitiveArray(Default::default())
        }
        InferredSetOperation::ProtectedSpread { source, interval } => {
            wire::inferred_set_operation::Value::ProtectedSpread(
                wire::inferred_set_operation::ProtectedSpread {
                    source: Some(snapshot_binding(source, ordinals)?),
                    interval: Some(snapshot_stable_set_use(interval, ordinals)?),
                },
            )
        }
    };
    Ok(wire::InferredSetConstruction {
        binding: Some(snapshot_binding(&value.binding, ordinals)?),
        operation: Some(wire::InferredSetOperation {
            value: Some(operation),
        }),
        dependencies: Some(value.dependencies.to_proto()?),
        inference: Some(value.inference.to_proto()?),
    })
}

fn restore_set_construction(
    value: wire::InferredSetConstruction,
    ordinals: &ContextOrdinals,
) -> Result<InferredSetConstruction> {
    let operation = required(value.operation, "InferredSetConstruction.operation")?;
    let operation = match required(operation.value, "InferredSetOperation.value")? {
        wire::inferred_set_operation::Value::PrimitiveArray(_) => {
            InferredSetOperation::PrimitiveArray
        }
        wire::inferred_set_operation::Value::ProtectedSpread(value) => {
            InferredSetOperation::ProtectedSpread {
                source: restore_binding(value.source, "ProtectedSpread.source", ordinals)?,
                interval: Box::new(restore_stable_set_use(
                    required(value.interval, "ProtectedSpread.interval")?,
                    ordinals,
                )?),
            }
        }
    };
    Ok(InferredSetConstruction {
        binding: restore_binding(value.binding, "InferredSetConstruction.binding", ordinals)?,
        operation,
        dependencies: read(value.dependencies, "InferredSetConstruction.dependencies")?,
        inference: read(value.inference, "InferredSetConstruction.inference")?,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use analysis::analysis_inputs::ImportedPrimitiveRead;
    use std::collections::BTreeSet;

    #[test]
    fn collection_defaults_and_native_census_availability_roundtrip() {
        let primitive_reads = BTreeMap::from([(
            "app".to_string(),
            BTreeMap::from([(
                "limit".to_string(),
                ImportedPrimitiveRead {
                    kinds: analysis_model::PrimKinds::NUMBER,
                    provider_module: "provider".to_string(),
                    dependency_closure: BTreeSet::from(["provider".to_string()]),
                    source_cells: BTreeSet::from([
                        analysis::analysis_inputs::PrimitiveExportCell {
                            module: "provider".to_string(),
                            export: "".to_string(),
                        },
                        analysis::analysis_inputs::PrimitiveExportCell {
                            module: "provider".to_string(),
                            export: "limit".to_string(),
                        },
                    ]),
                },
            )]),
        )]);
        assert_eq!(
            serde_json::to_string(&primitive_reads["app"]["limit"].source_cells).unwrap(),
            r#"[{"module":"provider","export":""},{"module":"provider","export":"limit"}]"#
        );
        let mut summary = ProgramSummary {
            bindings: BTreeMap::new(),
            values: BTreeMap::new(),
            primitive_reads: primitive_reads.clone(),
            members: BTreeMap::new(),
            fluent: BTreeMap::new(),
            constructors: BTreeMap::new(),
            imported_callables: BTreeMap::new(),
            native_integrity: None,
            bundle_globals: Default::default(),
            value_inputs: BTreeMap::new(),
            native_operation_inputs: BTreeMap::new(),
            initializer_call_effect_inputs: BTreeMap::new(),
            imported_primitive_array_spread_inputs: BTreeMap::new(),
            inferred_set_constructions: BTreeMap::new(),
            argument_inputs: BTreeMap::new(),
            stable_imports: BTreeMap::new(),
            inline_callback_factories: BTreeMap::new(),
            callback_factory_members: BTreeMap::new(),
            retained_callback_factories: BTreeMap::new(),
            retained_callback_factory_members: BTreeMap::new(),
            retained_callback_constructors: BTreeMap::new(),
            no_reentry_exports: BTreeMap::new(),
            no_reentry_callback_exports: BTreeMap::new(),
            no_reentry_members: BTreeMap::new(),
            no_reentry_record_members: BTreeMap::new(),
            namespace_independent_members: BTreeMap::new(),
        };
        let ordinals = ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>());
        let mut purities = PuritySnapshot::default();
        let wire =
            snapshot(&summary, &ordinals, &mut purities).expect("empty program summary snapshots");
        let restored_purities =
            RestoredPurities::restore(purities.finish()).expect("purity graph restores");
        assert!(wire.native_integrity.is_none());

        // Ordinary collections have proto3 empty defaults. Their omission is no
        // longer distinguished from an intentionally empty producer result.
        let mut empty_collections = wire.clone();
        empty_collections.primitive_reads.clear();
        empty_collections
            .imported_primitive_array_spread_inputs
            .clear();
        let restored = restore(empty_collections, &ordinals, &restored_purities).unwrap();
        assert!(restored.primitive_reads.is_empty());
        assert!(restored.imported_primitive_array_spread_inputs.is_empty());

        let mut missing_globals = wire.clone();
        missing_globals.bundle_globals = None;
        assert!(restore(missing_globals, &ordinals, &restored_purities).is_err());

        let mut missing_read_proof = wire.clone();
        missing_read_proof
            .primitive_reads
            .get_mut("app")
            .unwrap()
            .bindings
            .insert("limit".into(), proto::ImportedPrimitiveRead::default());
        assert!(restore(missing_read_proof, &ordinals, &restored_purities).is_err());

        let mut duplicate_cells = wire.clone();
        let read = duplicate_cells
            .primitive_reads
            .get_mut("app")
            .unwrap()
            .bindings
            .get_mut("limit")
            .unwrap();
        let duplicate = read.source_cells[0].clone();
        read.source_cells.push(duplicate);
        assert!(restore(duplicate_cells, &ordinals, &restored_purities).is_err());

        let mut missing_input_evidence = wire.clone();
        missing_input_evidence.value_inputs.insert(
            "app".into(),
            wire::ValueInputSites {
                inputs: vec![wire::ValueInput::default()],
            },
        );
        assert!(restore(missing_input_evidence, &ordinals, &restored_purities).is_err());

        let mut duplicate_names = wire.clone();
        duplicate_names.stable_imports.insert(
            "app".into(),
            wire::BindingNames {
                bindings: vec!["limit".into(), "limit".into()],
            },
        );
        assert!(restore(duplicate_names, &ordinals, &restored_purities).is_err());

        let bytes = analysis_codec::encode_protobuf(&wire).expect("encode saved summary");
        let decoded: wire::ProgramSummary =
            analysis_codec::decode_protobuf(&bytes).expect("decode saved summary");
        let restored =
            restore(decoded, &ordinals, &restored_purities).expect("absent native census restores");
        assert!(restored.native_integrity.is_none());
        assert_eq!(restored.primitive_reads, primitive_reads);

        summary.native_integrity = Some(Default::default());
        summary
            .value_inputs
            .insert("empty-provider".into(), Vec::new());
        summary.retained_callback_factory_members.insert(
            "app".into(),
            BTreeMap::from([(
                "factory".into(),
                BTreeMap::from([("member".into(), Vec::new())]),
            )]),
        );
        let mut purities = PuritySnapshot::default();
        let available = snapshot(&summary, &ordinals, &mut purities).unwrap();
        assert!(available.native_integrity.is_some());
        let bytes = analysis_codec::encode_protobuf(&available).unwrap();
        let restored = restore(
            analysis_codec::decode_protobuf(&bytes).unwrap(),
            &ordinals,
            &restored_purities,
        )
        .unwrap();
        let census = restored
            .native_integrity
            .expect("empty census remains available");
        assert!(census.mutated.is_empty());
        assert!(restored.value_inputs.contains_key("empty-provider"));
        assert!(
            restored.retained_callback_factory_members["app"]["factory"].contains_key("member")
        );
    }

    #[test]
    fn contextual_wire_rejects_unknown_bindings_and_missing_interval_proof() {
        let ordinals = ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>());
        assert!(
            restore_binding(
                Some(proto::SavedBinding {
                    symbol: Some("input".to_string()),
                    context_ordinal: Some(u32::MAX),
                }),
                "binding",
                &ordinals
            )
            .is_err()
        );

        let interval = StableSetIterableUse {
            source_span: swc_common::Span::new(swc_common::BytePos(3), swc_common::BytePos(7)),
            spread_token_span: swc_common::Span::new(
                swc_common::BytePos(2),
                swc_common::BytePos(3),
            ),
            interval_bindings: Vec::new(),
            dependencies: analysis::analysis_inputs::ProofDependencies::default(),
        };
        let mut wire = snapshot_stable_set_use(&interval, &ordinals).expect("interval snapshots");
        wire.dependencies = None;
        let error = restore_stable_set_use(wire, &ordinals).expect_err("proof is required");
        assert!(
            error
                .to_string()
                .contains("StableSetIterableUse.dependencies")
        );
    }

    #[test]
    fn missing_set_operation_discriminant_is_rejected() {
        let ordinals = ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>());
        let value = wire::InferredSetConstruction {
            operation: Some(wire::InferredSetOperation { value: None }),
            ..wire::InferredSetConstruction::default()
        };
        let error = restore_set_construction(value, &ordinals).expect_err("operation required");
        assert!(error.to_string().contains("InferredSetOperation.value"));
    }

    #[test]
    fn native_maps_encode_deterministically_and_purity_roots_stay_shared() {
        use prost::Message;
        use std::sync::Arc;
        let purity = Arc::new(analysis::purity::Purity::Pure {
            dependencies: Default::default(),
        });
        let mut snapshot = PuritySnapshot::default();
        let id = snapshot.snapshot(&purity).unwrap().to_proto().unwrap();
        let repeated_id = snapshot.snapshot(&purity).unwrap().to_proto().unwrap();
        let graph = snapshot.finish();
        assert_eq!(graph.nodes.len(), 1);
        let roots = wire::ImportedBindingPurities {
            bindings: BTreeMap::from([("left".into(), id), ("right".into(), repeated_id)]),
        };
        let a = wire::ProgramSummary {
            bindings: BTreeMap::from([("z".into(), roots.clone()), ("a".into(), roots.clone())]),
            bundle_globals: Some(
                analysis::analysis_inputs::BundleGlobalNames::default()
                    .to_proto()
                    .unwrap(),
            ),
            ..Default::default()
        };
        let mut b = a.clone();
        b.bindings = BTreeMap::from([("a".into(), roots.clone()), ("z".into(), roots)]);
        assert_eq!(a.encode_to_vec(), b.encode_to_vec());
        let decoded = wire::ProgramSummary::decode(a.encode_to_vec().as_slice()).unwrap();
        let ordinals = ContextOrdinals::new(std::iter::empty::<&swc_ecma_ast::Module>());
        let graph = RestoredPurities::restore(graph).unwrap();
        let restored = restore(decoded, &ordinals, &graph).unwrap();
        assert!(Arc::ptr_eq(
            &restored.bindings["a"]["left"],
            &restored.bindings["z"]["right"]
        ));
    }
}
