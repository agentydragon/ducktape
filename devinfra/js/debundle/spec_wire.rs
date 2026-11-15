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
use std::path::PathBuf;

pub trait Wire: Sized {
    type Proto;
    fn to_proto(&self) -> Self::Proto;
    fn from_proto(value: Self::Proto) -> Result<Self>;
}
pub fn required<T: Wire>(value: Option<T::Proto>) -> Result<T> {
    T::from_proto(value.context("missing required projection field")?)
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
impl Wire for String {
    type Proto = String;
    fn to_proto(&self) -> String {
        self.clone()
    }
    fn from_proto(value: String) -> Result<Self> {
        Ok(value)
    }
}
impl Wire for bool {
    type Proto = bool;
    fn to_proto(&self) -> bool {
        *self
    }
    fn from_proto(value: bool) -> Result<Self> {
        Ok(value)
    }
}
impl Wire for u32 {
    type Proto = u32;
    fn to_proto(&self) -> u32 {
        *self
    }
    fn from_proto(value: u32) -> Result<Self> {
        Ok(value)
    }
}
impl Wire for usize {
    type Proto = u64;
    fn to_proto(&self) -> u64 {
        *self as u64
    }
    fn from_proto(value: u64) -> Result<Self> {
        Ok(value.try_into()?)
    }
}
impl Wire for PathBuf {
    type Proto = Vec<u8>;
    fn to_proto(&self) -> Self::Proto {
        self.as_os_str().as_bytes().to_vec()
    }
    fn from_proto(value: Self::Proto) -> Result<Self> {
        Ok(OsString::from_vec(value).into())
    }
}
impl<T: Wire> Wire for Option<T> {
    type Proto = Option<T::Proto>;
    fn to_proto(&self) -> Self::Proto {
        self.as_ref().map(Wire::to_proto)
    }
    fn from_proto(value: Self::Proto) -> Result<Self> {
        value.map(T::from_proto).transpose()
    }
}

impl<T: Wire> Wire for Vec<T> {
    type Proto = Vec<T::Proto>;
    fn to_proto(&self) -> Self::Proto {
        self.iter().map(Wire::to_proto).collect()
    }
    fn from_proto(value: Self::Proto) -> Result<Self> {
        value.into_iter().map(Wire::from_proto).collect()
    }
}
impl<T: Wire + Ord> Wire for BTreeSet<T> {
    type Proto = Vec<T::Proto>;
    fn to_proto(&self) -> Self::Proto {
        self.iter().map(Wire::to_proto).collect()
    }
    fn from_proto(value: Self::Proto) -> Result<Self> {
        unique_set(value.into_iter().map(Wire::from_proto))
    }
}
impl<T: Wire> Wire for BTreeMap<String, T> {
    type Proto = BTreeMap<String, T::Proto>;
    fn to_proto(&self) -> Self::Proto {
        self.iter()
            .map(|(key, value)| (key.clone(), value.to_proto()))
            .collect()
    }
    fn from_proto(value: Self::Proto) -> Result<Self> {
        value
            .into_iter()
            .map(|(key, value)| Ok((key, T::from_proto(value)?)))
            .collect()
    }
}

pub fn global_names_to_proto(
    value: &BTreeMap<spec::BuiltinGlobalRoot, BTreeSet<String>>,
) -> Vec<pb::GlobalNames> {
    value
        .iter()
        .map(|(root, names)| pb::GlobalNames {
            root: Some(*root as i32),
            names: names.to_proto(),
        })
        .collect()
}
pub fn global_names_from_proto(
    value: Vec<pb::GlobalNames>,
) -> Result<BTreeMap<spec::BuiltinGlobalRoot, BTreeSet<String>>> {
    unique_map(value.into_iter().map(|entry| {
        Ok((
            pb::BuiltinGlobalRoot::try_from(entry.root.context("missing global root")?)?,
            Wire::from_proto(entry.names)?,
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
                    names: names.to_proto(),
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
        .map(|(key, value)| Ok((key, Wire::from_proto(value.names)?)))
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
                    signatures: signatures.to_proto(),
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
        .map(|(key, value)| Ok((key, Wire::from_proto(value.signatures)?)))
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

impl Wire for spec::TransformSpec {
    type Proto = pb::TransformSpec;
    fn to_proto(&self) -> Self::Proto {
        pb::TransformSpec {
            inputs: Some(self.inputs.to_proto()),
            host_behavior: Some(self.host_behavior.to_proto()),
            program_premises: Some(self.program_premises.to_proto()),
            vendor: self.vendor.to_proto(),
            logical_modules: self
                .logical_modules
                .iter()
                .map(|(key, modules)| {
                    (
                        key.clone(),
                        pb::LogicalModules {
                            modules: modules.to_proto(),
                        },
                    )
                })
                .collect(),
            chunk_renames: self.chunk_renames.to_proto(),
            unassigned_mode: self.unassigned_mode.to_proto(),
            chunk_analysis_options: self.chunk_analysis_options.to_proto(),
            chunk_export_purity: self.chunk_export_purity.to_proto(),
            swap_vendor_chunks: Some(self.swap_vendor_chunks.to_proto()),
            materialize_logical_modules: Some(self.materialize_logical_modules.to_proto()),
            write_js_tree: self.write_js_tree.to_proto(),
            emit_browser_harness: self.emit_browser_harness.to_proto(),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            inputs: required(v.inputs)?,
            host_behavior: required(v.host_behavior)?,
            program_premises: required(v.program_premises)?,
            vendor: Wire::from_proto(v.vendor)?,
            logical_modules: v
                .logical_modules
                .into_iter()
                .map(|(key, value)| Ok((key, Wire::from_proto(value.modules)?)))
                .collect::<Result<_>>()?,
            chunk_renames: Wire::from_proto(v.chunk_renames)?,
            unassigned_mode: Wire::from_proto(v.unassigned_mode)?,
            chunk_analysis_options: Wire::from_proto(v.chunk_analysis_options)?,
            chunk_export_purity: Wire::from_proto(v.chunk_export_purity)?,
            swap_vendor_chunks: required(v.swap_vendor_chunks)?,
            materialize_logical_modules: required(v.materialize_logical_modules)?,
            write_js_tree: Wire::from_proto(v.write_js_tree)?,
            emit_browser_harness: Wire::from_proto(v.emit_browser_harness)?,
        })
    }
}
impl Wire for spec::LoadJsChunksArgs {
    type Proto = pb::LoadJsChunksArgs;
    fn to_proto(&self) -> Self::Proto {
        pb::LoadJsChunksArgs {
            input_root: Some(self.input_root.to_proto()),
            js_list_path: Some(self.js_list_path.to_proto()),
            embedded_loaders_path: self.embedded_loaders_path.to_proto(),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            input_root: required(v.input_root)?,
            js_list_path: required(v.js_list_path)?,
            embedded_loaders_path: Wire::from_proto(v.embedded_loaders_path)?,
        })
    }
}
impl Wire for spec::HostBehavior {
    type Proto = pb::HostBehavior;
    fn to_proto(&self) -> Self::Proto {
        pb::HostBehavior {
            synchronous_module_loader: Some(self.synchronous_module_loader as i32),
            loader_result_for_evaluating_module: self
                .loader_result_for_evaluating_module
                .map(|value| value as i32),
            synchronous_load_runs_app_callbacks: self
                .synchronous_load_runs_app_callbacks
                .map(|value| value as i32),
            synchronous_load_drains_microtasks: self
                .synchronous_load_drains_microtasks
                .map(|value| value as i32),
            reentry_free_builtin_modules: self
                .reentry_free_builtin_modules
                .iter()
                .map(|module| *module as i32)
                .collect(),
            reentry_free_bun_modules: self
                .reentry_free_bun_modules
                .iter()
                .map(|module| *module as i32)
                .collect(),
            reentry_free_embedded_loaders: self
                .reentry_free_embedded_loaders
                .iter()
                .map(|value| *value as i32)
                .collect(),
            reentry_free_operations: self
                .reentry_free_operations
                .iter()
                .map(|operation| *operation as i32)
                .collect(),
            predefined_globals: Some(self.predefined_globals as i32),
            note: self.note.clone(),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
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
}
impl Wire for spec::ProgramPremises {
    type Proto = pb::ProgramPremises;
    fn to_proto(&self) -> Self::Proto {
        pb::ProgramPremises {
            process_prototype_chain_accessors: Some(self.process_prototype_chain_accessors as i32),
            non_literal_loader_calls: Some(self.non_literal_loader_calls as i32),
            intrinsics_never_written: self.intrinsics_never_written.to_proto(),
            note: self.note.clone(),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            process_prototype_chain_accessors: pb::ProcessPrototypeChainAccessors::try_from(
                v.process_prototype_chain_accessors
                    .context("missing required projection field")?,
            )?,
            non_literal_loader_calls: pb::NonLiteralLoaderCalls::try_from(
                v.non_literal_loader_calls
                    .context("missing required projection field")?,
            )?,
            intrinsics_never_written: Wire::from_proto(v.intrinsics_never_written)?,
            note: v.note,
        })
    }
}
impl Wire for spec::IntrinsicsNeverWritten {
    type Proto = pb::IntrinsicsNeverWritten;
    fn to_proto(&self) -> Self::Proto {
        pb::IntrinsicsNeverWritten {
            families: Some(self.families.to_proto()),
            note: self.note.clone(),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            families: required(v.families)?,
            note: v.note,
        })
    }
}
impl Wire for spec::IntrinsicFamilies {
    type Proto = pb::IntrinsicFamilies;
    fn to_proto(&self) -> Self::Proto {
        use pb::intrinsic_families::Value;
        pb::IntrinsicFamilies {
            value: Some(match self {
                Self::All(v) => Value::All(*v as i32),
                Self::Listed(v) => Value::Listed(pb::IntrinsicFamilySelection {
                    families: v.iter().map(|family| *family as i32).collect(),
                }),
            }),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        use pb::intrinsic_families::Value;
        Ok(match v.value.context("missing intrinsic families")? {
            Value::All(v) => Self::All(pb::AllIntrinsicFamilies::try_from(v)?),
            Value::Listed(v) => Self::Listed(unique_set(
                v.families
                    .into_iter()
                    .map(|value| Ok(pb::IntrinsicFamily::try_from(value)?)),
            )?),
        })
    }
}
impl Wire for spec::VendorMark {
    type Proto = pb::VendorMark;
    fn to_proto(&self) -> Self::Proto {
        pb::VendorMark {
            identity: Some(self.identity.clone()),
            level: Some(self.level.to_proto()),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            identity: required(v.identity)?,
            level: required(v.level)?,
        })
    }
}
impl Wire for spec::VendorLevel {
    type Proto = pb::VendorLevel;
    fn to_proto(&self) -> Self::Proto {
        use pb::vendor_level::Value;
        pb::VendorLevel {
            value: Some(match self {
                Self::Swap(v) => Value::Swap(v.to_proto()),
                Self::PartialSwap(v) => Value::PartialSwap(v.to_proto()),
                Self::BundledPartialSwap(v) => Value::BundledPartialSwap(v.to_proto()),
            }),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        use pb::vendor_level::Value;
        Ok(match v.value.context("missing vendor level")? {
            Value::Swap(v) => Self::Swap(Wire::from_proto(v)?),
            Value::PartialSwap(v) => Self::PartialSwap(Wire::from_proto(v)?),
            Value::BundledPartialSwap(v) => Self::BundledPartialSwap(Wire::from_proto(v)?),
        })
    }
}
impl Wire for spec::SwapMark {
    type Proto = pb::SwapMark;
    fn to_proto(&self) -> Self::Proto {
        pb::SwapMark {
            package: Some(self.package.clone()),
            version: Some(self.version.clone()),
            subpath: Some(self.subpath.clone()),
            wrapper_shape: self.wrapper_shape.map(|shape| shape as i32),
            default_export_aliases: self.default_export_aliases.to_proto(),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            package: required(v.package)?,
            version: required(v.version)?,
            subpath: required(v.subpath)?,
            wrapper_shape: v
                .wrapper_shape
                .map(pb::WrapperShape::try_from)
                .transpose()?,
            default_export_aliases: Wire::from_proto(v.default_export_aliases)?,
        })
    }
}
impl Wire for spec::PartialSwapMark {
    type Proto = pb::PartialSwapMark;
    fn to_proto(&self) -> Self::Proto {
        pb::PartialSwapMark {
            packages: self.packages.to_proto(),
            symbols: self.symbols.to_proto(),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            packages: Wire::from_proto(v.packages)?,
            symbols: Wire::from_proto(v.symbols)?,
        })
    }
}
impl Wire for spec::PartialSwapPackage {
    type Proto = pb::PartialSwapPackage;
    fn to_proto(&self) -> Self::Proto {
        pb::PartialSwapPackage {
            version: Some(self.version.clone()),
            subpath: Some(self.subpath.clone()),
            namespace: self.namespace.clone(),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            version: required(v.version)?,
            subpath: required(v.subpath)?,
            namespace: v.namespace,
        })
    }
}
impl Wire for spec::PartialSwapSymbol {
    type Proto = pb::PartialSwapSymbol;
    fn to_proto(&self) -> Self::Proto {
        pb::PartialSwapSymbol {
            package: Some(self.package.clone()),
            kind: Some(self.kind as i32),
            upstream_export: self.upstream_export.clone(),
            local: self.local.clone(),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            package: required(v.package)?,
            kind: pb::PartialSwapKind::try_from(
                v.kind.context("missing required projection field")?,
            )?,
            upstream_export: v.upstream_export,
            local: v.local,
        })
    }
}
impl Wire for spec::BundledPartialSwapMark {
    type Proto = pb::BundledPartialSwapMark;
    fn to_proto(&self) -> Self::Proto {
        pb::BundledPartialSwapMark {
            bundle: Some(self.bundle.to_proto()),
            packages: self.packages.to_proto(),
            symbols: self.symbols.to_proto(),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            bundle: required(v.bundle)?,
            packages: Wire::from_proto(v.packages)?,
            symbols: Wire::from_proto(v.symbols)?,
        })
    }
}
impl Wire for spec::BundledPartialSwapBundle {
    type Proto = pb::BundledPartialSwapBundle;
    fn to_proto(&self) -> Self::Proto {
        pb::BundledPartialSwapBundle {
            path: Some(self.path.to_proto()),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            path: required(v.path)?,
        })
    }
}
impl Wire for spec::BundledPartialSwapPackage {
    type Proto = pb::BundledPartialSwapPackage;
    fn to_proto(&self) -> Self::Proto {
        pb::BundledPartialSwapPackage {
            version: Some(self.version.clone()),
            subpath: Some(self.subpath.clone()),
            bundle_export: Some(self.bundle_export.clone()),
            namespace: self.namespace.clone(),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            version: required(v.version)?,
            subpath: required(v.subpath)?,
            bundle_export: required(v.bundle_export)?,
            namespace: v.namespace,
        })
    }
}
impl Wire for spec::LogicalModule {
    type Proto = pb::LogicalModule;
    fn to_proto(&self) -> Self::Proto {
        pb::LogicalModule {
            members: self.members.to_proto(),
            source_matches: self.source_matches.to_proto(),
            annotations: self.annotations.to_proto(),
            anonymous_statements: self.anonymous_statements.to_proto(),
            comment: self.comment.clone(),
            note: self.note.clone(),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            members: Wire::from_proto(v.members)?,
            source_matches: Wire::from_proto(v.source_matches)?,
            annotations: Wire::from_proto(v.annotations)?,
            anonymous_statements: Wire::from_proto(v.anonymous_statements)?,
            comment: v.comment,
            note: v.note,
        })
    }
}
impl Wire for spec::Member {
    type Proto = pb::Member;
    fn to_proto(&self) -> Self::Proto {
        pb::Member {
            name: self.name.clone(),
            selector: Some(self.selector.to_proto()),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            name: v.name,
            selector: required(v.selector)?,
        })
    }
}
impl Wire for spec::RawMemberSelector {
    type Proto = pb::RawMemberSelector;
    fn to_proto(&self) -> Self::Proto {
        pb::RawMemberSelector {
            binding: self.binding.to_proto(),
            cross_ref: self.cross_ref.to_proto(),
            reads_member: self.reads_member.to_proto(),
            member_of_module: self.member_of_module.to_proto(),
            passed_to_call: self.passed_to_call.to_proto(),
            makes_decorate_call: self.makes_decorate_call.to_proto(),
            intrinsic_alias: self.intrinsic_alias.to_proto(),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            binding: Wire::from_proto(v.binding)?,
            cross_ref: Wire::from_proto(v.cross_ref)?,
            reads_member: Wire::from_proto(v.reads_member)?,
            member_of_module: Wire::from_proto(v.member_of_module)?,
            passed_to_call: Wire::from_proto(v.passed_to_call)?,
            makes_decorate_call: Wire::from_proto(v.makes_decorate_call)?,
            intrinsic_alias: Wire::from_proto(v.intrinsic_alias)?,
        })
    }
}
impl Wire for spec::BindingSelector {
    type Proto = pb::BindingSelector;
    fn to_proto(&self) -> Self::Proto {
        pb::BindingSelector {
            name: Some(self.name.clone()),
            kind: self.kind.map(|kind| kind as i32),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            name: required(v.name)?,
            kind: v.kind.map(pb::BindingSourceKind::try_from).transpose()?,
        })
    }
}
impl Wire for spec::CrossRefSelector {
    type Proto = pb::CrossRefSelector;
    fn to_proto(&self) -> Self::Proto {
        pb::CrossRefSelector {
            references: Some(self.references.clone()),
            kind: self.kind.map(|kind| kind as i32),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            references: required(v.references)?,
            kind: v.kind.map(pb::BindingSourceKind::try_from).transpose()?,
        })
    }
}
impl Wire for spec::ReadsMemberSelector {
    type Proto = pb::ReadsMemberSelector;
    fn to_proto(&self) -> Self::Proto {
        pb::ReadsMemberSelector {
            member: Some(self.member.clone()),
            object: self.object.clone(),
            kind: self.kind.map(|kind| kind as i32),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            member: required(v.member)?,
            object: v.object,
            kind: v.kind.map(pb::BindingSourceKind::try_from).transpose()?,
        })
    }
}
impl Wire for spec::MemberOfModuleSelector {
    type Proto = pb::MemberOfModuleSelector;
    fn to_proto(&self) -> Self::Proto {
        pb::MemberOfModuleSelector {
            module: Some(self.module.clone()),
            member: Some(self.member.clone()),
            kind: self.kind.map(|kind| kind as i32),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            module: required(v.module)?,
            member: required(v.member)?,
            kind: v.kind.map(pb::BindingSourceKind::try_from).transpose()?,
        })
    }
}
impl Wire for spec::PassedToCallSelector {
    type Proto = pb::PassedToCallSelector;
    fn to_proto(&self) -> Self::Proto {
        pb::PassedToCallSelector {
            callee_member: Some(self.callee_member.clone()),
            object: self.object.clone(),
            arg_index: self.arg_index.to_proto(),
            kind: self.kind.map(|kind| kind as i32),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            callee_member: required(v.callee_member)?,
            object: v.object,
            arg_index: Wire::from_proto(v.arg_index)?,
            kind: v.kind.map(pb::BindingSourceKind::try_from).transpose()?,
        })
    }
}
impl Wire for spec::MakesDecorateCallSelector {
    type Proto = pb::MakesDecorateCallSelector;
    fn to_proto(&self) -> Self::Proto {
        pb::MakesDecorateCallSelector {
            class: Some(self.class.clone()),
            member: self.member.clone(),
            kind: self.kind.map(|kind| kind as i32),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            class: required(v.class)?,
            member: v.member,
            kind: v.kind.map(pb::BindingSourceKind::try_from).transpose()?,
        })
    }
}
impl Wire for spec::IntrinsicAliasSelector {
    type Proto = pb::IntrinsicAliasSelector;
    fn to_proto(&self) -> Self::Proto {
        pb::IntrinsicAliasSelector {
            property: Some(self.property.clone()),
            referenced_by: Some(self.referenced_by.clone()),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            property: required(v.property)?,
            referenced_by: required(v.referenced_by)?,
        })
    }
}
impl Wire for spec::SourceMatchClaim {
    type Proto = pb::SourceMatchClaim;
    fn to_proto(&self) -> Self::Proto {
        pb::SourceMatchClaim {
            identifiers: Some(self.identifiers as i32),
            match_source: Some(self.match_source.clone()),
            bindings: self.bindings.to_proto(),
            note: self.note.clone(),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            identifiers: pb::SourceMatchIdentifierMode::try_from(
                v.identifiers.context("missing required projection field")?,
            )?,
            match_source: required(v.match_source)?,
            bindings: Wire::from_proto(v.bindings)?,
            note: v.note,
        })
    }
}
impl Wire for spec::SourceMatchBinding {
    type Proto = pb::SourceMatchBinding;
    fn to_proto(&self) -> Self::Proto {
        use pb::source_match_binding::Value;
        pb::SourceMatchBinding {
            value: Some(match self {
                Self::Local(v) => Value::Local(v.clone()),
                Self::Detailed(v) => Value::Detailed(v.to_proto()),
            }),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        use pb::source_match_binding::Value;
        Ok(match v.value.context("missing source match binding")? {
            Value::Local(v) => Self::Local(v),
            Value::Detailed(v) => Self::Detailed(Wire::from_proto(v)?),
        })
    }
}
impl Wire for spec::SourceMatchBindingDetail {
    type Proto = pb::SourceMatchBindingDetail;
    fn to_proto(&self) -> Self::Proto {
        pb::SourceMatchBindingDetail {
            local: Some(self.local.clone()),
            name: self.name.clone(),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            local: required(v.local)?,
            name: v.name,
        })
    }
}
impl Wire for spec::BindingAnnotation {
    type Proto = pb::BindingAnnotation;
    fn to_proto(&self) -> Self::Proto {
        pb::BindingAnnotation {
            retained_record_factory: Some(self.retained_record_factory),
            purity: Some(self.purity as i32),
            initializer_purity: Some(self.initializer_purity as i32),
            initializer_binding_effects: self
                .initializer_binding_effects
                .map(|effects| effects as i32),
            initializer_external_effects: self
                .initializer_external_effects
                .map(|effects| effects as i32),
            effect: Some(self.effect as i32),
            pure_members: self.pure_members.to_proto(),
            no_sync_callback_members: self.no_sync_callback_members.to_proto(),
            comment: self.comment.clone(),
            note: self.note.clone(),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
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
            pure_members: Wire::from_proto(v.pure_members)?,
            no_sync_callback_members: Wire::from_proto(v.no_sync_callback_members)?,
            comment: v.comment,
            note: v.note,
        })
    }
}
impl Wire for spec::AnonymousStatement {
    type Proto = pb::AnonymousStatement;
    fn to_proto(&self) -> Self::Proto {
        pb::AnonymousStatement {
            match_source: self.match_source.clone(),
            source_match: self.source_match.to_proto(),
            no_reentry_call: self.no_reentry_call.to_proto(),
            note: self.note.clone(),
            comment: self.comment.clone(),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            match_source: v.match_source,
            source_match: Wire::from_proto(v.source_match)?,
            no_reentry_call: Wire::from_proto(v.no_reentry_call)?,
            note: v.note,
            comment: v.comment,
        })
    }
}
impl Wire for spec::SourceMatch {
    type Proto = pb::SourceMatch;
    fn to_proto(&self) -> Self::Proto {
        pb::SourceMatch {
            identifiers: Some(self.identifiers as i32),
            target_binding: self.target_binding.clone(),
            match_source: Some(self.match_source.clone()),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            identifiers: pb::SourceMatchIdentifierMode::try_from(
                v.identifiers.context("missing required projection field")?,
            )?,
            target_binding: v.target_binding,
            match_source: required(v.match_source)?,
        })
    }
}
impl Wire for spec::NoReentryCall {
    type Proto = pb::NoReentryCall;
    fn to_proto(&self) -> Self::Proto {
        pb::NoReentryCall {
            callback_arguments: self.callback_arguments.to_proto(),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        let result = Self {
            callback_arguments: Wire::from_proto(v.callback_arguments)?,
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
}
impl Wire for spec::ChunkRenames {
    type Proto = pb::ChunkRenames;
    fn to_proto(&self) -> Self::Proto {
        pb::ChunkRenames {
            members: self.members.to_proto(),
            annotations: self.annotations.to_proto(),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            members: Wire::from_proto(v.members)?,
            annotations: Wire::from_proto(v.annotations)?,
        })
    }
}
impl Wire for spec::ChunkRenameMember {
    type Proto = pb::ChunkRenameMember;
    fn to_proto(&self) -> Self::Proto {
        pb::ChunkRenameMember {
            name: self.name.clone(),
            selector: Some(self.selector.to_proto()),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            name: v.name,
            selector: required(v.selector)?,
        })
    }
}
impl Wire for spec::ChunkRenameSelector {
    type Proto = pb::ChunkRenameSelector;
    fn to_proto(&self) -> Self::Proto {
        pb::ChunkRenameSelector {
            binding: Some(self.binding.to_proto()),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            binding: required(v.binding)?,
        })
    }
}
impl Wire for spec::UnassignedMode {
    type Proto = pb::UnassignedMode;
    fn to_proto(&self) -> Self::Proto {
        use pb::unassigned_mode::Value;
        pb::UnassignedMode {
            value: Some(match self {
                Self::InlineInEntry => Value::InlineInEntry(pb::Unit {}),
                Self::CatchallFile { target } => {
                    Value::CatchallFile(pb::UnassignedModeCatchallFile {
                        target: target.clone(),
                    })
                }
                Self::PerAtomicGroup => Value::PerAtomicGroup(pb::Unit {}),
                Self::CoarseAtomicGroups {
                    complete_partial_atomic_groups_for,
                } => Value::CoarseAtomicGroups(pb::UnassignedModeCoarseAtomicGroups {
                    complete_partial_atomic_groups_for: complete_partial_atomic_groups_for
                        .to_proto(),
                }),
            }),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        use pb::unassigned_mode::Value;
        Ok(match v.value.context("missing unassigned mode")? {
            Value::InlineInEntry(_) => Self::InlineInEntry,
            Value::CatchallFile(v) => Self::CatchallFile { target: v.target },
            Value::PerAtomicGroup(_) => Self::PerAtomicGroup,
            Value::CoarseAtomicGroups(v) => Self::CoarseAtomicGroups {
                complete_partial_atomic_groups_for: Wire::from_proto(
                    v.complete_partial_atomic_groups_for,
                )?,
            },
        })
    }
}
impl Wire for spec::DependencyGraphOptions {
    type Proto = pb::DependencyGraphOptions;
    fn to_proto(&self) -> Self::Proto {
        pb::DependencyGraphOptions {
            value_inputs: self.value_inputs.to_proto(),
            native_operation_inputs: self.native_operation_inputs.to_proto(),
            initializer_call_effect_inputs: self.initializer_call_effect_inputs.to_proto(),
            imported_primitive_array_spread_inputs: self
                .imported_primitive_array_spread_inputs
                .to_proto(),
            commuting_statements: self.commuting_statements.to_proto(),
            native_call_identity_inputs: self.native_call_identity_inputs.to_proto(),
            argument_inputs: self.argument_inputs.to_proto(),
            no_accessor_install_callables: self.no_accessor_install_callables.to_proto(),
            deferred_global_publications: global_names_to_proto(&self.deferred_global_publications),
            non_reentrant_global_membership: global_names_to_proto(
                &self.non_reentrant_global_membership,
            ),
            dataflow_aware_s_chain: Some(self.dataflow_aware_s_chain),
            trusted_dataflow_summaries: Some(self.trusted_dataflow_summaries),
            admission_overrides: Some(self.admission_overrides.to_proto()),
            local_property_effects: Some(self.local_property_effects),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            value_inputs: Wire::from_proto(v.value_inputs)?,
            native_operation_inputs: Wire::from_proto(v.native_operation_inputs)?,
            initializer_call_effect_inputs: Wire::from_proto(v.initializer_call_effect_inputs)?,
            imported_primitive_array_spread_inputs: Wire::from_proto(
                v.imported_primitive_array_spread_inputs,
            )?,
            commuting_statements: Wire::from_proto(v.commuting_statements)?,
            native_call_identity_inputs: Wire::from_proto(v.native_call_identity_inputs)?,
            argument_inputs: Wire::from_proto(v.argument_inputs)?,
            no_accessor_install_callables: Wire::from_proto(v.no_accessor_install_callables)?,
            deferred_global_publications: global_names_from_proto(v.deferred_global_publications)?,
            non_reentrant_global_membership: global_names_from_proto(
                v.non_reentrant_global_membership,
            )?,
            dataflow_aware_s_chain: required(v.dataflow_aware_s_chain)?,
            trusted_dataflow_summaries: required(v.trusted_dataflow_summaries)?,
            admission_overrides: required(v.admission_overrides)?,
            local_property_effects: required(v.local_property_effects)?,
        })
    }
}
impl Wire for spec::ValueInputContract {
    type Proto = pb::ValueInputContract;
    fn to_proto(&self) -> Self::Proto {
        pb::ValueInputContract {
            source_match: Some(self.source_match.to_proto()),
            value: Some(self.value.to_proto()),
            audited_source_sha256: Some(self.audited_source_sha256.clone()),
            expected_export: self.expected_export.clone(),
            note: self.note.clone(),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            source_match: required(v.source_match)?,
            value: required(v.value)?,
            audited_source_sha256: required(v.audited_source_sha256)?,
            expected_export: v.expected_export,
            note: v.note,
        })
    }
}
impl Wire for spec::CapturedValueContract {
    type Proto = pb::CapturedValueContract;
    fn to_proto(&self) -> Self::Proto {
        pb::CapturedValueContract {
            kind: Some(self.kind as i32),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            kind: pb::CapturedValueKind::try_from(
                v.kind.context("missing required projection field")?,
            )?,
        })
    }
}
impl Wire for spec::NativeOperationInputContract {
    type Proto = pb::NativeOperationInputContract;
    fn to_proto(&self) -> Self::Proto {
        pb::NativeOperationInputContract {
            selector: Some(self.selector.to_proto()),
            operation: Some(self.operation as i32),
            audited_source_sha256: Some(self.audited_source_sha256.clone()),
            note: self.note.clone(),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            selector: required(v.selector)?,
            operation: pb::NativeOperationKind::try_from(
                v.operation.context("missing required projection field")?,
            )?,
            audited_source_sha256: required(v.audited_source_sha256)?,
            note: v.note,
        })
    }
}
impl Wire for spec::SourceUnitSelector {
    type Proto = pb::SourceUnitSelector;
    fn to_proto(&self) -> Self::Proto {
        use pb::source_unit_selector::Value;
        pb::SourceUnitSelector {
            value: Some(match self {
                Self::SourceMatch(v) => Value::SourceMatch(v.to_proto()),
                Self::ResolvedMember(v) => Value::ResolvedMember(v.to_proto()),
            }),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        use pb::source_unit_selector::Value;
        Ok(match v.value.context("missing source unit selector")? {
            Value::SourceMatch(v) => Self::SourceMatch(Wire::from_proto(v)?),
            Value::ResolvedMember(v) => Self::ResolvedMember(Wire::from_proto(v)?),
        })
    }
}
impl Wire for spec::ResolvedMemberSelector {
    type Proto = pb::ResolvedMemberSelector;
    fn to_proto(&self) -> Self::Proto {
        pb::ResolvedMemberSelector {
            module: Some(self.module.clone()),
            name: Some(self.name.clone()),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            module: required(v.module)?,
            name: required(v.name)?,
        })
    }
}
impl Wire for spec::InitializerCallEffectInputContract {
    type Proto = pb::InitializerCallEffectInputContract;
    fn to_proto(&self) -> Self::Proto {
        pb::InitializerCallEffectInputContract {
            resolved_member: Some(self.resolved_member.to_proto()),
            audited_source_sha256: Some(self.audited_source_sha256.clone()),
            provider_chunk: Some(self.provider_chunk.clone()),
            provider_export: Some(self.provider_export.clone()),
            audited_provider_sha256: Some(self.audited_provider_sha256.clone()),
            returns_opaque_function_from_const_primitive: Some(
                self.returns_opaque_function_from_const_primitive,
            ),
            effective_replacement: self.effective_replacement.to_proto(),
            note: self.note.clone(),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            resolved_member: required(v.resolved_member)?,
            audited_source_sha256: required(v.audited_source_sha256)?,
            provider_chunk: required(v.provider_chunk)?,
            provider_export: required(v.provider_export)?,
            audited_provider_sha256: required(v.audited_provider_sha256)?,
            returns_opaque_function_from_const_primitive: required(
                v.returns_opaque_function_from_const_primitive,
            )?,
            effective_replacement: Wire::from_proto(v.effective_replacement)?,
            note: v.note,
        })
    }
}
impl Wire for spec::InitializerCallEffectiveReplacement {
    type Proto = pb::InitializerCallEffectiveReplacement;
    fn to_proto(&self) -> Self::Proto {
        pb::InitializerCallEffectiveReplacement {
            package: Some(self.package.clone()),
            bundle_export: Some(self.bundle_export.clone()),
            symbol_kind: Some(self.symbol_kind as i32),
            upstream_export: self.upstream_export.clone(),
            audited_bundle_sha256: Some(self.audited_bundle_sha256.clone()),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            package: required(v.package)?,
            bundle_export: required(v.bundle_export)?,
            symbol_kind: pb::PartialSwapKind::try_from(
                v.symbol_kind.context("missing required projection field")?,
            )?,
            upstream_export: v.upstream_export,
            audited_bundle_sha256: required(v.audited_bundle_sha256)?,
        })
    }
}
impl Wire for spec::ImportedPrimitiveArraySpreadInputContract {
    type Proto = pb::ImportedPrimitiveArraySpreadInputContract;
    fn to_proto(&self) -> Self::Proto {
        pb::ImportedPrimitiveArraySpreadInputContract {
            source_match: Some(self.source_match.to_proto()),
            operands: self.operands.to_proto(),
            audited_source_sha256: Some(self.audited_source_sha256.clone()),
            lifetime: Some(self.lifetime.to_proto()),
            note: self.note.clone(),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            source_match: required(v.source_match)?,
            operands: Wire::from_proto(v.operands)?,
            audited_source_sha256: required(v.audited_source_sha256)?,
            lifetime: required(v.lifetime)?,
            note: v.note,
        })
    }
}
impl Wire for spec::ImportedPrimitiveArrayOperandContract {
    type Proto = pb::ImportedPrimitiveArrayOperandContract;
    fn to_proto(&self) -> Self::Proto {
        pb::ImportedPrimitiveArrayOperandContract {
            selector_local: Some(self.selector_local.clone()),
            provider_chunk: Some(self.provider_chunk.clone()),
            provider_source_match: Some(self.provider_source_match.to_proto()),
            audited_provider_source_sha256: Some(self.audited_provider_source_sha256.clone()),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            selector_local: required(v.selector_local)?,
            provider_chunk: required(v.provider_chunk)?,
            provider_source_match: required(v.provider_source_match)?,
            audited_provider_source_sha256: required(v.audited_provider_source_sha256)?,
        })
    }
}
impl Wire for spec::ImportedPrimitiveArrayLifetimeContract {
    type Proto = pb::ImportedPrimitiveArrayLifetimeContract;
    fn to_proto(&self) -> Self::Proto {
        pb::ImportedPrimitiveArrayLifetimeContract {
            identity_unchanged_through_spread: Some(self.identity_unchanged_through_spread),
            dense_own_data_primitive_contents_unchanged_through_spread: Some(
                self.dense_own_data_primitive_contents_unchanged_through_spread,
            ),
            ordinary_array_prototype_chain_unchanged_through_spread: Some(
                self.ordinary_array_prototype_chain_unchanged_through_spread,
            ),
            no_own_iterator_or_accessors_through_spread: Some(
                self.no_own_iterator_or_accessors_through_spread,
            ),
            no_rebinding_through_spread: Some(self.no_rebinding_through_spread),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
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
}
impl Wire for spec::CommutingStatementsContract {
    type Proto = pb::CommutingStatementsContract;
    fn to_proto(&self) -> Self::Proto {
        pb::CommutingStatementsContract {
            a: Some(self.a.to_proto()),
            b: Some(self.b.to_proto()),
            note: self.note.clone(),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            a: required(v.a)?,
            b: required(v.b)?,
            note: v.note,
        })
    }
}
impl Wire for spec::NativeCallIdentityInputContract {
    type Proto = pb::NativeCallIdentityInputContract;
    fn to_proto(&self) -> Self::Proto {
        pb::NativeCallIdentityInputContract {
            source_match: Some(self.source_match.to_proto()),
            callee: Some(self.callee as i32),
            audited_source_sha256: Some(self.audited_source_sha256.clone()),
            note: self.note.clone(),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            source_match: required(v.source_match)?,
            callee: pb::NativeCallIdentityKind::try_from(
                v.callee.context("missing required projection field")?,
            )?,
            audited_source_sha256: required(v.audited_source_sha256)?,
            note: v.note,
        })
    }
}
impl Wire for spec::ArgumentInputContract {
    type Proto = pb::ArgumentInputContract;
    fn to_proto(&self) -> Self::Proto {
        pb::ArgumentInputContract {
            source_match: Some(self.source_match.to_proto()),
            parameter_index: Some(self.parameter_index.to_proto()),
            exact_arity: Some(self.exact_arity.to_proto()),
            audited_source_sha256: Some(self.audited_source_sha256.clone()),
            note: self.note.clone(),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            source_match: required(v.source_match)?,
            parameter_index: required(v.parameter_index)?,
            exact_arity: required(v.exact_arity)?,
            audited_source_sha256: required(v.audited_source_sha256)?,
            note: v.note,
        })
    }
}
impl Wire for spec::NoAccessorInstallCallableContract {
    type Proto = pb::NoAccessorInstallCallableContract;
    fn to_proto(&self) -> Self::Proto {
        pb::NoAccessorInstallCallableContract {
            source_match: Some(self.source_match.to_proto()),
            note: self.note.clone(),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            source_match: required(v.source_match)?,
            note: v.note,
        })
    }
}
impl Wire for spec::AdmissionOverrides {
    type Proto = pb::AdmissionOverrides;
    fn to_proto(&self) -> Self::Proto {
        pb::AdmissionOverrides {
            a1_eval: Some(self.a1_eval),
            a3_dynamic_import: Some(self.a3_dynamic_import),
            a5_import_meta: Some(self.a5_import_meta),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            a1_eval: required(v.a1_eval)?,
            a3_dynamic_import: required(v.a3_dynamic_import)?,
            a5_import_meta: required(v.a5_import_meta)?,
        })
    }
}
impl Wire for spec::ChunkExportPurity {
    type Proto = pb::ChunkExportPurity;
    fn to_proto(&self) -> Self::Proto {
        pb::ChunkExportPurity {
            source_markers: self
                .source_markers
                .as_ref()
                .map(|markers| pb::SourceMarkers {
                    markers: markers.to_proto(),
                }),
            pure_exports: self.pure_exports.to_proto(),
            pure_new_exports: self.pure_new_exports.to_proto(),
            pure_members: member_names_to_proto(&self.pure_members),
            fluent_exports: self.fluent_exports.to_proto(),
            callback_factory_exports: self.callback_factory_exports.to_proto(),
            inline_callback_factory_exports: self.inline_callback_factory_exports.to_proto(),
            callback_factory_members: member_names_to_proto(&self.callback_factory_members),
            retained_callback_factory_exports: callback_overloads_to_proto(
                &self.retained_callback_factory_exports,
            ),
            descriptor_copy_factory_exports: self
                .descriptor_copy_factory_exports
                .iter()
                .map(|(key, signatures)| {
                    (
                        key.clone(),
                        pb::DescriptorCopyOverloads {
                            signatures: signatures.to_proto(),
                        },
                    )
                })
                .collect(),
            retained_callback_factory_members: member_callback_overloads_to_proto(
                &self.retained_callback_factory_members,
            ),
            retained_callback_constructor_exports: callback_overloads_to_proto(
                &self.retained_callback_constructor_exports,
            ),
            no_reentry_exports: self.no_reentry_exports.to_proto(),
            no_reentry_callback_exports: callback_overloads_to_proto(
                &self.no_reentry_callback_exports,
            ),
            no_reentry_members: member_names_to_proto(&self.no_reentry_members),
            no_reentry_record_members: member_names_to_proto(&self.no_reentry_record_members),
            namespace_independent_members: member_names_to_proto(
                &self.namespace_independent_members,
            ),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            source_markers: v
                .source_markers
                .map(|value| Wire::from_proto(value.markers))
                .transpose()?,
            pure_exports: Wire::from_proto(v.pure_exports)?,
            pure_new_exports: Wire::from_proto(v.pure_new_exports)?,
            pure_members: member_names_from_proto(v.pure_members)?,
            fluent_exports: Wire::from_proto(v.fluent_exports)?,
            callback_factory_exports: Wire::from_proto(v.callback_factory_exports)?,
            inline_callback_factory_exports: Wire::from_proto(v.inline_callback_factory_exports)?,
            callback_factory_members: member_names_from_proto(v.callback_factory_members)?,
            retained_callback_factory_exports: callback_overloads_from_proto(
                v.retained_callback_factory_exports,
            )?,
            descriptor_copy_factory_exports: v
                .descriptor_copy_factory_exports
                .into_iter()
                .map(|(key, value)| Ok((key, Wire::from_proto(value.signatures)?)))
                .collect::<Result<_>>()?,
            retained_callback_factory_members: member_callback_overloads_from_proto(
                v.retained_callback_factory_members,
            )?,
            retained_callback_constructor_exports: callback_overloads_from_proto(
                v.retained_callback_constructor_exports,
            )?,
            no_reentry_exports: Wire::from_proto(v.no_reentry_exports)?,
            no_reentry_callback_exports: callback_overloads_from_proto(
                v.no_reentry_callback_exports,
            )?,
            no_reentry_members: member_names_from_proto(v.no_reentry_members)?,
            no_reentry_record_members: member_names_from_proto(v.no_reentry_record_members)?,
            namespace_independent_members: member_names_from_proto(
                v.namespace_independent_members,
            )?,
        })
    }
}
impl Wire for spec::RetainedCallbackSignature {
    type Proto = pb::RetainedCallbackSignature;
    fn to_proto(&self) -> Self::Proto {
        pb::RetainedCallbackSignature {
            argument_count: Some(self.argument_count.to_proto()),
            callback_arguments: self.callback_arguments.to_proto(),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        let result = Self {
            argument_count: required(v.argument_count)?,
            callback_arguments: Wire::from_proto(v.callback_arguments)?,
        };
        result.validate().map_err(anyhow::Error::msg)?;
        Ok(result)
    }
}
impl Wire for spec::DescriptorCopyFactorySignature {
    type Proto = pb::DescriptorCopyFactorySignature;
    fn to_proto(&self) -> Self::Proto {
        pb::DescriptorCopyFactorySignature {
            argument_count: Some(self.argument_count.to_proto()),
            object_argument: Some(self.object_argument.to_proto()),
            eager_own_data_keys: self.eager_own_data_keys.to_proto(),
            assigned_data_keys: self.assigned_data_keys.to_proto(),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            argument_count: required(v.argument_count)?,
            object_argument: required(v.object_argument)?,
            eager_own_data_keys: Wire::from_proto(v.eager_own_data_keys)?,
            assigned_data_keys: Wire::from_proto(v.assigned_data_keys)?,
        })
    }
}
impl Wire for spec::SwapVendorChunksConfig {
    type Proto = pb::SwapVendorChunksConfig;
    fn to_proto(&self) -> Self::Proto {
        pb::SwapVendorChunksConfig {
            output_manifest_path: self.output_manifest_path.to_proto(),
            output_wrapper_dir: self.output_wrapper_dir.to_proto(),
            write: Some(self.write),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            output_manifest_path: Wire::from_proto(v.output_manifest_path)?,
            output_wrapper_dir: Wire::from_proto(v.output_wrapper_dir)?,
            write: required(v.write)?,
        })
    }
}
impl Wire for spec::MaterializeLogicalModulesConfig {
    type Proto = pb::MaterializeLogicalModulesConfig;
    fn to_proto(&self) -> Self::Proto {
        pb::MaterializeLogicalModulesConfig {
            file: self.file.clone(),
            prune_other_chunks: Some(self.prune_other_chunks),
            report_out_dir: self.report_out_dir.to_proto(),
            target_dir: Some(self.target_dir.clone()),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            file: v.file,
            prune_other_chunks: required(v.prune_other_chunks)?,
            report_out_dir: Wire::from_proto(v.report_out_dir)?,
            target_dir: required(v.target_dir)?,
        })
    }
}
impl Wire for spec::WriteJsTreeConfig {
    type Proto = pb::WriteJsTreeConfig;
    fn to_proto(&self) -> Self::Proto {
        pb::WriteJsTreeConfig {
            out_dir: Some(self.out_dir.to_proto()),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            out_dir: required(v.out_dir)?,
        })
    }
}
impl Wire for spec::EmitBrowserHarnessConfig {
    type Proto = pb::EmitBrowserHarnessConfig;
    fn to_proto(&self) -> Self::Proto {
        pb::EmitBrowserHarnessConfig {
            asset_summary_path: Some(self.asset_summary_path.to_proto()),
            out_dir: Some(self.out_dir.to_proto()),
            snapshot_root: Some(self.snapshot_root.to_proto()),
        }
    }
    fn from_proto(v: Self::Proto) -> Result<Self> {
        Ok(Self {
            asset_summary_path: required(v.asset_summary_path)?,
            out_dir: required(v.out_dir)?,
            snapshot_root: required(v.snapshot_root)?,
        })
    }
}
