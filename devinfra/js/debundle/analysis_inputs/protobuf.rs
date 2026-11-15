//! Protobuf persistence for callable capabilities and native census data.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use anyhow::{Result, ensure};

use super::{
    ArgumentInputEvidence, ArgumentInputRef, BundleGlobalNames, CallableSummary,
    CallbackFactoryEvidence, CallbackObjectArguments, DeferredRecordMember,
    DescriptorCopyAuthorRef, DescriptorCopyFactorySummary, ForeignCallSummary,
    ImportedPrimitiveArraySpreadInputEvidence, ImportedPrimitiveArraySpreadInputRef,
    ImportedPrimitiveArraySpreadInputUse, ImportedPrimitiveRead, InitializerCallEffectInputRef,
    NamespaceWrapperSummary, NativeIntegrity, NativeOperationInputEvidence,
    NativeOperationInputRef, NoAccessorInstallRef, ProofDependencies, ReturnedRecordSummary,
    SourceDeclaration, ValueInputEvidence, ValueInputRef,
};
use super::{
    EffectiveInitializerCallReplacement, ForeignInvocation, ForeignReturn, HostBehaviorKey,
    ImportedPrimitiveArrayOperandEvidence, NativeInputKind, NativeOperationSelectorProvenance,
    ProgramPremiseKey,
};
use crate::saved_proto::{
    ProtoConvert, decode_native_intrinsics, decode_set, encode_native_intrinsics, proto, required,
};

impl ProtoConvert<proto::CallbackFactoryEvidence> for CallbackFactoryEvidence {
    fn to_proto(&self) -> Result<proto::CallbackFactoryEvidence> {
        use proto::callback_factory_evidence::{AuthorAssertion, InferredImmutableWrapper, Value};
        Ok(proto::CallbackFactoryEvidence {
            value: Some(match self {
                Self::AuthorAssertion {
                    defining_module,
                    export,
                } => Value::AuthorAssertion(AuthorAssertion {
                    defining_module: Some(defining_module.clone()),
                    export: Some(export.clone()),
                }),
                Self::InferredImmutableWrapper {
                    defining_module,
                    export,
                    local,
                    source_location,
                } => Value::InferredImmutableWrapper(InferredImmutableWrapper {
                    defining_module: Some(defining_module.clone()),
                    export: Some(export.clone()),
                    local: Some(local.clone()),
                    source_location: source_location
                        .as_ref()
                        .map(ProtoConvert::to_proto)
                        .transpose()?,
                }),
            }),
        })
    }

    fn from_proto(value: proto::CallbackFactoryEvidence) -> Result<Self> {
        use proto::callback_factory_evidence::Value;
        Ok(
            match required(value.value, "CallbackFactoryEvidence.value")? {
                Value::AuthorAssertion(value) => Self::AuthorAssertion {
                    defining_module: required(
                        value.defining_module,
                        "CallbackFactoryEvidence.author_assertion.defining_module",
                    )?,
                    export: required(
                        value.export,
                        "CallbackFactoryEvidence.author_assertion.export",
                    )?,
                },
                Value::InferredImmutableWrapper(value) => Self::InferredImmutableWrapper {
                    defining_module: required(
                        value.defining_module,
                        "CallbackFactoryEvidence.inferred_immutable_wrapper.defining_module",
                    )?,
                    export: required(
                        value.export,
                        "CallbackFactoryEvidence.inferred_immutable_wrapper.export",
                    )?,
                    local: required(
                        value.local,
                        "CallbackFactoryEvidence.inferred_immutable_wrapper.local",
                    )?,
                    source_location: value
                        .source_location
                        .map(ProtoConvert::from_proto)
                        .transpose()?,
                },
            },
        )
    }
}

impl ProtoConvert<proto::ForeignReturn> for ForeignReturn {
    fn to_proto(&self) -> Result<proto::ForeignReturn> {
        use proto::foreign_return::Value;
        Ok(proto::ForeignReturn {
            value: Some(match self {
                Self::Argument { index } => Value::ArgumentIndex(index.to_proto()?),
                Self::FreshAllocation => Value::FreshAllocation(Default::default()),
                Self::Primitive => Value::Primitive(Default::default()),
            }),
        })
    }

    fn from_proto(value: proto::ForeignReturn) -> Result<Self> {
        use proto::foreign_return::Value;
        Ok(match required(value.value, "ForeignReturn.value")? {
            Value::ArgumentIndex(index) => Self::Argument {
                index: usize::from_proto(index)?,
            },
            Value::FreshAllocation(_) => Self::FreshAllocation,
            Value::Primitive(_) => Self::Primitive,
        })
    }
}

impl ProtoConvert<proto::HostBehaviorKey> for HostBehaviorKey {
    fn to_proto(&self) -> Result<proto::HostBehaviorKey> {
        use proto::host_behavior_key::Value;
        Ok(proto::HostBehaviorKey {
            value: Some(match self {
                Self::LoaderResultForEvaluatingModule => {
                    Value::LoaderResultForEvaluatingModule(Default::default())
                }
                Self::SynchronousLoadRunsAppCallbacks => {
                    Value::SynchronousLoadRunsAppCallbacks(Default::default())
                }
                Self::SynchronousLoadDrainsMicrotasks => {
                    Value::SynchronousLoadDrainsMicrotasks(Default::default())
                }
                Self::ReentryFreeBuiltinModule(module) => {
                    Value::ReentryFreeBuiltinModule(*module as i32)
                }
                Self::ReentryFreeBunModule(module) => Value::ReentryFreeBunModule(*module as i32),
                Self::ReentryFreeEmbeddedLoader(loader) => {
                    Value::ReentryFreeEmbeddedLoader(*loader as i32)
                }
                Self::ReentryFreeOperation(operation) => {
                    Value::ReentryFreeOperation(*operation as i32)
                }
                Self::PredefinedGlobals => Value::PredefinedGlobals(Default::default()),
            }),
        })
    }

    fn from_proto(value: proto::HostBehaviorKey) -> Result<Self> {
        use proto::host_behavior_key::Value;
        Ok(match required(value.value, "HostBehaviorKey.value")? {
            Value::LoaderResultForEvaluatingModule(_) => Self::LoaderResultForEvaluatingModule,
            Value::SynchronousLoadRunsAppCallbacks(_) => Self::SynchronousLoadRunsAppCallbacks,
            Value::SynchronousLoadDrainsMicrotasks(_) => Self::SynchronousLoadDrainsMicrotasks,
            Value::ReentryFreeBuiltinModule(value) => {
                Self::ReentryFreeBuiltinModule(spec::NodeBuiltinModule::try_from(value)?)
            }
            Value::ReentryFreeBunModule(value) => {
                Self::ReentryFreeBunModule(spec::BunModule::try_from(value)?)
            }
            Value::ReentryFreeEmbeddedLoader(value) => {
                Self::ReentryFreeEmbeddedLoader(spec::EmbeddedLoader::try_from(value)?)
            }
            Value::ReentryFreeOperation(value) => {
                Self::ReentryFreeOperation(spec::ReentryFreeOperation::try_from(value)?)
            }
            Value::PredefinedGlobals(_) => Self::PredefinedGlobals,
        })
    }
}

impl ProtoConvert<proto::NativeInputKind> for NativeInputKind {
    fn to_proto(&self) -> Result<proto::NativeInputKind> {
        use proto::native_input_kind::Value;
        Ok(proto::NativeInputKind {
            value: Some(match self {
                Self::Operation(operation) => Value::Operation(*operation as i32),
                Self::CallIdentity(identity) => Value::CallIdentity(*identity as i32),
            }),
        })
    }

    fn from_proto(value: proto::NativeInputKind) -> Result<Self> {
        use proto::native_input_kind::Value;
        Ok(match required(value.value, "NativeInputKind.value")? {
            Value::Operation(value) => Self::Operation(spec::NativeOperationKind::try_from(value)?),
            Value::CallIdentity(value) => {
                Self::CallIdentity(spec::NativeCallIdentityKind::try_from(value)?)
            }
        })
    }
}

impl ProtoConvert<proto::NativeOperationSelectorProvenance> for NativeOperationSelectorProvenance {
    fn to_proto(&self) -> Result<proto::NativeOperationSelectorProvenance> {
        use proto::native_operation_selector_provenance::Value;
        Ok(proto::NativeOperationSelectorProvenance {
            value: Some(match self {
                Self::SourceMatch(claim) => Value::SourceMatch(spec_wire::Wire::to_proto(claim)),
                Self::Declaration(declaration) => Value::Declaration(declaration.to_proto()?),
                Self::ResolvedMember(member) => {
                    Value::ResolvedMember(spec_wire::Wire::to_proto(member))
                }
            }),
        })
    }

    fn from_proto(value: proto::NativeOperationSelectorProvenance) -> Result<Self> {
        use proto::native_operation_selector_provenance::Value;
        Ok(
            match required(value.value, "NativeOperationSelectorProvenance.value")? {
                Value::SourceMatch(value) => Self::SourceMatch(spec_wire::Wire::from_proto(value)?),
                Value::Declaration(value) => {
                    Self::Declaration(SourceDeclaration::from_proto(value)?)
                }
                Value::ResolvedMember(value) => {
                    Self::ResolvedMember(spec_wire::Wire::from_proto(value)?)
                }
            },
        )
    }
}

impl ProtoConvert<proto::EffectiveInitializerCallReplacement>
    for EffectiveInitializerCallReplacement
{
    fn to_proto(&self) -> Result<proto::EffectiveInitializerCallReplacement> {
        Ok(proto::EffectiveInitializerCallReplacement {
            package: Some(self.package.clone()),
            bundle_path: Some(self.bundle_path.to_proto()?),
            bundle_export: Some(self.bundle_export.clone()),
            symbol_kind: Some(self.symbol_kind as i32),
            upstream_export: self.upstream_export.clone(),
            bundle_sha256: Some(self.bundle_sha256.clone()),
        })
    }

    fn from_proto(value: proto::EffectiveInitializerCallReplacement) -> Result<Self> {
        Ok(Self {
            package: required(value.package, "EffectiveInitializerCallReplacement.package")?,
            bundle_path: ProtoConvert::from_proto(required(
                value.bundle_path,
                "EffectiveInitializerCallReplacement.bundle_path",
            )?)?,
            bundle_export: required(
                value.bundle_export,
                "EffectiveInitializerCallReplacement.bundle_export",
            )?,
            symbol_kind: spec::PartialSwapKind::try_from(required(
                value.symbol_kind,
                "EffectiveInitializerCallReplacement.symbol_kind",
            )?)?,
            upstream_export: value.upstream_export,
            bundle_sha256: required(
                value.bundle_sha256,
                "EffectiveInitializerCallReplacement.bundle_sha256",
            )?,
        })
    }
}

impl ProtoConvert<proto::ImportedPrimitiveArrayOperandEvidence>
    for ImportedPrimitiveArrayOperandEvidence
{
    fn to_proto(&self) -> Result<proto::ImportedPrimitiveArrayOperandEvidence> {
        Ok(proto::ImportedPrimitiveArrayOperandEvidence {
            selector_local: Some(self.selector_local.clone()),
            consumer_binding: Some(self.consumer_binding.clone()),
            provider_module: Some(self.provider_module.clone()),
            provider_export: Some(self.provider_export.clone()),
            provider_local: Some(self.provider_local.clone()),
            source_specifier: Some(self.source_specifier.clone()),
            provider_source_match: Some(spec_wire::Wire::to_proto(&self.provider_source_match)),
            provider_source_sha256: Some(self.provider_source_sha256.clone()),
            primitive_slots: self
                .primitive_slots
                .iter()
                .map(ProtoConvert::to_proto)
                .collect::<Result<_>>()?,
            provider_source_location: self
                .provider_source_location
                .as_ref()
                .map(ProtoConvert::to_proto)
                .transpose()?,
        })
    }

    fn from_proto(value: proto::ImportedPrimitiveArrayOperandEvidence) -> Result<Self> {
        Ok(Self {
            selector_local: required(
                value.selector_local,
                "ImportedPrimitiveArrayOperandEvidence.selector_local",
            )?,
            consumer_binding: required(
                value.consumer_binding,
                "ImportedPrimitiveArrayOperandEvidence.consumer_binding",
            )?,
            provider_module: required(
                value.provider_module,
                "ImportedPrimitiveArrayOperandEvidence.provider_module",
            )?,
            provider_export: required(
                value.provider_export,
                "ImportedPrimitiveArrayOperandEvidence.provider_export",
            )?,
            provider_local: required(
                value.provider_local,
                "ImportedPrimitiveArrayOperandEvidence.provider_local",
            )?,
            source_specifier: required(
                value.source_specifier,
                "ImportedPrimitiveArrayOperandEvidence.source_specifier",
            )?,
            provider_source_match: spec_wire::Wire::from_proto(required(
                value.provider_source_match,
                "ImportedPrimitiveArrayOperandEvidence.provider_source_match",
            )?)?,
            provider_source_sha256: required(
                value.provider_source_sha256,
                "ImportedPrimitiveArrayOperandEvidence.provider_source_sha256",
            )?,
            primitive_slots: value
                .primitive_slots
                .into_iter()
                .map(ProtoConvert::from_proto)
                .collect::<Result<_>>()?,
            provider_source_location: value
                .provider_source_location
                .map(ProtoConvert::from_proto)
                .transpose()?,
        })
    }
}

impl ProtoConvert<proto::ForeignCallSummary> for ForeignCallSummary {
    fn to_proto(&self) -> Result<proto::ForeignCallSummary> {
        Ok(proto::ForeignCallSummary {
            defining_module: Some(self.defining_module.clone()),
            export: Some(self.export.clone()),
            local: Some(self.local.clone()),
            source_location: self
                .source_location
                .as_ref()
                .map(ProtoConvert::to_proto)
                .transpose()?,
            invocation: Some(self.invocation as i32),
            returns: self
                .returns
                .iter()
                .map(ProtoConvert::to_proto)
                .collect::<Result<_>>()?,
            retained_arguments: self
                .retained_arguments
                .iter()
                .map(ProtoConvert::to_proto)
                .collect::<Result<_>>()?,
            retained_provider_captures: self.retained_provider_captures.iter().cloned().collect(),
            provider_cell_reads: self.provider_cell_reads.iter().cloned().collect(),
            native_constructions: encode_native_intrinsics(&self.native_constructions)?,
            replacement_package: self.replacement_package.clone(),
            dependencies: Some(self.dependencies.to_proto()?),
        })
    }

    fn from_proto(value: proto::ForeignCallSummary) -> Result<Self> {
        let invocation = ForeignInvocation::try_from(required(
            value.invocation,
            "ForeignCallSummary.invocation",
        )?)?;
        Ok(Self {
            defining_module: required(value.defining_module, "ForeignCallSummary.defining_module")?,
            export: required(value.export, "ForeignCallSummary.export")?,
            local: required(value.local, "ForeignCallSummary.local")?,
            source_location: value
                .source_location
                .map(ProtoConvert::from_proto)
                .transpose()?,
            invocation,
            returns: decode_set(value.returns)?,
            retained_arguments: decode_set(value.retained_arguments)?,
            retained_provider_captures: decode_set(value.retained_provider_captures)?,
            provider_cell_reads: decode_set(value.provider_cell_reads)?,
            native_constructions: decode_native_intrinsics(value.native_constructions)?,
            replacement_package: value.replacement_package,
            dependencies: ProtoConvert::from_proto(required(
                value.dependencies,
                "ForeignCallSummary.dependencies",
            )?)?,
        })
    }
}

impl ProtoConvert<proto::ArgumentInputEvidence> for ArgumentInputEvidence {
    fn to_proto(&self) -> Result<proto::ArgumentInputEvidence> {
        Ok(proto::ArgumentInputEvidence {
            reference: Some(self.reference.to_proto()?),
            spec_location: Some(self.spec_location.clone()),
            source_match: Some(spec_wire::Wire::to_proto(&self.source_match)),
            source_path: Some(self.source_path.clone()),
            audited_source_sha256: Some(self.audited_source_sha256.clone()),
            selected_binding: Some(self.selected_binding.clone()),
            parameter_index: Some(self.parameter_index.to_proto()?),
            exact_arity: Some(self.exact_arity.to_proto()?),
            declaration_location: self
                .declaration_location
                .as_ref()
                .map(ProtoConvert::to_proto)
                .transpose()?,
        })
    }

    fn from_proto(value: proto::ArgumentInputEvidence) -> Result<Self> {
        Ok(Self {
            reference: ProtoConvert::from_proto(required(
                value.reference,
                "ArgumentInputEvidence.reference",
            )?)?,
            spec_location: required(value.spec_location, "ArgumentInputEvidence.spec_location")?,
            source_match: spec_wire::Wire::from_proto(required(
                value.source_match,
                "ArgumentInputEvidence.source_match",
            )?)?,
            source_path: required(value.source_path, "ArgumentInputEvidence.source_path")?,
            audited_source_sha256: required(
                value.audited_source_sha256,
                "ArgumentInputEvidence.audited_source_sha256",
            )?,
            selected_binding: required(
                value.selected_binding,
                "ArgumentInputEvidence.selected_binding",
            )?,
            parameter_index: ProtoConvert::from_proto(required(
                value.parameter_index,
                "ArgumentInputEvidence.parameter_index",
            )?)?,
            exact_arity: ProtoConvert::from_proto(required(
                value.exact_arity,
                "ArgumentInputEvidence.exact_arity",
            )?)?,
            declaration_location: value
                .declaration_location
                .map(ProtoConvert::from_proto)
                .transpose()?,
        })
    }
}

impl ProtoConvert<proto::NamespaceWrapperSummary> for NamespaceWrapperSummary {
    fn to_proto(&self) -> Result<proto::NamespaceWrapperSummary> {
        Ok(proto::NamespaceWrapperSummary {
            defining_module: Some(self.defining_module.clone()),
            export: Some(self.export.clone()),
            local: Some(self.local.clone()),
            source_location: self
                .source_location
                .as_ref()
                .map(ProtoConvert::to_proto)
                .transpose()?,
            inference: Some(self.inference.to_proto()?),
            dependencies: Some(self.dependencies.to_proto()?),
        })
    }

    fn from_proto(value: proto::NamespaceWrapperSummary) -> Result<Self> {
        Ok(Self {
            defining_module: required(
                value.defining_module,
                "NamespaceWrapperSummary.defining_module",
            )?,
            export: required(value.export, "NamespaceWrapperSummary.export")?,
            local: required(value.local, "NamespaceWrapperSummary.local")?,
            source_location: value
                .source_location
                .map(ProtoConvert::from_proto)
                .transpose()?,
            inference: ProtoConvert::from_proto(required(
                value.inference,
                "NamespaceWrapperSummary.inference",
            )?)?,
            dependencies: ProtoConvert::from_proto(required(
                value.dependencies,
                "NamespaceWrapperSummary.dependencies",
            )?)?,
        })
    }
}

impl ProtoConvert<proto::ReturnedRecordSummary> for ReturnedRecordSummary {
    fn to_proto(&self) -> Result<proto::ReturnedRecordSummary> {
        Ok(proto::ReturnedRecordSummary {
            defining_module: Some(self.defining_module.clone()),
            export: Some(self.export.clone()),
            local: Some(self.local.clone()),
            source_location: self
                .source_location
                .as_ref()
                .map(ProtoConvert::to_proto)
                .transpose()?,
            members: self
                .members
                .iter()
                .map(ProtoConvert::to_proto)
                .collect::<Result<_>>()?,
            inference: Some(self.inference.to_proto()?),
            replacement_package: self.replacement_package.clone(),
        })
    }

    fn from_proto(value: proto::ReturnedRecordSummary) -> Result<Self> {
        Ok(Self {
            defining_module: required(
                value.defining_module,
                "ReturnedRecordSummary.defining_module",
            )?,
            export: required(value.export, "ReturnedRecordSummary.export")?,
            local: required(value.local, "ReturnedRecordSummary.local")?,
            source_location: value
                .source_location
                .map(ProtoConvert::from_proto)
                .transpose()?,
            members: value
                .members
                .into_iter()
                .map(ProtoConvert::from_proto)
                .collect::<Result<_>>()?,
            inference: ProtoConvert::from_proto(required(
                value.inference,
                "ReturnedRecordSummary.inference",
            )?)?,
            replacement_package: value.replacement_package,
        })
    }
}

impl ProtoConvert<proto::SourceDeclaration> for SourceDeclaration {
    fn to_proto(&self) -> Result<proto::SourceDeclaration> {
        Ok(proto::SourceDeclaration {
            binding: Some(self.binding.clone()),
            body_index: Some(self.body_index.to_proto()?),
        })
    }

    fn from_proto(value: proto::SourceDeclaration) -> Result<Self> {
        Ok(Self {
            binding: required(value.binding, "SourceDeclaration.binding")?,
            body_index: usize::from_proto(required(
                value.body_index,
                "SourceDeclaration.body_index",
            )?)?,
        })
    }
}

impl ProtoConvert<proto::ImportedPrimitiveRead> for ImportedPrimitiveRead {
    fn to_proto(&self) -> Result<proto::ImportedPrimitiveRead> {
        Ok(proto::ImportedPrimitiveRead {
            kinds: Some(self.kinds.to_proto()?),
            provider_module: Some(self.provider_module.clone()),
            dependency_closure: self.dependency_closure.iter().cloned().collect(),
            source_cells: self.source_cells.iter().cloned().collect(),
        })
    }

    fn from_proto(value: proto::ImportedPrimitiveRead) -> Result<Self> {
        Ok(Self {
            kinds: ProtoConvert::from_proto(required(value.kinds, "ImportedPrimitiveRead.kinds")?)?,
            provider_module: required(
                value.provider_module,
                "ImportedPrimitiveRead.provider_module",
            )?,
            dependency_closure: decode_set(value.dependency_closure)?,
            source_cells: {
                let count = value.source_cells.len();
                let cells: BTreeSet<_> = value.source_cells.into_iter().collect();
                ensure!(cells.len() == count, "duplicate saved set member");
                cells
            },
        })
    }
}

impl ProtoConvert<proto::ImportedPrimitiveArraySpreadInputUse>
    for ImportedPrimitiveArraySpreadInputUse
{
    fn to_proto(&self) -> Result<proto::ImportedPrimitiveArraySpreadInputUse> {
        Ok(proto::ImportedPrimitiveArraySpreadInputUse {
            evidence: Some(self.evidence.to_proto()?),
            operand_index: Some(self.operand_index.to_proto()?),
        })
    }

    fn from_proto(value: proto::ImportedPrimitiveArraySpreadInputUse) -> Result<Self> {
        Ok(Self {
            evidence: ProtoConvert::from_proto(required(
                value.evidence,
                "ImportedPrimitiveArraySpreadInputUse.evidence",
            )?)?,
            operand_index: usize::from_proto(required(
                value.operand_index,
                "ImportedPrimitiveArraySpreadInputUse.operand_index",
            )?)?,
        })
    }
}

impl ProtoConvert<proto::DeferredRecordMember> for DeferredRecordMember {
    fn to_proto(&self) -> Result<proto::DeferredRecordMember> {
        Ok(proto::DeferredRecordMember {
            name: Some(self.name.clone()),
            source_location: self
                .source_location
                .as_ref()
                .map(ProtoConvert::to_proto)
                .transpose()?,
            captures: self.captures.iter().cloned().collect(),
        })
    }

    fn from_proto(value: proto::DeferredRecordMember) -> Result<Self> {
        Ok(Self {
            name: required(value.name, "DeferredRecordMember.name")?,
            source_location: value
                .source_location
                .map(ProtoConvert::from_proto)
                .transpose()?,
            captures: decode_set(value.captures)?,
        })
    }
}

impl ProtoConvert<proto::CallbackObjectArguments> for CallbackObjectArguments {
    fn to_proto(&self) -> Result<proto::CallbackObjectArguments> {
        Ok(proto::CallbackObjectArguments {
            wrapper: Some(self.wrapper.to_proto()?),
            object_arguments: self
                .object_arguments
                .iter()
                .map(ProtoConvert::to_proto)
                .collect::<Result<_>>()?,
        })
    }

    fn from_proto(value: proto::CallbackObjectArguments) -> Result<Self> {
        Ok(Self {
            wrapper: ProtoConvert::from_proto(required(
                value.wrapper,
                "CallbackObjectArguments.wrapper",
            )?)?,
            object_arguments: decode_set(value.object_arguments)?,
        })
    }
}

impl ProtoConvert<proto::DescriptorCopyFactorySummary> for DescriptorCopyFactorySummary {
    fn to_proto(&self) -> Result<proto::DescriptorCopyFactorySummary> {
        Ok(proto::DescriptorCopyFactorySummary {
            defining_module: Some(self.defining_module.clone()),
            export: Some(self.export.clone()),
            author_location: Some(self.author_location.clone()),
            signatures: self
                .signatures
                .iter()
                .map(ProtoConvert::to_proto)
                .collect::<Result<_>>()?,
        })
    }

    fn from_proto(value: proto::DescriptorCopyFactorySummary) -> Result<Self> {
        Ok(Self {
            defining_module: required(
                value.defining_module,
                "DescriptorCopyFactorySummary.defining_module",
            )?,
            export: required(value.export, "DescriptorCopyFactorySummary.export")?,
            author_location: required(
                value.author_location,
                "DescriptorCopyFactorySummary.author_location",
            )?,
            signatures: value
                .signatures
                .into_iter()
                .map(ProtoConvert::from_proto)
                .collect::<Result<_>>()?,
        })
    }
}

impl ProtoConvert<proto::ImportedPrimitiveArraySpreadInputEvidence>
    for ImportedPrimitiveArraySpreadInputEvidence
{
    fn to_proto(&self) -> Result<proto::ImportedPrimitiveArraySpreadInputEvidence> {
        ensure!(
            self.note.is_none(),
            "report-only imported array note must not enter saved analysis"
        );
        Ok(proto::ImportedPrimitiveArraySpreadInputEvidence {
            reference: Some(self.reference.to_proto()?),
            spec_location: Some(self.spec_location.clone()),
            consumer_source_match: Some(spec_wire::Wire::to_proto(&self.consumer_source_match)),
            consumer_source_sha256: Some(self.consumer_source_sha256.clone()),
            selected_consumer_binding: Some(self.selected_consumer_binding.clone()),
            consumer_source_location: self
                .consumer_source_location
                .as_ref()
                .map(ProtoConvert::to_proto)
                .transpose()?,
            operands: self
                .operands
                .iter()
                .map(ProtoConvert::to_proto)
                .collect::<Result<_>>()?,
            lifetime: Some(spec_wire::Wire::to_proto(&self.lifetime)),
        })
    }

    fn from_proto(value: proto::ImportedPrimitiveArraySpreadInputEvidence) -> Result<Self> {
        Ok(Self {
            reference: ProtoConvert::from_proto(required(
                value.reference,
                "ImportedPrimitiveArraySpreadInputEvidence.reference",
            )?)?,
            spec_location: required(
                value.spec_location,
                "ImportedPrimitiveArraySpreadInputEvidence.spec_location",
            )?,
            consumer_source_match: spec_wire::Wire::from_proto(required(
                value.consumer_source_match,
                "ImportedPrimitiveArraySpreadInputEvidence.consumer_source_match",
            )?)?,
            consumer_source_sha256: required(
                value.consumer_source_sha256,
                "ImportedPrimitiveArraySpreadInputEvidence.consumer_source_sha256",
            )?,
            selected_consumer_binding: required(
                value.selected_consumer_binding,
                "ImportedPrimitiveArraySpreadInputEvidence.selected_consumer_binding",
            )?,
            consumer_source_location: value
                .consumer_source_location
                .map(ProtoConvert::from_proto)
                .transpose()?,
            operands: value
                .operands
                .into_iter()
                .map(ProtoConvert::from_proto)
                .collect::<Result<_>>()?,
            lifetime: spec_wire::Wire::from_proto(required(
                value.lifetime,
                "ImportedPrimitiveArraySpreadInputEvidence.lifetime",
            )?)?,
            note: None,
        })
    }
}

impl ProtoConvert<proto::ValueInputEvidence> for ValueInputEvidence {
    fn to_proto(&self) -> Result<proto::ValueInputEvidence> {
        ensure!(
            self.parser_input_file.is_none() && self.note.is_none(),
            "report-only value input fields must not enter saved analysis"
        );
        Ok(proto::ValueInputEvidence {
            reference: Some(self.reference.to_proto()?),
            spec_location: Some(self.spec_location.clone()),
            source_match: Some(spec_wire::Wire::to_proto(&self.source_match)),
            value_kind: Some(self.value_kind as i32),
            source_path: Some(self.source_path.clone()),
            audited_source_sha256: Some(self.audited_source_sha256.clone()),
            selected_binding: Some(self.selected_binding.clone()),
            declaration_location: self
                .declaration_location
                .as_ref()
                .map(ProtoConvert::to_proto)
                .transpose()?,
            initializer_location: self
                .initializer_location
                .as_ref()
                .map(ProtoConvert::to_proto)
                .transpose()?,
            expected_export: self.expected_export.clone(),
        })
    }

    fn from_proto(value: proto::ValueInputEvidence) -> Result<Self> {
        Ok(Self {
            reference: ProtoConvert::from_proto(required(
                value.reference,
                "ValueInputEvidence.reference",
            )?)?,
            spec_location: required(value.spec_location, "ValueInputEvidence.spec_location")?,
            source_match: spec_wire::Wire::from_proto(required(
                value.source_match,
                "ValueInputEvidence.source_match",
            )?)?,
            value_kind: spec::CapturedValueKind::try_from(required(
                value.value_kind,
                "ValueInputEvidence.value_kind",
            )?)?,
            source_path: required(value.source_path, "ValueInputEvidence.source_path")?,
            parser_input_file: None,
            audited_source_sha256: required(
                value.audited_source_sha256,
                "ValueInputEvidence.audited_source_sha256",
            )?,
            selected_binding: required(
                value.selected_binding,
                "ValueInputEvidence.selected_binding",
            )?,
            declaration_location: value
                .declaration_location
                .map(ProtoConvert::from_proto)
                .transpose()?,
            initializer_location: value
                .initializer_location
                .map(ProtoConvert::from_proto)
                .transpose()?,
            expected_export: value.expected_export,
            note: None,
        })
    }
}

impl ProtoConvert<proto::NativeOperationInputEvidence> for NativeOperationInputEvidence {
    fn to_proto(&self) -> Result<proto::NativeOperationInputEvidence> {
        ensure!(
            self.parser_input_file.is_none() && self.note.is_none(),
            "report-only native operation input fields must not enter saved analysis"
        );
        Ok(proto::NativeOperationInputEvidence {
            reference: Some(self.reference.to_proto()?),
            spec_location: Some(self.spec_location.clone()),
            selector: Some(self.selector.to_proto()?),
            operation: Some(self.operation.to_proto()?),
            source_path: Some(self.source_path.clone()),
            audited_source_sha256: Some(self.audited_source_sha256.clone()),
            selected_binding: Some(self.selected_binding.clone()),
            declaration_location: self
                .declaration_location
                .as_ref()
                .map(ProtoConvert::to_proto)
                .transpose()?,
            initializer_location: self
                .initializer_location
                .as_ref()
                .map(ProtoConvert::to_proto)
                .transpose()?,
        })
    }

    fn from_proto(value: proto::NativeOperationInputEvidence) -> Result<Self> {
        Ok(Self {
            reference: ProtoConvert::from_proto(required(
                value.reference,
                "NativeOperationInputEvidence.reference",
            )?)?,
            spec_location: required(
                value.spec_location,
                "NativeOperationInputEvidence.spec_location",
            )?,
            selector: ProtoConvert::from_proto(required(
                value.selector,
                "NativeOperationInputEvidence.selector",
            )?)?,
            operation: ProtoConvert::from_proto(required(
                value.operation,
                "NativeOperationInputEvidence.operation",
            )?)?,
            source_path: required(
                value.source_path,
                "NativeOperationInputEvidence.source_path",
            )?,
            parser_input_file: None,
            audited_source_sha256: required(
                value.audited_source_sha256,
                "NativeOperationInputEvidence.audited_source_sha256",
            )?,
            selected_binding: required(
                value.selected_binding,
                "NativeOperationInputEvidence.selected_binding",
            )?,
            declaration_location: value
                .declaration_location
                .map(ProtoConvert::from_proto)
                .transpose()?,
            initializer_location: value
                .initializer_location
                .map(ProtoConvert::from_proto)
                .transpose()?,
            note: None,
        })
    }
}

impl ProtoConvert<proto::ValueInputRef> for ValueInputRef {
    fn to_proto(&self) -> Result<proto::ValueInputRef> {
        Ok(proto::ValueInputRef {
            defining_chunk: Some(self.defining_chunk.clone()),
            input_index: Some(self.input_index.to_proto()?),
        })
    }

    fn from_proto(value: proto::ValueInputRef) -> Result<Self> {
        Ok(Self {
            defining_chunk: required(value.defining_chunk, "ValueInputRef.defining_chunk")?,
            input_index: usize::from_proto(required(
                value.input_index,
                "ValueInputRef.input_index",
            )?)?,
        })
    }
}

impl ProtoConvert<proto::ArgumentInputRef> for ArgumentInputRef {
    fn to_proto(&self) -> Result<proto::ArgumentInputRef> {
        Ok(proto::ArgumentInputRef {
            defining_chunk: Some(self.defining_chunk.clone()),
            input_index: Some(self.input_index.to_proto()?),
        })
    }

    fn from_proto(value: proto::ArgumentInputRef) -> Result<Self> {
        Ok(Self {
            defining_chunk: required(value.defining_chunk, "ArgumentInputRef.defining_chunk")?,
            input_index: usize::from_proto(required(
                value.input_index,
                "ArgumentInputRef.input_index",
            )?)?,
        })
    }
}

impl ProtoConvert<proto::InitializerCallEffectInputRef> for InitializerCallEffectInputRef {
    fn to_proto(&self) -> Result<proto::InitializerCallEffectInputRef> {
        Ok(proto::InitializerCallEffectInputRef {
            defining_chunk: Some(self.defining_chunk.clone()),
            input_index: Some(self.input_index.to_proto()?),
        })
    }

    fn from_proto(value: proto::InitializerCallEffectInputRef) -> Result<Self> {
        Ok(Self {
            defining_chunk: required(
                value.defining_chunk,
                "InitializerCallEffectInputRef.defining_chunk",
            )?,
            input_index: usize::from_proto(required(
                value.input_index,
                "InitializerCallEffectInputRef.input_index",
            )?)?,
        })
    }
}

impl ProtoConvert<proto::ImportedPrimitiveArraySpreadInputRef>
    for ImportedPrimitiveArraySpreadInputRef
{
    fn to_proto(&self) -> Result<proto::ImportedPrimitiveArraySpreadInputRef> {
        Ok(proto::ImportedPrimitiveArraySpreadInputRef {
            defining_chunk: Some(self.defining_chunk.clone()),
            input_index: Some(self.input_index.to_proto()?),
        })
    }

    fn from_proto(value: proto::ImportedPrimitiveArraySpreadInputRef) -> Result<Self> {
        Ok(Self {
            defining_chunk: required(
                value.defining_chunk,
                "ImportedPrimitiveArraySpreadInputRef.defining_chunk",
            )?,
            input_index: usize::from_proto(required(
                value.input_index,
                "ImportedPrimitiveArraySpreadInputRef.input_index",
            )?)?,
        })
    }
}

impl ProtoConvert<proto::NativeOperationInputRef> for NativeOperationInputRef {
    fn to_proto(&self) -> Result<proto::NativeOperationInputRef> {
        Ok(proto::NativeOperationInputRef {
            defining_chunk: Some(self.defining_chunk.clone()),
            input_index: Some(self.input_index.to_proto()?),
        })
    }

    fn from_proto(value: proto::NativeOperationInputRef) -> Result<Self> {
        Ok(Self {
            defining_chunk: required(
                value.defining_chunk,
                "NativeOperationInputRef.defining_chunk",
            )?,
            input_index: usize::from_proto(required(
                value.input_index,
                "NativeOperationInputRef.input_index",
            )?)?,
        })
    }
}

impl ProtoConvert<proto::NoAccessorInstallRef> for NoAccessorInstallRef {
    fn to_proto(&self) -> Result<proto::NoAccessorInstallRef> {
        Ok(proto::NoAccessorInstallRef {
            defining_chunk: Some(self.defining_chunk.clone()),
            input_index: Some(self.input_index.to_proto()?),
        })
    }

    fn from_proto(value: proto::NoAccessorInstallRef) -> Result<Self> {
        Ok(Self {
            defining_chunk: required(value.defining_chunk, "NoAccessorInstallRef.defining_chunk")?,
            input_index: usize::from_proto(required(
                value.input_index,
                "NoAccessorInstallRef.input_index",
            )?)?,
        })
    }
}

impl ProtoConvert<proto::DescriptorCopyAuthorRef> for DescriptorCopyAuthorRef {
    fn to_proto(&self) -> Result<proto::DescriptorCopyAuthorRef> {
        Ok(proto::DescriptorCopyAuthorRef {
            defining_module: Some(self.defining_module.clone()),
            export: Some(self.export.clone()),
            spec_location: Some(self.spec_location.clone()),
        })
    }

    fn from_proto(value: proto::DescriptorCopyAuthorRef) -> Result<Self> {
        Ok(Self {
            defining_module: required(
                value.defining_module,
                "DescriptorCopyAuthorRef.defining_module",
            )?,
            export: required(value.export, "DescriptorCopyAuthorRef.export")?,
            spec_location: required(value.spec_location, "DescriptorCopyAuthorRef.spec_location")?,
        })
    }
}

impl ProtoConvert<proto::ProofDependencies> for ProofDependencies {
    fn to_proto(&self) -> Result<proto::ProofDependencies> {
        Ok(proto::ProofDependencies {
            value_inputs: self
                .value_inputs
                .iter()
                .map(ProtoConvert::to_proto)
                .collect::<Result<_>>()?,
            native_operation_inputs: self
                .native_operation_inputs
                .iter()
                .map(ProtoConvert::to_proto)
                .collect::<Result<_>>()?,
            initializer_call_effect_inputs: self
                .initializer_call_effect_inputs
                .iter()
                .map(ProtoConvert::to_proto)
                .collect::<Result<_>>()?,
            imported_primitive_array_spread_inputs: self
                .imported_primitive_array_spread_inputs
                .iter()
                .map(ProtoConvert::to_proto)
                .collect::<Result<_>>()?,
            descriptor_copy_authors: self
                .descriptor_copy_authors
                .iter()
                .map(ProtoConvert::to_proto)
                .collect::<Result<_>>()?,
            argument_inputs: self
                .argument_inputs
                .iter()
                .map(ProtoConvert::to_proto)
                .collect::<Result<_>>()?,
            host_behavior: self
                .host_behavior
                .iter()
                .map(ProtoConvert::to_proto)
                .collect::<Result<_>>()?,
            program_premises: self
                .program_premises
                .iter()
                .map(|value| *value as i32)
                .collect(),
            no_accessor_install_callables: self
                .no_accessor_install_callables
                .iter()
                .map(ProtoConvert::to_proto)
                .collect::<Result<_>>()?,
        })
    }

    fn from_proto(value: proto::ProofDependencies) -> Result<Self> {
        let mut program_premises = BTreeSet::new();
        for key in value.program_premises {
            let key = ProgramPremiseKey::try_from(key)?;
            ensure!(program_premises.insert(key), "duplicate program premise");
        }
        Ok(Self {
            value_inputs: decode_set(value.value_inputs)?,
            native_operation_inputs: decode_set(value.native_operation_inputs)?,
            initializer_call_effect_inputs: decode_set(value.initializer_call_effect_inputs)?,
            imported_primitive_array_spread_inputs: decode_set(
                value.imported_primitive_array_spread_inputs,
            )?,
            descriptor_copy_authors: decode_set(value.descriptor_copy_authors)?,
            argument_inputs: decode_set(value.argument_inputs)?,
            host_behavior: decode_set(value.host_behavior)?,
            program_premises,
            no_accessor_install_callables: decode_set(value.no_accessor_install_callables)?,
        })
    }
}

impl ProtoConvert<proto::BundleGlobalNames> for BundleGlobalNames {
    fn to_proto(&self) -> Result<proto::BundleGlobalNames> {
        Ok(proto::BundleGlobalNames {
            names: self.names.iter().cloned().collect(),
            registered_symbols: Some(self.registered_symbols),
            fresh_collections: Some(self.fresh_collections),
            no_accessor_install_callables: self
                .no_accessor_install_callables
                .iter()
                .map(ProtoConvert::to_proto)
                .collect::<Result<_>>()?,
        })
    }

    fn from_proto(value: proto::BundleGlobalNames) -> Result<Self> {
        Ok(Self {
            names: decode_set(value.names)?,
            registered_symbols: required(
                value.registered_symbols,
                "BundleGlobalNames.registered_symbols",
            )?,
            fresh_collections: required(
                value.fresh_collections,
                "BundleGlobalNames.fresh_collections",
            )?,
            no_accessor_install_callables: decode_set(value.no_accessor_install_callables)?,
        })
    }
}

/// The protobuf is the saved callable representation. Nested capability
/// messages include trusted dependencies even though report JSON omits them.
impl ProtoConvert<proto::CallableSummary> for CallableSummary {
    fn to_proto(&self) -> Result<proto::CallableSummary> {
        Ok(proto::CallableSummary {
            callback_factory_evidence: self
                .callback_factory_evidence
                .iter()
                .map(ProtoConvert::to_proto)
                .collect::<Result<_>>()?,
            returned_record: self
                .returned_record
                .as_ref()
                .map(ProtoConvert::to_proto)
                .transpose()?,
            namespace_wrapper: self
                .namespace_wrapper
                .as_ref()
                .map(ProtoConvert::to_proto)
                .transpose()?,
            callback_object_arguments: self
                .callback_object_arguments
                .as_ref()
                .map(ProtoConvert::to_proto)
                .transpose()?,
            descriptor_copy_factory: self
                .descriptor_copy_factory
                .as_ref()
                .map(ProtoConvert::to_proto)
                .transpose()?,
            foreign_call: self
                .foreign_call
                .as_ref()
                .map(ProtoConvert::to_proto)
                .transpose()?,
            may_read_importer_cells: self.may_read_importer_cells.iter().cloned().collect(),
        })
    }

    fn from_proto(value: proto::CallableSummary) -> Result<Self> {
        Ok(Self {
            callback_factory_evidence: decode_set(value.callback_factory_evidence)?,
            returned_record: value
                .returned_record
                .map(ProtoConvert::from_proto)
                .transpose()?,
            namespace_wrapper: value
                .namespace_wrapper
                .map(ProtoConvert::from_proto)
                .transpose()?,
            callback_object_arguments: value
                .callback_object_arguments
                .map(ProtoConvert::from_proto)
                .transpose()?,
            descriptor_copy_factory: value
                .descriptor_copy_factory
                .map(ProtoConvert::from_proto)
                .transpose()?,
            foreign_call: value
                .foreign_call
                .map(ProtoConvert::from_proto)
                .transpose()?,
            may_read_importer_cells: decode_set(value.may_read_importer_cells)?,
        })
    }
}

impl ProtoConvert<proto::NativeIntegrity> for NativeIntegrity {
    fn to_proto(&self) -> Result<proto::NativeIntegrity> {
        let value_input_evidence = self
            .value_input_evidence
            .iter()
            .map(|(reference, evidence)| {
                ensure!(
                    *reference == evidence.reference,
                    "value input evidence has an inconsistent reference"
                );
                evidence.to_proto()
            })
            .collect::<Result<_>>()?;
        Ok(proto::NativeIntegrity {
            mutated: encode_native_intrinsics(&self.mutated)?,
            prototype_mutations: self
                .prototype_mutations
                .iter()
                .map(ProtoConvert::to_proto)
                .collect::<Result<_>>()?,
            witnesses: self
                .witnesses
                .iter()
                .map(ProtoConvert::to_proto)
                .collect::<Result<_>>()?,
            value_input_evidence,
            consumed_value_inputs: self
                .consumed_value_inputs
                .iter()
                .map(ProtoConvert::to_proto)
                .collect::<Result<_>>()?,
            consumed_host_behavior: self
                .consumed_host_behavior
                .iter()
                .map(ProtoConvert::to_proto)
                .collect::<Result<_>>()?,
            never_written: encode_native_intrinsics(&self.never_written)?,
            never_written_overrides: self
                .never_written_overrides
                .iter()
                .map(ProtoConvert::to_proto)
                .collect::<Result<_>>()?,
        })
    }

    fn from_proto(value: proto::NativeIntegrity) -> Result<Self> {
        let mut value_input_evidence = BTreeMap::new();
        for evidence in value.value_input_evidence {
            let evidence = ValueInputEvidence::from_proto(evidence)?;
            ensure!(
                value_input_evidence
                    .insert(evidence.reference.clone(), evidence)
                    .is_none(),
                "duplicate value input evidence"
            );
        }
        Ok(Self {
            mutated: decode_native_intrinsics(value.mutated)?,
            prototype_mutations: Arc::new(
                value
                    .prototype_mutations
                    .into_iter()
                    .map(ProtoConvert::from_proto)
                    .collect::<Result<_>>()?,
            ),
            witnesses: Arc::new(
                value
                    .witnesses
                    .into_iter()
                    .map(ProtoConvert::from_proto)
                    .collect::<Result<_>>()?,
            ),
            value_input_evidence,
            consumed_value_inputs: decode_set(value.consumed_value_inputs)?,
            consumed_host_behavior: decode_set(value.consumed_host_behavior)?,
            never_written: decode_native_intrinsics(value.never_written)?,
            never_written_overrides: value
                .never_written_overrides
                .into_iter()
                .map(ProtoConvert::from_proto)
                .collect::<Result<_>>()?,
        })
    }
}

#[cfg(test)]
mod tests {
    use crate::analysis_inputs::{
        ArgumentInputRef, CallableSummary, ForeignCallSummary, ForeignInvocation, ForeignReturn,
        NamespaceWrapperSummary, NativeIntrinsic, ProofDependencies, ValueInputRef,
    };
    use crate::analysis_provenance::{InferenceRule, InferenceUse};
    use crate::execution_evidence::SourceLocation;
    use std::collections::BTreeSet;

    #[test]
    fn native_intrinsics_keep_report_names_and_strict_saved_sets() {
        use crate::saved_proto::{decode_native_intrinsics, encode_native_intrinsics};

        assert_eq!(
            serde_json::to_value(NativeIntrinsic::UnknownGlobalValue).unwrap(),
            "unknown_global_value"
        );
        assert_eq!(NativeIntrinsic::RegExp.to_string(), "reg_exp");
        assert_eq!(NativeIntrinsic::GlobalObject as i32, 0);

        let families = BTreeSet::from([NativeIntrinsic::Array, NativeIntrinsic::Symbol]);
        let encoded = encode_native_intrinsics(&families).unwrap();
        assert_eq!(decode_native_intrinsics(encoded.clone()).unwrap(), families);
        assert!(decode_native_intrinsics([encoded.clone(), encoded].concat()).is_err());
        assert!(decode_native_intrinsics(vec![i32::MAX]).is_err());
    }

    #[test]
    fn saved_host_prerequisites_preserve_keys_and_reject_unknown_values() {
        use super::{HostBehaviorKey, ProtoConvert, proto};
        use proto::host_behavior_key::Value;
        let dependencies = ProofDependencies {
            host_behavior: BTreeSet::from([
                HostBehaviorKey::SynchronousLoadDrainsMicrotasks,
                HostBehaviorKey::ReentryFreeBuiltinModule(spec::NodeBuiltinModule::Fs),
                HostBehaviorKey::ReentryFreeOperation(spec::ReentryFreeOperation::PromiseResolve),
            ]),
            ..ProofDependencies::default()
        };
        let wire = dependencies.to_proto().unwrap();
        assert_eq!(
            ProofDependencies::from_proto(wire.clone()).unwrap(),
            dependencies
        );
        let mut invalid_proof = wire;
        invalid_proof
            .host_behavior
            .push(proto::HostBehaviorKey { value: None });
        assert!(ProofDependencies::from_proto(invalid_proof).is_err());
        for value in [
            None,
            Some(Value::ReentryFreeBuiltinModule(i32::MAX)),
            Some(Value::ReentryFreeOperation(999)),
        ] {
            assert!(HostBehaviorKey::from_proto(proto::HostBehaviorKey { value }).is_err());
        }
        assert_eq!(
            HostBehaviorKey::from_proto(proto::HostBehaviorKey {
                value: Some(Value::ReentryFreeBuiltinModule(0)),
            })
            .unwrap(),
            HostBehaviorKey::ReentryFreeBuiltinModule(spec::NodeBuiltinModule::Assert)
        );
    }

    #[test]
    fn first_program_premise_round_trips_and_duplicate_keys_are_rejected() {
        use crate::saved_proto::ProtoConvert;
        let dependencies = ProofDependencies {
            program_premises: [super::super::ProgramPremiseKey::ProcessPrototypeChainAccessors]
                .into(),
            ..ProofDependencies::default()
        };
        let mut wire = dependencies.to_proto().unwrap();
        assert_eq!(wire.program_premises, vec![0]);
        assert_eq!(
            ProofDependencies::from_proto(wire.clone()).unwrap(),
            dependencies
        );

        wire.program_premises.push(0);
        assert!(ProofDependencies::from_proto(wire).is_err());
    }

    #[test]
    fn saved_capabilities_keep_zero_indexes_and_optional_locations() {
        use super::{
            CallbackFactoryEvidence, NativeInputKind, NativeOperationSelectorProvenance,
            ProtoConvert, proto,
        };
        let returned = ForeignReturn::Argument { index: 0 };
        assert_eq!(
            ForeignReturn::from_proto(returned.to_proto().unwrap()).unwrap(),
            returned
        );
        assert!(ForeignReturn::from_proto(proto::ForeignReturn { value: None }).is_err());
        assert_eq!(
            NativeInputKind::from_proto(proto::NativeInputKind {
                value: Some(proto::native_input_kind::Value::Operation(0)),
            })
            .unwrap(),
            NativeInputKind::Operation(spec::NativeOperationKind::TextEncoderEmpty)
        );
        assert_eq!(
            NativeInputKind::from_proto(proto::NativeInputKind {
                value: Some(proto::native_input_kind::Value::CallIdentity(0)),
            })
            .unwrap(),
            NativeInputKind::CallIdentity(spec::NativeCallIdentityKind::ObjectFreeze)
        );
        for value in [i32::MIN, i32::MAX] {
            assert!(
                NativeInputKind::from_proto(proto::NativeInputKind {
                    value: Some(proto::native_input_kind::Value::Operation(value)),
                })
                .is_err()
            );
            assert!(
                NativeInputKind::from_proto(proto::NativeInputKind {
                    value: Some(proto::native_input_kind::Value::CallIdentity(value)),
                })
                .is_err()
            );
        }
        let evidence = CallbackFactoryEvidence::InferredImmutableWrapper {
            defining_module: "provider".into(),
            export: "factory".into(),
            local: "factory".into(),
            source_location: None,
        };
        let mut wire = evidence.to_proto().unwrap();
        assert_eq!(
            CallbackFactoryEvidence::from_proto(wire.clone()).unwrap(),
            evidence
        );
        let Some(proto::callback_factory_evidence::Value::InferredImmutableWrapper(wrapper)) =
            wire.value.as_mut()
        else {
            panic!("inferred wrapper evidence");
        };
        assert!(wrapper.source_location.is_none());
        wrapper.defining_module = None;
        assert!(CallbackFactoryEvidence::from_proto(wire).is_err());
        assert!(
            NativeOperationSelectorProvenance::from_proto(
                proto::NativeOperationSelectorProvenance {
                    value: Some(
                        proto::native_operation_selector_provenance::Value::Declaration(
                            proto::SourceDeclaration {
                                binding: None,
                                body_index: Some(0),
                            }
                        )
                    ),
                }
            )
            .is_err()
        );
    }

    #[test]
    fn report_only_input_details_cannot_enter_saved_analysis() {
        use super::ProtoConvert;
        use crate::analysis_inputs::{
            NativeInputKind, NativeOperationInputEvidence, NativeOperationInputRef,
            NativeOperationSelectorProvenance, SourceDeclaration, ValueInputEvidence,
        };

        let mut value = ValueInputEvidence {
            reference: ValueInputRef {
                defining_chunk: "chunk".into(),
                input_index: 0,
            },
            spec_location: "value_inputs[0]".into(),
            source_match: spec::SourceMatchClaim {
                identifiers: spec::SourceMatchIdentifierMode::Exact,
                match_source: "const value = {}".into(),
                bindings: Vec::new(),
                note: None,
            },
            value_kind: spec::CapturedValueKind::OrdinaryObject,
            source_path: "input.js".into(),
            parser_input_file: None,
            audited_source_sha256: "digest".into(),
            selected_binding: "value".into(),
            declaration_location: None,
            initializer_location: None,
            expected_export: None,
            note: None,
        };
        let wire = value.to_proto().expect("analysis evidence serializes");
        assert_eq!(ValueInputEvidence::from_proto(wire).unwrap(), value);
        value.parser_input_file = Some("/report/input.js".into());
        assert!(value.to_proto().is_err());
        value.parser_input_file = None;
        value.note = Some("author note".into());
        assert!(value.to_proto().is_err());

        let mut native = NativeOperationInputEvidence {
            reference: NativeOperationInputRef {
                defining_chunk: "chunk".into(),
                input_index: 0,
            },
            spec_location: "native_operation_inputs[0]".into(),
            selector: NativeOperationSelectorProvenance::Declaration(SourceDeclaration {
                binding: "value".into(),
                body_index: 0,
            }),
            operation: NativeInputKind::Operation(spec::NativeOperationKind::MapEmpty),
            source_path: "input.js".into(),
            parser_input_file: None,
            audited_source_sha256: "digest".into(),
            selected_binding: "value".into(),
            declaration_location: None,
            initializer_location: None,
            note: None,
        };
        let wire = native.to_proto().expect("analysis evidence serializes");
        assert_eq!(
            NativeOperationInputEvidence::from_proto(wire).unwrap(),
            native
        );
        native.note = Some("author note".into());
        assert!(native.to_proto().is_err());
        native.note = None;
        native.parser_input_file = Some("/report/input.js".into());
        assert!(native.to_proto().is_err());
    }

    #[test]
    fn protobuf_callable_dependencies_round_trip_and_stay_out_of_report_json() {
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
            imported_primitive_array_spread_input_refs: BTreeSet::new(),
        };
        let mut summary = CallableSummary::namespace_wrapper(NamespaceWrapperSummary {
            defining_module: "provider".to_string(),
            export: "wrap".to_string(),
            local: "wrap".to_string(),
            source_location: Some(SourceLocation {
                source_path: "provider.js".to_string(),
                start_line: 3,
                end_line: 5,
                start_column: Some(7),
            }),
            inference,
            dependencies: namespace_dependencies,
        });
        summary.foreign_call = Some(ForeignCallSummary {
            defining_module: "provider".to_string(),
            export: "invoke".to_string(),
            local: "invoke".to_string(),
            source_location: None,
            invocation: ForeignInvocation::Call,
            returns: BTreeSet::from([ForeignReturn::Primitive]),
            retained_arguments: BTreeSet::new(),
            retained_provider_captures: BTreeSet::new(),
            provider_cell_reads: BTreeSet::new(),
            native_constructions: BTreeSet::new(),
            replacement_package: None,
            dependencies: foreign_dependencies,
        });
        summary.may_read_importer_cells = BTreeSet::from(["caller_export".to_owned()]);

        let report = serde_json::to_value(&summary).expect("callable report serializes");
        assert!(report["namespace_wrapper"].get("dependencies").is_none());
        assert!(report["foreign_call"].get("dependencies").is_none());

        use crate::saved_proto::{ProtoConvert, proto};
        let wire = summary.to_proto().expect("typed snapshot");
        let bytes = analysis_codec::encode_protobuf(&wire).expect("encode callable");
        let decoded: proto::CallableSummary =
            analysis_codec::decode_protobuf(&bytes).expect("decode callable");
        assert_eq!(
            CallableSummary::from_proto(decoded).expect("complete callable"),
            summary
        );

        let mut missing_proof = wire.clone();
        missing_proof
            .namespace_wrapper
            .as_mut()
            .unwrap()
            .dependencies = None;
        assert!(CallableSummary::from_proto(missing_proof).is_err());

        let mut duplicate_cells = wire.clone();
        duplicate_cells
            .may_read_importer_cells
            .push("caller_export".to_owned());
        assert!(CallableSummary::from_proto(duplicate_cells).is_err());

        let mut missing_proof = wire.clone();
        missing_proof.foreign_call.as_mut().unwrap().dependencies = None;
        assert!(CallableSummary::from_proto(missing_proof).is_err());

        let mut missing_invocation = wire.clone();
        missing_invocation.foreign_call.as_mut().unwrap().invocation = None;
        assert!(CallableSummary::from_proto(missing_invocation).is_err());

        let absent_capability = CallableSummary::default();
        let wire = absent_capability
            .to_proto()
            .expect("absent capability encodes");
        assert_eq!(
            CallableSummary::from_proto(wire).expect("explicit absence decodes"),
            absent_capability
        );
    }
}
