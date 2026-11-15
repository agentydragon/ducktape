//! Explicit conversions for the compiled spec vocabulary shared by projections.
//!
//! Serde remains the authoring/report boundary: its YAML-only selector rules must
//! not run when restoring internal selectors. Collections retain canonical BTree
//! order. Protobuf maps use last-value semantics; sets and composite keys reject
//! duplicates where the domain requires unique members.
use anyhow::{Context, Result, ensure};
use spec_wire_proto::debundle::spec as pb;
use std::collections::{BTreeMap, BTreeSet};
use std::ffi::OsString;
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::path::{Path, PathBuf};

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

pub fn encode_vendor_mark(value: &spec::VendorMark) -> pb::VendorMark {
    pb::VendorMark {
        identity: Some(value.identity.clone()),
        level: Some(encode_vendor_level(&value.level)),
    }
}

pub fn decode_vendor_mark(v: pb::VendorMark) -> Result<spec::VendorMark> {
    Ok(spec::VendorMark {
        identity: required(v.identity)?,
        level: decode_vendor_level(required(v.level)?)?,
    })
}

pub fn encode_vendor_level(value: &spec::VendorLevel) -> pb::VendorLevel {
    use pb::vendor_level::Value;
    pb::VendorLevel {
        value: Some(match value {
            spec::VendorLevel::Swap(v) => Value::Swap(encode_swap_mark(v)),
            spec::VendorLevel::PartialSwap(v) => Value::PartialSwap(encode_partial_swap_mark(v)),
            spec::VendorLevel::BundledPartialSwap(v) => {
                Value::BundledPartialSwap(encode_bundled_partial_swap_mark(v))
            }
        }),
    }
}

pub fn decode_vendor_level(v: pb::VendorLevel) -> Result<spec::VendorLevel> {
    use pb::vendor_level::Value;
    Ok(match v.value.context("missing vendor level")? {
        Value::Swap(v) => spec::VendorLevel::Swap(decode_swap_mark(v)?),
        Value::PartialSwap(v) => spec::VendorLevel::PartialSwap(decode_partial_swap_mark(v)?),
        Value::BundledPartialSwap(v) => {
            spec::VendorLevel::BundledPartialSwap(decode_bundled_partial_swap_mark(v)?)
        }
    })
}

pub fn encode_swap_mark(value: &spec::SwapMark) -> pb::SwapMark {
    pb::SwapMark {
        package: Some(value.package.clone()),
        version: Some(value.version.clone()),
        subpath: Some(value.subpath.clone()),
        wrapper_shape: value.wrapper_shape.map(|shape| shape as i32),
        default_export_aliases: value.default_export_aliases.iter().cloned().collect(),
    }
}

pub fn decode_swap_mark(v: pb::SwapMark) -> Result<spec::SwapMark> {
    Ok(spec::SwapMark {
        package: required(v.package)?,
        version: required(v.version)?,
        subpath: required(v.subpath)?,
        wrapper_shape: v
            .wrapper_shape
            .map(pb::WrapperShape::try_from)
            .transpose()?,
        default_export_aliases: v.default_export_aliases,
    })
}

pub fn encode_partial_swap_mark(value: &spec::PartialSwapMark) -> pb::PartialSwapMark {
    pb::PartialSwapMark {
        packages: value
            .packages
            .iter()
            .map(|(key, package)| (key.clone(), encode_partial_swap_package(package)))
            .collect(),
        symbols: value
            .symbols
            .iter()
            .map(|(key, symbol)| (key.clone(), encode_partial_swap_symbol(symbol)))
            .collect(),
    }
}

pub fn decode_partial_swap_mark(v: pb::PartialSwapMark) -> Result<spec::PartialSwapMark> {
    Ok(spec::PartialSwapMark {
        packages: v
            .packages
            .into_iter()
            .map(|(key, package)| Ok((key, decode_partial_swap_package(package)?)))
            .collect::<Result<_>>()?,
        symbols: v
            .symbols
            .into_iter()
            .map(|(key, symbol)| Ok((key, decode_partial_swap_symbol(symbol)?)))
            .collect::<Result<_>>()?,
    })
}

pub fn encode_partial_swap_package(value: &spec::PartialSwapPackage) -> pb::PartialSwapPackage {
    pb::PartialSwapPackage {
        version: Some(value.version.clone()),
        subpath: Some(value.subpath.clone()),
        namespace: value.namespace.clone(),
    }
}

pub fn decode_partial_swap_package(v: pb::PartialSwapPackage) -> Result<spec::PartialSwapPackage> {
    Ok(spec::PartialSwapPackage {
        version: required(v.version)?,
        subpath: required(v.subpath)?,
        namespace: v.namespace,
    })
}

pub fn encode_partial_swap_symbol(value: &spec::PartialSwapSymbol) -> pb::PartialSwapSymbol {
    pb::PartialSwapSymbol {
        package: Some(value.package.clone()),
        kind: Some(value.kind as i32),
        upstream_export: value.upstream_export.clone(),
        local: value.local.clone(),
    }
}

pub fn decode_partial_swap_symbol(v: pb::PartialSwapSymbol) -> Result<spec::PartialSwapSymbol> {
    Ok(spec::PartialSwapSymbol {
        package: required(v.package)?,
        kind: pb::PartialSwapKind::try_from(v.kind.context("missing required projection field")?)?,
        upstream_export: v.upstream_export,
        local: v.local,
    })
}

pub fn encode_bundled_partial_swap_mark(
    value: &spec::BundledPartialSwapMark,
) -> pb::BundledPartialSwapMark {
    pb::BundledPartialSwapMark {
        bundle: Some(encode_bundled_partial_swap_bundle(&value.bundle)),
        packages: value
            .packages
            .iter()
            .map(|(key, package)| (key.clone(), encode_bundled_partial_swap_package(package)))
            .collect(),
        symbols: value
            .symbols
            .iter()
            .map(|(key, symbol)| (key.clone(), encode_partial_swap_symbol(symbol)))
            .collect(),
    }
}

pub fn decode_bundled_partial_swap_mark(
    v: pb::BundledPartialSwapMark,
) -> Result<spec::BundledPartialSwapMark> {
    Ok(spec::BundledPartialSwapMark {
        bundle: decode_bundled_partial_swap_bundle(required(v.bundle)?)?,
        packages: v
            .packages
            .into_iter()
            .map(|(key, package)| Ok((key, decode_bundled_partial_swap_package(package)?)))
            .collect::<Result<_>>()?,
        symbols: v
            .symbols
            .into_iter()
            .map(|(key, symbol)| Ok((key, decode_partial_swap_symbol(symbol)?)))
            .collect::<Result<_>>()?,
    })
}

pub fn encode_bundled_partial_swap_bundle(
    value: &spec::BundledPartialSwapBundle,
) -> pb::BundledPartialSwapBundle {
    pb::BundledPartialSwapBundle {
        path: Some(encode_path(&value.path)),
    }
}

pub fn decode_bundled_partial_swap_bundle(
    v: pb::BundledPartialSwapBundle,
) -> Result<spec::BundledPartialSwapBundle> {
    Ok(spec::BundledPartialSwapBundle {
        path: decode_path(required(v.path)?),
    })
}

pub fn encode_bundled_partial_swap_package(
    value: &spec::BundledPartialSwapPackage,
) -> pb::BundledPartialSwapPackage {
    pb::BundledPartialSwapPackage {
        version: Some(value.version.clone()),
        subpath: Some(value.subpath.clone()),
        bundle_export: Some(value.bundle_export.clone()),
        namespace: value.namespace.clone(),
    }
}

pub fn decode_bundled_partial_swap_package(
    v: pb::BundledPartialSwapPackage,
) -> Result<spec::BundledPartialSwapPackage> {
    Ok(spec::BundledPartialSwapPackage {
        version: required(v.version)?,
        subpath: required(v.subpath)?,
        bundle_export: required(v.bundle_export)?,
        namespace: v.namespace,
    })
}

pub fn encode_logical_module(value: &spec::LogicalModule) -> pb::LogicalModule {
    pb::LogicalModule {
        members: value.members.iter().map(encode_member).collect(),
        source_matches: value
            .source_matches
            .iter()
            .map(encode_source_match_claim)
            .collect(),
        annotations: value
            .annotations
            .iter()
            .map(|(key, annotation)| (key.clone(), encode_binding_annotation(annotation)))
            .collect(),
        anonymous_statements: value
            .anonymous_statements
            .iter()
            .map(encode_anonymous_statement)
            .collect(),
        comment: value.comment.clone(),
        note: value.note.clone(),
    }
}

pub fn decode_logical_module(v: pb::LogicalModule) -> Result<spec::LogicalModule> {
    Ok(spec::LogicalModule {
        members: v
            .members
            .into_iter()
            .map(decode_member)
            .collect::<Result<_>>()?,
        source_matches: v
            .source_matches
            .into_iter()
            .map(decode_source_match_claim)
            .collect::<Result<_>>()?,
        annotations: v
            .annotations
            .into_iter()
            .map(|(key, annotation)| Ok((key, decode_binding_annotation(annotation)?)))
            .collect::<Result<_>>()?,
        anonymous_statements: v
            .anonymous_statements
            .into_iter()
            .map(decode_anonymous_statement)
            .collect::<Result<_>>()?,
        comment: v.comment,
        note: v.note,
    })
}

pub fn encode_member(value: &spec::Member) -> pb::Member {
    pb::Member {
        name: value.name.clone(),
        selector: Some(encode_raw_member_selector(&value.selector)),
    }
}

pub fn decode_member(v: pb::Member) -> Result<spec::Member> {
    Ok(spec::Member {
        name: v.name,
        selector: decode_raw_member_selector(required(v.selector)?)?,
    })
}

pub fn encode_raw_member_selector(value: &spec::RawMemberSelector) -> pb::RawMemberSelector {
    pb::RawMemberSelector {
        binding: value.binding.as_ref().map(encode_binding_selector),
        cross_ref: value.cross_ref.as_ref().map(encode_cross_ref_selector),
        reads_member: value
            .reads_member
            .as_ref()
            .map(encode_reads_member_selector),
        member_of_module: value
            .member_of_module
            .as_ref()
            .map(encode_member_of_module_selector),
        passed_to_call: value
            .passed_to_call
            .as_ref()
            .map(encode_passed_to_call_selector),
        makes_decorate_call: value
            .makes_decorate_call
            .as_ref()
            .map(encode_makes_decorate_call_selector),
        intrinsic_alias: value
            .intrinsic_alias
            .as_ref()
            .map(encode_intrinsic_alias_selector),
    }
}

pub fn decode_raw_member_selector(v: pb::RawMemberSelector) -> Result<spec::RawMemberSelector> {
    Ok(spec::RawMemberSelector {
        binding: v.binding.map(decode_binding_selector).transpose()?,
        cross_ref: v.cross_ref.map(decode_cross_ref_selector).transpose()?,
        reads_member: v
            .reads_member
            .map(decode_reads_member_selector)
            .transpose()?,
        member_of_module: v
            .member_of_module
            .map(decode_member_of_module_selector)
            .transpose()?,
        passed_to_call: v
            .passed_to_call
            .map(decode_passed_to_call_selector)
            .transpose()?,
        makes_decorate_call: v
            .makes_decorate_call
            .map(decode_makes_decorate_call_selector)
            .transpose()?,
        intrinsic_alias: v
            .intrinsic_alias
            .map(decode_intrinsic_alias_selector)
            .transpose()?,
    })
}

pub fn encode_binding_selector(value: &spec::BindingSelector) -> pb::BindingSelector {
    pb::BindingSelector {
        name: Some(value.name.clone()),
        kind: value.kind.map(|kind| kind as i32),
    }
}

pub fn decode_binding_selector(v: pb::BindingSelector) -> Result<spec::BindingSelector> {
    Ok(spec::BindingSelector {
        name: required(v.name)?,
        kind: v.kind.map(pb::BindingSourceKind::try_from).transpose()?,
    })
}

pub fn encode_cross_ref_selector(value: &spec::CrossRefSelector) -> pb::CrossRefSelector {
    pb::CrossRefSelector {
        references: Some(value.references.clone()),
        kind: value.kind.map(|kind| kind as i32),
    }
}

pub fn decode_cross_ref_selector(v: pb::CrossRefSelector) -> Result<spec::CrossRefSelector> {
    Ok(spec::CrossRefSelector {
        references: required(v.references)?,
        kind: v.kind.map(pb::BindingSourceKind::try_from).transpose()?,
    })
}

pub fn encode_reads_member_selector(value: &spec::ReadsMemberSelector) -> pb::ReadsMemberSelector {
    pb::ReadsMemberSelector {
        member: Some(value.member.clone()),
        object: value.object.clone(),
        kind: value.kind.map(|kind| kind as i32),
    }
}

pub fn decode_reads_member_selector(
    v: pb::ReadsMemberSelector,
) -> Result<spec::ReadsMemberSelector> {
    Ok(spec::ReadsMemberSelector {
        member: required(v.member)?,
        object: v.object,
        kind: v.kind.map(pb::BindingSourceKind::try_from).transpose()?,
    })
}

pub fn encode_member_of_module_selector(
    value: &spec::MemberOfModuleSelector,
) -> pb::MemberOfModuleSelector {
    pb::MemberOfModuleSelector {
        module: Some(value.module.clone()),
        member: Some(value.member.clone()),
        kind: value.kind.map(|kind| kind as i32),
    }
}

pub fn decode_member_of_module_selector(
    v: pb::MemberOfModuleSelector,
) -> Result<spec::MemberOfModuleSelector> {
    Ok(spec::MemberOfModuleSelector {
        module: required(v.module)?,
        member: required(v.member)?,
        kind: v.kind.map(pb::BindingSourceKind::try_from).transpose()?,
    })
}

pub fn encode_passed_to_call_selector(
    value: &spec::PassedToCallSelector,
) -> pb::PassedToCallSelector {
    pb::PassedToCallSelector {
        callee_member: Some(value.callee_member.clone()),
        object: value.object.clone(),
        arg_index: value.arg_index.map(|index| index as u64),
        kind: value.kind.map(|kind| kind as i32),
    }
}

pub fn decode_passed_to_call_selector(
    v: pb::PassedToCallSelector,
) -> Result<spec::PassedToCallSelector> {
    Ok(spec::PassedToCallSelector {
        callee_member: required(v.callee_member)?,
        object: v.object,
        arg_index: v
            .arg_index
            .map(|index| usize::try_from(index))
            .transpose()?,
        kind: v.kind.map(pb::BindingSourceKind::try_from).transpose()?,
    })
}

pub fn encode_makes_decorate_call_selector(
    value: &spec::MakesDecorateCallSelector,
) -> pb::MakesDecorateCallSelector {
    pb::MakesDecorateCallSelector {
        class: Some(value.class.clone()),
        member: value.member.clone(),
        kind: value.kind.map(|kind| kind as i32),
    }
}

pub fn decode_makes_decorate_call_selector(
    v: pb::MakesDecorateCallSelector,
) -> Result<spec::MakesDecorateCallSelector> {
    Ok(spec::MakesDecorateCallSelector {
        class: required(v.class)?,
        member: v.member,
        kind: v.kind.map(pb::BindingSourceKind::try_from).transpose()?,
    })
}

pub fn encode_intrinsic_alias_selector(
    value: &spec::IntrinsicAliasSelector,
) -> pb::IntrinsicAliasSelector {
    pb::IntrinsicAliasSelector {
        property: Some(value.property.clone()),
        referenced_by: Some(value.referenced_by.clone()),
    }
}

pub fn decode_intrinsic_alias_selector(
    v: pb::IntrinsicAliasSelector,
) -> Result<spec::IntrinsicAliasSelector> {
    Ok(spec::IntrinsicAliasSelector {
        property: required(v.property)?,
        referenced_by: required(v.referenced_by)?,
    })
}

pub fn encode_source_match_claim(value: &spec::SourceMatchClaim) -> pb::SourceMatchClaim {
    pb::SourceMatchClaim {
        identifiers: Some(value.identifiers as i32),
        match_source: Some(value.match_source.clone()),
        bindings: value
            .bindings
            .iter()
            .map(encode_source_match_binding)
            .collect(),
        note: value.note.clone(),
    }
}

pub fn decode_source_match_claim(v: pb::SourceMatchClaim) -> Result<spec::SourceMatchClaim> {
    Ok(spec::SourceMatchClaim {
        identifiers: pb::SourceMatchIdentifierMode::try_from(
            v.identifiers.context("missing required projection field")?,
        )?,
        match_source: required(v.match_source)?,
        bindings: v
            .bindings
            .into_iter()
            .map(decode_source_match_binding)
            .collect::<Result<_>>()?,
        note: v.note,
    })
}

pub fn encode_source_match_binding(value: &spec::SourceMatchBinding) -> pb::SourceMatchBinding {
    use pb::source_match_binding::Value;
    pb::SourceMatchBinding {
        value: Some(match value {
            spec::SourceMatchBinding::Local(v) => Value::Local(v.clone()),
            spec::SourceMatchBinding::Detailed(v) => Value::Detailed(v.clone()),
        }),
    }
}

pub fn decode_source_match_binding(v: pb::SourceMatchBinding) -> Result<spec::SourceMatchBinding> {
    use pb::source_match_binding::Value;
    Ok(match v.value.context("missing source match binding")? {
        Value::Local(v) => spec::SourceMatchBinding::Local(v),
        Value::Detailed(v) => spec::SourceMatchBinding::Detailed(v),
    })
}

pub fn encode_binding_annotation(value: &spec::BindingAnnotation) -> pb::BindingAnnotation {
    pb::BindingAnnotation {
        retained_record_factory: Some(value.retained_record_factory),
        purity: Some(value.purity as i32),
        initializer_purity: Some(value.initializer_purity as i32),
        initializer_binding_effects: value
            .initializer_binding_effects
            .map(|effects| effects as i32),
        initializer_external_effects: value
            .initializer_external_effects
            .map(|effects| effects as i32),
        effect: Some(value.effect as i32),
        pure_members: value.pure_members.iter().cloned().collect(),
        no_sync_callback_members: value.no_sync_callback_members.iter().cloned().collect(),
        comment: value.comment.clone(),
        note: value.note.clone(),
    }
}

pub fn decode_binding_annotation(v: pb::BindingAnnotation) -> Result<spec::BindingAnnotation> {
    Ok(spec::BindingAnnotation {
        retained_record_factory: required(v.retained_record_factory)?,
        purity: pb::MemberPurity::try_from(v.purity.context("missing member purity")?)?,
        initializer_purity: pb::InitializerPurity::try_from(
            v.initializer_purity.context("missing initializer purity")?,
        )?,
        initializer_binding_effects: v
            .initializer_binding_effects
            .map(pb::InitializerBindingEffects::try_from)
            .transpose()?,
        initializer_external_effects: v
            .initializer_external_effects
            .map(pb::InitializerExternalEffects::try_from)
            .transpose()?,
        effect: pb::MemberEffect::try_from(v.effect.context("missing member effect")?)?,
        pure_members: v.pure_members,
        no_sync_callback_members: v.no_sync_callback_members,
        comment: v.comment,
        note: v.note,
    })
}

pub fn encode_anonymous_statement(value: &spec::AnonymousStatement) -> pb::AnonymousStatement {
    pb::AnonymousStatement {
        match_source: value.match_source.clone(),
        source_match: value.source_match.as_ref().map(encode_source_match),
        no_reentry_call: value.no_reentry_call.as_ref().map(encode_no_reentry_call),
        note: value.note.clone(),
        comment: value.comment.clone(),
    }
}

pub fn decode_anonymous_statement(v: pb::AnonymousStatement) -> Result<spec::AnonymousStatement> {
    Ok(spec::AnonymousStatement {
        match_source: v.match_source,
        source_match: v.source_match.map(decode_source_match).transpose()?,
        no_reentry_call: v.no_reentry_call.map(decode_no_reentry_call).transpose()?,
        note: v.note,
        comment: v.comment,
    })
}

pub fn encode_source_match(value: &spec::SourceMatch) -> pb::SourceMatch {
    pb::SourceMatch {
        identifiers: Some(value.identifiers as i32),
        target_binding: value.target_binding.clone(),
        match_source: Some(value.match_source.clone()),
    }
}

pub fn decode_source_match(v: pb::SourceMatch) -> Result<spec::SourceMatch> {
    Ok(spec::SourceMatch {
        identifiers: pb::SourceMatchIdentifierMode::try_from(
            v.identifiers.context("missing required projection field")?,
        )?,
        target_binding: v.target_binding,
        match_source: required(v.match_source)?,
    })
}

pub fn encode_no_reentry_call(value: &spec::NoReentryCall) -> pb::NoReentryCall {
    pb::NoReentryCall {
        callback_arguments: value
            .callback_arguments
            .iter()
            .map(|index| *index as u64)
            .collect(),
    }
}

pub fn decode_no_reentry_call(v: pb::NoReentryCall) -> Result<spec::NoReentryCall> {
    let result = spec::NoReentryCall {
        callback_arguments: v
            .callback_arguments
            .into_iter()
            .map(|index| Ok(usize::try_from(index)?))
            .collect::<Result<_>>()?,
    };
    ensure!(
        !result.callback_arguments.is_empty(),
        "no_reentry_call must list at least one callback argument position"
    );
    ensure!(
        result
            .callback_arguments
            .iter()
            .collect::<BTreeSet<_>>()
            .len()
            == result.callback_arguments.len(),
        "duplicate no_reentry_call callback argument position"
    );
    Ok(result)
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
                complete_partial_atomic_groups_for: complete_partial_atomic_groups_for
                    .iter()
                    .cloned()
                    .collect(),
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

pub fn encode_value_input_contract(value: &spec::ValueInputContract) -> pb::ValueInputContract {
    pb::ValueInputContract {
        source_match: Some(encode_source_match_claim(&value.source_match)),
        value: Some(encode_captured_value_contract(&value.value)),
        audited_source_sha256: Some(value.audited_source_sha256.clone()),
        expected_export: value.expected_export.clone(),
        note: value.note.clone(),
    }
}

pub fn decode_value_input_contract(v: pb::ValueInputContract) -> Result<spec::ValueInputContract> {
    Ok(spec::ValueInputContract {
        source_match: decode_source_match_claim(required(v.source_match)?)?,
        value: decode_captured_value_contract(required(v.value)?)?,
        audited_source_sha256: required(v.audited_source_sha256)?,
        expected_export: v.expected_export,
        note: v.note,
    })
}

pub fn encode_captured_value_contract(
    value: &spec::CapturedValueContract,
) -> pb::CapturedValueContract {
    pb::CapturedValueContract {
        kind: Some(value.kind as i32),
    }
}

pub fn decode_captured_value_contract(
    v: pb::CapturedValueContract,
) -> Result<spec::CapturedValueContract> {
    Ok(spec::CapturedValueContract {
        kind: pb::CapturedValueKind::try_from(
            v.kind.context("missing required projection field")?,
        )?,
    })
}

pub fn encode_native_operation_input_contract(
    value: &spec::NativeOperationInputContract,
) -> pb::NativeOperationInputContract {
    pb::NativeOperationInputContract {
        selector: Some(encode_source_unit_selector(&value.selector)),
        operation: Some(value.operation as i32),
        audited_source_sha256: Some(value.audited_source_sha256.clone()),
        note: value.note.clone(),
    }
}

pub fn decode_native_operation_input_contract(
    v: pb::NativeOperationInputContract,
) -> Result<spec::NativeOperationInputContract> {
    Ok(spec::NativeOperationInputContract {
        selector: decode_source_unit_selector(required(v.selector)?)?,
        operation: pb::NativeOperationKind::try_from(
            v.operation.context("missing required projection field")?,
        )?,
        audited_source_sha256: required(v.audited_source_sha256)?,
        note: v.note,
    })
}

pub fn encode_source_unit_selector(value: &spec::SourceUnitSelector) -> pb::SourceUnitSelector {
    use pb::source_unit_selector::Value;
    pb::SourceUnitSelector {
        value: Some(match value {
            spec::SourceUnitSelector::SourceMatch(v) => {
                Value::SourceMatch(encode_source_match_claim(v))
            }
            spec::SourceUnitSelector::ResolvedMember(v) => Value::ResolvedMember(v.clone()),
        }),
    }
}

pub fn decode_source_unit_selector(v: pb::SourceUnitSelector) -> Result<spec::SourceUnitSelector> {
    use pb::source_unit_selector::Value;
    Ok(match v.value.context("missing source unit selector")? {
        Value::SourceMatch(v) => {
            spec::SourceUnitSelector::SourceMatch(decode_source_match_claim(v)?)
        }
        Value::ResolvedMember(v) => spec::SourceUnitSelector::ResolvedMember(v),
    })
}

pub fn encode_initializer_call_effect_input_contract(
    value: &spec::InitializerCallEffectInputContract,
) -> pb::InitializerCallEffectInputContract {
    pb::InitializerCallEffectInputContract {
        resolved_member: Some(value.resolved_member.clone()),
        audited_source_sha256: Some(value.audited_source_sha256.clone()),
        provider_chunk: Some(value.provider_chunk.clone()),
        provider_export: Some(value.provider_export.clone()),
        audited_provider_sha256: Some(value.audited_provider_sha256.clone()),
        returns_opaque_function_from_const_primitive: Some(
            value.returns_opaque_function_from_const_primitive,
        ),
        effective_replacement: value
            .effective_replacement
            .as_ref()
            .map(encode_initializer_call_effective_replacement),
        note: value.note.clone(),
    }
}

pub fn decode_initializer_call_effect_input_contract(
    v: pb::InitializerCallEffectInputContract,
) -> Result<spec::InitializerCallEffectInputContract> {
    Ok(spec::InitializerCallEffectInputContract {
        resolved_member: v
            .resolved_member
            .context("missing required projection field")?,
        audited_source_sha256: required(v.audited_source_sha256)?,
        provider_chunk: required(v.provider_chunk)?,
        provider_export: required(v.provider_export)?,
        audited_provider_sha256: required(v.audited_provider_sha256)?,
        returns_opaque_function_from_const_primitive: required(
            v.returns_opaque_function_from_const_primitive,
        )?,
        effective_replacement: v
            .effective_replacement
            .map(decode_initializer_call_effective_replacement)
            .transpose()?,
        note: v.note,
    })
}

pub fn encode_initializer_call_effective_replacement(
    value: &spec::InitializerCallEffectiveReplacement,
) -> pb::InitializerCallEffectiveReplacement {
    pb::InitializerCallEffectiveReplacement {
        package: Some(value.package.clone()),
        bundle_export: Some(value.bundle_export.clone()),
        symbol_kind: Some(value.symbol_kind as i32),
        upstream_export: value.upstream_export.clone(),
        audited_bundle_sha256: Some(value.audited_bundle_sha256.clone()),
    }
}

pub fn decode_initializer_call_effective_replacement(
    v: pb::InitializerCallEffectiveReplacement,
) -> Result<spec::InitializerCallEffectiveReplacement> {
    Ok(spec::InitializerCallEffectiveReplacement {
        package: required(v.package)?,
        bundle_export: required(v.bundle_export)?,
        symbol_kind: pb::PartialSwapKind::try_from(
            v.symbol_kind.context("missing required projection field")?,
        )?,
        upstream_export: v.upstream_export,
        audited_bundle_sha256: required(v.audited_bundle_sha256)?,
    })
}

pub fn encode_imported_primitive_array_spread_input_contract(
    value: &spec::ImportedPrimitiveArraySpreadInputContract,
) -> pb::ImportedPrimitiveArraySpreadInputContract {
    pb::ImportedPrimitiveArraySpreadInputContract {
        source_match: Some(encode_source_match_claim(&value.source_match)),
        operands: value
            .operands
            .iter()
            .map(encode_imported_primitive_array_operand_contract)
            .collect(),
        audited_source_sha256: Some(value.audited_source_sha256.clone()),
        lifetime: Some(encode_imported_primitive_array_lifetime_contract(
            &value.lifetime,
        )),
        note: value.note.clone(),
    }
}

pub fn decode_imported_primitive_array_spread_input_contract(
    v: pb::ImportedPrimitiveArraySpreadInputContract,
) -> Result<spec::ImportedPrimitiveArraySpreadInputContract> {
    Ok(spec::ImportedPrimitiveArraySpreadInputContract {
        source_match: decode_source_match_claim(required(v.source_match)?)?,
        operands: v
            .operands
            .into_iter()
            .map(decode_imported_primitive_array_operand_contract)
            .collect::<Result<_>>()?,
        audited_source_sha256: required(v.audited_source_sha256)?,
        lifetime: decode_imported_primitive_array_lifetime_contract(required(v.lifetime)?)?,
        note: v.note,
    })
}

pub fn encode_imported_primitive_array_operand_contract(
    value: &spec::ImportedPrimitiveArrayOperandContract,
) -> pb::ImportedPrimitiveArrayOperandContract {
    pb::ImportedPrimitiveArrayOperandContract {
        selector_local: Some(value.selector_local.clone()),
        provider_chunk: Some(value.provider_chunk.clone()),
        provider_source_match: Some(encode_source_match_claim(&value.provider_source_match)),
        audited_provider_source_sha256: Some(value.audited_provider_source_sha256.clone()),
    }
}

pub fn decode_imported_primitive_array_operand_contract(
    v: pb::ImportedPrimitiveArrayOperandContract,
) -> Result<spec::ImportedPrimitiveArrayOperandContract> {
    Ok(spec::ImportedPrimitiveArrayOperandContract {
        selector_local: required(v.selector_local)?,
        provider_chunk: required(v.provider_chunk)?,
        provider_source_match: decode_source_match_claim(required(v.provider_source_match)?)?,
        audited_provider_source_sha256: required(v.audited_provider_source_sha256)?,
    })
}

pub fn encode_imported_primitive_array_lifetime_contract(
    value: &spec::ImportedPrimitiveArrayLifetimeContract,
) -> pb::ImportedPrimitiveArrayLifetimeContract {
    pb::ImportedPrimitiveArrayLifetimeContract {
        identity_unchanged_through_spread: Some(value.identity_unchanged_through_spread),
        dense_own_data_primitive_contents_unchanged_through_spread: Some(
            value.dense_own_data_primitive_contents_unchanged_through_spread,
        ),
        ordinary_array_prototype_chain_unchanged_through_spread: Some(
            value.ordinary_array_prototype_chain_unchanged_through_spread,
        ),
        no_own_iterator_or_accessors_through_spread: Some(
            value.no_own_iterator_or_accessors_through_spread,
        ),
        no_rebinding_through_spread: Some(value.no_rebinding_through_spread),
    }
}

pub fn decode_imported_primitive_array_lifetime_contract(
    v: pb::ImportedPrimitiveArrayLifetimeContract,
) -> Result<spec::ImportedPrimitiveArrayLifetimeContract> {
    Ok(spec::ImportedPrimitiveArrayLifetimeContract {
        identity_unchanged_through_spread: required(v.identity_unchanged_through_spread)?,
        dense_own_data_primitive_contents_unchanged_through_spread: required(
            v.dense_own_data_primitive_contents_unchanged_through_spread,
        )?,
        ordinary_array_prototype_chain_unchanged_through_spread: required(
            v.ordinary_array_prototype_chain_unchanged_through_spread,
        )?,
        no_own_iterator_or_accessors_through_spread: required(
            v.no_own_iterator_or_accessors_through_spread,
        )?,
        no_rebinding_through_spread: required(v.no_rebinding_through_spread)?,
    })
}

pub fn encode_commuting_statements_contract(
    value: &spec::CommutingStatementsContract,
) -> pb::CommutingStatementsContract {
    pb::CommutingStatementsContract {
        a: Some(encode_source_unit_selector(&value.a)),
        b: Some(encode_source_unit_selector(&value.b)),
        note: value.note.clone(),
    }
}

pub fn decode_commuting_statements_contract(
    v: pb::CommutingStatementsContract,
) -> Result<spec::CommutingStatementsContract> {
    Ok(spec::CommutingStatementsContract {
        a: decode_source_unit_selector(required(v.a)?)?,
        b: decode_source_unit_selector(required(v.b)?)?,
        note: v.note,
    })
}

pub fn encode_native_call_identity_input_contract(
    value: &spec::NativeCallIdentityInputContract,
) -> pb::NativeCallIdentityInputContract {
    pb::NativeCallIdentityInputContract {
        source_match: Some(encode_source_match_claim(&value.source_match)),
        callee: Some(value.callee as i32),
        audited_source_sha256: Some(value.audited_source_sha256.clone()),
        note: value.note.clone(),
    }
}

pub fn decode_native_call_identity_input_contract(
    v: pb::NativeCallIdentityInputContract,
) -> Result<spec::NativeCallIdentityInputContract> {
    Ok(spec::NativeCallIdentityInputContract {
        source_match: decode_source_match_claim(required(v.source_match)?)?,
        callee: pb::NativeCallIdentityKind::try_from(
            v.callee.context("missing required projection field")?,
        )?,
        audited_source_sha256: required(v.audited_source_sha256)?,
        note: v.note,
    })
}

pub fn encode_argument_input_contract(
    value: &spec::ArgumentInputContract,
) -> pb::ArgumentInputContract {
    pb::ArgumentInputContract {
        source_match: Some(encode_source_match_claim(&value.source_match)),
        parameter_index: Some(value.parameter_index as u64),
        exact_arity: Some(value.exact_arity as u64),
        audited_source_sha256: Some(value.audited_source_sha256.clone()),
        note: value.note.clone(),
    }
}

pub fn decode_argument_input_contract(
    v: pb::ArgumentInputContract,
) -> Result<spec::ArgumentInputContract> {
    Ok(spec::ArgumentInputContract {
        source_match: decode_source_match_claim(required(v.source_match)?)?,
        parameter_index: usize::try_from(required(v.parameter_index)?)?,
        exact_arity: usize::try_from(required(v.exact_arity)?)?,
        audited_source_sha256: required(v.audited_source_sha256)?,
        note: v.note,
    })
}

pub fn encode_no_accessor_install_callable_contract(
    value: &spec::NoAccessorInstallCallableContract,
) -> pb::NoAccessorInstallCallableContract {
    pb::NoAccessorInstallCallableContract {
        source_match: Some(encode_source_match_claim(&value.source_match)),
        note: value.note.clone(),
    }
}

pub fn decode_no_accessor_install_callable_contract(
    v: pb::NoAccessorInstallCallableContract,
) -> Result<spec::NoAccessorInstallCallableContract> {
    Ok(spec::NoAccessorInstallCallableContract {
        source_match: decode_source_match_claim(required(v.source_match)?)?,
        note: v.note,
    })
}

pub fn encode_admission_overrides(value: &spec::AdmissionOverrides) -> pb::AdmissionOverrides {
    pb::AdmissionOverrides {
        a1_eval: Some(value.a1_eval),
        a3_dynamic_import: Some(value.a3_dynamic_import),
        a5_import_meta: Some(value.a5_import_meta),
    }
}

pub fn decode_admission_overrides(v: pb::AdmissionOverrides) -> Result<spec::AdmissionOverrides> {
    Ok(spec::AdmissionOverrides {
        a1_eval: required(v.a1_eval)?,
        a3_dynamic_import: required(v.a3_dynamic_import)?,
        a5_import_meta: required(v.a5_import_meta)?,
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
