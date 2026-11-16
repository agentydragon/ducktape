//! Explicit protobuf conversion for persisted imported-callable capabilities.

use std::collections::BTreeMap;

use anyhow::{Context, Result};
use prost::Message;
use saved_callable_proto::debundle::saved_callable as proto;
use saved_purity_proto::debundle::saved_purity as purity_proto;

use crate::analysis_inputs::{
    ArgumentInputRef, CallableSummary, CallbackFactoryEvidence, CallbackObjectArguments,
    DeferredRecordMember, DescriptorCopyAuthorRef, DescriptorCopyFactorySummary,
    ForeignCallSummary, ForeignInvocation, ForeignReturn, NamespaceWrapperSummary, NativeIntrinsic,
    ReturnedRecordSummary, ValueInputRef,
};
use crate::analysis_provenance::{InferenceRule, InferenceUse};
use crate::purity::saved as saved_purity;
use spec_wire::{
    decode_descriptor_copy_factory_signature, encode_descriptor_copy_factory_signature,
};

pub(crate) fn encode_callback_factory_evidence(
    evidence: &CallbackFactoryEvidence,
) -> proto::CallbackFactoryEvidence {
    use proto::callback_factory_evidence::Origin;
    proto::CallbackFactoryEvidence {
        origin: Some(match evidence {
            CallbackFactoryEvidence::AuthorAssertion {
                defining_module,
                export,
            } => Origin::AuthorAssertion(proto::AuthorAssertion {
                defining_module: defining_module.clone(),
                export: export.clone(),
            }),
            CallbackFactoryEvidence::InferredImmutableWrapper {
                defining_module,
                export,
                local,
                source_location,
            } => Origin::InferredImmutableWrapper(proto::InferredImmutableWrapper {
                defining_module: defining_module.clone(),
                export: export.clone(),
                local: local.clone(),
                source_location: source_location
                    .as_ref()
                    .map(saved_purity::encode_source_location),
            }),
        }),
    }
}

pub(crate) fn decode_callback_factory_evidence(
    evidence: proto::CallbackFactoryEvidence,
) -> Result<CallbackFactoryEvidence> {
    use proto::callback_factory_evidence::Origin;
    Ok(
        match evidence
            .origin
            .context("missing saved callback factory evidence origin")?
        {
            Origin::AuthorAssertion(assertion) => CallbackFactoryEvidence::AuthorAssertion {
                defining_module: assertion.defining_module,
                export: assertion.export,
            },
            Origin::InferredImmutableWrapper(wrapper) => {
                CallbackFactoryEvidence::InferredImmutableWrapper {
                    defining_module: wrapper.defining_module,
                    export: wrapper.export,
                    local: wrapper.local,
                    source_location: wrapper
                        .source_location
                        .map(saved_purity::decode_source_location)
                        .transpose()?,
                }
            }
        },
    )
}

pub(crate) fn encode_callback_object_arguments(
    arguments: &CallbackObjectArguments,
) -> proto::CallbackObjectArguments {
    proto::CallbackObjectArguments {
        wrapper: Some(encode_callback_factory_evidence(&arguments.wrapper)),
        object_arguments: arguments
            .object_arguments
            .iter()
            .map(|index| u64::try_from(*index).expect("callback argument index fits uint64"))
            .collect(),
    }
}

pub(crate) fn decode_callback_object_arguments(
    arguments: proto::CallbackObjectArguments,
) -> Result<CallbackObjectArguments> {
    Ok(CallbackObjectArguments {
        wrapper: decode_callback_factory_evidence(
            arguments
                .wrapper
                .context("missing saved callback object-arguments wrapper")?,
        )?,
        object_arguments: arguments
            .object_arguments
            .into_iter()
            .map(usize::try_from)
            .collect::<std::result::Result<_, _>>()?,
    })
}

pub fn encode_callable_table(
    callables: &BTreeMap<String, BTreeMap<String, CallableSummary>>,
) -> Vec<u8> {
    proto::CallableTable {
        chunks_by_id: callables
            .iter()
            .map(|(chunk, callables)| {
                (
                    chunk.clone(),
                    proto::CallableChunk {
                        callables_by_local: callables
                            .iter()
                            .map(|(local, callable)| {
                                (local.clone(), encode_callable_summary(callable))
                            })
                            .collect(),
                    },
                )
            })
            .collect(),
    }
    .encode_to_vec()
}

pub fn decode_callable_table(
    bytes: &[u8],
) -> Result<BTreeMap<String, BTreeMap<String, CallableSummary>>> {
    proto::CallableTable::decode(bytes)
        .context("decode saved callable table protobuf")?
        .chunks_by_id
        .into_iter()
        .map(|(chunk, saved)| {
            Ok((
                chunk,
                saved
                    .callables_by_local
                    .into_iter()
                    .map(|(local, callable)| Ok((local, decode_callable_summary(callable)?)))
                    .collect::<Result<_>>()?,
            ))
        })
        .collect()
}

fn encode_callable_summary(summary: &CallableSummary) -> proto::CallableSummary {
    proto::CallableSummary {
        callback_factory_evidence: summary
            .callback_factory_evidence()
            .iter()
            .map(encode_callback_factory_evidence)
            .collect(),
        returned_record: summary.returned_record.as_ref().map(encode_returned_record),
        namespace_wrapper: summary
            .namespace_wrapper
            .as_ref()
            .map(encode_namespace_wrapper),
        callback_object_arguments: summary
            .callback_object_arguments
            .as_ref()
            .map(encode_callback_object_arguments),
        descriptor_copy_factory: summary
            .descriptor_copy_factory
            .as_ref()
            .map(encode_descriptor_copy_factory),
        foreign_call: summary.foreign_call.as_ref().map(encode_foreign_call),
        may_read_importer_cells: summary.may_read_importer_cells.iter().cloned().collect(),
    }
}

fn decode_callable_summary(saved: proto::CallableSummary) -> Result<CallableSummary> {
    let mut summary = CallableSummary::default();
    for evidence in saved.callback_factory_evidence {
        summary.merge(CallableSummary::callback_factory(
            decode_callback_factory_evidence(evidence)?,
        ));
    }
    summary.returned_record = saved
        .returned_record
        .map(decode_returned_record)
        .transpose()?;
    summary.namespace_wrapper = saved
        .namespace_wrapper
        .map(decode_namespace_wrapper)
        .transpose()?;
    summary.callback_object_arguments = saved
        .callback_object_arguments
        .map(decode_callback_object_arguments)
        .transpose()?;
    summary.descriptor_copy_factory = saved
        .descriptor_copy_factory
        .map(decode_descriptor_copy_factory)
        .transpose()?;
    summary.foreign_call = saved.foreign_call.map(decode_foreign_call).transpose()?;
    summary.may_read_importer_cells = saved.may_read_importer_cells.into_iter().collect();
    Ok(summary)
}

fn encode_returned_record(summary: &ReturnedRecordSummary) -> proto::ReturnedRecordSummary {
    proto::ReturnedRecordSummary {
        defining_module: summary.defining_module.clone(),
        export: summary.export.clone(),
        local: summary.local.clone(),
        source_location: summary
            .source_location
            .as_ref()
            .map(saved_purity::encode_source_location),
        members: summary
            .members
            .iter()
            .map(encode_deferred_record_member)
            .collect(),
        inference: Some(encode_inference_use(&summary.inference)),
        replacement_package: summary.replacement_package.clone(),
    }
}

fn decode_returned_record(saved: proto::ReturnedRecordSummary) -> Result<ReturnedRecordSummary> {
    Ok(ReturnedRecordSummary {
        defining_module: saved.defining_module,
        export: saved.export,
        local: saved.local,
        source_location: saved
            .source_location
            .map(saved_purity::decode_source_location)
            .transpose()?,
        members: saved
            .members
            .into_iter()
            .map(decode_deferred_record_member)
            .collect::<Result<_>>()?,
        inference: decode_inference_use(
            saved
                .inference
                .context("missing saved returned-record inference use")?,
        )?,
        replacement_package: saved.replacement_package,
    })
}

fn encode_deferred_record_member(member: &DeferredRecordMember) -> proto::DeferredRecordMember {
    proto::DeferredRecordMember {
        name: member.name.clone(),
        source_location: member
            .source_location
            .as_ref()
            .map(saved_purity::encode_source_location),
        captures: member.captures.iter().cloned().collect(),
    }
}

fn decode_deferred_record_member(
    saved: proto::DeferredRecordMember,
) -> Result<DeferredRecordMember> {
    Ok(DeferredRecordMember {
        name: saved.name,
        source_location: saved
            .source_location
            .map(saved_purity::decode_source_location)
            .transpose()?,
        captures: saved.captures.into_iter().collect(),
    })
}

fn encode_namespace_wrapper(summary: &NamespaceWrapperSummary) -> proto::NamespaceWrapperSummary {
    proto::NamespaceWrapperSummary {
        defining_module: summary.defining_module.clone(),
        export: summary.export.clone(),
        local: summary.local.clone(),
        source_location: summary
            .source_location
            .as_ref()
            .map(saved_purity::encode_source_location),
        inference: Some(encode_inference_use(&summary.inference)),
        dependencies: Some(saved_purity::dependencies::encode(&summary.dependencies)),
    }
}

fn decode_namespace_wrapper(
    saved: proto::NamespaceWrapperSummary,
) -> Result<NamespaceWrapperSummary> {
    Ok(NamespaceWrapperSummary {
        defining_module: saved.defining_module,
        export: saved.export,
        local: saved.local,
        source_location: saved
            .source_location
            .map(saved_purity::decode_source_location)
            .transpose()?,
        inference: decode_inference_use(
            saved
                .inference
                .context("missing saved namespace-wrapper inference use")?,
        )?,
        dependencies: saved_purity::dependencies::decode(
            saved
                .dependencies
                .context("missing saved namespace-wrapper dependencies")?,
        )?,
    })
}

fn encode_inference_use(inference: &InferenceUse) -> proto::InferenceUse {
    proto::InferenceUse {
        conclusion: Some(inference.conclusion as i32),
        derivation_rules: inference
            .derivation_rules
            .iter()
            .map(|rule| *rule as i32)
            .collect(),
        value_input_refs: inference
            .value_input_refs
            .iter()
            .map(|input| purity_proto::AuthorInputRef {
                defining_chunk: input.defining_chunk.clone(),
                input_index: input.input_index as u64,
            })
            .collect(),
        descriptor_copy_author_refs: inference
            .descriptor_copy_author_refs
            .iter()
            .map(|input| purity_proto::DescriptorCopyAuthorRef {
                defining_module: input.defining_module.clone(),
                export: input.export.clone(),
                spec_location: input.spec_location.clone(),
            })
            .collect(),
        argument_input_refs: inference
            .argument_input_refs
            .iter()
            .map(|input| purity_proto::AuthorInputRef {
                defining_chunk: input.defining_chunk.clone(),
                input_index: input.input_index as u64,
            })
            .collect(),
        native_operation_input_refs: inference
            .native_operation_input_refs
            .iter()
            .map(|input| purity_proto::AuthorInputRef {
                defining_chunk: input.defining_chunk.clone(),
                input_index: input.input_index as u64,
            })
            .collect(),
        imported_primitive_array_spread_input_refs: inference
            .imported_primitive_array_spread_input_refs
            .iter()
            .map(|input| purity_proto::AuthorInputRef {
                defining_chunk: input.defining_chunk.clone(),
                input_index: input.input_index as u64,
            })
            .collect(),
    }
}

fn decode_inference_use(saved: proto::InferenceUse) -> Result<InferenceUse> {
    Ok(InferenceUse {
        conclusion: InferenceRule::try_from(
            saved
                .conclusion
                .context("missing saved inference conclusion")?,
        )?,
        derivation_rules: saved
            .derivation_rules
            .into_iter()
            .map(InferenceRule::try_from)
            .collect::<std::result::Result<_, _>>()?,
        value_input_refs: saved
            .value_input_refs
            .into_iter()
            .map(|input| {
                Ok(ValueInputRef {
                    defining_chunk: input.defining_chunk,
                    input_index: input.input_index.try_into()?,
                })
            })
            .collect::<Result<_>>()?,
        descriptor_copy_author_refs: saved
            .descriptor_copy_author_refs
            .into_iter()
            .map(|input| DescriptorCopyAuthorRef {
                defining_module: input.defining_module,
                export: input.export,
                spec_location: input.spec_location,
            })
            .collect(),
        argument_input_refs: saved
            .argument_input_refs
            .into_iter()
            .map(|input| {
                Ok(ArgumentInputRef {
                    defining_chunk: input.defining_chunk,
                    input_index: input.input_index.try_into()?,
                })
            })
            .collect::<Result<_>>()?,
        native_operation_input_refs: saved
            .native_operation_input_refs
            .into_iter()
            .map(|input| {
                Ok(crate::analysis_inputs::NativeOperationInputRef {
                    defining_chunk: input.defining_chunk,
                    input_index: input.input_index.try_into()?,
                })
            })
            .collect::<Result<_>>()?,
        imported_primitive_array_spread_input_refs: saved
            .imported_primitive_array_spread_input_refs
            .into_iter()
            .map(|input| {
                Ok(
                    crate::analysis_inputs::ImportedPrimitiveArraySpreadInputRef {
                        defining_chunk: input.defining_chunk,
                        input_index: input.input_index.try_into()?,
                    },
                )
            })
            .collect::<Result<_>>()?,
    })
}

fn encode_descriptor_copy_factory(
    summary: &DescriptorCopyFactorySummary,
) -> proto::DescriptorCopyFactorySummary {
    proto::DescriptorCopyFactorySummary {
        defining_module: summary.defining_module.clone(),
        export: summary.export.clone(),
        author_location: summary.author_location.clone(),
        signatures: summary
            .signatures
            .iter()
            .map(encode_descriptor_copy_factory_signature)
            .collect(),
    }
}

fn decode_descriptor_copy_factory(
    saved: proto::DescriptorCopyFactorySummary,
) -> Result<DescriptorCopyFactorySummary> {
    Ok(DescriptorCopyFactorySummary {
        defining_module: saved.defining_module,
        export: saved.export,
        author_location: saved.author_location,
        signatures: saved
            .signatures
            .into_iter()
            .map(decode_descriptor_copy_factory_signature)
            .collect::<Result<_>>()?,
    })
}

fn encode_foreign_call(summary: &ForeignCallSummary) -> proto::ForeignCallSummary {
    proto::ForeignCallSummary {
        defining_module: summary.defining_module.clone(),
        export: summary.export.clone(),
        local: summary.local.clone(),
        source_location: summary
            .source_location
            .as_ref()
            .map(saved_purity::encode_source_location),
        invocation: Some(summary.invocation as i32),
        returns: summary.returns.iter().map(encode_foreign_return).collect(),
        retained_arguments: summary
            .retained_arguments
            .iter()
            .map(|index| u64::try_from(*index).expect("retained argument index fits uint64"))
            .collect(),
        retained_provider_captures: summary.retained_provider_captures.iter().cloned().collect(),
        provider_cell_reads: summary.provider_cell_reads.iter().cloned().collect(),
        native_constructions: summary
            .native_constructions
            .iter()
            .map(|native| *native as i32)
            .collect(),
        replacement_package: summary.replacement_package.clone(),
        dependencies: Some(saved_purity::dependencies::encode(&summary.dependencies)),
    }
}

fn decode_foreign_call(saved: proto::ForeignCallSummary) -> Result<ForeignCallSummary> {
    Ok(ForeignCallSummary {
        defining_module: saved.defining_module,
        export: saved.export,
        local: saved.local,
        source_location: saved
            .source_location
            .map(saved_purity::decode_source_location)
            .transpose()?,
        invocation: ForeignInvocation::try_from(
            saved
                .invocation
                .context("missing saved foreign invocation")?,
        )?,
        returns: saved
            .returns
            .into_iter()
            .map(decode_foreign_return)
            .collect::<Result<_>>()?,
        retained_arguments: saved
            .retained_arguments
            .into_iter()
            .map(usize::try_from)
            .collect::<std::result::Result<_, _>>()?,
        retained_provider_captures: saved.retained_provider_captures.into_iter().collect(),
        provider_cell_reads: saved.provider_cell_reads.into_iter().collect(),
        native_constructions: saved
            .native_constructions
            .into_iter()
            .map(NativeIntrinsic::try_from)
            .collect::<std::result::Result<_, _>>()?,
        replacement_package: saved.replacement_package,
        dependencies: saved_purity::dependencies::decode(
            saved
                .dependencies
                .context("missing saved foreign-call dependencies")?,
        )?,
    })
}

fn encode_foreign_return(value: &ForeignReturn) -> proto::ForeignReturn {
    use proto::foreign_return::Value;
    proto::ForeignReturn {
        value: Some(match value {
            ForeignReturn::Argument { index } => Value::ArgumentIndex(
                u64::try_from(*index).expect("foreign argument index fits uint64"),
            ),
            ForeignReturn::FreshAllocation => {
                Value::Simple(proto::ForeignReturnSimple::FreshAllocation as i32)
            }
            ForeignReturn::Primitive => Value::Simple(proto::ForeignReturnSimple::Primitive as i32),
        }),
    }
}

fn decode_foreign_return(saved: proto::ForeignReturn) -> Result<ForeignReturn> {
    use proto::foreign_return::Value;
    Ok(match saved.value.context("missing saved foreign return")? {
        Value::ArgumentIndex(index) => ForeignReturn::Argument {
            index: index.try_into()?,
        },
        Value::Simple(value) => match proto::ForeignReturnSimple::try_from(value)? {
            proto::ForeignReturnSimple::FreshAllocation => ForeignReturn::FreshAllocation,
            proto::ForeignReturnSimple::Primitive => ForeignReturn::Primitive,
        },
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeSet;

    use crate::analysis_inputs::ProofDependencies;
    use crate::execution_evidence::SourceLocation;

    #[test]
    fn callable_table_round_trips_capabilities_and_keeps_trusted_dependencies_out_of_reports() {
        let source_location = SourceLocation {
            source_path: "provider.js".into(),
            start_line: 7,
            end_line: 9,
            start_column: Some(2),
        };
        let inference = InferenceUse {
            conclusion: InferenceRule::NamespaceWrapper,
            derivation_rules: BTreeSet::from([
                InferenceRule::NamespaceWrapper,
                InferenceRule::NativeIntegrity,
            ]),
            value_input_refs: BTreeSet::from([ValueInputRef {
                defining_chunk: "provider".into(),
                input_index: 2,
            }]),
            descriptor_copy_author_refs: BTreeSet::from([DescriptorCopyAuthorRef {
                defining_module: "provider".into(),
                export: "copy".into(),
                spec_location: "descriptor_copy".into(),
            }]),
            argument_input_refs: BTreeSet::from([ArgumentInputRef {
                defining_chunk: "consumer".into(),
                input_index: 3,
            }]),
            native_operation_input_refs: BTreeSet::from([
                crate::analysis_inputs::NativeOperationInputRef {
                    defining_chunk: "consumer".into(),
                    input_index: 4,
                },
            ]),
            imported_primitive_array_spread_input_refs: BTreeSet::from([
                crate::analysis_inputs::ImportedPrimitiveArraySpreadInputRef {
                    defining_chunk: "provider".into(),
                    input_index: 5,
                },
            ]),
        };
        let namespace_dependencies = ProofDependencies {
            value_inputs: BTreeSet::from([ValueInputRef {
                defining_chunk: "provider".into(),
                input_index: 2,
            }]),
            ..ProofDependencies::default()
        };
        let foreign_dependencies = ProofDependencies {
            value_inputs: BTreeSet::from([ValueInputRef {
                defining_chunk: "provider".into(),
                input_index: 4,
            }]),
            ..ProofDependencies::default()
        };
        let mut summary = CallableSummary::namespace_wrapper(NamespaceWrapperSummary {
            defining_module: "provider".into(),
            export: "wrap".into(),
            local: "wrap".into(),
            source_location: Some(source_location.clone()),
            inference: inference.clone(),
            dependencies: namespace_dependencies,
        });
        summary.merge(CallableSummary::callback_factory(
            CallbackFactoryEvidence::AuthorAssertion {
                defining_module: "provider".into(),
                export: "factory".into(),
            },
        ));
        summary.merge(CallableSummary::callback_factory(
            CallbackFactoryEvidence::InferredImmutableWrapper {
                defining_module: "provider".into(),
                export: "make".into(),
                local: "factory".into(),
                source_location: Some(source_location.clone()),
            },
        ));
        summary.callback_object_arguments = Some(CallbackObjectArguments {
            wrapper: CallbackFactoryEvidence::InferredImmutableWrapper {
                defining_module: "provider".into(),
                export: "make".into(),
                local: "factory".into(),
                source_location: Some(source_location.clone()),
            },
            object_arguments: BTreeSet::from([0, 2]),
        });
        summary.returned_record = Some(ReturnedRecordSummary {
            defining_module: "provider".into(),
            export: "record".into(),
            local: "record".into(),
            source_location: Some(source_location.clone()),
            members: vec![DeferredRecordMember {
                name: "run".into(),
                source_location: Some(source_location.clone()),
                captures: BTreeSet::from(["state".into(), "helper".into()]),
            }],
            inference: inference.clone(),
            replacement_package: Some("replacement".into()),
        });
        summary.descriptor_copy_factory = Some(DescriptorCopyFactorySummary {
            defining_module: "provider".into(),
            export: "copy".into(),
            author_location: "descriptor_copy".into(),
            signatures: vec![spec::DescriptorCopyFactorySignature {
                argument_count: 2,
                object_argument: 1,
                eager_own_data_keys: BTreeSet::from(["value".into()]),
                assigned_data_keys: BTreeSet::from(["result".into()]),
            }],
        });
        summary.foreign_call = Some(ForeignCallSummary {
            defining_module: "provider".into(),
            export: "invoke".into(),
            local: "invoke".into(),
            source_location: Some(source_location),
            invocation: ForeignInvocation::Construct,
            returns: BTreeSet::from([
                ForeignReturn::Argument { index: 1 },
                ForeignReturn::FreshAllocation,
                ForeignReturn::Primitive,
            ]),
            retained_arguments: BTreeSet::from([0, 2]),
            retained_provider_captures: BTreeSet::from(["captured".into()]),
            provider_cell_reads: BTreeSet::from(["cell".into()]),
            native_constructions: BTreeSet::from([NativeIntrinsic::WeakMap]),
            replacement_package: Some("foreign-replacement".into()),
            dependencies: foreign_dependencies,
        });
        summary
            .may_read_importer_cells
            .extend(["outer".into(), "inner".into()]);

        let report = serde_json::to_value(&summary).expect("callable report serializes");
        assert!(report["namespace_wrapper"].get("dependencies").is_none());
        assert!(report["foreign_call"].get("dependencies").is_none());

        let callables = BTreeMap::from([(
            "consumer".into(),
            BTreeMap::from([("local".into(), summary.clone())]),
        )]);
        let restored = decode_callable_table(&encode_callable_table(&callables))
            .expect("valid callable table round-trips");
        assert_eq!(restored, callables);
        assert_eq!(
            serde_json::to_value(&restored["consumer"]["local"]).expect("restored report"),
            report
        );

        let absent_capability = CallableSummary::default();
        let restored = decode_callable_table(&encode_callable_table(&BTreeMap::from([(
            "consumer".into(),
            BTreeMap::from([("empty".into(), absent_capability.clone())]),
        )])))
        .expect("empty callable fields round-trip");
        assert_eq!(restored["consumer"]["empty"], absent_capability);
    }
}
