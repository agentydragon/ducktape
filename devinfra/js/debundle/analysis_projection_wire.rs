//! Explicit conversion for analysis-only inputs. Authored types use spec_wire.
use analysis_projection_wire_proto::debundle::analysis_projection as pb;
use analysis_vocabulary::inputs::{
    AnalysisAssumptions, AnalysisImportedPrimitiveArrayOperand,
    AnalysisImportedPrimitiveArraySpreadInput, BundleGlobalNames, KnownEffect, LocalEffectPolicy,
    NativeOperationSelectorProvenance, NoAccessorInstallRef, SourceDeclaration,
};
use anyhow::{Context, Result, ensure};
use spec_wire::{required, unique_map, unique_set};
use std::collections::BTreeMap;

fn source_declarations_to_wire(
    value: &BTreeMap<String, Vec<SourceDeclaration>>,
) -> Result<BTreeMap<String, pb::ChunkSourceDeclarations>> {
    value
        .iter()
        .map(|(key, declarations)| {
            Ok((
                key.clone(),
                pb::ChunkSourceDeclarations {
                    declarations: declarations
                        .iter()
                        .map(encode_declaration)
                        .collect::<Result<_>>()?,
                },
            ))
        })
        .collect()
}
fn source_declarations_from_wire(
    value: BTreeMap<String, pb::ChunkSourceDeclarations>,
) -> Result<BTreeMap<String, Vec<SourceDeclaration>>> {
    value
        .into_iter()
        .map(|(key, value)| {
            Ok((
                key,
                value
                    .declarations
                    .into_iter()
                    .map(decode_declaration)
                    .collect::<Result<_>>()?,
            ))
        })
        .collect()
}

fn encode_statement_callbacks(
    value: &BTreeMap<(u32, u32), spec::RetainedCallbackSignature>,
) -> Vec<pb::StatementCallback> {
    value
        .iter()
        .map(|((start, end), signature)| pb::StatementCallback {
            start: Some(*start),
            end: Some(*end),
            signature: Some(spec_wire::encode_retained_callback_signature(signature)),
        })
        .collect()
}
fn decode_statement_callbacks(
    value: Vec<pb::StatementCallback>,
) -> Result<BTreeMap<(u32, u32), spec::RetainedCallbackSignature>> {
    unique_map(value.into_iter().map(|entry| {
        Ok((
            (required(entry.start)?, required(entry.end)?),
            spec_wire::decode_retained_callback_signature(required(entry.signature)?)?,
        ))
    }))
}
// Analysis projections retain the existing UTF-8 source-path contract. Apply
// projections separately support all Linux path bytes through the shared codec.
fn validate_analysis_paths(value: &crate::AnalysisSpec) -> Result<()> {
    for path in [
        Some(&value.inputs.input_root),
        Some(&value.inputs.js_list_path),
        value.inputs.embedded_loaders_path.as_ref(),
    ]
    .into_iter()
    .flatten()
    {
        path.to_str()
            .context("analysis projection path is not UTF-8")?;
    }
    for vendor in value.vendor.values() {
        if let spec::VendorLevel::BundledPartialSwap(mark) = &vendor.level {
            mark.bundle
                .path
                .to_str()
                .context("analysis projection path is not UTF-8")?;
        }
    }
    Ok(())
}

pub(super) fn encode_projection(value: &crate::AnalysisSpec) -> Result<pb::AnalysisSpec> {
    validate_analysis_paths(value)?;
    Ok(pb::AnalysisSpec {
        inputs: Some(spec_wire::encode_load_js_chunks_args(&value.inputs)),
        host_behavior: Some(spec_wire::encode_host_behavior(&value.host_behavior)),
        program_premises: Some(spec_wire::encode_program_premises(&value.program_premises)),
        vendor: value
            .vendor
            .iter()
            .map(|(key, value)| (key.clone(), spec_wire::encode_vendor_mark(value)))
            .collect(),
        chunk_export_purity: value
            .chunk_export_purity
            .iter()
            .map(|(key, value)| (key.clone(), spec_wire::encode_chunk_export_purity(value)))
            .collect(),
        chunk_analysis_inputs: value
            .chunk_analysis_inputs
            .iter()
            .map(|(key, value)| Ok((key.clone(), encode_chunk_inputs(value)?)))
            .collect::<Result<_>>()?,
        analysed_chunks: value.analysed_chunks.clone(),
        entry_file: value.entry_file.clone(),
        prune_other_chunks: Some(value.prune_other_chunks),
        declared_assumptions: value
            .declared_assumptions
            .iter()
            .map(|(key, value)| Ok((key.clone(), encode_assumptions(value)?)))
            .collect::<Result<_>>()?,
        source_sites: Some(encode_source_sites(&value.source_sites)?),
        native_operation_inputs: value
            .native_operation_inputs
            .iter()
            .map(|(key, inputs)| {
                Ok((
                    key.clone(),
                    pb::ChunkNativeOperationInputs {
                        inputs: inputs
                            .iter()
                            .map(encode_native_input)
                            .collect::<Result<_>>()?,
                    },
                ))
            })
            .collect::<Result<_>>()?,
    })
}
pub(super) fn decode_projection(v: pb::AnalysisSpec) -> Result<crate::AnalysisSpec> {
    let result = crate::AnalysisSpec {
        inputs: spec_wire::decode_load_js_chunks_args(required(v.inputs)?)?,
        host_behavior: spec_wire::decode_host_behavior(required(v.host_behavior)?)?,
        program_premises: spec_wire::decode_program_premises(required(v.program_premises)?)?,
        vendor: v
            .vendor
            .into_iter()
            .map(|(key, value)| Ok((key, spec_wire::decode_vendor_mark(value)?)))
            .collect::<Result<_>>()?,
        chunk_export_purity: v
            .chunk_export_purity
            .into_iter()
            .map(|(key, value)| Ok((key, spec_wire::decode_chunk_export_purity(value)?)))
            .collect::<Result<_>>()?,
        chunk_analysis_inputs: v
            .chunk_analysis_inputs
            .into_iter()
            .map(|(key, value)| Ok((key, decode_chunk_inputs(value)?)))
            .collect::<Result<_>>()?,
        analysed_chunks: v.analysed_chunks,
        entry_file: v.entry_file,
        prune_other_chunks: required(v.prune_other_chunks)?,
        declared_assumptions: v
            .declared_assumptions
            .into_iter()
            .map(|(key, value)| Ok((key, decode_assumptions(value)?)))
            .collect::<Result<_>>()?,
        source_sites: decode_source_sites(required(v.source_sites)?)?,
        native_operation_inputs: v
            .native_operation_inputs
            .into_iter()
            .map(|(key, value)| {
                Ok((
                    key,
                    value
                        .inputs
                        .into_iter()
                        .map(decode_native_input)
                        .collect::<Result<_>>()?,
                ))
            })
            .collect::<Result<_>>()?,
    };
    validate_analysis_paths(&result)?;
    Ok(result)
}
fn encode_chunk_inputs(value: &crate::ChunkAnalysisInputs) -> Result<pb::ChunkAnalysisInputs> {
    Ok(pb::ChunkAnalysisInputs {
        trusted_dataflow_summaries: Some(value.trusted_dataflow_summaries),
        deferred_global_publications: spec_wire::global_names_to_proto(
            &value.deferred_global_publications,
        ),
        non_reentrant_global_membership: spec_wire::global_names_to_proto(
            &value.non_reentrant_global_membership,
        ),
        local_effect_policy: Some(value.local_effect_policy as i32),
        admission_overrides: Some(spec_wire::encode_admission_overrides(
            &value.admission_overrides,
        )),
        value_inputs: value
            .value_inputs
            .iter()
            .map(spec_wire::encode_value_input_contract)
            .collect(),
        native_call_identity_inputs: value
            .native_call_identity_inputs
            .iter()
            .map(spec_wire::encode_native_call_identity_input_contract)
            .collect(),
        initializer_call_effect_inputs: value
            .initializer_call_effect_inputs
            .iter()
            .map(spec_wire::encode_initializer_call_effect_input_contract)
            .collect(),
        argument_inputs: value
            .argument_inputs
            .iter()
            .map(spec_wire::encode_argument_input_contract)
            .collect(),
        no_accessor_install_callables: value
            .no_accessor_install_callables
            .iter()
            .map(spec_wire::encode_no_accessor_install_callable_contract)
            .collect(),
    })
}
fn decode_chunk_inputs(v: pb::ChunkAnalysisInputs) -> Result<crate::ChunkAnalysisInputs> {
    Ok(crate::ChunkAnalysisInputs {
        trusted_dataflow_summaries: required(v.trusted_dataflow_summaries)?,
        deferred_global_publications: spec_wire::global_names_from_proto(
            v.deferred_global_publications,
        )?,
        non_reentrant_global_membership: spec_wire::global_names_from_proto(
            v.non_reentrant_global_membership,
        )?,
        local_effect_policy: LocalEffectPolicy::try_from(
            v.local_effect_policy
                .context("missing local effect policy")?,
        )?,
        admission_overrides: spec_wire::decode_admission_overrides(required(
            v.admission_overrides,
        )?)?,
        value_inputs: v
            .value_inputs
            .into_iter()
            .map(spec_wire::decode_value_input_contract)
            .collect::<Result<_>>()?,
        native_call_identity_inputs: v
            .native_call_identity_inputs
            .into_iter()
            .map(spec_wire::decode_native_call_identity_input_contract)
            .collect::<Result<_>>()?,
        initializer_call_effect_inputs: v
            .initializer_call_effect_inputs
            .into_iter()
            .map(spec_wire::decode_initializer_call_effect_input_contract)
            .collect::<Result<_>>()?,
        argument_inputs: v
            .argument_inputs
            .into_iter()
            .map(spec_wire::decode_argument_input_contract)
            .collect::<Result<_>>()?,
        no_accessor_install_callables: v
            .no_accessor_install_callables
            .into_iter()
            .map(spec_wire::decode_no_accessor_install_callable_contract)
            .collect::<Result<_>>()?,
    })
}
fn encode_no_accessor_ref(value: &NoAccessorInstallRef) -> Result<pb::NoAccessorInstallRef> {
    Ok(pb::NoAccessorInstallRef {
        defining_chunk: Some(value.defining_chunk.clone()),
        input_index: Some(value.input_index.try_into()?),
    })
}
fn decode_no_accessor_ref(value: pb::NoAccessorInstallRef) -> Result<NoAccessorInstallRef> {
    Ok(NoAccessorInstallRef {
        defining_chunk: required(value.defining_chunk)?,
        input_index: required(value.input_index)?.try_into()?,
    })
}
fn encode_bundle_globals(value: &BundleGlobalNames) -> Result<pb::BundleGlobalNames> {
    Ok(pb::BundleGlobalNames {
        names: value.names.iter().cloned().collect(),
        registered_symbols: Some(value.registered_symbols),
        fresh_collections: Some(value.fresh_collections),
        no_accessor_install_callables: value
            .no_accessor_install_callables
            .iter()
            .map(encode_no_accessor_ref)
            .collect::<Result<_>>()?,
    })
}
fn decode_bundle_globals(value: pb::BundleGlobalNames) -> Result<BundleGlobalNames> {
    Ok(BundleGlobalNames {
        names: unique_set(value.names.into_iter().map(Ok))?,
        registered_symbols: required(value.registered_symbols)?,
        fresh_collections: required(value.fresh_collections)?,
        no_accessor_install_callables: unique_set(
            value
                .no_accessor_install_callables
                .into_iter()
                .map(decode_no_accessor_ref),
        )?,
    })
}
fn encode_assumptions(value: &AnalysisAssumptions) -> Result<pb::AnalysisAssumptions> {
    super::validate_declared_assumptions(value)?;
    let AnalysisAssumptions {
        retained_record_factories,
        reentry_free_operations,
        predefined_globals,
        bundle_globals,
        deferred_global_publications,
        non_reentrant_global_membership,
        declared_pure,
        declared_pure_new,
        declared_pure_initializers,
        declared_pure_members,
        no_sync_callback_members,
        known_effects,
        trusted_dataflow_summaries,
        inline_callback_factories,
        callback_factory_members,
        retained_callback_factories,
        retained_callback_factory_members,
        retained_callback_constructors,
        no_reentry_exports,
        no_reentry_callback_exports,
        no_reentry_statement_calls,
        no_reentry_members,
        no_reentry_record_members,
        namespace_independent_members,
        fluent_bindings,
        captured_value_inputs: _,
        native_operation_inputs: _,
        initializer_call_effect_inputs: _,
        imported_primitive_array_spread_inputs: _,
        argument_inputs: _,
    } = value;
    Ok(pb::AnalysisAssumptions {
        retained_record_factories: retained_record_factories.iter().cloned().collect(),
        reentry_free_operations: reentry_free_operations
            .iter()
            .map(|operation| *operation as i32)
            .collect(),
        predefined_globals: Some(*predefined_globals as i32),
        bundle_globals: Some(encode_bundle_globals(bundle_globals)?),
        deferred_global_publications: spec_wire::global_names_to_proto(
            deferred_global_publications,
        ),
        non_reentrant_global_membership: spec_wire::global_names_to_proto(
            non_reentrant_global_membership,
        ),
        declared_pure: declared_pure.iter().cloned().collect(),
        declared_pure_new: declared_pure_new.iter().cloned().collect(),
        declared_pure_initializers: declared_pure_initializers.iter().cloned().collect(),
        declared_pure_members: spec_wire::member_names_to_proto(declared_pure_members),
        no_sync_callback_members: spec_wire::member_names_to_proto(no_sync_callback_members),
        known_effects: known_effects
            .iter()
            .map(|(binding, effect)| (binding.clone(), *effect as i32))
            .collect(),
        trusted_dataflow_summaries: Some(*trusted_dataflow_summaries),
        inline_callback_factories: inline_callback_factories.iter().cloned().collect(),
        callback_factory_members: spec_wire::member_names_to_proto(callback_factory_members),
        retained_callback_factories: spec_wire::callback_overloads_to_proto(
            retained_callback_factories,
        ),
        retained_callback_factory_members: spec_wire::member_callback_overloads_to_proto(
            retained_callback_factory_members,
        ),
        retained_callback_constructors: spec_wire::callback_overloads_to_proto(
            retained_callback_constructors,
        ),
        no_reentry_exports: no_reentry_exports.iter().cloned().collect(),
        no_reentry_callback_exports: spec_wire::callback_overloads_to_proto(
            no_reentry_callback_exports,
        ),
        no_reentry_statement_calls: encode_statement_callbacks(no_reentry_statement_calls),
        no_reentry_members: spec_wire::member_names_to_proto(no_reentry_members),
        no_reentry_record_members: spec_wire::member_names_to_proto(no_reentry_record_members),
        namespace_independent_members: spec_wire::member_names_to_proto(
            namespace_independent_members,
        ),
        fluent_bindings: fluent_bindings.iter().cloned().collect(),
    })
}
fn decode_assumptions(v: pb::AnalysisAssumptions) -> Result<AnalysisAssumptions> {
    Ok(AnalysisAssumptions {
        retained_record_factories: unique_set(v.retained_record_factories.into_iter().map(Ok))?,
        reentry_free_operations: unique_set(
            v.reentry_free_operations
                .into_iter()
                .map(|value| Ok(spec::ReentryFreeOperation::try_from(value)?)),
        )?,
        predefined_globals: spec::PredefinedGlobals::try_from(
            v.predefined_globals.context("missing predefined globals")?,
        )?,
        bundle_globals: decode_bundle_globals(required(v.bundle_globals)?)?,
        deferred_global_publications: spec_wire::global_names_from_proto(
            v.deferred_global_publications,
        )?,
        non_reentrant_global_membership: spec_wire::global_names_from_proto(
            v.non_reentrant_global_membership,
        )?,
        declared_pure: unique_set(v.declared_pure.into_iter().map(Ok))?,
        declared_pure_new: unique_set(v.declared_pure_new.into_iter().map(Ok))?,
        declared_pure_initializers: unique_set(v.declared_pure_initializers.into_iter().map(Ok))?,
        declared_pure_members: spec_wire::member_names_from_proto(v.declared_pure_members)?,
        no_sync_callback_members: spec_wire::member_names_from_proto(v.no_sync_callback_members)?,
        known_effects: v
            .known_effects
            .into_iter()
            .map(|(binding, effect)| Ok((binding, KnownEffect::try_from(effect)?)))
            .collect::<Result<_>>()?,
        trusted_dataflow_summaries: required(v.trusted_dataflow_summaries)?,
        inline_callback_factories: unique_set(v.inline_callback_factories.into_iter().map(Ok))?,
        callback_factory_members: spec_wire::member_names_from_proto(v.callback_factory_members)?,
        retained_callback_factories: spec_wire::callback_overloads_from_proto(
            v.retained_callback_factories,
        )?,
        retained_callback_factory_members: spec_wire::member_callback_overloads_from_proto(
            v.retained_callback_factory_members,
        )?,
        retained_callback_constructors: spec_wire::callback_overloads_from_proto(
            v.retained_callback_constructors,
        )?,
        no_reentry_exports: unique_set(v.no_reentry_exports.into_iter().map(Ok))?,
        no_reentry_callback_exports: spec_wire::callback_overloads_from_proto(
            v.no_reentry_callback_exports,
        )?,
        no_reentry_statement_calls: decode_statement_callbacks(v.no_reentry_statement_calls)?,
        no_reentry_members: spec_wire::member_names_from_proto(v.no_reentry_members)?,
        no_reentry_record_members: spec_wire::member_names_from_proto(v.no_reentry_record_members)?,
        namespace_independent_members: spec_wire::member_names_from_proto(
            v.namespace_independent_members,
        )?,
        fluent_bindings: unique_set(v.fluent_bindings.into_iter().map(Ok))?,
        captured_value_inputs: Vec::new(),
        native_operation_inputs: Vec::new(),
        initializer_call_effect_inputs: Vec::new(),
        imported_primitive_array_spread_inputs: Vec::new(),
        argument_inputs: Vec::new(),
    })
}
fn encode_declaration(value: &SourceDeclaration) -> Result<pb::SourceDeclaration> {
    Ok(pb::SourceDeclaration {
        binding: Some(value.binding.clone()),
        body_index: Some(value.body_index.try_into()?),
    })
}
fn decode_declaration(value: pb::SourceDeclaration) -> Result<SourceDeclaration> {
    Ok(SourceDeclaration {
        binding: required(value.binding)?,
        body_index: required(value.body_index)?.try_into()?,
    })
}
fn encode_array_operand(
    value: &AnalysisImportedPrimitiveArrayOperand,
) -> Result<pb::AnalysisImportedPrimitiveArrayOperand> {
    Ok(pb::AnalysisImportedPrimitiveArrayOperand {
        selector_local: Some(value.selector_local.clone()),
        consumer_binding: Some(value.consumer_binding.clone()),
        provider_chunk: Some(value.provider_chunk.clone()),
        provider_source_match: Some(spec_wire::encode_source_match_claim(
            &value.provider_source_match,
        )),
        provider_declaration: Some(encode_declaration(&value.provider_declaration)?),
        audited_provider_source_sha256: Some(value.audited_provider_source_sha256.clone()),
    })
}
fn decode_array_operand(
    v: pb::AnalysisImportedPrimitiveArrayOperand,
) -> Result<AnalysisImportedPrimitiveArrayOperand> {
    Ok(AnalysisImportedPrimitiveArrayOperand {
        selector_local: required(v.selector_local)?,
        consumer_binding: required(v.consumer_binding)?,
        provider_chunk: required(v.provider_chunk)?,
        provider_source_match: spec_wire::decode_source_match_claim(required(
            v.provider_source_match,
        )?)?,
        provider_declaration: decode_declaration(required(v.provider_declaration)?)?,
        audited_provider_source_sha256: required(v.audited_provider_source_sha256)?,
    })
}
fn encode_array_input(
    value: &AnalysisImportedPrimitiveArraySpreadInput,
) -> Result<pb::AnalysisImportedPrimitiveArraySpreadInput> {
    ensure!(
        value.note.is_none(),
        "report-only imported array note must not enter analysis projection"
    );
    Ok(pb::AnalysisImportedPrimitiveArraySpreadInput {
        consumer_declaration: Some(encode_declaration(&value.consumer_declaration)?),
        source_match: Some(spec_wire::encode_source_match_claim(&value.source_match)),
        audited_source_sha256: Some(value.audited_source_sha256.clone()),
        operands: value
            .operands
            .iter()
            .map(encode_array_operand)
            .collect::<Result<_>>()?,
        lifetime: Some(
            spec_wire::encode_imported_primitive_array_lifetime_contract(&value.lifetime),
        ),
    })
}
fn decode_array_input(
    v: pb::AnalysisImportedPrimitiveArraySpreadInput,
) -> Result<AnalysisImportedPrimitiveArraySpreadInput> {
    Ok(AnalysisImportedPrimitiveArraySpreadInput {
        consumer_declaration: decode_declaration(required(v.consumer_declaration)?)?,
        source_match: spec_wire::decode_source_match_claim(required(v.source_match)?)?,
        audited_source_sha256: required(v.audited_source_sha256)?,
        operands: v
            .operands
            .into_iter()
            .map(decode_array_operand)
            .collect::<Result<_>>()?,
        lifetime: spec_wire::decode_imported_primitive_array_lifetime_contract(required(
            v.lifetime,
        )?)?,
        note: None,
    })
}
fn encode_source_sites(value: &crate::AnalysisSourceSites) -> Result<pb::AnalysisSourceSites> {
    Ok(pb::AnalysisSourceSites {
        source_sha256: value.source_sha256.clone(),
        value_inputs: source_declarations_to_wire(&value.value_inputs)?,
        native_call_identity_inputs: source_declarations_to_wire(
            &value.native_call_identity_inputs,
        )?,
        initializer_call_effect_inputs: source_declarations_to_wire(
            &value.initializer_call_effect_inputs,
        )?,
        argument_inputs: source_declarations_to_wire(&value.argument_inputs)?,
        no_accessor_install_callables: source_declarations_to_wire(
            &value.no_accessor_install_callables,
        )?,
        imported_primitive_array_spread_inputs: value
            .imported_primitive_array_spread_inputs
            .iter()
            .map(|(key, inputs)| {
                Ok((
                    key.clone(),
                    pb::ChunkImportedPrimitiveArrayInputs {
                        inputs: inputs
                            .iter()
                            .map(encode_array_input)
                            .collect::<Result<_>>()?,
                    },
                ))
            })
            .collect::<Result<_>>()?,
    })
}
fn decode_source_sites(v: pb::AnalysisSourceSites) -> Result<crate::AnalysisSourceSites> {
    Ok(crate::AnalysisSourceSites {
        source_sha256: v.source_sha256,
        value_inputs: source_declarations_from_wire(v.value_inputs)?,
        native_call_identity_inputs: source_declarations_from_wire(v.native_call_identity_inputs)?,
        initializer_call_effect_inputs: source_declarations_from_wire(
            v.initializer_call_effect_inputs,
        )?,
        argument_inputs: source_declarations_from_wire(v.argument_inputs)?,
        no_accessor_install_callables: source_declarations_from_wire(
            v.no_accessor_install_callables,
        )?,
        imported_primitive_array_spread_inputs: v
            .imported_primitive_array_spread_inputs
            .into_iter()
            .map(|(key, value)| {
                Ok((
                    key,
                    value
                        .inputs
                        .into_iter()
                        .map(decode_array_input)
                        .collect::<Result<_>>()?,
                ))
            })
            .collect::<Result<_>>()?,
    })
}
fn encode_native_selector(
    value: &NativeOperationSelectorProvenance,
) -> Result<pb::NativeOperationSelectorProvenance> {
    use pb::native_operation_selector_provenance::Kind;
    Ok(pb::NativeOperationSelectorProvenance {
        kind: Some(match value {
            NativeOperationSelectorProvenance::SourceMatch(claim) => {
                Kind::SourceMatch(spec_wire::encode_source_match_claim(claim))
            }
            NativeOperationSelectorProvenance::Declaration(declaration) => {
                Kind::Declaration(encode_declaration(declaration)?)
            }
            NativeOperationSelectorProvenance::ResolvedMember(member) => {
                Kind::ResolvedMember(member.clone())
            }
        }),
    })
}
fn decode_native_selector(
    value: pb::NativeOperationSelectorProvenance,
) -> Result<NativeOperationSelectorProvenance> {
    use pb::native_operation_selector_provenance::Kind;
    Ok(match required(value.kind)? {
        Kind::SourceMatch(claim) => NativeOperationSelectorProvenance::SourceMatch(
            spec_wire::decode_source_match_claim(claim)?,
        ),
        Kind::Declaration(declaration) => {
            NativeOperationSelectorProvenance::Declaration(decode_declaration(declaration)?)
        }
        Kind::ResolvedMember(member) => NativeOperationSelectorProvenance::ResolvedMember(member),
    })
}
fn encode_native_input(
    value: &crate::AnalysisNativeOperationInput,
) -> Result<pb::AnalysisNativeOperationInput> {
    Ok(pb::AnalysisNativeOperationInput {
        declaration: Some(encode_declaration(&value.declaration)?),
        selector: Some(encode_native_selector(&value.selector)?),
        operation: Some(value.operation as i32),
        audited_source_sha256: Some(value.audited_source_sha256.clone()),
    })
}
fn decode_native_input(
    v: pb::AnalysisNativeOperationInput,
) -> Result<crate::AnalysisNativeOperationInput> {
    Ok(crate::AnalysisNativeOperationInput {
        declaration: decode_declaration(required(v.declaration)?)?,
        selector: decode_native_selector(required(v.selector)?)?,
        operation: spec::NativeOperationKind::try_from(
            v.operation.context("missing native operation kind")?,
        )?,
        audited_source_sha256: required(v.audited_source_sha256)?,
    })
}
#[cfg(test)]
#[path = "analysis_projection_wire_test.rs"]
mod tests;
