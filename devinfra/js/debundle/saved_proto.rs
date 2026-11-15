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

pub(crate) fn decode_set_direct<T: Ord>(items: Vec<T>) -> Result<BTreeSet<T>> {
    let count = items.len();
    let values: BTreeSet<_> = items.into_iter().collect();
    ensure!(values.len() == count, "duplicate saved set member");
    Ok(values)
}

pub(crate) fn encode_native_intrinsics(
    items: &BTreeSet<crate::analysis_inputs::NativeIntrinsic>,
) -> Result<Vec<i32>> {
    Ok(items.iter().map(|item| *item as i32).collect())
}

pub(crate) fn decode_native_intrinsics(
    items: Vec<i32>,
) -> Result<BTreeSet<crate::analysis_inputs::NativeIntrinsic>> {
    let mut values = BTreeSet::new();
    for value in items {
        ensure!(
            values.insert(proto::NativeIntrinsic::try_from(value)?),
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

    fn round_trip_spec_wire<T>(value: &T)
    where
        T: spec_wire::Wire + std::fmt::Debug + PartialEq,
        T::Proto: prost::Message + Default,
    {
        let bytes = analysis_codec::encode_protobuf(&spec_wire::Wire::to_proto(value)).unwrap();
        let wire: T::Proto = analysis_codec::decode_protobuf(&bytes).unwrap();
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
        round_trip_spec_wire(&claim);
        round_trip_spec_wire(&spec::ResolvedMemberSelector {
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
            round_trip_spec_wire(&spec::ProgramPremises {
                process_prototype_chain_accessors: spec::ProcessPrototypeChainAccessors::None,
                non_literal_loader_calls: spec::NonLiteralLoaderCalls::NativeAddonsOnly,
                intrinsics_never_written: Some(spec::IntrinsicsNeverWritten {
                    families,
                    note: None,
                }),
                note: None,
            });
        }
        round_trip_spec_wire(&spec::ProgramPremises::default());
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
        let wire = spec_wire::Wire::to_proto(&asserted_host);
        let bytes = analysis_codec::encode_protobuf(&wire).unwrap();
        let decoded: <spec::HostBehavior as spec_wire::Wire>::Proto =
            analysis_codec::decode_protobuf(&bytes).unwrap();
        let restored: spec::HostBehavior = spec_wire::Wire::from_proto(decoded).unwrap();
        assert!(restored.note.is_none());
        assert_eq!(spec_wire::Wire::to_proto(&restored), wire);
        let mut host = spec_wire::Wire::to_proto(&spec::HostBehavior::default());
        assert!(<spec::HostBehavior as spec_wire::Wire>::from_proto(host.clone()).is_ok());
        host.synchronous_module_loader = None;
        assert!(<spec::HostBehavior as spec_wire::Wire>::from_proto(host).is_err());
        let mut host = spec_wire::Wire::to_proto(&spec::HostBehavior::default());
        host.loader_result_for_evaluating_module = Some(i32::MAX);
        assert!(<spec::HostBehavior as spec_wire::Wire>::from_proto(host).is_err());
        let mut host = spec_wire::Wire::to_proto(&spec::HostBehavior::default());
        host.reentry_free_builtin_modules
            .push(spec::NodeBuiltinModule::Fs as i32);
        host.reentry_free_builtin_modules
            .push(spec::NodeBuiltinModule::Fs as i32);
        assert!(<spec::HostBehavior as spec_wire::Wire>::from_proto(host).is_err());
        let mut premises = spec_wire::Wire::to_proto(&spec::ProgramPremises::default());
        premises.process_prototype_chain_accessors = None;
        assert!(<spec::ProgramPremises as spec_wire::Wire>::from_proto(premises).is_err());
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
        let mut families = spec_wire::Wire::to_proto(&spec::IntrinsicFamilies::All(
            spec::AllIntrinsicFamilies::All,
        ));
        families.value = None;
        assert!(<spec::IntrinsicFamilies as spec_wire::Wire>::from_proto(families).is_err());
        let mut families =
            spec_wire::Wire::to_proto(&spec::IntrinsicFamilies::Listed(BTreeSet::from([
                spec::IntrinsicFamily::Array,
            ])));
        if let Some(spec_wire_proto::debundle::spec::intrinsic_families::Value::Listed(selection)) =
            families.value.as_mut()
        {
            selection.families.push(spec::IntrinsicFamily::Array as i32);
        } else {
            panic!("listed intrinsic family selection should encode as Listed");
        }
        assert!(<spec::IntrinsicFamilies as spec_wire::Wire>::from_proto(families).is_err());
        let mut source_match = spec_wire::Wire::to_proto(&spec::SourceMatchClaim {
            identifiers: spec::SourceMatchIdentifierMode::Exact,
            match_source: String::new(),
            bindings: Vec::new(),
            note: None,
        });
        source_match.identifiers = None;
        assert!(<spec::SourceMatchClaim as spec_wire::Wire>::from_proto(source_match).is_err());
    }

    #[test]
    fn protobuf_optionals_and_empty_selections_keep_their_domain_meaning() {
        let host = spec::HostBehavior::default();
        let wire = spec_wire::Wire::to_proto(&host);
        assert!(wire.loader_result_for_evaluating_module.is_none());
        assert!(wire.reentry_free_builtin_modules.is_empty());
        let restored = <spec::HostBehavior as spec_wire::Wire>::from_proto(wire).unwrap();
        assert!(restored.loader_result_for_evaluating_module.is_none());
        assert!(restored.reentry_free_operations.is_empty());

        let mut no_intrinsic_promise = spec::ProgramPremises::default();
        let absent = spec_wire::Wire::to_proto(&no_intrinsic_promise);
        assert!(absent.intrinsics_never_written.is_none());
        no_intrinsic_promise.intrinsics_never_written = Some(spec::IntrinsicsNeverWritten {
            families: spec::IntrinsicFamilies::Listed(BTreeSet::new()),
            note: None,
        });
        let explicitly_empty = spec_wire::Wire::to_proto(&no_intrinsic_promise);
        assert_ne!(absent, explicitly_empty);
        round_trip_spec_wire(&no_intrinsic_promise);
        let mut missing_families = spec_wire::Wire::to_proto(&spec::IntrinsicFamilies::All(
            spec::AllIntrinsicFamilies::All,
        ));
        missing_families.value = None;
        assert!(
            <spec::IntrinsicFamilies as spec_wire::Wire>::from_proto(missing_families).is_err()
        );

        for name in [None, Some(String::new()), Some("renamed".into())] {
            round_trip_spec_wire(&spec::SourceMatchBindingDetail {
                local: "value".into(),
                name,
            });
        }
        round_trip_spec_wire(&spec::SourceMatchClaim {
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
        round_trip_spec_wire(&contract);
        let mut wire = spec_wire::Wire::to_proto(&contract);
        wire.no_rebinding_through_spread = None;
        assert!(
            <spec::ImportedPrimitiveArrayLifetimeContract as spec_wire::Wire>::from_proto(wire)
                .is_err()
        );
    }

    #[test]
    fn saved_evidence_and_report_notes_round_trip() {
        let host = spec::HostBehavior {
            note: Some("host audit".into()),
            ..Default::default()
        };
        let host_wire = spec_wire::Wire::to_proto(&host);
        let host_restored: spec::HostBehavior = spec_wire::Wire::from_proto(host_wire).unwrap();
        assert_eq!(host_restored.note, host.note);

        let mut premises = spec::ProgramPremises {
            note: Some("program audit".into()),
            ..Default::default()
        };
        round_trip_spec_wire(&premises);
        let wire = spec_wire::Wire::to_proto(&premises);
        let restored: spec::ProgramPremises = spec_wire::Wire::from_proto(wire).unwrap();
        assert_eq!(restored, premises);
        premises.note = None;
        premises.intrinsics_never_written = Some(spec::IntrinsicsNeverWritten {
            families: spec::IntrinsicFamilies::All(spec::AllIntrinsicFamilies::All),
            note: Some("intrinsic audit".into()),
        });
        round_trip_spec_wire(&premises);
        let wire = spec_wire::Wire::to_proto(&premises);
        let restored: spec::ProgramPremises = spec_wire::Wire::from_proto(wire).unwrap();
        assert_eq!(restored, premises);

        let claim = spec::SourceMatchClaim {
            identifiers: spec::SourceMatchIdentifierMode::Exact,
            match_source: "const value = 1;".into(),
            bindings: Vec::new(),
            note: Some("selector audit".into()),
        };
        round_trip_spec_wire(&claim);
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
        dependencies.program_premises.push(i32::MAX);
        assert!(ProofDependencies::from_proto(dependencies).is_err());
        let mut duplicate = ProofDependencies::default().to_proto().unwrap();
        duplicate.program_premises = vec![1, 1];
        assert!(ProofDependencies::from_proto(duplicate).is_err());
        let mut evidence = crate::facts::GlobalAccessEvidence::default()
            .to_proto()
            .expect("global access evidence");
        evidence.dependencies = None;
        assert!(crate::facts::GlobalAccessEvidence::from_proto(evidence).is_err());
        assert_eq!(
            spec::SourceMatchIdentifierMode::try_from(0).unwrap(),
            spec::SourceMatchIdentifierMode::AlphaAll
        );
        assert!(spec::SourceMatchIdentifierMode::try_from(i32::MAX).is_err());
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
