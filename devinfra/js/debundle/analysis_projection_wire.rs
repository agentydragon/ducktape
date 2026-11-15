//! Explicit conversion for analysis-only inputs. Authored types use spec_wire.
use analysis::analysis_inputs::{
    AnalysisAssumptions, AnalysisImportedPrimitiveArrayOperand,
    AnalysisImportedPrimitiveArraySpreadInput, BundleGlobalNames, KnownEffect, LocalEffectPolicy,
    NativeOperationSelectorProvenance, NoAccessorInstallRef, SourceDeclaration,
};
use analysis_projection_wire_proto::debundle::analysis_projection as pb;
use anyhow::{Context, Result, bail, ensure};
use spec_wire::{Wire, required, unique_map, unique_set};
use std::collections::{BTreeMap, BTreeSet};

trait AnalysisWire: Sized {
    type Message;
    fn to_wire(&self) -> Result<Self::Message>;
    fn from_wire(value: Self::Message) -> Result<Self>;
}
fn analysis_required<T: AnalysisWire>(value: Option<T::Message>) -> Result<T> {
    T::from_wire(value.context("missing required analysis projection field")?)
}

impl<T: AnalysisWire> AnalysisWire for Vec<T> {
    type Message = Vec<T::Message>;
    fn to_wire(&self) -> Result<Self::Message> {
        self.iter().map(AnalysisWire::to_wire).collect()
    }
    fn from_wire(value: Self::Message) -> Result<Self> {
        value.into_iter().map(AnalysisWire::from_wire).collect()
    }
}
impl<T: AnalysisWire + Ord> AnalysisWire for BTreeSet<T> {
    type Message = Vec<T::Message>;
    fn to_wire(&self) -> Result<Self::Message> {
        self.iter().map(AnalysisWire::to_wire).collect()
    }
    fn from_wire(value: Self::Message) -> Result<Self> {
        unique_set(value.into_iter().map(AnalysisWire::from_wire))
    }
}
impl<T: AnalysisWire> AnalysisWire for BTreeMap<String, T> {
    type Message = BTreeMap<String, T::Message>;
    fn to_wire(&self) -> Result<Self::Message> {
        self.iter()
            .map(|(key, value)| Ok((key.clone(), value.to_wire()?)))
            .collect()
    }
    fn from_wire(value: Self::Message) -> Result<Self> {
        value
            .into_iter()
            .map(|(key, value)| Ok((key, T::from_wire(value)?)))
            .collect()
    }
}

fn source_declarations_to_wire(
    value: &BTreeMap<String, Vec<SourceDeclaration>>,
) -> Result<BTreeMap<String, pb::ChunkSourceDeclarations>> {
    value
        .iter()
        .map(|(key, declarations)| {
            Ok((
                key.clone(),
                pb::ChunkSourceDeclarations {
                    declarations: declarations.to_wire()?,
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
        .map(|(key, value)| Ok((key, AnalysisWire::from_wire(value.declarations)?)))
        .collect()
}

impl AnalysisWire for BTreeMap<(u32, u32), spec::RetainedCallbackSignature> {
    type Message = Vec<pb::StatementCallback>;
    fn to_wire(&self) -> Result<Self::Message> {
        Ok(self
            .iter()
            .map(|((start, end), signature)| pb::StatementCallback {
                start: Some(*start),
                end: Some(*end),
                signature: Some(signature.to_proto()),
            })
            .collect())
    }
    fn from_wire(value: Self::Message) -> Result<Self> {
        unique_map(value.into_iter().map(|entry| {
            Ok((
                (required(entry.start)?, required(entry.end)?),
                required(entry.signature)?,
            ))
        }))
    }
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

impl AnalysisWire for crate::AnalysisSpec {
    type Message = pb::AnalysisSpec;
    fn to_wire(&self) -> Result<Self::Message> {
        validate_analysis_paths(self)?;
        Ok(pb::AnalysisSpec {
            inputs: Some(self.inputs.to_proto()),
            host_behavior: Some(self.host_behavior.to_proto()),
            program_premises: Some(self.program_premises.to_proto()),
            vendor: self.vendor.to_proto(),
            chunk_export_purity: self.chunk_export_purity.to_proto(),
            chunk_analysis_inputs: self.chunk_analysis_inputs.to_wire()?,
            analysed_chunks: self.analysed_chunks.to_proto(),
            entry_file: self.entry_file.clone(),
            prune_other_chunks: Some(self.prune_other_chunks),
            declared_assumptions: self.declared_assumptions.to_wire()?,
            source_sites: Some(self.source_sites.to_wire()?),
            native_operation_inputs: self
                .native_operation_inputs
                .iter()
                .map(|(key, inputs)| {
                    Ok((
                        key.clone(),
                        pb::ChunkNativeOperationInputs {
                            inputs: inputs.to_wire()?,
                        },
                    ))
                })
                .collect::<Result<_>>()?,
        })
    }
    fn from_wire(v: Self::Message) -> Result<Self> {
        let result = Self {
            inputs: required(v.inputs)?,
            host_behavior: required(v.host_behavior)?,
            program_premises: required(v.program_premises)?,
            vendor: Wire::from_proto(v.vendor)?,
            chunk_export_purity: Wire::from_proto(v.chunk_export_purity)?,
            chunk_analysis_inputs: AnalysisWire::from_wire(v.chunk_analysis_inputs)?,
            analysed_chunks: Wire::from_proto(v.analysed_chunks)?,
            entry_file: v.entry_file,
            prune_other_chunks: required(v.prune_other_chunks)?,
            declared_assumptions: AnalysisWire::from_wire(v.declared_assumptions)?,
            source_sites: analysis_required(v.source_sites)?,
            native_operation_inputs: v
                .native_operation_inputs
                .into_iter()
                .map(|(key, value)| Ok((key, AnalysisWire::from_wire(value.inputs)?)))
                .collect::<Result<_>>()?,
        };
        validate_analysis_paths(&result)?;
        Ok(result)
    }
}
impl AnalysisWire for crate::ChunkAnalysisInputs {
    type Message = pb::ChunkAnalysisInputs;
    fn to_wire(&self) -> Result<Self::Message> {
        Ok(pb::ChunkAnalysisInputs {
            trusted_dataflow_summaries: Some(self.trusted_dataflow_summaries),
            deferred_global_publications: spec_wire::global_names_to_proto(
                &self.deferred_global_publications,
            ),
            non_reentrant_global_membership: spec_wire::global_names_to_proto(
                &self.non_reentrant_global_membership,
            ),
            local_effect_policy: Some(self.local_effect_policy.to_wire()?),
            admission_overrides: Some(self.admission_overrides.to_proto()),
            value_inputs: self.value_inputs.to_proto(),
            native_call_identity_inputs: self.native_call_identity_inputs.to_proto(),
            initializer_call_effect_inputs: self.initializer_call_effect_inputs.to_proto(),
            argument_inputs: self.argument_inputs.to_proto(),
            no_accessor_install_callables: self.no_accessor_install_callables.to_proto(),
        })
    }
    fn from_wire(v: Self::Message) -> Result<Self> {
        Ok(Self {
            trusted_dataflow_summaries: required(v.trusted_dataflow_summaries)?,
            deferred_global_publications: spec_wire::global_names_from_proto(
                v.deferred_global_publications,
            )?,
            non_reentrant_global_membership: spec_wire::global_names_from_proto(
                v.non_reentrant_global_membership,
            )?,
            local_effect_policy: analysis_required(v.local_effect_policy)?,
            admission_overrides: required(v.admission_overrides)?,
            value_inputs: Wire::from_proto(v.value_inputs)?,
            native_call_identity_inputs: Wire::from_proto(v.native_call_identity_inputs)?,
            initializer_call_effect_inputs: Wire::from_proto(v.initializer_call_effect_inputs)?,
            argument_inputs: Wire::from_proto(v.argument_inputs)?,
            no_accessor_install_callables: Wire::from_proto(v.no_accessor_install_callables)?,
        })
    }
}
impl AnalysisWire for LocalEffectPolicy {
    type Message = i32;
    fn to_wire(&self) -> Result<Self::Message> {
        Ok(match self {
            Self::KnownEffectsOnly => pb::LocalEffectPolicy::KnownEffectsOnly as i32,
            Self::VendorPrune => pb::LocalEffectPolicy::VendorPrune as i32,
            Self::LocalPropertyWrites => pb::LocalEffectPolicy::LocalPropertyWrites as i32,
        })
    }
    fn from_wire(v: Self::Message) -> Result<Self> {
        Ok(match pb::LocalEffectPolicy::try_from(v)? {
            pb::LocalEffectPolicy::KnownEffectsOnly => Self::KnownEffectsOnly,
            pb::LocalEffectPolicy::VendorPrune => Self::VendorPrune,
            pb::LocalEffectPolicy::LocalPropertyWrites => Self::LocalPropertyWrites,
            pb::LocalEffectPolicy::Unspecified => bail!("unspecified local effect policy"),
        })
    }
}
impl AnalysisWire for NoAccessorInstallRef {
    type Message = pb::NoAccessorInstallRef;
    fn to_wire(&self) -> Result<Self::Message> {
        Ok(pb::NoAccessorInstallRef {
            defining_chunk: Some(self.defining_chunk.clone()),
            input_index: Some(self.input_index.to_proto()),
        })
    }
    fn from_wire(v: Self::Message) -> Result<Self> {
        Ok(Self {
            defining_chunk: required(v.defining_chunk)?,
            input_index: required(v.input_index)?,
        })
    }
}
impl AnalysisWire for BundleGlobalNames {
    type Message = pb::BundleGlobalNames;
    fn to_wire(&self) -> Result<Self::Message> {
        Ok(pb::BundleGlobalNames {
            names: self.names.to_proto(),
            registered_symbols: Some(self.registered_symbols),
            fresh_collections: Some(self.fresh_collections),
            no_accessor_install_callables: self.no_accessor_install_callables.to_wire()?,
        })
    }
    fn from_wire(v: Self::Message) -> Result<Self> {
        Ok(Self {
            names: Wire::from_proto(v.names)?,
            registered_symbols: required(v.registered_symbols)?,
            fresh_collections: required(v.fresh_collections)?,
            no_accessor_install_callables: AnalysisWire::from_wire(
                v.no_accessor_install_callables,
            )?,
        })
    }
}
impl AnalysisWire for KnownEffect {
    type Message = i32;
    fn to_wire(&self) -> Result<Self::Message> {
        Ok(match self {
            Self::TypescriptDecorateHelper => pb::KnownEffect::TypescriptDecorateHelper as i32,
            Self::TypescriptMetadataDecorateHelper => {
                pb::KnownEffect::TypescriptMetadataDecorateHelper as i32
            }
            Self::TypescriptMetadataDecorateHelperWithMemberDecorators => {
                pb::KnownEffect::TypescriptMetadataDecorateHelperWithMemberDecorators as i32
            }
            Self::TypescriptMetadataDecorateHelperThreeArgs => {
                pb::KnownEffect::TypescriptMetadataDecorateHelperThreeArgs as i32
            }
        })
    }
    fn from_wire(v: Self::Message) -> Result<Self> {
        Ok(match pb::KnownEffect::try_from(v)? {
            pb::KnownEffect::TypescriptDecorateHelper => Self::TypescriptDecorateHelper,
            pb::KnownEffect::TypescriptMetadataDecorateHelper => {
                Self::TypescriptMetadataDecorateHelper
            }
            pb::KnownEffect::TypescriptMetadataDecorateHelperWithMemberDecorators => {
                Self::TypescriptMetadataDecorateHelperWithMemberDecorators
            }
            pb::KnownEffect::TypescriptMetadataDecorateHelperThreeArgs => {
                Self::TypescriptMetadataDecorateHelperThreeArgs
            }
            pb::KnownEffect::Unspecified => bail!("unspecified known effect"),
        })
    }
}
impl AnalysisWire for AnalysisAssumptions {
    type Message = pb::AnalysisAssumptions;
    fn to_wire(&self) -> Result<Self::Message> {
        let Self {
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
            captured_value_inputs,
            native_operation_inputs,
            initializer_call_effect_inputs,
            imported_primitive_array_spread_inputs,
            argument_inputs,
        } = self;
        ensure!(
            captured_value_inputs.is_empty()
                && native_operation_inputs.is_empty()
                && initializer_call_effect_inputs.is_empty()
                && imported_primitive_array_spread_inputs.is_empty()
                && argument_inputs.is_empty(),
            "declared assumptions carry source-resolved inputs"
        );
        Ok(pb::AnalysisAssumptions {
            retained_record_factories: retained_record_factories.to_proto(),
            reentry_free_operations: reentry_free_operations.to_proto(),
            predefined_globals: Some(predefined_globals.to_proto()),
            bundle_globals: Some(bundle_globals.to_wire()?),
            deferred_global_publications: spec_wire::global_names_to_proto(
                deferred_global_publications,
            ),
            non_reentrant_global_membership: spec_wire::global_names_to_proto(
                non_reentrant_global_membership,
            ),
            declared_pure: declared_pure.to_proto(),
            declared_pure_new: declared_pure_new.to_proto(),
            declared_pure_initializers: declared_pure_initializers.to_proto(),
            declared_pure_members: spec_wire::member_names_to_proto(declared_pure_members),
            no_sync_callback_members: spec_wire::member_names_to_proto(no_sync_callback_members),
            known_effects: known_effects.to_wire()?,
            trusted_dataflow_summaries: Some(*trusted_dataflow_summaries),
            inline_callback_factories: inline_callback_factories.to_proto(),
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
            no_reentry_exports: no_reentry_exports.to_proto(),
            no_reentry_callback_exports: spec_wire::callback_overloads_to_proto(
                no_reentry_callback_exports,
            ),
            no_reentry_statement_calls: no_reentry_statement_calls.to_wire()?,
            no_reentry_members: spec_wire::member_names_to_proto(no_reentry_members),
            no_reentry_record_members: spec_wire::member_names_to_proto(no_reentry_record_members),
            namespace_independent_members: spec_wire::member_names_to_proto(
                namespace_independent_members,
            ),
            fluent_bindings: fluent_bindings.to_proto(),
        })
    }
    fn from_wire(v: Self::Message) -> Result<Self> {
        Ok(Self {
            retained_record_factories: Wire::from_proto(v.retained_record_factories)?,
            reentry_free_operations: Wire::from_proto(v.reentry_free_operations)?,
            predefined_globals: required(v.predefined_globals)?,
            bundle_globals: analysis_required(v.bundle_globals)?,
            deferred_global_publications: spec_wire::global_names_from_proto(
                v.deferred_global_publications,
            )?,
            non_reentrant_global_membership: spec_wire::global_names_from_proto(
                v.non_reentrant_global_membership,
            )?,
            declared_pure: Wire::from_proto(v.declared_pure)?,
            declared_pure_new: Wire::from_proto(v.declared_pure_new)?,
            declared_pure_initializers: Wire::from_proto(v.declared_pure_initializers)?,
            declared_pure_members: spec_wire::member_names_from_proto(v.declared_pure_members)?,
            no_sync_callback_members: spec_wire::member_names_from_proto(
                v.no_sync_callback_members,
            )?,
            known_effects: AnalysisWire::from_wire(v.known_effects)?,
            trusted_dataflow_summaries: required(v.trusted_dataflow_summaries)?,
            inline_callback_factories: Wire::from_proto(v.inline_callback_factories)?,
            callback_factory_members: spec_wire::member_names_from_proto(
                v.callback_factory_members,
            )?,
            retained_callback_factories: spec_wire::callback_overloads_from_proto(
                v.retained_callback_factories,
            )?,
            retained_callback_factory_members: spec_wire::member_callback_overloads_from_proto(
                v.retained_callback_factory_members,
            )?,
            retained_callback_constructors: spec_wire::callback_overloads_from_proto(
                v.retained_callback_constructors,
            )?,
            no_reentry_exports: Wire::from_proto(v.no_reentry_exports)?,
            no_reentry_callback_exports: spec_wire::callback_overloads_from_proto(
                v.no_reentry_callback_exports,
            )?,
            no_reentry_statement_calls: AnalysisWire::from_wire(v.no_reentry_statement_calls)?,
            no_reentry_members: spec_wire::member_names_from_proto(v.no_reentry_members)?,
            no_reentry_record_members: spec_wire::member_names_from_proto(
                v.no_reentry_record_members,
            )?,
            namespace_independent_members: spec_wire::member_names_from_proto(
                v.namespace_independent_members,
            )?,
            fluent_bindings: Wire::from_proto(v.fluent_bindings)?,
            captured_value_inputs: Vec::new(),
            native_operation_inputs: Vec::new(),
            initializer_call_effect_inputs: Vec::new(),
            imported_primitive_array_spread_inputs: Vec::new(),
            argument_inputs: Vec::new(),
        })
    }
}
impl AnalysisWire for SourceDeclaration {
    type Message = pb::SourceDeclaration;
    fn to_wire(&self) -> Result<Self::Message> {
        Ok(pb::SourceDeclaration {
            binding: Some(self.binding.clone()),
            body_index: Some(self.body_index.to_proto()),
        })
    }
    fn from_wire(v: Self::Message) -> Result<Self> {
        Ok(Self {
            binding: required(v.binding)?,
            body_index: required(v.body_index)?,
        })
    }
}
impl AnalysisWire for AnalysisImportedPrimitiveArrayOperand {
    type Message = pb::AnalysisImportedPrimitiveArrayOperand;
    fn to_wire(&self) -> Result<Self::Message> {
        Ok(pb::AnalysisImportedPrimitiveArrayOperand {
            selector_local: Some(self.selector_local.clone()),
            consumer_binding: Some(self.consumer_binding.clone()),
            provider_chunk: Some(self.provider_chunk.clone()),
            provider_source_match: Some(self.provider_source_match.to_proto()),
            provider_declaration: Some(self.provider_declaration.to_wire()?),
            audited_provider_source_sha256: Some(self.audited_provider_source_sha256.clone()),
        })
    }
    fn from_wire(v: Self::Message) -> Result<Self> {
        Ok(Self {
            selector_local: required(v.selector_local)?,
            consumer_binding: required(v.consumer_binding)?,
            provider_chunk: required(v.provider_chunk)?,
            provider_source_match: required(v.provider_source_match)?,
            provider_declaration: analysis_required(v.provider_declaration)?,
            audited_provider_source_sha256: required(v.audited_provider_source_sha256)?,
        })
    }
}
impl AnalysisWire for AnalysisImportedPrimitiveArraySpreadInput {
    type Message = pb::AnalysisImportedPrimitiveArraySpreadInput;
    fn to_wire(&self) -> Result<Self::Message> {
        ensure!(
            self.note.is_none(),
            "report-only imported array note must not enter analysis projection"
        );
        Ok(pb::AnalysisImportedPrimitiveArraySpreadInput {
            consumer_declaration: Some(self.consumer_declaration.to_wire()?),
            source_match: Some(self.source_match.to_proto()),
            audited_source_sha256: Some(self.audited_source_sha256.clone()),
            operands: self.operands.to_wire()?,
            lifetime: Some(self.lifetime.to_proto()),
        })
    }
    fn from_wire(v: Self::Message) -> Result<Self> {
        Ok(Self {
            consumer_declaration: analysis_required(v.consumer_declaration)?,
            source_match: required(v.source_match)?,
            audited_source_sha256: required(v.audited_source_sha256)?,
            operands: AnalysisWire::from_wire(v.operands)?,
            lifetime: required(v.lifetime)?,
            note: None,
        })
    }
}
impl AnalysisWire for crate::AnalysisSourceSites {
    type Message = pb::AnalysisSourceSites;
    fn to_wire(&self) -> Result<Self::Message> {
        Ok(pb::AnalysisSourceSites {
            source_sha256: self.source_sha256.to_proto(),
            value_inputs: source_declarations_to_wire(&self.value_inputs)?,
            native_call_identity_inputs: source_declarations_to_wire(
                &self.native_call_identity_inputs,
            )?,
            initializer_call_effect_inputs: source_declarations_to_wire(
                &self.initializer_call_effect_inputs,
            )?,
            argument_inputs: source_declarations_to_wire(&self.argument_inputs)?,
            no_accessor_install_callables: source_declarations_to_wire(
                &self.no_accessor_install_callables,
            )?,
            imported_primitive_array_spread_inputs: self
                .imported_primitive_array_spread_inputs
                .iter()
                .map(|(key, inputs)| {
                    Ok((
                        key.clone(),
                        pb::ChunkImportedPrimitiveArrayInputs {
                            inputs: inputs.to_wire()?,
                        },
                    ))
                })
                .collect::<Result<_>>()?,
        })
    }
    fn from_wire(v: Self::Message) -> Result<Self> {
        Ok(Self {
            source_sha256: Wire::from_proto(v.source_sha256)?,
            value_inputs: source_declarations_from_wire(v.value_inputs)?,
            native_call_identity_inputs: source_declarations_from_wire(
                v.native_call_identity_inputs,
            )?,
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
                .map(|(key, value)| Ok((key, AnalysisWire::from_wire(value.inputs)?)))
                .collect::<Result<_>>()?,
        })
    }
}
impl AnalysisWire for NativeOperationSelectorProvenance {
    type Message = pb::NativeOperationSelectorProvenance;
    fn to_wire(&self) -> Result<Self::Message> {
        use pb::native_operation_selector_provenance::Kind;
        Ok(pb::NativeOperationSelectorProvenance {
            kind: Some(match self {
                Self::SourceMatch(v) => Kind::SourceMatch(v.to_proto()),
                Self::Declaration(v) => Kind::Declaration(v.to_wire()?),
                Self::ResolvedMember(v) => Kind::ResolvedMember(v.to_proto()),
            }),
        })
    }
    fn from_wire(v: Self::Message) -> Result<Self> {
        use pb::native_operation_selector_provenance::Kind;
        Ok(
            match v
                .kind
                .context("missing native operation selector provenance")?
            {
                Kind::SourceMatch(v) => Self::SourceMatch(Wire::from_proto(v)?),
                Kind::Declaration(v) => Self::Declaration(AnalysisWire::from_wire(v)?),
                Kind::ResolvedMember(v) => Self::ResolvedMember(Wire::from_proto(v)?),
            },
        )
    }
}
impl AnalysisWire for crate::AnalysisNativeOperationInput {
    type Message = pb::AnalysisNativeOperationInput;
    fn to_wire(&self) -> Result<Self::Message> {
        Ok(pb::AnalysisNativeOperationInput {
            declaration: Some(self.declaration.to_wire()?),
            selector: Some(self.selector.to_wire()?),
            operation: Some(self.operation as i32),
            audited_source_sha256: Some(self.audited_source_sha256.clone()),
        })
    }
    fn from_wire(v: Self::Message) -> Result<Self> {
        Ok(Self {
            declaration: analysis_required(v.declaration)?,
            selector: analysis_required(v.selector)?,
            operation: spec::NativeOperationKind::try_from(
                v.operation.context("missing native operation kind")?,
            )?,
            audited_source_sha256: required(v.audited_source_sha256)?,
        })
    }
}
pub(super) fn to_wire(value: &crate::AnalysisSpec) -> Result<pb::AnalysisSpec> {
    value.to_wire()
}
pub(super) fn from_wire(value: pb::AnalysisSpec) -> Result<crate::AnalysisSpec> {
    crate::AnalysisSpec::from_wire(value)
}
#[cfg(test)]
#[path = "analysis_projection_wire_test.rs"]
mod tests;
