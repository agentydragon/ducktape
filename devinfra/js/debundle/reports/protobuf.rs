//! Saved report evidence; report enrichment stays outside the stage boundary.

use anyhow::{Result, ensure};
use std::collections::BTreeSet;

use super::schema::{
    BoundedInvocationFallbackCallSite, BoundedInvocationFallbackInputPath,
    BoundedInvocationFallbackPathEdge, BoundedInvocationFallbackSeed,
    BoundedInvocationFallbackUnknownPathReason, ClassMembersPublicationWriter,
    FirstClassMembersPublication, InitializerBindingEffectsUse, InitializerExternalEffectsUse,
    LocalRecordFactoryRejection, LocalRecordFactoryUse, NativeIntegrityCheck, NativeIntegrityGuard,
    NativeIntegrityUse, NativeMutationOperation, NativeMutationWitness, NativeOperationInputUse,
    NativePrefixObservation, NativeProgramPointKind, NativeProgramPointStep, NativePropertyKey,
    NativePrototypeMutation, NativePrototypeMutationKind, NativePrototypePropertyCheck,
    NativeReceiverPath, NativeUnreachableBranch, NativeWriteSlot, NeverWrittenOverride,
    NeverWrittenReach, RecordFactoryRejectionReason, ReentryFreeOperationUse,
};
use crate::analysis_inputs::ProgramPremiseKey;
use crate::saved_proto::{ProtoConvert, decode_set, proto, required};

fn read<T, P>(value: Option<P>, field: &str) -> Result<T>
where
    T: ProtoConvert<P>,
{
    T::from_proto(required(value, field)?)
}

fn encode_optional<T: ProtoConvert<P>, P>(value: &Option<T>) -> Result<Option<P>> {
    value.as_ref().map(T::to_proto).transpose()
}

fn decode_optional<T: ProtoConvert<P>, P>(value: Option<P>) -> Result<Option<T>> {
    value.map(T::from_proto).transpose()
}

fn encode_items<'a, T: ProtoConvert<P> + 'a, P>(
    items: impl IntoIterator<Item = &'a T>,
) -> Result<Vec<P>> {
    items.into_iter().map(T::to_proto).collect()
}

fn decode_items<T: ProtoConvert<P>, P>(items: Vec<P>) -> Result<Vec<T>> {
    items.into_iter().map(T::from_proto).collect()
}

impl ProtoConvert<proto::BoundedInvocationFallbackCallSite> for BoundedInvocationFallbackCallSite {
    fn to_proto(&self) -> Result<proto::BoundedInvocationFallbackCallSite> {
        ensure!(
            self.source_location.is_none(),
            "report-only fallback callsite source location cannot be saved"
        );
        Ok(proto::BoundedInvocationFallbackCallSite {
            top_level_owner: encode_optional(&self.top_level_owner)?,
            span_start: Some(self.span_start),
            span_end: Some(self.span_end),
        })
    }
    fn from_proto(value: proto::BoundedInvocationFallbackCallSite) -> Result<Self> {
        Ok(Self {
            top_level_owner: decode_optional(value.top_level_owner)?,
            span_start: required(
                value.span_start,
                "BoundedInvocationFallbackCallSite.span_start",
            )?,
            span_end: required(value.span_end, "BoundedInvocationFallbackCallSite.span_end")?,
            source_location: None,
        })
    }
}

impl ProtoConvert<proto::FirstClassMembersPublication> for FirstClassMembersPublication {
    fn to_proto(&self) -> Result<proto::FirstClassMembersPublication> {
        ensure!(
            self.source_location.is_none(),
            "report-only class publication source location cannot be saved"
        );
        Ok(proto::FirstClassMembersPublication {
            writer: Some(self.writer as i32),
            top_level_owner: encode_optional(&self.top_level_owner)?,
            span_start: self.span_start,
            span_end: self.span_end,
            bounded_fallback_input_path: encode_optional(&self.bounded_fallback_input_path)?,
            bounded_fallback_callsite: encode_optional(&self.bounded_fallback_callsite)?,
        })
    }
    fn from_proto(value: proto::FirstClassMembersPublication) -> Result<Self> {
        Ok(Self {
            writer: ClassMembersPublicationWriter::try_from(required(
                value.writer,
                "FirstClassMembersPublication.writer",
            )?)?,
            top_level_owner: decode_optional(value.top_level_owner)?,
            span_start: value.span_start,
            span_end: value.span_end,
            source_location: None,
            bounded_fallback_input_path: decode_optional(value.bounded_fallback_input_path)?,
            bounded_fallback_callsite: decode_optional(value.bounded_fallback_callsite)?,
        })
    }
}

impl ProtoConvert<proto::InitializerExternalEffectsUse> for InitializerExternalEffectsUse {
    fn to_proto(&self) -> Result<proto::InitializerExternalEffectsUse> {
        Ok(proto::InitializerExternalEffectsUse {
            binding: Some(self.binding.clone()),
            effects: Some(self.effects.to_proto()?),
        })
    }
    fn from_proto(value: proto::InitializerExternalEffectsUse) -> Result<Self> {
        Ok(Self {
            binding: required(value.binding, "InitializerExternalEffectsUse.binding")?,
            effects: read(value.effects, "InitializerExternalEffectsUse.effects")?,
        })
    }
}

impl ProtoConvert<proto::LocalRecordFactoryRejection> for LocalRecordFactoryRejection {
    fn to_proto(&self) -> Result<proto::LocalRecordFactoryRejection> {
        Ok(proto::LocalRecordFactoryRejection {
            factory_binding: Some(self.factory_binding.clone()),
            reason: Some(self.reason as i32),
            property: self.property.clone(),
            construction: encode_optional(&self.construction)?,
            first_callback_publication_owner: encode_optional(
                &self.first_callback_publication_owner,
            )?,
            first_class_members_publication: encode_optional(
                &self.first_class_members_publication,
            )?,
            lexical_statement_ordinal: Some(self.lexical_statement_ordinal.to_proto()?),
            source_location: encode_optional(&self.source_location)?,
            span: Some(self.span.to_proto()?),
        })
    }
    fn from_proto(value: proto::LocalRecordFactoryRejection) -> Result<Self> {
        Ok(Self {
            factory_binding: required(
                value.factory_binding,
                "LocalRecordFactoryRejection.factory_binding",
            )?,
            reason: RecordFactoryRejectionReason::try_from(required(
                value.reason,
                "LocalRecordFactoryRejection.reason",
            )?)?,
            property: value.property,
            construction: decode_optional(value.construction)?,
            first_callback_publication_owner: decode_optional(
                value.first_callback_publication_owner,
            )?,
            first_class_members_publication: decode_optional(
                value.first_class_members_publication,
            )?,
            lexical_statement_ordinal: read(
                value.lexical_statement_ordinal,
                "LocalRecordFactoryRejection.lexical_statement_ordinal",
            )?,
            source_location: decode_optional(value.source_location)?,
            span: read(value.span, "LocalRecordFactoryRejection.span")?,
        })
    }
}

impl ProtoConvert<proto::LocalRecordFactoryUse> for LocalRecordFactoryUse {
    fn to_proto(&self) -> Result<proto::LocalRecordFactoryUse> {
        Ok(proto::LocalRecordFactoryUse {
            factory_binding: Some(self.factory_binding.clone()),
            prototype_properties: self.prototype_properties.clone(),
            lexical_statement_ordinal: Some(self.lexical_statement_ordinal.to_proto()?),
            source_location: encode_optional(&self.source_location)?,
            span: Some(self.span.to_proto()?),
        })
    }
    fn from_proto(value: proto::LocalRecordFactoryUse) -> Result<Self> {
        Ok(Self {
            factory_binding: required(
                value.factory_binding,
                "LocalRecordFactoryUse.factory_binding",
            )?,
            prototype_properties: value.prototype_properties,
            lexical_statement_ordinal: read(
                value.lexical_statement_ordinal,
                "LocalRecordFactoryUse.lexical_statement_ordinal",
            )?,
            source_location: decode_optional(value.source_location)?,
            span: read(value.span, "LocalRecordFactoryUse.span")?,
        })
    }
}

impl ProtoConvert<proto::NativeIntegrityCheck> for NativeIntegrityCheck {
    fn to_proto(&self) -> Result<proto::NativeIntegrityCheck> {
        Ok(proto::NativeIntegrityCheck {
            guard: Some(self.guard as i32),
            slots: encode_items(&self.slots)?,
            affected_roots: encode_items(&self.affected_roots)?,
        })
    }
    fn from_proto(value: proto::NativeIntegrityCheck) -> Result<Self> {
        Ok(Self {
            guard: NativeIntegrityGuard::try_from(required(
                value.guard,
                "NativeIntegrityCheck.guard",
            )?)?,
            slots: decode_set(value.slots)?,
            affected_roots: decode_set(value.affected_roots)?,
        })
    }
}

impl ProtoConvert<proto::NativeIntegrityUse> for NativeIntegrityUse {
    fn to_proto(&self) -> Result<proto::NativeIntegrityUse> {
        Ok(proto::NativeIntegrityUse {
            guard: Some(self.guard as i32),
            value_input_refs: encode_items(&self.value_input_refs)?,
            no_accessor_install_refs: encode_items(&self.no_accessor_install_refs)?,
            program_premises: self
                .program_premises
                .iter()
                .map(|key| *key as i32)
                .collect(),
        })
    }
    fn from_proto(value: proto::NativeIntegrityUse) -> Result<Self> {
        Ok(Self {
            guard: NativeIntegrityGuard::try_from(required(
                value.guard,
                "NativeIntegrityUse.guard",
            )?)?,
            value_input_refs: decode_set(value.value_input_refs)?,
            no_accessor_install_refs: decode_set(value.no_accessor_install_refs)?,
            program_premises: value.program_premises.into_iter().try_fold(
                BTreeSet::new(),
                |mut keys, key| {
                    let key = ProgramPremiseKey::try_from(key)?;
                    ensure!(
                        key != ProgramPremiseKey::Unspecified,
                        "unspecified program premise"
                    );
                    ensure!(keys.insert(key), "duplicate program premise");
                    Ok(keys)
                },
            )?,
        })
    }
}

impl ProtoConvert<proto::NativeMutationWitness> for NativeMutationWitness {
    fn to_proto(&self) -> Result<proto::NativeMutationWitness> {
        Ok(proto::NativeMutationWitness {
            chunk_id: Some(self.chunk_id.clone()),
            statement_ordinal: Some(self.statement_ordinal.to_proto()?),
            source_location: encode_optional(&self.source_location)?,
            operation: Some(self.operation as i32),
            receiver_roots: encode_items(&self.receiver_roots)?,
            through_local_alias: Some(self.through_local_alias),
            receiver_global: self.receiver_global.clone(),
            affected_roots: encode_items(&self.affected_roots)?,
            target_slots: encode_items(&self.target_slots)?,
            key_dependencies: encode_items(&self.key_dependencies)?,
            invalidated_key_dependencies: encode_items(&self.invalidated_key_dependencies)?,
            value_input_refs: encode_items(&self.value_input_refs)?,
            value_input_evidence: encode_items(&self.value_input_evidence)?,
            unreachable_branch: encode_optional(&self.unreachable_branch)?,
            span: Some(self.span.to_proto()?),
        })
    }
    fn from_proto(value: proto::NativeMutationWitness) -> Result<Self> {
        Ok(Self {
            chunk_id: required(value.chunk_id, "NativeMutationWitness.chunk_id")?,
            statement_ordinal: read(
                value.statement_ordinal,
                "NativeMutationWitness.statement_ordinal",
            )?,
            source_location: decode_optional(value.source_location)?,
            operation: NativeMutationOperation::try_from(required(
                value.operation,
                "NativeMutationWitness.operation",
            )?)?,
            receiver_roots: decode_set(value.receiver_roots)?,
            through_local_alias: required(
                value.through_local_alias,
                "NativeMutationWitness.through_local_alias",
            )?,
            receiver_global: value.receiver_global,
            affected_roots: decode_set(value.affected_roots)?,
            target_slots: decode_set(value.target_slots)?,
            key_dependencies: decode_set(value.key_dependencies)?,
            invalidated_key_dependencies: decode_set(value.invalidated_key_dependencies)?,
            value_input_refs: decode_set(value.value_input_refs)?,
            value_input_evidence: decode_items(value.value_input_evidence)?,
            unreachable_branch: decode_optional(value.unreachable_branch)?,
            span: read(value.span, "NativeMutationWitness.span")?,
        })
    }
}

impl ProtoConvert<proto::NativeOperationInputUse> for NativeOperationInputUse {
    fn to_proto(&self) -> Result<proto::NativeOperationInputUse> {
        Ok(proto::NativeOperationInputUse {
            evidence: Some(self.evidence.to_proto()?),
            source_location: encode_optional(&self.source_location)?,
            span: Some(self.span.to_proto()?),
        })
    }
    fn from_proto(value: proto::NativeOperationInputUse) -> Result<Self> {
        Ok(Self {
            evidence: read(value.evidence, "NativeOperationInputUse.evidence")?,
            source_location: decode_optional(value.source_location)?,
            span: read(value.span, "NativeOperationInputUse.span")?,
        })
    }
}

impl ProtoConvert<proto::NativePrefixObservation> for NativePrefixObservation {
    fn to_proto(&self) -> Result<proto::NativePrefixObservation> {
        Ok(proto::NativePrefixObservation {
            guard: Some(self.guard as i32),
            program_point_path: encode_items(&self.program_point_path)?,
            property: self.property.clone(),
            prior_affected_roots: encode_items(&self.prior_affected_roots)?,
            prior_blocking_prototype_mutations: Some(
                self.prior_blocking_prototype_mutations.to_proto()?,
            ),
            uncertain_execution: Some(self.uncertain_execution),
        })
    }
    fn from_proto(value: proto::NativePrefixObservation) -> Result<Self> {
        Ok(Self {
            guard: NativeIntegrityGuard::try_from(required(
                value.guard,
                "NativePrefixObservation.guard",
            )?)?,
            program_point_path: decode_items(value.program_point_path)?,
            property: value.property,
            prior_affected_roots: decode_set(value.prior_affected_roots)?,
            prior_blocking_prototype_mutations: read(
                value.prior_blocking_prototype_mutations,
                "NativePrefixObservation.prior_blocking_prototype_mutations",
            )?,
            uncertain_execution: required(
                value.uncertain_execution,
                "NativePrefixObservation.uncertain_execution",
            )?,
        })
    }
}

impl ProtoConvert<proto::NativeProgramPointStep> for NativeProgramPointStep {
    fn to_proto(&self) -> Result<proto::NativeProgramPointStep> {
        Ok(proto::NativeProgramPointStep {
            kind: Some(self.kind as i32),
            ordinal: Some(self.ordinal.to_proto()?),
        })
    }
    fn from_proto(value: proto::NativeProgramPointStep) -> Result<Self> {
        Ok(Self {
            kind: NativeProgramPointKind::try_from(required(
                value.kind,
                "NativeProgramPointStep.kind",
            )?)?,
            ordinal: read(value.ordinal, "NativeProgramPointStep.ordinal")?,
        })
    }
}

impl ProtoConvert<proto::NativePrototypeMutation> for NativePrototypeMutation {
    fn to_proto(&self) -> Result<proto::NativePrototypeMutation> {
        Ok(proto::NativePrototypeMutation {
            chunk_id: Some(self.chunk_id.clone()),
            statement_ordinal: Some(self.statement_ordinal.to_proto()?),
            source_location: encode_optional(&self.source_location)?,
            kind: Some(self.kind as i32),
            properties: self.properties.as_ref().map(|properties| {
                proto::native_prototype_mutation::NamedProperties {
                    names: properties.iter().cloned().collect(),
                }
            }),
            key_dependencies: encode_items(&self.key_dependencies)?,
            span: Some(self.span.to_proto()?),
        })
    }
    fn from_proto(value: proto::NativePrototypeMutation) -> Result<Self> {
        Ok(Self {
            chunk_id: required(value.chunk_id, "NativePrototypeMutation.chunk_id")?,
            statement_ordinal: read(
                value.statement_ordinal,
                "NativePrototypeMutation.statement_ordinal",
            )?,
            source_location: decode_optional(value.source_location)?,
            kind: NativePrototypeMutationKind::try_from(required(
                value.kind,
                "NativePrototypeMutation.kind",
            )?)?,
            properties: value
                .properties
                .map(|properties| decode_set(properties.names))
                .transpose()?,
            key_dependencies: decode_set(value.key_dependencies)?,
            span: read(value.span, "NativePrototypeMutation.span")?,
        })
    }
}

impl ProtoConvert<proto::NativePrototypePropertyCheck> for NativePrototypePropertyCheck {
    fn to_proto(&self) -> Result<proto::NativePrototypePropertyCheck> {
        Ok(proto::NativePrototypePropertyCheck {
            property: Some(self.property.clone()),
            intact: Some(self.intact),
            blocking_mutation_count: Some(self.blocking_mutation_count.to_proto()?),
        })
    }
    fn from_proto(value: proto::NativePrototypePropertyCheck) -> Result<Self> {
        Ok(Self {
            property: required(value.property, "NativePrototypePropertyCheck.property")?,
            intact: required(value.intact, "NativePrototypePropertyCheck.intact")?,
            blocking_mutation_count: read(
                value.blocking_mutation_count,
                "NativePrototypePropertyCheck.blocking_mutation_count",
            )?,
        })
    }
}

impl ProtoConvert<proto::NativeUnreachableBranch> for NativeUnreachableBranch {
    fn to_proto(&self) -> Result<proto::NativeUnreachableBranch> {
        Ok(proto::NativeUnreachableBranch {
            arguments: Some(self.arguments.to_proto()?),
            imported_local: Some(self.imported_local.clone()),
            wrapper_call_location: encode_optional(&self.wrapper_call_location)?,
            callback_location: encode_optional(&self.callback_location)?,
            guard_location: encode_optional(&self.guard_location)?,
            guard_span: Some(self.guard_span.to_proto()?),
            wrapper_call_span: Some(self.wrapper_call_span.to_proto()?),
            callback_span: Some(self.callback_span.to_proto()?),
        })
    }
    fn from_proto(value: proto::NativeUnreachableBranch) -> Result<Self> {
        Ok(Self {
            arguments: read(value.arguments, "NativeUnreachableBranch.arguments")?,
            imported_local: required(
                value.imported_local,
                "NativeUnreachableBranch.imported_local",
            )?,
            wrapper_call_location: decode_optional(value.wrapper_call_location)?,
            callback_location: decode_optional(value.callback_location)?,
            guard_location: decode_optional(value.guard_location)?,
            guard_span: read(value.guard_span, "NativeUnreachableBranch.guard_span")?,
            wrapper_call_span: read(
                value.wrapper_call_span,
                "NativeUnreachableBranch.wrapper_call_span",
            )?,
            callback_span: read(value.callback_span, "NativeUnreachableBranch.callback_span")?,
        })
    }
}

impl ProtoConvert<proto::NativeWriteSlot> for NativeWriteSlot {
    fn to_proto(&self) -> Result<proto::NativeWriteSlot> {
        Ok(proto::NativeWriteSlot {
            receiver: Some(self.receiver.to_proto()?),
            key: Some(self.key.to_proto()?),
        })
    }
    fn from_proto(value: proto::NativeWriteSlot) -> Result<Self> {
        Ok(Self {
            receiver: read(value.receiver, "NativeWriteSlot.receiver")?,
            key: read(value.key, "NativeWriteSlot.key")?,
        })
    }
}

impl ProtoConvert<proto::NeverWrittenOverride> for NeverWrittenOverride {
    fn to_proto(&self) -> Result<proto::NeverWrittenOverride> {
        Ok(proto::NeverWrittenOverride {
            witness: Some(self.witness.to_proto()?),
            chunk_id: Some(self.chunk_id.clone()),
            source_location: encode_optional(&self.source_location)?,
            families: encode_items(&self.families)?,
            reach: Some(self.reach as i32),
        })
    }
    fn from_proto(value: proto::NeverWrittenOverride) -> Result<Self> {
        Ok(Self {
            witness: read(value.witness, "NeverWrittenOverride.witness")?,
            chunk_id: required(value.chunk_id, "NeverWrittenOverride.chunk_id")?,
            source_location: decode_optional(value.source_location)?,
            families: decode_set(value.families)?,
            reach: NeverWrittenReach::try_from(required(
                value.reach,
                "NeverWrittenOverride.reach",
            )?)?,
        })
    }
}

impl ProtoConvert<proto::ReentryFreeOperationUse> for ReentryFreeOperationUse {
    fn to_proto(&self) -> Result<proto::ReentryFreeOperationUse> {
        Ok(proto::ReentryFreeOperationUse {
            operation: Some(self.operation.to_proto()?),
            source_location: encode_optional(&self.source_location)?,
            span: Some(self.span.to_proto()?),
        })
    }
    fn from_proto(value: proto::ReentryFreeOperationUse) -> Result<Self> {
        Ok(Self {
            operation: read(value.operation, "ReentryFreeOperationUse.operation")?,
            source_location: decode_optional(value.source_location)?,
            span: read(value.span, "ReentryFreeOperationUse.span")?,
        })
    }
}

impl ProtoConvert<proto::InitializerBindingEffectsUse> for InitializerBindingEffectsUse {
    fn to_proto(&self) -> Result<proto::InitializerBindingEffectsUse> {
        use proto::initializer_binding_effects_use::{Applied, Contradicted, Value};
        let value = match self {
            Self::Applied {
                binding,
                narrowed_reads,
                narrowed_rebinds,
                narrowed_uninitialized_reads,
            } => Value::Applied(Applied {
                binding: Some(binding.clone()),
                narrowed_reads: Some(narrowed_reads.to_proto()?),
                narrowed_rebinds: Some(narrowed_rebinds.to_proto()?),
                narrowed_uninitialized_reads: Some(narrowed_uninitialized_reads.to_proto()?),
            }),
            Self::Contradicted {
                binding,
                reads,
                rebinds,
            } => Value::Contradicted(Contradicted {
                binding: Some(binding.clone()),
                reads: encode_items(reads)?,
                rebinds: encode_items(rebinds)?,
            }),
        };
        Ok(proto::InitializerBindingEffectsUse { value: Some(value) })
    }
    fn from_proto(value: proto::InitializerBindingEffectsUse) -> Result<Self> {
        use proto::initializer_binding_effects_use::Value;
        Ok(
            match required(value.value, "InitializerBindingEffectsUse.value")? {
                Value::Applied(value) => Self::Applied {
                    binding: required(
                        value.binding,
                        "InitializerBindingEffectsUse.Applied.binding",
                    )?,
                    narrowed_reads: read(
                        value.narrowed_reads,
                        "InitializerBindingEffectsUse.Applied.narrowed_reads",
                    )?,
                    narrowed_rebinds: read(
                        value.narrowed_rebinds,
                        "InitializerBindingEffectsUse.Applied.narrowed_rebinds",
                    )?,
                    narrowed_uninitialized_reads: read(
                        value.narrowed_uninitialized_reads,
                        "InitializerBindingEffectsUse.Applied.narrowed_uninitialized_reads",
                    )?,
                },
                Value::Contradicted(value) => Self::Contradicted {
                    binding: required(
                        value.binding,
                        "InitializerBindingEffectsUse.Contradicted.binding",
                    )?,
                    reads: decode_set(value.reads)?,
                    rebinds: decode_set(value.rebinds)?,
                },
            },
        )
    }
}

impl ProtoConvert<proto::NativeReceiverPath> for NativeReceiverPath {
    fn to_proto(&self) -> Result<proto::NativeReceiverPath> {
        use proto::native_receiver_path::Value;
        Ok(proto::NativeReceiverPath {
            value: Some(match self {
                Self::GlobalObject => Value::GlobalObject(Default::default()),
                Self::Constructor(root) => Value::Constructor(root.to_proto()?),
                Self::Prototype(root) => Value::Prototype(root.to_proto()?),
                Self::Descendant(root) => Value::Descendant(root.to_proto()?),
                Self::ArrayIteratorPrototype => Value::ArrayIteratorPrototype(Default::default()),
                Self::SetIteratorPrototype => Value::SetIteratorPrototype(Default::default()),
                Self::Unknown => Value::Unknown(Default::default()),
            }),
        })
    }
    fn from_proto(value: proto::NativeReceiverPath) -> Result<Self> {
        use proto::native_receiver_path::Value;
        Ok(match required(value.value, "NativeReceiverPath.value")? {
            Value::GlobalObject(_) => Self::GlobalObject,
            Value::Constructor(value) => {
                Self::Constructor(crate::analysis_inputs::NativeIntrinsic::from_proto(value)?)
            }
            Value::Prototype(value) => {
                Self::Prototype(crate::analysis_inputs::NativeIntrinsic::from_proto(value)?)
            }
            Value::Descendant(value) => {
                Self::Descendant(crate::analysis_inputs::NativeIntrinsic::from_proto(value)?)
            }
            Value::ArrayIteratorPrototype(_) => Self::ArrayIteratorPrototype,
            Value::SetIteratorPrototype(_) => Self::SetIteratorPrototype,
            Value::Unknown(_) => Self::Unknown,
        })
    }
}

impl ProtoConvert<proto::NativePropertyKey> for NativePropertyKey {
    fn to_proto(&self) -> Result<proto::NativePropertyKey> {
        use proto::native_property_key::{Affixed, Value};
        Ok(proto::NativePropertyKey {
            value: Some(match self {
                Self::String(key) => Value::String(key.clone()),
                Self::Affixed { prefix, suffix } => Value::Affixed(Affixed {
                    prefix: Some(prefix.clone()),
                    suffix: Some(suffix.clone()),
                }),
                Self::SymbolIterator => Value::SymbolIterator(Default::default()),
                Self::OtherSymbol => Value::OtherSymbol(Default::default()),
                Self::Unknown => Value::Unknown(Default::default()),
            }),
        })
    }
    fn from_proto(value: proto::NativePropertyKey) -> Result<Self> {
        use proto::native_property_key::Value;
        Ok(match required(value.value, "NativePropertyKey.value")? {
            Value::String(value) => Self::String(value),
            Value::Affixed(value) => Self::Affixed {
                prefix: required(value.prefix, "NativePropertyKey.Affixed.prefix")?,
                suffix: required(value.suffix, "NativePropertyKey.Affixed.suffix")?,
            },
            Value::SymbolIterator(_) => Self::SymbolIterator,
            Value::OtherSymbol(_) => Self::OtherSymbol,
            Value::Unknown(_) => Self::Unknown,
        })
    }
}

impl ProtoConvert<proto::BoundedInvocationFallbackInputPath>
    for BoundedInvocationFallbackInputPath
{
    fn to_proto(&self) -> Result<proto::BoundedInvocationFallbackInputPath> {
        use proto::bounded_invocation_fallback_input_path::{Retained, Value};
        Ok(proto::BoundedInvocationFallbackInputPath {
            value: Some(match self {
                Self::Retained { seed, edges } => Value::Retained(Retained {
                    seed: Some(seed.to_proto()?),
                    edges: encode_items(edges)?,
                }),
                Self::Unknown { reason } => Value::Unknown(*reason as i32),
            }),
        })
    }
    fn from_proto(value: proto::BoundedInvocationFallbackInputPath) -> Result<Self> {
        use proto::bounded_invocation_fallback_input_path::Value;
        Ok(
            match required(value.value, "BoundedInvocationFallbackInputPath.value")? {
                Value::Retained(value) => Self::Retained {
                    seed: read(
                        value.seed,
                        "BoundedInvocationFallbackInputPath.Retained.seed",
                    )?,
                    edges: decode_items(value.edges)?,
                },
                Value::Unknown(value) => {
                    let reason = BoundedInvocationFallbackUnknownPathReason::try_from(value)?;
                    ensure!(
                        reason != BoundedInvocationFallbackUnknownPathReason::Unspecified,
                        "unspecified fallback path reason"
                    );
                    Self::Unknown { reason }
                }
            },
        )
    }
}

impl ProtoConvert<proto::BoundedInvocationFallbackSeed> for BoundedInvocationFallbackSeed {
    fn to_proto(&self) -> Result<proto::BoundedInvocationFallbackSeed> {
        use proto::bounded_invocation_fallback_seed::Value;
        Ok(proto::BoundedInvocationFallbackSeed {
            value: Some(match self {
                Self::Argument { position } => Value::Argument(position.to_proto()?),
                Self::CapturedBinding { binding } => Value::CapturedBinding(binding.clone()),
                Self::EscapedCallbackPool => Value::EscapedCallbackPool(Default::default()),
                Self::AvailableExportBinding { binding } => {
                    Value::AvailableExportBinding(binding.clone())
                }
                Self::AvailableExportCallbacks => {
                    Value::AvailableExportCallbacks(Default::default())
                }
            }),
        })
    }
    fn from_proto(value: proto::BoundedInvocationFallbackSeed) -> Result<Self> {
        use proto::bounded_invocation_fallback_seed::Value;
        Ok(
            match required(value.value, "BoundedInvocationFallbackSeed.value")? {
                Value::Argument(value) => Self::Argument {
                    position: usize::from_proto(value)?,
                },
                Value::CapturedBinding(value) => Self::CapturedBinding { binding: value },
                Value::EscapedCallbackPool(_) => Self::EscapedCallbackPool,
                Value::AvailableExportBinding(value) => {
                    Self::AvailableExportBinding { binding: value }
                }
                Value::AvailableExportCallbacks(_) => Self::AvailableExportCallbacks,
            },
        )
    }
}

impl ProtoConvert<proto::BoundedInvocationFallbackPathEdge> for BoundedInvocationFallbackPathEdge {
    fn to_proto(&self) -> Result<proto::BoundedInvocationFallbackPathEdge> {
        use proto::bounded_invocation_fallback_path_edge::{AccessorProperty, Value};
        Ok(proto::BoundedInvocationFallbackPathEdge {
            value: Some(match self {
                Self::ObjectProperty { property } => Value::ObjectProperty(property.clone()),
                Self::UnknownObjectProperty => Value::UnknownObjectProperty(Default::default()),
                Self::ArrayElement { index } => Value::ArrayElement(*index),
                Self::UnknownArrayProperty => Value::UnknownArrayProperty(Default::default()),
                Self::AppliedGetter { property } => Value::AppliedGetter(AccessorProperty {
                    property: property.clone(),
                }),
                Self::AppliedSetter { property } => Value::AppliedSetter(AccessorProperty {
                    property: property.clone(),
                }),
                Self::ClosureHomeObject => Value::ClosureHomeObject(Default::default()),
                Self::ClosureSnapshot { binding } => Value::ClosureSnapshot(binding.clone()),
                Self::ClosureCell { binding } => Value::ClosureCell(binding.clone()),
                Self::InstanceRetainedValue => Value::InstanceRetainedValue(Default::default()),
                Self::DeferredCallbackRetainedValue => {
                    Value::DeferredCallbackRetainedValue(Default::default())
                }
                Self::AuditedResultRetainedValue => {
                    Value::AuditedResultRetainedValue(Default::default())
                }
                Self::DescriptorMapRetainedValue => {
                    Value::DescriptorMapRetainedValue(Default::default())
                }
                Self::EscapedCallbackPoolExpansion => {
                    Value::EscapedCallbackPoolExpansion(Default::default())
                }
                Self::ClassMembersCapability => Value::ClassMembersCapability(Default::default()),
            }),
        })
    }
    fn from_proto(value: proto::BoundedInvocationFallbackPathEdge) -> Result<Self> {
        use proto::bounded_invocation_fallback_path_edge::Value;
        Ok(
            match required(value.value, "BoundedInvocationFallbackPathEdge.value")? {
                Value::ObjectProperty(value) => Self::ObjectProperty { property: value },
                Value::UnknownObjectProperty(_) => Self::UnknownObjectProperty,
                Value::ArrayElement(value) => Self::ArrayElement { index: value },
                Value::UnknownArrayProperty(_) => Self::UnknownArrayProperty,
                Value::AppliedGetter(value) => Self::AppliedGetter {
                    property: value.property,
                },
                Value::AppliedSetter(value) => Self::AppliedSetter {
                    property: value.property,
                },
                Value::ClosureHomeObject(_) => Self::ClosureHomeObject,
                Value::ClosureSnapshot(value) => Self::ClosureSnapshot { binding: value },
                Value::ClosureCell(value) => Self::ClosureCell { binding: value },
                Value::InstanceRetainedValue(_) => Self::InstanceRetainedValue,
                Value::DeferredCallbackRetainedValue(_) => Self::DeferredCallbackRetainedValue,
                Value::AuditedResultRetainedValue(_) => Self::AuditedResultRetainedValue,
                Value::DescriptorMapRetainedValue(_) => Self::DescriptorMapRetainedValue,
                Value::EscapedCallbackPoolExpansion(_) => Self::EscapedCallbackPoolExpansion,
                Value::ClassMembersCapability(_) => Self::ClassMembersCapability,
            },
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::analysis_inputs::{NativeIntrinsic, ValueInputRef};
    use crate::execution_evidence::SourceLocation;
    use source_identity::SourceUnitOrdinal;
    use std::collections::BTreeSet;
    use swc_common::{BytePos, Span};

    fn report_location() -> SourceLocation {
        SourceLocation {
            source_path: "app.js".to_string(),
            start_line: 3,
            end_line: 4,
            start_column: Some(2),
        }
    }

    #[test]
    fn publication_context_round_trips_before_report_enrichment() {
        let mut publication = FirstClassMembersPublication {
            writer: ClassMembersPublicationWriter::BoundedInvocationFallback,
            top_level_owner: Some(SourceUnitOrdinal(7)),
            span_start: Some(11),
            span_end: Some(19),
            source_location: None,
            bounded_fallback_input_path: Some(BoundedInvocationFallbackInputPath::Retained {
                seed: BoundedInvocationFallbackSeed::Argument { position: 2 },
                edges: vec![
                    BoundedInvocationFallbackPathEdge::ObjectProperty {
                        property: "listener".to_string(),
                    },
                    BoundedInvocationFallbackPathEdge::AppliedGetter { property: None },
                    BoundedInvocationFallbackPathEdge::ClassMembersCapability,
                ],
            }),
            bounded_fallback_callsite: Some(BoundedInvocationFallbackCallSite {
                top_level_owner: Some(SourceUnitOrdinal(9)),
                span_start: 31,
                span_end: 47,
                source_location: None,
            }),
        };
        let wire = publication
            .to_proto()
            .expect("source-only publication saves");
        let bytes = analysis_codec::encode_protobuf(&wire).expect("encode publication");
        let decoded: proto::FirstClassMembersPublication =
            analysis_codec::decode_protobuf(&bytes).expect("decode publication");
        assert_eq!(
            FirstClassMembersPublication::from_proto(decoded).expect("restore publication"),
            publication
        );

        publication.source_location = Some(report_location());
        assert!(publication.to_proto().is_err());
        publication.source_location = None;
        publication
            .bounded_fallback_callsite
            .as_mut()
            .unwrap()
            .source_location = Some(report_location());
        assert!(publication.to_proto().is_err());
        assert!(
            publication
                .bounded_fallback_callsite
                .as_ref()
                .unwrap()
                .to_proto()
                .is_err()
        );
    }

    #[test]
    fn report_oneofs_preserve_zero_empty_and_unknown_payloads() {
        for receiver in [
            NativeReceiverPath::GlobalObject,
            NativeReceiverPath::Constructor(NativeIntrinsic::Array),
            NativeReceiverPath::Prototype(NativeIntrinsic::Object),
            NativeReceiverPath::Descendant(NativeIntrinsic::Symbol),
            NativeReceiverPath::ArrayIteratorPrototype,
            NativeReceiverPath::SetIteratorPrototype,
            NativeReceiverPath::Unknown,
        ] {
            let bytes = analysis_codec::encode_protobuf(&receiver.to_proto().unwrap()).unwrap();
            let wire: proto::NativeReceiverPath = analysis_codec::decode_protobuf(&bytes).unwrap();
            assert_eq!(NativeReceiverPath::from_proto(wire).unwrap(), receiver);
        }
        for key in [
            NativePropertyKey::String(String::new()),
            NativePropertyKey::Affixed {
                prefix: String::new(),
                suffix: "suffix".into(),
            },
            NativePropertyKey::SymbolIterator,
            NativePropertyKey::OtherSymbol,
            NativePropertyKey::Unknown,
        ] {
            let bytes = analysis_codec::encode_protobuf(&key.to_proto().unwrap()).unwrap();
            let wire: proto::NativePropertyKey = analysis_codec::decode_protobuf(&bytes).unwrap();
            assert_eq!(NativePropertyKey::from_proto(wire).unwrap(), key);
        }
        for seed in [
            BoundedInvocationFallbackSeed::Argument { position: 0 },
            BoundedInvocationFallbackSeed::CapturedBinding {
                binding: String::new(),
            },
            BoundedInvocationFallbackSeed::EscapedCallbackPool,
            BoundedInvocationFallbackSeed::AvailableExportBinding {
                binding: "exported".into(),
            },
            BoundedInvocationFallbackSeed::AvailableExportCallbacks,
        ] {
            let bytes = analysis_codec::encode_protobuf(&seed.to_proto().unwrap()).unwrap();
            let wire: proto::BoundedInvocationFallbackSeed =
                analysis_codec::decode_protobuf(&bytes).unwrap();
            assert_eq!(
                BoundedInvocationFallbackSeed::from_proto(wire).unwrap(),
                seed
            );
        }
        let edges = vec![
            BoundedInvocationFallbackPathEdge::ObjectProperty {
                property: String::new(),
            },
            BoundedInvocationFallbackPathEdge::UnknownObjectProperty,
            BoundedInvocationFallbackPathEdge::ArrayElement { index: 0 },
            BoundedInvocationFallbackPathEdge::UnknownArrayProperty,
            BoundedInvocationFallbackPathEdge::AppliedGetter { property: None },
            BoundedInvocationFallbackPathEdge::AppliedGetter {
                property: Some(String::new()),
            },
            BoundedInvocationFallbackPathEdge::AppliedSetter { property: None },
            BoundedInvocationFallbackPathEdge::AppliedSetter {
                property: Some("name".into()),
            },
            BoundedInvocationFallbackPathEdge::ClosureHomeObject,
            BoundedInvocationFallbackPathEdge::ClosureSnapshot {
                binding: "snapshot".into(),
            },
            BoundedInvocationFallbackPathEdge::ClosureCell {
                binding: "cell".into(),
            },
            BoundedInvocationFallbackPathEdge::InstanceRetainedValue,
            BoundedInvocationFallbackPathEdge::DeferredCallbackRetainedValue,
            BoundedInvocationFallbackPathEdge::AuditedResultRetainedValue,
            BoundedInvocationFallbackPathEdge::DescriptorMapRetainedValue,
            BoundedInvocationFallbackPathEdge::EscapedCallbackPoolExpansion,
            BoundedInvocationFallbackPathEdge::ClassMembersCapability,
        ];
        for edges in [Vec::new(), edges] {
            let path = BoundedInvocationFallbackInputPath::Retained {
                seed: BoundedInvocationFallbackSeed::EscapedCallbackPool,
                edges,
            };
            let bytes = analysis_codec::encode_protobuf(&path.to_proto().unwrap()).unwrap();
            let wire: proto::BoundedInvocationFallbackInputPath =
                analysis_codec::decode_protobuf(&bytes).unwrap();
            assert_eq!(
                BoundedInvocationFallbackInputPath::from_proto(wire).unwrap(),
                path
            );
        }
    }

    #[test]
    fn repeated_report_sets_reject_duplicates_and_invalid_intrinsics() {
        let check = NativeIntegrityCheck {
            guard: NativeIntegrityGuard::ArrayIntrinsics,
            slots: BTreeSet::from(["%Array.prototype%".into()]),
            affected_roots: BTreeSet::from([NativeIntrinsic::Array]),
        };
        let mut duplicate = check.to_proto().unwrap();
        duplicate.slots.push(duplicate.slots[0].clone());
        assert!(NativeIntegrityCheck::from_proto(duplicate).is_err());
        let mut duplicate = check.to_proto().unwrap();
        duplicate.affected_roots.push(duplicate.affected_roots[0]);
        assert!(NativeIntegrityCheck::from_proto(duplicate).is_err());
        for value in [0, i32::MAX] {
            let mut invalid = check.to_proto().unwrap();
            invalid.affected_roots = vec![value];
            assert!(NativeIntegrityCheck::from_proto(invalid).is_err());
            assert!(
                NativeReceiverPath::from_proto(proto::NativeReceiverPath {
                    value: Some(proto::native_receiver_path::Value::Constructor(value)),
                })
                .is_err()
            );
        }
        let empty = proto::NativeIntegrityCheck {
            guard: Some(NativeIntegrityGuard::ArrayIntrinsics as i32),
            slots: Vec::new(),
            affected_roots: Vec::new(),
        };
        let restored = NativeIntegrityCheck::from_proto(empty).unwrap();
        assert!(restored.slots.is_empty());
        assert!(restored.affected_roots.is_empty());
    }

    #[test]
    fn successful_native_guard_preserves_proofs_and_rejects_duplicate_references() {
        let guard = NativeIntegrityUse {
            guard: NativeIntegrityGuard::NativeCallIdentity,
            value_input_refs: BTreeSet::from([ValueInputRef {
                defining_chunk: "provider".to_string(),
                input_index: 4,
            }]),
            no_accessor_install_refs: BTreeSet::new(),
            program_premises: BTreeSet::new(),
        };
        let wire = guard.to_proto().expect("guard saves");
        assert_eq!(
            NativeIntegrityUse::from_proto(wire.clone()).expect("guard restores"),
            guard
        );

        let mut duplicate = wire.clone();
        duplicate
            .value_input_refs
            .push(duplicate.value_input_refs[0].clone());
        assert!(NativeIntegrityUse::from_proto(duplicate).is_err());
        let mut missing = wire;
        missing.guard = None;
        assert!(NativeIntegrityUse::from_proto(missing).is_err());
    }

    #[test]
    fn prototype_writes_keep_unknown_symbol_only_and_exact_property_scopes_distinct() {
        for properties in [
            None,
            Some(BTreeSet::new()),
            Some(BTreeSet::from(["name".to_string()])),
        ] {
            let mutation = NativePrototypeMutation {
                chunk_id: "app".to_string(),
                statement_ordinal: SourceUnitOrdinal(2),
                source_location: Some(report_location()),
                kind: NativePrototypeMutationKind::Write,
                properties,
                key_dependencies: BTreeSet::from([NativeIntrinsic::GlobalObject]),
                span: Span::new(BytePos(5), BytePos(13)),
            };
            let wire = mutation.to_proto().expect("mutation saves");
            let bytes = analysis_codec::encode_protobuf(&wire).expect("encode mutation");
            let decoded: proto::NativePrototypeMutation =
                analysis_codec::decode_protobuf(&bytes).expect("decode mutation");
            assert_eq!(
                NativePrototypeMutation::from_proto(decoded).expect("mutation restores"),
                mutation
            );

            let mut unknown_properties = wire.clone();
            unknown_properties.properties = None;
            let restored = NativePrototypeMutation::from_proto(unknown_properties).unwrap();
            assert!(restored.properties.is_none());
            let mut missing_span = wire;
            missing_span.span = None;
            assert!(NativePrototypeMutation::from_proto(missing_span).is_err());

            let mut duplicate = mutation.to_proto().unwrap();
            duplicate.properties = Some(proto::native_prototype_mutation::NamedProperties {
                names: vec!["name".into(), "name".into()],
            });
            assert!(NativePrototypeMutation::from_proto(duplicate).is_err());
        }
    }

    #[test]
    fn malformed_report_variants_cannot_become_valid_capabilities() {
        assert!(
            BoundedInvocationFallbackInputPath::from_proto(
                proto::BoundedInvocationFallbackInputPath {
                    value: Some(proto::bounded_invocation_fallback_input_path::Value::Unknown(0)),
                }
            )
            .is_err()
        );
        assert!(NativeReceiverPath::from_proto(proto::NativeReceiverPath { value: None }).is_err());
        assert!(NativePropertyKey::from_proto(proto::NativePropertyKey { value: None }).is_err());
        assert!(
            BoundedInvocationFallbackSeed::from_proto(proto::BoundedInvocationFallbackSeed {
                value: None,
            })
            .is_err()
        );
        assert!(
            BoundedInvocationFallbackPathEdge::from_proto(
                proto::BoundedInvocationFallbackPathEdge { value: None }
            )
            .is_err()
        );
        assert!(
            InitializerBindingEffectsUse::from_proto(proto::InitializerBindingEffectsUse {
                value: Some(proto::initializer_binding_effects_use::Value::Contradicted(
                    proto::initializer_binding_effects_use::Contradicted {
                        binding: None,
                        reads: Vec::new(),
                        rebinds: Vec::new(),
                    }
                )),
            })
            .is_err()
        );
        assert!(
            BoundedInvocationFallbackInputPath::from_proto(
                proto::BoundedInvocationFallbackInputPath::default()
            )
            .is_err()
        );
    }
}
