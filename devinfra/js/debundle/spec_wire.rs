//! Explicit conversions for the compiled spec vocabulary shared by projections.
//!
//! Serde remains the authoring/report boundary: its YAML-only selector rules must
//! not run when restoring internal selectors. Collections retain canonical BTree
//! order. Protobuf maps use last-value semantics; sets and composite keys reject
//! duplicates where the domain requires unique members.
use anyhow::{Context, Result, ensure};
use contracts::{
    decode_admission_overrides, decode_argument_input_contract,
    decode_commuting_statements_contract, decode_imported_primitive_array_spread_input_contract,
    decode_initializer_call_effect_input_contract, decode_native_call_identity_input_contract,
    decode_native_operation_input_contract, decode_no_accessor_install_callable_contract,
    decode_value_input_contract, encode_admission_overrides, encode_argument_input_contract,
    encode_commuting_statements_contract, encode_imported_primitive_array_spread_input_contract,
    encode_initializer_call_effect_input_contract, encode_native_call_identity_input_contract,
    encode_native_operation_input_contract, encode_no_accessor_install_callable_contract,
    encode_value_input_contract,
};
use selectors::{
    decode_binding_annotation, decode_binding_selector, decode_logical_module,
    encode_binding_annotation, encode_binding_selector, encode_logical_module,
};
use spec_wire_proto::debundle::spec as pb;
use std::collections::{BTreeMap, BTreeSet};
use std::ffi::OsString;
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::path::{Path, PathBuf};
use vendor_marks::{decode_vendor_mark, encode_vendor_mark};

#[path = "spec_wire/contracts.rs"]
pub mod contracts;
#[path = "spec_wire/selectors.rs"]
pub mod selectors;
#[path = "spec_wire/vendor_marks.rs"]
pub mod vendor_marks;

pub fn required<T>(value: Option<T>) -> Result<T> {
    value.context("missing required projection field")
}

pub fn encode_path(value: &Path) -> Vec<u8> {
    value.as_os_str().as_bytes().to_vec()
}

pub fn decode_path(value: Vec<u8>) -> PathBuf {
    OsString::from_vec(value).into()
}

pub fn unique_map<K: Ord, V>(
    entries: impl IntoIterator<Item = Result<(K, V)>>,
) -> Result<BTreeMap<K, V>> {
    let mut result = BTreeMap::new();
    for entry in entries {
        let (key, value) = entry?;
        ensure!(
            result.insert(key, value).is_none(),
            "duplicate projection map key"
        );
    }
    Ok(result)
}

pub fn unique_set<T: Ord>(items: impl IntoIterator<Item = Result<T>>) -> Result<BTreeSet<T>> {
    let mut result = BTreeSet::new();
    for item in items {
        ensure!(result.insert(item?), "duplicate projection set entry");
    }
    Ok(result)
}

pub fn global_names_to_proto(
    value: &BTreeMap<spec::BuiltinGlobalRoot, BTreeSet<String>>,
) -> Vec<pb::GlobalNames> {
    value
        .iter()
        .map(|(root, names)| pb::GlobalNames {
            root: Some(*root as i32),
            names: names.iter().cloned().collect(),
        })
        .collect()
}

pub fn global_names_from_proto(
    value: Vec<pb::GlobalNames>,
) -> Result<BTreeMap<spec::BuiltinGlobalRoot, BTreeSet<String>>> {
    unique_map(value.into_iter().map(|entry| {
        Ok((
            pb::BuiltinGlobalRoot::try_from(entry.root.context("missing global root")?)?,
            unique_set(entry.names.into_iter().map(Ok))?,
        ))
    }))
}

pub fn member_names_to_proto(
    value: &BTreeMap<String, BTreeSet<String>>,
) -> BTreeMap<String, pb::MemberNames> {
    value
        .iter()
        .map(|(key, names)| {
            (
                key.clone(),
                pb::MemberNames {
                    names: names.iter().cloned().collect(),
                },
            )
        })
        .collect()
}

pub fn member_names_from_proto(
    value: BTreeMap<String, pb::MemberNames>,
) -> Result<BTreeMap<String, BTreeSet<String>>> {
    value
        .into_iter()
        .map(|(key, value)| Ok((key, unique_set(value.names.into_iter().map(Ok))?)))
        .collect()
}

pub fn callback_overloads_to_proto(
    value: &BTreeMap<String, Vec<spec::RetainedCallbackSignature>>,
) -> BTreeMap<String, pb::CallbackOverloads> {
    value
        .iter()
        .map(|(key, signatures)| {
            (
                key.clone(),
                pb::CallbackOverloads {
                    signatures: signatures
                        .iter()
                        .map(encode_retained_callback_signature)
                        .collect(),
                },
            )
        })
        .collect()
}

pub fn callback_overloads_from_proto(
    value: BTreeMap<String, pb::CallbackOverloads>,
) -> Result<BTreeMap<String, Vec<spec::RetainedCallbackSignature>>> {
    value
        .into_iter()
        .map(|(key, value)| {
            Ok((
                key,
                value
                    .signatures
                    .into_iter()
                    .map(decode_retained_callback_signature)
                    .collect::<Result<_>>()?,
            ))
        })
        .collect()
}

pub fn member_callback_overloads_to_proto(
    value: &BTreeMap<String, BTreeMap<String, Vec<spec::RetainedCallbackSignature>>>,
) -> BTreeMap<String, pb::MemberCallbackOverloads> {
    value
        .iter()
        .map(|(key, members)| {
            (
                key.clone(),
                pb::MemberCallbackOverloads {
                    members: callback_overloads_to_proto(members),
                },
            )
        })
        .collect()
}

pub fn member_callback_overloads_from_proto(
    value: BTreeMap<String, pb::MemberCallbackOverloads>,
) -> Result<BTreeMap<String, BTreeMap<String, Vec<spec::RetainedCallbackSignature>>>> {
    value
        .into_iter()
        .map(|(key, value)| Ok((key, callback_overloads_from_proto(value.members)?)))
        .collect()
}
pub fn encode_transform_spec(value: &spec::TransformSpec) -> pb::TransformSpec {
    pb::TransformSpec {
        inputs: Some(encode_load_js_chunks_args(&value.inputs)),
        host_behavior: Some(encode_host_behavior(&value.host_behavior)),
        program_premises: Some(encode_program_premises(&value.program_premises)),
        vendor: value
            .vendor
            .iter()
            .map(|(key, mark)| (key.clone(), encode_vendor_mark(mark)))
            .collect(),
        logical_modules: value
            .logical_modules
            .iter()
            .map(|(key, modules)| {
                (
                    key.clone(),
                    pb::LogicalModules {
                        modules: modules
                            .iter()
                            .map(|(key, module)| (key.clone(), encode_logical_module(module)))
                            .collect(),
                    },
                )
            })
            .collect(),
        import_order: value
            .import_order
            .iter()
            .map(|(chunk, paths)| {
                (
                    chunk.clone(),
                    pb::ModulePaths {
                        paths: paths.iter().map(ToString::to_string).collect(),
                    },
                )
            })
            .collect(),
        chunk_renames: value
            .chunk_renames
            .iter()
            .map(|(key, renames)| (key.clone(), encode_chunk_renames(renames)))
            .collect(),
        unassigned_mode: value
            .unassigned_mode
            .iter()
            .map(|(key, mode)| (key.clone(), encode_unassigned_mode(mode)))
            .collect(),
        chunk_analysis_options: value
            .chunk_analysis_options
            .iter()
            .map(|(key, options)| (key.clone(), encode_dependency_graph_options(options)))
            .collect(),
        chunk_export_purity: value
            .chunk_export_purity
            .iter()
            .map(|(key, purity)| (key.clone(), encode_chunk_export_purity(purity)))
            .collect(),
        swap_vendor_chunks: Some(encode_swap_vendor_chunks_config(&value.swap_vendor_chunks)),
        materialize_logical_modules: Some(encode_materialize_logical_modules_config(
            &value.materialize_logical_modules,
        )),
        write_js_tree: value
            .write_js_tree
            .as_ref()
            .map(encode_write_js_tree_config),
        emit_browser_harness: value
            .emit_browser_harness
            .as_ref()
            .map(encode_emit_browser_harness_config),
    }
}

pub fn decode_transform_spec(v: pb::TransformSpec) -> Result<spec::TransformSpec> {
    Ok(spec::TransformSpec {
        inputs: decode_load_js_chunks_args(required(v.inputs)?)?,
        host_behavior: decode_host_behavior(required(v.host_behavior)?)?,
        program_premises: decode_program_premises(required(v.program_premises)?)?,
        vendor: v
            .vendor
            .into_iter()
            .map(|(key, mark)| Ok((key, decode_vendor_mark(mark)?)))
            .collect::<Result<_>>()?,
        logical_modules: v
            .logical_modules
            .into_iter()
            .map(|(key, value)| {
                Ok((
                    key,
                    value
                        .modules
                        .into_iter()
                        .map(|(key, module)| Ok((key, decode_logical_module(module)?)))
                        .collect::<Result<_>>()?,
                ))
            })
            .collect::<Result<_>>()?,
        import_order: v
            .import_order
            .into_iter()
            .map(|(chunk, paths)| {
                Ok((
                    chunk.clone(),
                    paths
                        .paths
                        .into_iter()
                        .map(|path| {
                            spec::ModulePath::parse(&path, "").with_context(|| {
                                format!(
                                    "invalid import_order module path {path:?} for chunk {chunk:?}"
                                )
                            })
                        })
                        .collect::<Result<_>>()?,
                ))
            })
            .collect::<Result<_>>()?,
        chunk_renames: v
            .chunk_renames
            .into_iter()
            .map(|(key, value)| Ok((key, decode_chunk_renames(value)?)))
            .collect::<Result<_>>()?,
        unassigned_mode: v
            .unassigned_mode
            .into_iter()
            .map(|(key, value)| Ok((key, decode_unassigned_mode(value)?)))
            .collect::<Result<_>>()?,
        chunk_analysis_options: v
            .chunk_analysis_options
            .into_iter()
            .map(|(key, value)| Ok((key, decode_dependency_graph_options(value)?)))
            .collect::<Result<_>>()?,
        chunk_export_purity: v
            .chunk_export_purity
            .into_iter()
            .map(|(key, value)| Ok((key, decode_chunk_export_purity(value)?)))
            .collect::<Result<_>>()?,
        swap_vendor_chunks: decode_swap_vendor_chunks_config(required(v.swap_vendor_chunks)?)?,
        materialize_logical_modules: decode_materialize_logical_modules_config(required(
            v.materialize_logical_modules,
        )?)?,
        write_js_tree: v
            .write_js_tree
            .map(decode_write_js_tree_config)
            .transpose()?,
        emit_browser_harness: v
            .emit_browser_harness
            .map(decode_emit_browser_harness_config)
            .transpose()?,
    })
}

pub fn encode_load_js_chunks_args(value: &spec::LoadJsChunksArgs) -> pb::LoadJsChunksArgs {
    pb::LoadJsChunksArgs {
        input_root: Some(encode_path(&value.input_root)),
        js_list_path: Some(encode_path(&value.js_list_path)),
        embedded_loaders_path: value
            .embedded_loaders_path
            .as_ref()
            .map(|path| encode_path(path)),
    }
}

pub fn decode_load_js_chunks_args(v: pb::LoadJsChunksArgs) -> Result<spec::LoadJsChunksArgs> {
    Ok(spec::LoadJsChunksArgs {
        input_root: decode_path(required(v.input_root)?),
        js_list_path: decode_path(required(v.js_list_path)?),
        embedded_loaders_path: v.embedded_loaders_path.map(decode_path),
    })
}

pub fn encode_host_behavior(value: &spec::HostBehavior) -> pb::HostBehavior {
    pb::HostBehavior {
        synchronous_module_loader: Some(value.synchronous_module_loader as i32),
        loader_result_for_evaluating_module: value
            .loader_result_for_evaluating_module
            .map(|value| value as i32),
        synchronous_load_runs_app_callbacks: value
            .synchronous_load_runs_app_callbacks
            .map(|value| value as i32),
        synchronous_load_drains_microtasks: value
            .synchronous_load_drains_microtasks
            .map(|value| value as i32),
        reentry_free_builtin_modules: value
            .reentry_free_builtin_modules
            .iter()
            .map(|module| *module as i32)
            .collect(),
        reentry_free_bun_modules: value
            .reentry_free_bun_modules
            .iter()
            .map(|module| *module as i32)
            .collect(),
        reentry_free_embedded_loaders: value
            .reentry_free_embedded_loaders
            .iter()
            .map(|value| *value as i32)
            .collect(),
        reentry_free_operations: value
            .reentry_free_operations
            .iter()
            .map(|operation| *operation as i32)
            .collect(),
        predefined_globals: Some(value.predefined_globals as i32),
        note: value.note.clone(),
    }
}

pub fn decode_host_behavior(v: pb::HostBehavior) -> Result<spec::HostBehavior> {
    Ok(spec::HostBehavior {
        synchronous_module_loader: pb::SynchronousModuleLoader::try_from(
            v.synchronous_module_loader
                .context("missing required projection field")?,
        )?,
        loader_result_for_evaluating_module: v
            .loader_result_for_evaluating_module
            .map(pb::LoaderResultForEvaluatingModule::try_from)
            .transpose()?,
        synchronous_load_runs_app_callbacks: v
            .synchronous_load_runs_app_callbacks
            .map(pb::SynchronousLoadRunsAppCallbacks::try_from)
            .transpose()?,
        synchronous_load_drains_microtasks: v
            .synchronous_load_drains_microtasks
            .map(pb::SynchronousLoadDrainsMicrotasks::try_from)
            .transpose()?,
        reentry_free_builtin_modules: unique_set(
            v.reentry_free_builtin_modules
                .into_iter()
                .map(|value| Ok(pb::NodeBuiltinModule::try_from(value)?)),
        )?,
        reentry_free_bun_modules: unique_set(
            v.reentry_free_bun_modules
                .into_iter()
                .map(|value| Ok(pb::BunModule::try_from(value)?)),
        )?,
        reentry_free_embedded_loaders: unique_set(
            v.reentry_free_embedded_loaders
                .into_iter()
                .map(|value| Ok(pb::EmbeddedLoader::try_from(value)?)),
        )?,
        reentry_free_operations: unique_set(
            v.reentry_free_operations
                .into_iter()
                .map(|value| Ok(pb::ReentryFreeOperation::try_from(value)?)),
        )?,
        predefined_globals: pb::PredefinedGlobals::try_from(
            v.predefined_globals.context("missing predefined globals")?,
        )?,
        note: v.note,
    })
}

pub fn encode_program_premises(value: &spec::ProgramPremises) -> pb::ProgramPremises {
    pb::ProgramPremises {
        process_prototype_chain_accessors: Some(value.process_prototype_chain_accessors as i32),
        non_literal_loader_calls: Some(value.non_literal_loader_calls as i32),
        intrinsics_never_written: value
            .intrinsics_never_written
            .as_ref()
            .map(encode_intrinsics_never_written),
        note: value.note.clone(),
    }
}

pub fn decode_program_premises(v: pb::ProgramPremises) -> Result<spec::ProgramPremises> {
    Ok(spec::ProgramPremises {
        process_prototype_chain_accessors: pb::ProcessPrototypeChainAccessors::try_from(
            v.process_prototype_chain_accessors
                .context("missing required projection field")?,
        )?,
        non_literal_loader_calls: pb::NonLiteralLoaderCalls::try_from(
            v.non_literal_loader_calls
                .context("missing required projection field")?,
        )?,
        intrinsics_never_written: v
            .intrinsics_never_written
            .map(decode_intrinsics_never_written)
            .transpose()?,
        note: v.note,
    })
}

pub fn encode_intrinsics_never_written(
    value: &spec::IntrinsicsNeverWritten,
) -> pb::IntrinsicsNeverWritten {
    pb::IntrinsicsNeverWritten {
        families: Some(encode_intrinsic_families(&value.families)),
        note: value.note.clone(),
    }
}

pub fn decode_intrinsics_never_written(
    v: pb::IntrinsicsNeverWritten,
) -> Result<spec::IntrinsicsNeverWritten> {
    Ok(spec::IntrinsicsNeverWritten {
        families: decode_intrinsic_families(required(v.families)?)?,
        note: v.note,
    })
}

pub fn encode_intrinsic_families(value: &spec::IntrinsicFamilies) -> pb::IntrinsicFamilies {
    use pb::intrinsic_families::Value;
    pb::IntrinsicFamilies {
        value: Some(match value {
            spec::IntrinsicFamilies::All(v) => Value::All(*v as i32),
            spec::IntrinsicFamilies::Listed(v) => Value::Listed(pb::IntrinsicFamilySelection {
                families: v.iter().map(|family| *family as i32).collect(),
            }),
        }),
    }
}

pub fn decode_intrinsic_families(v: pb::IntrinsicFamilies) -> Result<spec::IntrinsicFamilies> {
    use pb::intrinsic_families::Value;
    Ok(match v.value.context("missing intrinsic families")? {
        Value::All(v) => spec::IntrinsicFamilies::All(pb::AllIntrinsicFamilies::try_from(v)?),
        Value::Listed(v) => spec::IntrinsicFamilies::Listed(unique_set(
            v.families
                .into_iter()
                .map(|value| Ok(pb::IntrinsicFamily::try_from(value)?)),
        )?),
    })
}

pub fn encode_chunk_renames(value: &spec::ChunkRenames) -> pb::ChunkRenames {
    pb::ChunkRenames {
        members: value
            .members
            .iter()
            .map(encode_chunk_rename_member)
            .collect(),
        annotations: value
            .annotations
            .iter()
            .map(|(key, annotation)| (key.clone(), encode_binding_annotation(annotation)))
            .collect(),
    }
}

pub fn decode_chunk_renames(v: pb::ChunkRenames) -> Result<spec::ChunkRenames> {
    Ok(spec::ChunkRenames {
        members: v
            .members
            .into_iter()
            .map(decode_chunk_rename_member)
            .collect::<Result<_>>()?,
        annotations: v
            .annotations
            .into_iter()
            .map(|(key, annotation)| Ok((key, decode_binding_annotation(annotation)?)))
            .collect::<Result<_>>()?,
    })
}

pub fn encode_chunk_rename_member(value: &spec::ChunkRenameMember) -> pb::ChunkRenameMember {
    pb::ChunkRenameMember {
        name: value.name.clone(),
        selector: Some(encode_chunk_rename_selector(&value.selector)),
    }
}

pub fn decode_chunk_rename_member(v: pb::ChunkRenameMember) -> Result<spec::ChunkRenameMember> {
    Ok(spec::ChunkRenameMember {
        name: v.name,
        selector: decode_chunk_rename_selector(required(v.selector)?)?,
    })
}

pub fn encode_chunk_rename_selector(value: &spec::ChunkRenameSelector) -> pb::ChunkRenameSelector {
    pb::ChunkRenameSelector {
        binding: Some(encode_binding_selector(&value.binding)),
    }
}

pub fn decode_chunk_rename_selector(
    v: pb::ChunkRenameSelector,
) -> Result<spec::ChunkRenameSelector> {
    Ok(spec::ChunkRenameSelector {
        binding: decode_binding_selector(required(v.binding)?)?,
    })
}

pub fn encode_unassigned_mode(value: &spec::UnassignedMode) -> pb::UnassignedMode {
    use pb::unassigned_mode::Value;
    pb::UnassignedMode {
        value: Some(match value {
            spec::UnassignedMode::InlineInEntry => Value::InlineInEntry(pb::Unit {}),
            spec::UnassignedMode::CatchallFile { target } => {
                Value::CatchallFile(pb::UnassignedModeCatchallFile {
                    target: target.clone(),
                })
            }
            spec::UnassignedMode::PerAtomicGroup => Value::PerAtomicGroup(pb::Unit {}),
            spec::UnassignedMode::CoarseAtomicGroups {
                complete_partial_atomic_groups_for,
            } => Value::CoarseAtomicGroups(pb::UnassignedModeCoarseAtomicGroups {
                complete_partial_atomic_groups_for: complete_partial_atomic_groups_for.clone(),
            }),
        }),
    }
}

pub fn decode_unassigned_mode(v: pb::UnassignedMode) -> Result<spec::UnassignedMode> {
    use pb::unassigned_mode::Value;
    Ok(match v.value.context("missing unassigned mode")? {
        Value::InlineInEntry(_) => spec::UnassignedMode::InlineInEntry,
        Value::CatchallFile(v) => spec::UnassignedMode::CatchallFile { target: v.target },
        Value::PerAtomicGroup(_) => spec::UnassignedMode::PerAtomicGroup,
        Value::CoarseAtomicGroups(v) => spec::UnassignedMode::CoarseAtomicGroups {
            complete_partial_atomic_groups_for: v.complete_partial_atomic_groups_for,
        },
    })
}

pub fn encode_dependency_graph_options(
    value: &spec::DependencyGraphOptions,
) -> pb::DependencyGraphOptions {
    pb::DependencyGraphOptions {
        value_inputs: value
            .value_inputs
            .iter()
            .map(encode_value_input_contract)
            .collect(),
        native_operation_inputs: value
            .native_operation_inputs
            .iter()
            .map(encode_native_operation_input_contract)
            .collect(),
        initializer_call_effect_inputs: value
            .initializer_call_effect_inputs
            .iter()
            .map(encode_initializer_call_effect_input_contract)
            .collect(),
        imported_primitive_array_spread_inputs: value
            .imported_primitive_array_spread_inputs
            .iter()
            .map(encode_imported_primitive_array_spread_input_contract)
            .collect(),
        commuting_statements: value
            .commuting_statements
            .iter()
            .map(encode_commuting_statements_contract)
            .collect(),
        native_call_identity_inputs: value
            .native_call_identity_inputs
            .iter()
            .map(encode_native_call_identity_input_contract)
            .collect(),
        argument_inputs: value
            .argument_inputs
            .iter()
            .map(encode_argument_input_contract)
            .collect(),
        no_accessor_install_callables: value
            .no_accessor_install_callables
            .iter()
            .map(encode_no_accessor_install_callable_contract)
            .collect(),
        deferred_global_publications: global_names_to_proto(&value.deferred_global_publications),
        non_reentrant_global_membership: global_names_to_proto(
            &value.non_reentrant_global_membership,
        ),
        dataflow_aware_s_chain: Some(value.dataflow_aware_s_chain),
        trusted_dataflow_summaries: Some(value.trusted_dataflow_summaries),
        admission_overrides: Some(encode_admission_overrides(&value.admission_overrides)),
        local_property_effects: Some(value.local_property_effects),
    }
}

pub fn decode_dependency_graph_options(
    v: pb::DependencyGraphOptions,
) -> Result<spec::DependencyGraphOptions> {
    Ok(spec::DependencyGraphOptions {
        value_inputs: v
            .value_inputs
            .into_iter()
            .map(decode_value_input_contract)
            .collect::<Result<_>>()?,
        native_operation_inputs: v
            .native_operation_inputs
            .into_iter()
            .map(decode_native_operation_input_contract)
            .collect::<Result<_>>()?,
        initializer_call_effect_inputs: v
            .initializer_call_effect_inputs
            .into_iter()
            .map(decode_initializer_call_effect_input_contract)
            .collect::<Result<_>>()?,
        imported_primitive_array_spread_inputs: v
            .imported_primitive_array_spread_inputs
            .into_iter()
            .map(decode_imported_primitive_array_spread_input_contract)
            .collect::<Result<_>>()?,
        commuting_statements: v
            .commuting_statements
            .into_iter()
            .map(decode_commuting_statements_contract)
            .collect::<Result<_>>()?,
        native_call_identity_inputs: v
            .native_call_identity_inputs
            .into_iter()
            .map(decode_native_call_identity_input_contract)
            .collect::<Result<_>>()?,
        argument_inputs: v
            .argument_inputs
            .into_iter()
            .map(decode_argument_input_contract)
            .collect::<Result<_>>()?,
        no_accessor_install_callables: v
            .no_accessor_install_callables
            .into_iter()
            .map(decode_no_accessor_install_callable_contract)
            .collect::<Result<_>>()?,
        deferred_global_publications: global_names_from_proto(v.deferred_global_publications)?,
        non_reentrant_global_membership: global_names_from_proto(
            v.non_reentrant_global_membership,
        )?,
        dataflow_aware_s_chain: required(v.dataflow_aware_s_chain)?,
        trusted_dataflow_summaries: required(v.trusted_dataflow_summaries)?,
        admission_overrides: decode_admission_overrides(required(v.admission_overrides)?)?,
        local_property_effects: required(v.local_property_effects)?,
    })
}

pub fn encode_chunk_export_purity(value: &spec::ChunkExportPurity) -> pb::ChunkExportPurity {
    pb::ChunkExportPurity {
        source_markers: value
            .source_markers
            .as_ref()
            .map(|markers| pb::SourceMarkers {
                markers: markers.iter().cloned().collect(),
            }),
        pure_exports: value.pure_exports.iter().cloned().collect(),
        fluent_exports: value.fluent_exports.iter().cloned().collect(),
        callback_factory_exports: value.callback_factory_exports.iter().cloned().collect(),
        inline_callback_factory_exports: value
            .inline_callback_factory_exports
            .iter()
            .cloned()
            .collect(),
        callback_factory_members: member_names_to_proto(&value.callback_factory_members),
        retained_callback_factory_exports: callback_overloads_to_proto(
            &value.retained_callback_factory_exports,
        ),
        descriptor_copy_factory_exports: value
            .descriptor_copy_factory_exports
            .iter()
            .map(|(key, signatures)| {
                (
                    key.clone(),
                    pb::DescriptorCopyOverloads {
                        signatures: signatures
                            .iter()
                            .map(encode_descriptor_copy_factory_signature)
                            .collect(),
                    },
                )
            })
            .collect(),
        retained_callback_factory_members: member_callback_overloads_to_proto(
            &value.retained_callback_factory_members,
        ),
        retained_callback_constructor_exports: callback_overloads_to_proto(
            &value.retained_callback_constructor_exports,
        ),
        no_reentry_exports: value.no_reentry_exports.iter().cloned().collect(),
        no_reentry_callback_exports: callback_overloads_to_proto(
            &value.no_reentry_callback_exports,
        ),
        no_reentry_members: member_names_to_proto(&value.no_reentry_members),
        no_reentry_record_members: member_names_to_proto(&value.no_reentry_record_members),
        namespace_independent_members: member_names_to_proto(&value.namespace_independent_members),
    }
}

pub fn decode_chunk_export_purity(v: pb::ChunkExportPurity) -> Result<spec::ChunkExportPurity> {
    Ok(spec::ChunkExportPurity {
        source_markers: v
            .source_markers
            .map(|value| unique_set(value.markers.into_iter().map(Ok)))
            .transpose()?,
        pure_exports: unique_set(v.pure_exports.into_iter().map(Ok))?,
        fluent_exports: unique_set(v.fluent_exports.into_iter().map(Ok))?,
        callback_factory_exports: unique_set(v.callback_factory_exports.into_iter().map(Ok))?,
        inline_callback_factory_exports: unique_set(
            v.inline_callback_factory_exports.into_iter().map(Ok),
        )?,
        callback_factory_members: member_names_from_proto(v.callback_factory_members)?,
        retained_callback_factory_exports: callback_overloads_from_proto(
            v.retained_callback_factory_exports,
        )?,
        descriptor_copy_factory_exports: v
            .descriptor_copy_factory_exports
            .into_iter()
            .map(|(key, value)| {
                Ok((
                    key,
                    value
                        .signatures
                        .into_iter()
                        .map(decode_descriptor_copy_factory_signature)
                        .collect::<Result<_>>()?,
                ))
            })
            .collect::<Result<_>>()?,
        retained_callback_factory_members: member_callback_overloads_from_proto(
            v.retained_callback_factory_members,
        )?,
        retained_callback_constructor_exports: callback_overloads_from_proto(
            v.retained_callback_constructor_exports,
        )?,
        no_reentry_exports: unique_set(v.no_reentry_exports.into_iter().map(Ok))?,
        no_reentry_callback_exports: callback_overloads_from_proto(v.no_reentry_callback_exports)?,
        no_reentry_members: member_names_from_proto(v.no_reentry_members)?,
        no_reentry_record_members: member_names_from_proto(v.no_reentry_record_members)?,
        namespace_independent_members: member_names_from_proto(v.namespace_independent_members)?,
    })
}

pub fn encode_retained_callback_signature(
    value: &spec::RetainedCallbackSignature,
) -> pb::RetainedCallbackSignature {
    pb::RetainedCallbackSignature {
        argument_count: Some(value.argument_count as u64),
        callback_arguments: value
            .callback_arguments
            .iter()
            .map(|index| *index as u64)
            .collect(),
    }
}

pub fn decode_retained_callback_signature(
    v: pb::RetainedCallbackSignature,
) -> Result<spec::RetainedCallbackSignature> {
    let result = spec::RetainedCallbackSignature {
        argument_count: usize::try_from(required(v.argument_count)?)?,
        callback_arguments: unique_set(
            v.callback_arguments
                .into_iter()
                .map(|index| Ok(usize::try_from(index)?)),
        )?,
    };
    result.validate().map_err(anyhow::Error::msg)?;
    Ok(result)
}

pub fn encode_descriptor_copy_factory_signature(
    value: &spec::DescriptorCopyFactorySignature,
) -> pb::DescriptorCopyFactorySignature {
    pb::DescriptorCopyFactorySignature {
        argument_count: Some(value.argument_count as u64),
        object_argument: Some(value.object_argument as u64),
        eager_own_data_keys: value.eager_own_data_keys.iter().cloned().collect(),
        assigned_data_keys: value.assigned_data_keys.iter().cloned().collect(),
    }
}

pub fn decode_descriptor_copy_factory_signature(
    v: pb::DescriptorCopyFactorySignature,
) -> Result<spec::DescriptorCopyFactorySignature> {
    Ok(spec::DescriptorCopyFactorySignature {
        argument_count: usize::try_from(required(v.argument_count)?)?,
        object_argument: usize::try_from(required(v.object_argument)?)?,
        eager_own_data_keys: unique_set(v.eager_own_data_keys.into_iter().map(Ok))?,
        assigned_data_keys: unique_set(v.assigned_data_keys.into_iter().map(Ok))?,
    })
}

pub fn encode_swap_vendor_chunks_config(
    value: &spec::SwapVendorChunksConfig,
) -> pb::SwapVendorChunksConfig {
    pb::SwapVendorChunksConfig {
        output_manifest_path: value.output_manifest_path.as_deref().map(encode_path),
        output_wrapper_dir: value.output_wrapper_dir.as_deref().map(encode_path),
        write: Some(value.write),
    }
}

pub fn decode_swap_vendor_chunks_config(
    v: pb::SwapVendorChunksConfig,
) -> Result<spec::SwapVendorChunksConfig> {
    Ok(spec::SwapVendorChunksConfig {
        output_manifest_path: v.output_manifest_path.map(decode_path),
        output_wrapper_dir: v.output_wrapper_dir.map(decode_path),
        write: required(v.write)?,
    })
}

pub fn encode_materialize_logical_modules_config(
    value: &spec::MaterializeLogicalModulesConfig,
) -> pb::MaterializeLogicalModulesConfig {
    pb::MaterializeLogicalModulesConfig {
        file: value.file.clone(),
        prune_other_chunks: Some(value.prune_other_chunks),
        report_out_dir: value.report_out_dir.as_deref().map(encode_path),
        target_dir: Some(value.target_dir.clone()),
    }
}

pub fn decode_materialize_logical_modules_config(
    v: pb::MaterializeLogicalModulesConfig,
) -> Result<spec::MaterializeLogicalModulesConfig> {
    Ok(spec::MaterializeLogicalModulesConfig {
        file: v.file,
        prune_other_chunks: required(v.prune_other_chunks)?,
        report_out_dir: v.report_out_dir.map(decode_path),
        target_dir: required(v.target_dir)?,
    })
}

pub fn encode_write_js_tree_config(value: &spec::WriteJsTreeConfig) -> pb::WriteJsTreeConfig {
    pb::WriteJsTreeConfig {
        out_dir: Some(encode_path(&value.out_dir)),
    }
}

pub fn decode_write_js_tree_config(v: pb::WriteJsTreeConfig) -> Result<spec::WriteJsTreeConfig> {
    Ok(spec::WriteJsTreeConfig {
        out_dir: decode_path(required(v.out_dir)?),
    })
}

pub fn encode_emit_browser_harness_config(
    value: &spec::EmitBrowserHarnessConfig,
) -> pb::EmitBrowserHarnessConfig {
    pb::EmitBrowserHarnessConfig {
        asset_summary_path: Some(encode_path(&value.asset_summary_path)),
        out_dir: Some(encode_path(&value.out_dir)),
        snapshot_root: Some(encode_path(&value.snapshot_root)),
    }
}

pub fn decode_emit_browser_harness_config(
    v: pb::EmitBrowserHarnessConfig,
) -> Result<spec::EmitBrowserHarnessConfig> {
    Ok(spec::EmitBrowserHarnessConfig {
        asset_summary_path: decode_path(required(v.asset_summary_path)?),
        out_dir: decode_path(required(v.out_dir)?),
        snapshot_root: decode_path(required(v.snapshot_root)?),
    })
}
