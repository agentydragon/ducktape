//! Strict typed adapters for saved semantic evidence.

use std::borrow::Cow;
use std::collections::BTreeSet;
use std::path::PathBuf;
use std::sync::Arc;

use anyhow::{Context, Result, ensure};

pub use saved_analysis_proto::ducktape::debundle::saved_analysis as proto;

pub trait ProtoConvert<P>: Sized {
    fn to_proto(&self) -> Result<P>;
    fn from_proto(value: P) -> Result<Self>;
}

pub fn required<T>(value: Option<T>, field: &str) -> Result<T> {
    value.with_context(|| format!("missing saved-analysis field {field}"))
}

// Domain-owned spec enums use the same Protobuf vocabulary in both projection
// and saved artifacts. Keep their conversion in spec_wire rather than
// maintaining a second set of enum mappings here. Generated spec enums are
// cast only at their Protobuf fields.
impl ProtoConvert<i32> for spec::AllIntrinsicFamilies {
    fn to_proto(&self) -> Result<i32> {
        Ok(spec_wire::Wire::to_proto(self))
    }
    fn from_proto(value: i32) -> Result<Self> {
        spec_wire::Wire::from_proto(value)
    }
}
impl ProtoConvert<i32> for spec::CapturedValueKind {
    fn to_proto(&self) -> Result<i32> {
        Ok(spec_wire::Wire::to_proto(self))
    }
    fn from_proto(value: i32) -> Result<Self> {
        spec_wire::Wire::from_proto(value)
    }
}
impl ProtoConvert<i32> for spec::EmbeddedLoader {
    fn to_proto(&self) -> Result<i32> {
        Ok(spec_wire::Wire::to_proto(self))
    }
    fn from_proto(value: i32) -> Result<Self> {
        spec_wire::Wire::from_proto(value)
    }
}
impl ProtoConvert<i32> for spec::InitializerExternalEffects {
    fn to_proto(&self) -> Result<i32> {
        Ok(spec_wire::Wire::to_proto(self))
    }
    fn from_proto(value: i32) -> Result<Self> {
        spec_wire::Wire::from_proto(value)
    }
}
impl ProtoConvert<i32> for spec::IntrinsicFamily {
    fn to_proto(&self) -> Result<i32> {
        Ok(spec_wire::Wire::to_proto(self))
    }
    fn from_proto(value: i32) -> Result<Self> {
        spec_wire::Wire::from_proto(value)
    }
}
impl ProtoConvert<i32> for spec::LoaderResultForEvaluatingModule {
    fn to_proto(&self) -> Result<i32> {
        Ok(spec_wire::Wire::to_proto(self))
    }
    fn from_proto(value: i32) -> Result<Self> {
        spec_wire::Wire::from_proto(value)
    }
}
impl ProtoConvert<i32> for spec::NativeCallIdentityKind {
    fn to_proto(&self) -> Result<i32> {
        Ok(spec_wire::Wire::to_proto(self))
    }
    fn from_proto(value: i32) -> Result<Self> {
        spec_wire::Wire::from_proto(value)
    }
}
impl ProtoConvert<i32> for spec::NativeOperationKind {
    fn to_proto(&self) -> Result<i32> {
        Ok(spec_wire::Wire::to_proto(self))
    }
    fn from_proto(value: i32) -> Result<Self> {
        spec_wire::Wire::from_proto(value)
    }
}
impl ProtoConvert<i32> for spec::NonLiteralLoaderCalls {
    fn to_proto(&self) -> Result<i32> {
        Ok(spec_wire::Wire::to_proto(self))
    }
    fn from_proto(value: i32) -> Result<Self> {
        spec_wire::Wire::from_proto(value)
    }
}
impl ProtoConvert<i32> for spec::PartialSwapKind {
    fn to_proto(&self) -> Result<i32> {
        Ok(spec_wire::Wire::to_proto(self))
    }
    fn from_proto(value: i32) -> Result<Self> {
        spec_wire::Wire::from_proto(value)
    }
}
impl ProtoConvert<i32> for spec::PredefinedGlobals {
    fn to_proto(&self) -> Result<i32> {
        Ok(spec_wire::Wire::to_proto(self))
    }
    fn from_proto(value: i32) -> Result<Self> {
        spec_wire::Wire::from_proto(value)
    }
}
impl ProtoConvert<i32> for spec::ProcessPrototypeChainAccessors {
    fn to_proto(&self) -> Result<i32> {
        Ok(spec_wire::Wire::to_proto(self))
    }
    fn from_proto(value: i32) -> Result<Self> {
        spec_wire::Wire::from_proto(value)
    }
}
impl ProtoConvert<i32> for spec::ReentryFreeOperation {
    fn to_proto(&self) -> Result<i32> {
        Ok(spec_wire::Wire::to_proto(self))
    }
    fn from_proto(value: i32) -> Result<Self> {
        spec_wire::Wire::from_proto(value)
    }
}
impl ProtoConvert<i32> for spec::SourceMatchIdentifierMode {
    fn to_proto(&self) -> Result<i32> {
        Ok(spec_wire::Wire::to_proto(self))
    }
    fn from_proto(value: i32) -> Result<Self> {
        spec_wire::Wire::from_proto(value)
    }
}
impl ProtoConvert<i32> for spec::SynchronousLoadDrainsMicrotasks {
    fn to_proto(&self) -> Result<i32> {
        Ok(spec_wire::Wire::to_proto(self))
    }
    fn from_proto(value: i32) -> Result<Self> {
        spec_wire::Wire::from_proto(value)
    }
}
impl ProtoConvert<i32> for spec::SynchronousLoadRunsAppCallbacks {
    fn to_proto(&self) -> Result<i32> {
        Ok(spec_wire::Wire::to_proto(self))
    }
    fn from_proto(value: i32) -> Result<Self> {
        spec_wire::Wire::from_proto(value)
    }
}
impl ProtoConvert<i32> for spec::SynchronousModuleLoader {
    fn to_proto(&self) -> Result<i32> {
        Ok(spec_wire::Wire::to_proto(self))
    }
    fn from_proto(value: i32) -> Result<Self> {
        spec_wire::Wire::from_proto(value)
    }
}

impl ProtoConvert<String> for String {
    fn to_proto(&self) -> Result<String> {
        Ok(self.clone())
    }
    fn from_proto(value: String) -> Result<Self> {
        Ok(value)
    }
}

impl ProtoConvert<u32> for u32 {
    fn to_proto(&self) -> Result<u32> {
        Ok(*self)
    }
    fn from_proto(value: u32) -> Result<Self> {
        Ok(value)
    }
}

impl ProtoConvert<u64> for u64 {
    fn to_proto(&self) -> Result<u64> {
        Ok(*self)
    }
    fn from_proto(value: u64) -> Result<Self> {
        Ok(value)
    }
}

impl ProtoConvert<i32> for i32 {
    fn to_proto(&self) -> Result<i32> {
        Ok(*self)
    }
    fn from_proto(value: i32) -> Result<Self> {
        Ok(value)
    }
}

impl ProtoConvert<bool> for bool {
    fn to_proto(&self) -> Result<bool> {
        Ok(*self)
    }
    fn from_proto(value: bool) -> Result<Self> {
        Ok(value)
    }
}

impl ProtoConvert<u64> for usize {
    fn to_proto(&self) -> Result<u64> {
        Ok(u64::try_from(*self)?)
    }
    fn from_proto(value: u64) -> Result<Self> {
        Ok(usize::try_from(value)?)
    }
}

impl ProtoConvert<u32> for u8 {
    fn to_proto(&self) -> Result<u32> {
        Ok(u32::from(*self))
    }
    fn from_proto(value: u32) -> Result<Self> {
        Ok(u8::try_from(value)?)
    }
}

impl ProtoConvert<String> for swc_atoms::Atom {
    fn to_proto(&self) -> Result<String> {
        Ok(self.to_string())
    }
    fn from_proto(value: String) -> Result<Self> {
        Ok(value.into())
    }
}

impl ProtoConvert<String> for PathBuf {
    fn to_proto(&self) -> Result<String> {
        Ok(self
            .to_str()
            .context("non-UTF8 saved-analysis path")?
            .to_owned())
    }
    fn from_proto(value: String) -> Result<Self> {
        Ok(value.into())
    }
}

impl ProtoConvert<u64> for source_identity::SourceUnitOrdinal {
    fn to_proto(&self) -> Result<u64> {
        Ok(u64::try_from(self.0)?)
    }
    fn from_proto(value: u64) -> Result<Self> {
        Ok(Self(usize::try_from(value)?))
    }
}

impl ProtoConvert<proto::SourceSpan> for swc_common::Span {
    fn to_proto(&self) -> Result<proto::SourceSpan> {
        Ok(proto::SourceSpan {
            lo: Some(self.lo.0),
            hi: Some(self.hi.0),
        })
    }
    fn from_proto(value: proto::SourceSpan) -> Result<Self> {
        let lo = required(value.lo, "SourceSpan.lo")?;
        let hi = required(value.hi, "SourceSpan.hi")?;
        ensure!(lo <= hi, "inverted saved-analysis source span");
        Ok(Self::new(swc_common::BytePos(lo), swc_common::BytePos(hi)))
    }
}

impl ProtoConvert<proto::SavedBinding> for analysis_codec::SavedBinding {
    fn to_proto(&self) -> Result<proto::SavedBinding> {
        Ok(proto::SavedBinding {
            symbol: Some(self.symbol().to_string()),
            context_ordinal: Some(self.context_ordinal()),
        })
    }
    fn from_proto(value: proto::SavedBinding) -> Result<Self> {
        Ok(Self::from_parts(
            required(value.symbol, "SavedBinding.symbol")?.into(),
            required(value.context_ordinal, "SavedBinding.context_ordinal")?,
        ))
    }
}

impl ProtoConvert<u32> for analysis_model::PrimKinds {
    fn to_proto(&self) -> Result<u32> {
        Ok([
            Self::UNDEFINED,
            Self::NULL,
            Self::BOOLEAN,
            Self::NUMBER,
            Self::BIGINT,
            Self::STRING,
            Self::SYMBOL,
        ]
        .into_iter()
        .enumerate()
        .filter(|(_, kind)| self.intersects(*kind))
        .fold(0, |bits, (index, _)| bits | (1 << index)))
    }
    fn from_proto(value: u32) -> Result<Self> {
        ensure!(value < 128, "unknown saved-analysis primitive kind bits");
        Ok([
            Self::UNDEFINED,
            Self::NULL,
            Self::BOOLEAN,
            Self::NUMBER,
            Self::BIGINT,
            Self::STRING,
            Self::SYMBOL,
        ]
        .into_iter()
        .enumerate()
        .filter(|(index, _)| value & (1 << index) != 0)
        .fold(Self::NONE, |kinds, (_, kind)| kinds.union(kind)))
    }
}

impl<T, P> ProtoConvert<Option<P>> for Option<T>
where
    T: ProtoConvert<P>,
{
    fn to_proto(&self) -> Result<Option<P>> {
        self.as_ref().map(T::to_proto).transpose()
    }
    fn from_proto(value: Option<P>) -> Result<Self> {
        value.map(T::from_proto).transpose()
    }
}

impl<T, P> ProtoConvert<Vec<P>> for Vec<T>
where
    T: ProtoConvert<P>,
{
    fn to_proto(&self) -> Result<Vec<P>> {
        self.iter().map(T::to_proto).collect()
    }
    fn from_proto(value: Vec<P>) -> Result<Self> {
        value.into_iter().map(T::from_proto).collect()
    }
}

impl<T, P> ProtoConvert<Vec<P>> for BTreeSet<T>
where
    T: ProtoConvert<P> + Ord,
{
    fn to_proto(&self) -> Result<Vec<P>> {
        self.iter().map(T::to_proto).collect()
    }
    fn from_proto(value: Vec<P>) -> Result<Self> {
        decode_set(value)
    }
}

impl<T, P> ProtoConvert<P> for Box<T>
where
    T: ProtoConvert<P>,
{
    fn to_proto(&self) -> Result<P> {
        self.as_ref().to_proto()
    }
    fn from_proto(value: P) -> Result<Self> {
        Ok(Self::new(T::from_proto(value)?))
    }
}

impl<T, P> ProtoConvert<P> for Arc<T>
where
    T: ProtoConvert<P>,
{
    fn to_proto(&self) -> Result<P> {
        self.as_ref().to_proto()
    }
    fn from_proto(value: P) -> Result<Self> {
        Ok(Self::new(T::from_proto(value)?))
    }
}

impl<'a, T, P> ProtoConvert<P> for Cow<'a, T>
where
    T: Clone + ProtoConvert<P>,
{
    fn to_proto(&self) -> Result<P> {
        self.as_ref().to_proto()
    }
    fn from_proto(value: P) -> Result<Self> {
        Ok(Self::Owned(T::from_proto(value)?))
    }
}

impl<'a, T, P> ProtoConvert<Vec<P>> for Cow<'a, [T]>
where
    T: Clone + ProtoConvert<P>,
{
    fn to_proto(&self) -> Result<Vec<P>> {
        self.iter().map(T::to_proto).collect()
    }
    fn from_proto(value: Vec<P>) -> Result<Self> {
        Ok(Self::Owned(Vec::from_proto(value)?))
    }
}

impl crate::saved_proto::ProtoConvert<crate::saved_proto::proto::RetainedCallbackSignature>
    for spec::RetainedCallbackSignature
{
    fn to_proto(&self) -> anyhow::Result<crate::saved_proto::proto::RetainedCallbackSignature> {
        self.validate().map_err(anyhow::Error::msg)?;
        Ok(crate::saved_proto::proto::RetainedCallbackSignature {
            argument_count: Some(crate::saved_proto::ProtoConvert::to_proto(
                &self.argument_count,
            )?),
            callback_arguments: self
                .callback_arguments
                .iter()
                .map(ProtoConvert::to_proto)
                .collect::<Result<_>>()?,
        })
    }
    fn from_proto(
        value: crate::saved_proto::proto::RetainedCallbackSignature,
    ) -> anyhow::Result<Self> {
        let signature = Self {
            argument_count: crate::saved_proto::ProtoConvert::from_proto(
                crate::saved_proto::required(
                    value.argument_count,
                    "RetainedCallbackSignature.argument_count",
                )?,
            )?,
            callback_arguments: decode_set(value.callback_arguments)?,
        };
        signature.validate().map_err(anyhow::Error::msg)?;
        Ok(signature)
    }
}

fn read_domain<T, P>(value: Option<P>, field: &str) -> Result<T>
where
    T: ProtoConvert<P>,
{
    T::from_proto(required(value, field)?)
}

pub(crate) fn decode_set<T, P>(items: Vec<P>) -> Result<BTreeSet<T>>
where
    T: Ord + ProtoConvert<P>,
{
    let mut values = BTreeSet::new();
    for item in items {
        ensure!(
            values.insert(T::from_proto(item)?),
            "duplicate saved set member"
        );
    }
    Ok(values)
}

fn descriptor_copy_signature_is_valid(
    signature: &spec::DescriptorCopyFactorySignature,
) -> Result<()> {
    // These are the same preconditions checked when cross-module analysis
    // admits an audited descriptor-copy provider. Missing repeated fields must
    // not shrink its read/write effects into an empty, usable certificate.
    ensure!(
        signature.argument_count == 1
            && signature.object_argument == 0
            && !signature.eager_own_data_keys.is_empty()
            && !signature.assigned_data_keys.is_empty()
            && !signature.eager_own_data_keys.contains("__proto__")
            && !signature.assigned_data_keys.contains("__proto__"),
        "saved descriptor-copy signature requires one object argument and nonempty safe key sets"
    );
    Ok(())
}

impl ProtoConvert<proto::DescriptorCopyFactorySignature> for spec::DescriptorCopyFactorySignature {
    fn to_proto(&self) -> Result<proto::DescriptorCopyFactorySignature> {
        descriptor_copy_signature_is_valid(self)?;
        Ok(proto::DescriptorCopyFactorySignature {
            argument_count: Some(self.argument_count.to_proto()?),
            object_argument: Some(self.object_argument.to_proto()?),
            eager_own_data_keys: self.eager_own_data_keys.iter().cloned().collect(),
            assigned_data_keys: self.assigned_data_keys.iter().cloned().collect(),
        })
    }
    fn from_proto(value: proto::DescriptorCopyFactorySignature) -> Result<Self> {
        let signature = Self {
            argument_count: read_domain(
                value.argument_count,
                "DescriptorCopyFactorySignature.argument_count",
            )?,
            object_argument: read_domain(
                value.object_argument,
                "DescriptorCopyFactorySignature.object_argument",
            )?,
            eager_own_data_keys: decode_set(value.eager_own_data_keys)?,
            assigned_data_keys: decode_set(value.assigned_data_keys)?,
        };
        descriptor_copy_signature_is_valid(&signature)?;
        Ok(signature)
    }
}

impl ProtoConvert<proto::HostBehavior> for spec::HostBehavior {
    fn to_proto(&self) -> Result<proto::HostBehavior> {
        Ok(proto::HostBehavior {
            synchronous_module_loader: Some(self.synchronous_module_loader.to_proto()?),
            loader_result_for_evaluating_module: self
                .loader_result_for_evaluating_module
                .as_ref()
                .map(ProtoConvert::to_proto)
                .transpose()?,
            synchronous_load_runs_app_callbacks: self
                .synchronous_load_runs_app_callbacks
                .as_ref()
                .map(ProtoConvert::to_proto)
                .transpose()?,
            synchronous_load_drains_microtasks: self
                .synchronous_load_drains_microtasks
                .as_ref()
                .map(ProtoConvert::to_proto)
                .transpose()?,
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
                .map(ProtoConvert::to_proto)
                .collect::<Result<_>>()?,
            reentry_free_operations: self
                .reentry_free_operations
                .iter()
                .map(ProtoConvert::to_proto)
                .collect::<Result<_>>()?,
            predefined_globals: Some(self.predefined_globals.to_proto()?),
            note: self.note.clone(),
        })
    }
    fn from_proto(value: proto::HostBehavior) -> Result<Self> {
        Ok(Self {
            synchronous_module_loader: read_domain(
                value.synchronous_module_loader,
                "HostBehavior.synchronous_module_loader",
            )?,
            loader_result_for_evaluating_module: value
                .loader_result_for_evaluating_module
                .map(spec::LoaderResultForEvaluatingModule::from_proto)
                .transpose()?,
            synchronous_load_runs_app_callbacks: value
                .synchronous_load_runs_app_callbacks
                .map(spec::SynchronousLoadRunsAppCallbacks::from_proto)
                .transpose()?,
            synchronous_load_drains_microtasks: value
                .synchronous_load_drains_microtasks
                .map(spec::SynchronousLoadDrainsMicrotasks::from_proto)
                .transpose()?,
            reentry_free_builtin_modules: spec_wire::unique_set(
                value
                    .reentry_free_builtin_modules
                    .into_iter()
                    .map(|value| Ok(spec::NodeBuiltinModule::try_from(value)?)),
            )?,
            reentry_free_bun_modules: spec_wire::unique_set(
                value
                    .reentry_free_bun_modules
                    .into_iter()
                    .map(|value| Ok(spec::BunModule::try_from(value)?)),
            )?,
            reentry_free_embedded_loaders: decode_set(value.reentry_free_embedded_loaders)?,
            reentry_free_operations: decode_set(value.reentry_free_operations)?,
            predefined_globals: read_domain(
                value.predefined_globals,
                "HostBehavior.predefined_globals",
            )?,
            note: value.note,
        })
    }
}

impl ProtoConvert<proto::HostModule> for spec::HostModule {
    fn to_proto(&self) -> Result<proto::HostModule> {
        use proto::host_module::Value;
        Ok(proto::HostModule {
            value: Some(match self {
                Self::Node(module) => Value::Node(*module as i32),
                Self::Bun(module) => Value::Bun(*module as i32),
            }),
        })
    }
    fn from_proto(value: proto::HostModule) -> Result<Self> {
        use proto::host_module::Value;
        Ok(match required(value.value, "HostModule.value")? {
            Value::Node(value) => Self::Node(spec::NodeBuiltinModule::try_from(value)?),
            Value::Bun(value) => Self::Bun(spec::BunModule::try_from(value)?),
        })
    }
}

impl ProtoConvert<proto::ImportedPrimitiveArrayLifetimeContract>
    for spec::ImportedPrimitiveArrayLifetimeContract
{
    fn to_proto(&self) -> Result<proto::ImportedPrimitiveArrayLifetimeContract> {
        Ok(proto::ImportedPrimitiveArrayLifetimeContract {
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
        })
    }
    fn from_proto(value: proto::ImportedPrimitiveArrayLifetimeContract) -> Result<Self> {
        Ok(Self {
            identity_unchanged_through_spread: required(
                value.identity_unchanged_through_spread,
                "ImportedPrimitiveArrayLifetimeContract.identity_unchanged_through_spread",
            )?,
            dense_own_data_primitive_contents_unchanged_through_spread: required(
                value.dense_own_data_primitive_contents_unchanged_through_spread,
                "ImportedPrimitiveArrayLifetimeContract.dense_own_data_primitive_contents_unchanged_through_spread",
            )?,
            ordinary_array_prototype_chain_unchanged_through_spread: required(
                value.ordinary_array_prototype_chain_unchanged_through_spread,
                "ImportedPrimitiveArrayLifetimeContract.ordinary_array_prototype_chain_unchanged_through_spread",
            )?,
            no_own_iterator_or_accessors_through_spread: required(
                value.no_own_iterator_or_accessors_through_spread,
                "ImportedPrimitiveArrayLifetimeContract.no_own_iterator_or_accessors_through_spread",
            )?,
            no_rebinding_through_spread: required(
                value.no_rebinding_through_spread,
                "ImportedPrimitiveArrayLifetimeContract.no_rebinding_through_spread",
            )?,
        })
    }
}

impl ProtoConvert<proto::IntrinsicFamilies> for spec::IntrinsicFamilies {
    fn to_proto(&self) -> Result<proto::IntrinsicFamilies> {
        use proto::intrinsic_families::{ListedIntrinsics, Value};
        Ok(proto::IntrinsicFamilies {
            value: Some(match self {
                Self::All(all) => Value::All(all.to_proto()?),
                Self::Listed(families) => Value::Listed(ListedIntrinsics {
                    families: families
                        .iter()
                        .map(ProtoConvert::to_proto)
                        .collect::<Result<_>>()?,
                }),
            }),
        })
    }
    fn from_proto(value: proto::IntrinsicFamilies) -> Result<Self> {
        use proto::intrinsic_families::Value;
        Ok(match required(value.value, "IntrinsicFamilies.value")? {
            Value::All(value) => Self::All(spec::AllIntrinsicFamilies::from_proto(value)?),
            Value::Listed(value) => Self::Listed(decode_set(value.families)?),
        })
    }
}

impl ProtoConvert<proto::IntrinsicsNeverWritten> for spec::IntrinsicsNeverWritten {
    fn to_proto(&self) -> Result<proto::IntrinsicsNeverWritten> {
        Ok(proto::IntrinsicsNeverWritten {
            families: Some(self.families.to_proto()?),
            note: self.note.clone(),
        })
    }
    fn from_proto(value: proto::IntrinsicsNeverWritten) -> Result<Self> {
        Ok(Self {
            families: read_domain(value.families, "IntrinsicsNeverWritten.families")?,
            note: value.note,
        })
    }
}

impl ProtoConvert<proto::ProgramPremises> for spec::ProgramPremises {
    fn to_proto(&self) -> Result<proto::ProgramPremises> {
        Ok(proto::ProgramPremises {
            process_prototype_chain_accessors: Some(
                self.process_prototype_chain_accessors.to_proto()?,
            ),
            non_literal_loader_calls: Some(self.non_literal_loader_calls.to_proto()?),
            intrinsics_never_written: self
                .intrinsics_never_written
                .as_ref()
                .map(ProtoConvert::to_proto)
                .transpose()?,
            note: self.note.clone(),
        })
    }
    fn from_proto(value: proto::ProgramPremises) -> Result<Self> {
        Ok(Self {
            process_prototype_chain_accessors: read_domain(
                value.process_prototype_chain_accessors,
                "ProgramPremises.process_prototype_chain_accessors",
            )?,
            non_literal_loader_calls: read_domain(
                value.non_literal_loader_calls,
                "ProgramPremises.non_literal_loader_calls",
            )?,
            intrinsics_never_written: value
                .intrinsics_never_written
                .map(spec::IntrinsicsNeverWritten::from_proto)
                .transpose()?,
            note: value.note,
        })
    }
}

impl ProtoConvert<proto::ResolvedMemberSelector> for spec::ResolvedMemberSelector {
    fn to_proto(&self) -> Result<proto::ResolvedMemberSelector> {
        Ok(proto::ResolvedMemberSelector {
            module: Some(self.module.clone()),
            name: Some(self.name.clone()),
        })
    }
    fn from_proto(value: proto::ResolvedMemberSelector) -> Result<Self> {
        Ok(Self {
            module: required(value.module, "ResolvedMemberSelector.module")?,
            name: required(value.name, "ResolvedMemberSelector.name")?,
        })
    }
}

impl ProtoConvert<proto::SourceMatchBinding> for spec::SourceMatchBinding {
    fn to_proto(&self) -> Result<proto::SourceMatchBinding> {
        use proto::source_match_binding::Value;
        Ok(proto::SourceMatchBinding {
            value: Some(match self {
                Self::Local(local) => Value::Local(local.clone()),
                Self::Detailed(detail) => Value::Detailed(detail.to_proto()?),
            }),
        })
    }
    fn from_proto(value: proto::SourceMatchBinding) -> Result<Self> {
        use proto::source_match_binding::Value;
        Ok(match required(value.value, "SourceMatchBinding.value")? {
            Value::Local(value) => Self::Local(value),
            Value::Detailed(value) => {
                Self::Detailed(spec::SourceMatchBindingDetail::from_proto(value)?)
            }
        })
    }
}

impl ProtoConvert<proto::SourceMatchBindingDetail> for spec::SourceMatchBindingDetail {
    fn to_proto(&self) -> Result<proto::SourceMatchBindingDetail> {
        Ok(proto::SourceMatchBindingDetail {
            local: Some(self.local.clone()),
            name: self.name.clone(),
        })
    }
    fn from_proto(value: proto::SourceMatchBindingDetail) -> Result<Self> {
        Ok(Self {
            local: required(value.local, "SourceMatchBindingDetail.local")?,
            name: value.name,
        })
    }
}

impl ProtoConvert<proto::SourceMatchClaim> for spec::SourceMatchClaim {
    fn to_proto(&self) -> Result<proto::SourceMatchClaim> {
        Ok(proto::SourceMatchClaim {
            identifiers: Some(self.identifiers.to_proto()?),
            match_source: Some(self.match_source.clone()),
            bindings: self
                .bindings
                .iter()
                .map(ProtoConvert::to_proto)
                .collect::<Result<_>>()?,
            note: self.note.clone(),
        })
    }
    fn from_proto(value: proto::SourceMatchClaim) -> Result<Self> {
        Ok(Self {
            identifiers: read_domain(value.identifiers, "SourceMatchClaim.identifiers")?,
            match_source: required(value.match_source, "SourceMatchClaim.match_source")?,
            bindings: value
                .bindings
                .into_iter()
                .map(spec::SourceMatchBinding::from_proto)
                .collect::<Result<_>>()?,
            note: value.note,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::analysis_inputs::ProofDependencies;

    fn round_trip<T, P>(value: &T)
    where
        T: ProtoConvert<P> + std::fmt::Debug + PartialEq,
        P: prost::Message + Default,
    {
        let bytes = analysis_codec::encode_protobuf(&value.to_proto().unwrap()).unwrap();
        let wire: P = analysis_codec::decode_protobuf(&bytes).unwrap();
        assert_eq!(&T::from_proto(wire).unwrap(), value);
    }

    #[test]
    fn domain_evidence_preserves_internal_selectors_and_exact_capabilities() {
        let claim = spec::SourceMatchClaim {
            identifiers: spec::SourceMatchIdentifierMode::Exact,
            match_source: "const value = factory();".into(),
            bindings: vec![
                spec::SourceMatchBinding::Local("value".into()),
                spec::SourceMatchBinding::Detailed(spec::SourceMatchBindingDetail {
                    local: "factory".into(),
                    name: None,
                }),
                spec::SourceMatchBinding::Detailed(spec::SourceMatchBindingDetail {
                    local: "value".into(),
                    name: Some(String::new()),
                }),
            ],
            note: None,
        };
        round_trip(&claim);
        round_trip(&spec::ResolvedMemberSelector {
            module: "provider".into(),
            name: "factory".into(),
        });
        round_trip(&spec::DescriptorCopyFactorySignature {
            argument_count: 1,
            object_argument: 0,
            eager_own_data_keys: BTreeSet::from(["read".into()]),
            assigned_data_keys: BTreeSet::from(["write".into()]),
        });
        for families in [
            spec::IntrinsicFamilies::All(spec::AllIntrinsicFamilies::All),
            spec::IntrinsicFamilies::Listed(BTreeSet::new()),
            spec::IntrinsicFamilies::Listed(BTreeSet::from([spec::IntrinsicFamily::Array])),
        ] {
            round_trip(&spec::ProgramPremises {
                process_prototype_chain_accessors: spec::ProcessPrototypeChainAccessors::None,
                non_literal_loader_calls: spec::NonLiteralLoaderCalls::NativeAddonsOnly,
                intrinsics_never_written: Some(spec::IntrinsicsNeverWritten {
                    families,
                    note: None,
                }),
                note: None,
            });
        }
        round_trip(&spec::ProgramPremises::default());
        round_trip(&spec::HostModule::Node(spec::NodeBuiltinModule::Fs));
        round_trip(&spec::HostModule::Bun(spec::BunModule::BunFfi));
    }

    #[test]
    fn domain_authorities_require_presence_and_reject_invalid_variants() {
        let asserted_host = spec::HostBehavior {
            synchronous_module_loader: spec::SynchronousModuleLoader::Present,
            loader_result_for_evaluating_module: Some(
                spec::LoaderResultForEvaluatingModule::Throws,
            ),
            synchronous_load_runs_app_callbacks: Some(spec::SynchronousLoadRunsAppCallbacks::Never),
            synchronous_load_drains_microtasks: Some(spec::SynchronousLoadDrainsMicrotasks::Never),
            reentry_free_builtin_modules: BTreeSet::from([spec::NodeBuiltinModule::Fs]),
            reentry_free_bun_modules: BTreeSet::from([spec::BunModule::BunFfi]),
            reentry_free_embedded_loaders: BTreeSet::from([spec::EmbeddedLoader::Text]),
            reentry_free_operations: BTreeSet::from([spec::ReentryFreeOperation::PromiseResolve]),
            predefined_globals: spec::PredefinedGlobals::Standard,
            note: None,
        };
        let wire = asserted_host.to_proto().unwrap();
        let bytes = analysis_codec::encode_protobuf(&wire).unwrap();
        let decoded: proto::HostBehavior = analysis_codec::decode_protobuf(&bytes).unwrap();
        let restored = spec::HostBehavior::from_proto(decoded).unwrap();
        assert!(restored.note.is_none());
        assert_eq!(restored.to_proto().unwrap(), wire);
        let mut host = spec::HostBehavior::default().to_proto().unwrap();
        assert!(spec::HostBehavior::from_proto(host.clone()).is_ok());
        host.synchronous_module_loader = None;
        assert!(spec::HostBehavior::from_proto(host).is_err());
        let mut host = spec::HostBehavior::default().to_proto().unwrap();
        host.loader_result_for_evaluating_module = Some(0);
        assert!(spec::HostBehavior::from_proto(host).is_err());
        let mut premises = spec::ProgramPremises::default().to_proto().unwrap();
        premises.process_prototype_chain_accessors = None;
        assert!(spec::ProgramPremises::from_proto(premises).is_err());
        assert!(spec::HostModule::from_proto(proto::HostModule { value: None }).is_err());
        assert_eq!(
            spec::HostModule::from_proto(proto::HostModule {
                value: Some(proto::host_module::Value::Node(0)),
            })
            .unwrap(),
            spec::HostModule::Node(spec::NodeBuiltinModule::Assert)
        );
        assert!(
            spec::HostModule::from_proto(proto::HostModule {
                value: Some(proto::host_module::Value::Node(i32::MAX)),
            })
            .is_err()
        );
        assert!(
            spec::IntrinsicFamilies::from_proto(proto::IntrinsicFamilies { value: None }).is_err()
        );
        let family = spec::IntrinsicFamily::Array.to_proto().unwrap();
        assert!(
            spec::IntrinsicFamilies::from_proto(proto::IntrinsicFamilies {
                value: Some(proto::intrinsic_families::Value::Listed(
                    proto::intrinsic_families::ListedIntrinsics {
                        families: vec![family, family],
                    }
                )),
            })
            .is_err()
        );
        assert!(
            spec::SourceMatchBinding::from_proto(proto::SourceMatchBinding {
                value: Some(proto::source_match_binding::Value::Detailed(
                    proto::SourceMatchBindingDetail {
                        local: None,
                        name: None
                    }
                )),
            })
            .is_err()
        );
        assert!(
            spec::SourceMatchClaim::from_proto(proto::SourceMatchClaim {
                identifiers: None,
                match_source: Some(String::new()),
                bindings: Vec::new(),
                note: None,
            })
            .is_err()
        );
    }

    #[test]
    fn protobuf_optionals_and_empty_selections_keep_their_domain_meaning() {
        let host = spec::HostBehavior::default();
        let wire = host.to_proto().unwrap();
        assert!(wire.loader_result_for_evaluating_module.is_none());
        assert!(wire.reentry_free_builtin_modules.is_empty());
        let restored = spec::HostBehavior::from_proto(wire).unwrap();
        assert!(restored.loader_result_for_evaluating_module.is_none());
        assert!(restored.reentry_free_operations.is_empty());

        let mut no_intrinsic_promise = spec::ProgramPremises::default();
        let absent = no_intrinsic_promise.to_proto().unwrap();
        assert!(absent.intrinsics_never_written.is_none());
        no_intrinsic_promise.intrinsics_never_written = Some(spec::IntrinsicsNeverWritten {
            families: spec::IntrinsicFamilies::Listed(BTreeSet::new()),
            note: None,
        });
        let explicitly_empty = no_intrinsic_promise.to_proto().unwrap();
        assert_ne!(absent, explicitly_empty);
        round_trip(&no_intrinsic_promise);
        assert!(
            spec::IntrinsicsNeverWritten::from_proto(proto::IntrinsicsNeverWritten {
                families: None,
                note: None,
            })
            .is_err()
        );

        for name in [None, Some(String::new()), Some("renamed".into())] {
            round_trip(&spec::SourceMatchBindingDetail {
                local: "value".into(),
                name,
            });
        }
        round_trip(&spec::SourceMatchClaim {
            identifiers: spec::SourceMatchIdentifierMode::Exact,
            match_source: "run();".into(),
            bindings: Vec::new(),
            note: None,
        });
    }

    #[test]
    fn descriptor_copy_effects_cannot_be_lost_to_protobuf_empty_defaults() {
        let valid = spec::DescriptorCopyFactorySignature {
            argument_count: 1,
            object_argument: 0,
            eager_own_data_keys: BTreeSet::from(["read".into()]),
            assigned_data_keys: BTreeSet::from(["write".into()]),
        };
        let mut wire = valid.to_proto().unwrap();
        wire.eager_own_data_keys.clear();
        assert!(spec::DescriptorCopyFactorySignature::from_proto(wire).is_err());
        let mut wire = valid.to_proto().unwrap();
        wire.assigned_data_keys.clear();
        assert!(spec::DescriptorCopyFactorySignature::from_proto(wire).is_err());
        let mut wire = valid.to_proto().unwrap();
        wire.eager_own_data_keys.push("read".into());
        assert!(spec::DescriptorCopyFactorySignature::from_proto(wire).is_err());
        let mut wire = valid.to_proto().unwrap();
        wire.assigned_data_keys.push("__proto__".into());
        assert!(spec::DescriptorCopyFactorySignature::from_proto(wire).is_err());
        let mut invalid = valid;
        invalid.assigned_data_keys.clear();
        assert!(invalid.to_proto().is_err());
    }

    #[test]
    fn array_lifetime_promises_keep_explicit_false_distinct_from_missing() {
        let contract = spec::ImportedPrimitiveArrayLifetimeContract {
            identity_unchanged_through_spread: true,
            dense_own_data_primitive_contents_unchanged_through_spread: true,
            ordinary_array_prototype_chain_unchanged_through_spread: true,
            no_own_iterator_or_accessors_through_spread: true,
            no_rebinding_through_spread: false,
        };
        round_trip(&contract);
        let mut wire = contract.to_proto().unwrap();
        wire.no_rebinding_through_spread = None;
        assert!(spec::ImportedPrimitiveArrayLifetimeContract::from_proto(wire).is_err());
    }

    #[test]
    fn saved_evidence_and_report_notes_round_trip() {
        let host = spec::HostBehavior {
            note: Some("host audit".into()),
            ..Default::default()
        };
        let saved = host.to_proto().unwrap();
        assert_eq!(
            spec::HostBehavior::from_proto(saved).unwrap().note,
            host.note
        );
        let host_wire = spec_wire::Wire::to_proto(&host);
        let host_restored: spec::HostBehavior = spec_wire::Wire::from_proto(host_wire).unwrap();
        assert_eq!(host_restored.note, host.note);

        let mut premises = spec::ProgramPremises {
            note: Some("program audit".into()),
            ..Default::default()
        };
        round_trip(&premises);
        let wire = spec_wire::Wire::to_proto(&premises);
        let restored: spec::ProgramPremises = spec_wire::Wire::from_proto(wire).unwrap();
        assert_eq!(restored, premises);
        premises.note = None;
        premises.intrinsics_never_written = Some(spec::IntrinsicsNeverWritten {
            families: spec::IntrinsicFamilies::All(spec::AllIntrinsicFamilies::All),
            note: Some("intrinsic audit".into()),
        });
        round_trip(&premises);
        let wire = spec_wire::Wire::to_proto(&premises);
        let restored: spec::ProgramPremises = spec_wire::Wire::from_proto(wire).unwrap();
        assert_eq!(restored, premises);

        let claim = spec::SourceMatchClaim {
            identifiers: spec::SourceMatchIdentifierMode::Exact,
            match_source: "const value = 1;".into(),
            bindings: Vec::new(),
            note: Some("selector audit".into()),
        };
        round_trip(&claim);
        let wire = spec_wire::Wire::to_proto(&claim);
        let restored: spec::SourceMatchClaim = spec_wire::Wire::from_proto(wire).unwrap();
        assert_eq!(restored, claim);
    }

    #[test]
    fn invalid_proof_dependencies_and_variants_fail_closed() {
        let mut dependencies = ProofDependencies::default()
            .to_proto()
            .expect("complete proof");
        assert!(ProofDependencies::from_proto(dependencies.clone()).is_ok());
        dependencies.program_premises.push(0);
        assert!(ProofDependencies::from_proto(dependencies).is_err());
        let mut duplicate = ProofDependencies::default().to_proto().unwrap();
        duplicate.program_premises = vec![1, 1];
        assert!(ProofDependencies::from_proto(duplicate).is_err());
        let mut evidence = crate::facts::GlobalAccessEvidence::default()
            .to_proto()
            .expect("global access evidence");
        evidence.dependencies = None;
        assert!(crate::facts::GlobalAccessEvidence::from_proto(evidence).is_err());
        for value in [0, i32::MAX] {
            assert!(spec::SourceMatchIdentifierMode::from_proto(value).is_err());
        }
    }

    #[test]
    fn retained_callback_signature_checks_semantic_constraints() {
        let valid = spec::RetainedCallbackSignature {
            argument_count: 2,
            callback_arguments: BTreeSet::from([1]),
        };
        let wire = valid.to_proto().expect("valid signature");
        assert_eq!(
            spec::RetainedCallbackSignature::from_proto(wire).unwrap(),
            valid
        );
        for (count, positions) in [(2, vec![]), (2, vec![2]), (0, vec![0]), (3, vec![1, 1])] {
            let wire = proto::RetainedCallbackSignature {
                argument_count: Some(count),
                callback_arguments: positions,
            };
            assert!(spec::RetainedCallbackSignature::from_proto(wire).is_err());
        }
    }

    #[test]
    fn duplicate_set_members_are_not_silently_merged() {
        assert!(decode_set::<String, _>(vec!["duplicate".into(), "duplicate".into()]).is_err());
    }

    #[test]
    fn source_spans_reject_missing_and_inverted_coordinates() {
        for (lo, hi) in [(None, Some(3)), (Some(5), None), (Some(5), Some(3))] {
            assert!(swc_common::Span::from_proto(proto::SourceSpan { lo, hi }).is_err());
        }
        let ordinal = source_identity::SourceUnitOrdinal(usize::MAX);
        assert_eq!(
            source_identity::SourceUnitOrdinal::from_proto(ordinal.to_proto().unwrap()).unwrap(),
            ordinal
        );
        assert!(analysis_model::PrimKinds::from_proto(128).is_err());
    }

    #[test]
    fn generated_fact_enums_keep_report_names_and_numeric_values() {
        macro_rules! check {
            ($type:ident, $variant:ident, $json:literal) => {{
                let value = proto::$type::$variant;
                assert_eq!(serde_json::to_value(value).unwrap(), $json);
                assert_eq!(
                    serde_json::from_value::<proto::$type>(serde_json::json!($json)).unwrap(),
                    value
                );
                assert!(proto::$type::try_from(0).is_ok());
                assert!(proto::$type::try_from(i32::MAX).is_err());
            }};
        }

        check!(
            DataflowOpacityCause,
            DynamicCellWrites,
            "dynamic_cell_writes"
        );
        check!(ExportLinkage, Dependency, "dependency");
        check!(GlobalAccessKind, Read, "read");
        check!(
            PurityRule,
            ToPropertyKeyCoercion,
            "to_property_key_coercion"
        );
        check!(
            RedundantPureMemberReason,
            WhitelistedStaticCall,
            "whitelisted_static_call"
        );
        check!(
            RedundantPurityProofBasis,
            WithOtherPurityHints,
            "with_other_purity_hints"
        );
        check!(
            RedundantPurityReason,
            InferredPlainDataBinding,
            "inferred_plain_data_binding"
        );
        check!(
            RootAttemptRewind,
            LexicalReceiptUnsealed,
            "lexical_receipt_unsealed"
        );
        check!(StatementKind, SideEffect, "side_effect");
    }
}
