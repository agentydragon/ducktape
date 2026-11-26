use analysis_vocabulary::inputs::{
    ArgumentInputRef, DescriptorCopyAuthorRef, HostBehaviorKey,
    ImportedPrimitiveArraySpreadInputRef, InitializerCallEffectInputRef, NativeOperationInputRef,
    NoAccessorInstallRef, ProofDependencies, ValueInputRef,
};
use anyhow::{Context, Result};
use saved_purity_proto::debundle::saved_purity as proto;

fn author_input(defining_chunk: &str, input_index: usize) -> proto::AuthorInputRef {
    proto::AuthorInputRef {
        defining_chunk: defining_chunk.to_owned(),
        input_index: input_index as u64,
    }
}

pub(super) fn encode(dependencies: &ProofDependencies) -> proto::ProofDependencies {
    proto::ProofDependencies {
        value_inputs: dependencies
            .value_inputs
            .iter()
            .map(|input| author_input(&input.defining_chunk, input.input_index))
            .collect(),
        native_operation_inputs: dependencies
            .native_operation_inputs
            .iter()
            .map(|input| author_input(&input.defining_chunk, input.input_index))
            .collect(),
        initializer_call_effect_inputs: dependencies
            .initializer_call_effect_inputs
            .iter()
            .map(|input| author_input(&input.defining_chunk, input.input_index))
            .collect(),
        imported_primitive_array_spread_inputs: dependencies
            .imported_primitive_array_spread_inputs
            .iter()
            .map(|input| author_input(&input.defining_chunk, input.input_index))
            .collect(),
        descriptor_copy_authors: dependencies
            .descriptor_copy_authors
            .iter()
            .map(|input| proto::DescriptorCopyAuthorRef {
                defining_module: input.defining_module.clone(),
                export: input.export.clone(),
                spec_location: input.spec_location.clone(),
            })
            .collect(),
        argument_inputs: dependencies
            .argument_inputs
            .iter()
            .map(|input| author_input(&input.defining_chunk, input.input_index))
            .collect(),
        host_behavior: dependencies
            .host_behavior
            .iter()
            .map(encode_host_behavior)
            .collect(),
        program_premises: dependencies
            .program_premises
            .iter()
            .map(|key| *key as i32)
            .collect(),
        no_accessor_install_callables: dependencies
            .no_accessor_install_callables
            .iter()
            .map(|input| author_input(&input.defining_chunk, input.input_index))
            .collect(),
    }
}

pub(super) fn decode(dependencies: proto::ProofDependencies) -> Result<ProofDependencies> {
    Ok(ProofDependencies {
        value_inputs: dependencies
            .value_inputs
            .into_iter()
            .map(|input| {
                Ok(ValueInputRef {
                    defining_chunk: input.defining_chunk,
                    input_index: input.input_index.try_into()?,
                })
            })
            .collect::<Result<_>>()?,
        native_operation_inputs: dependencies
            .native_operation_inputs
            .into_iter()
            .map(|input| {
                Ok(NativeOperationInputRef {
                    defining_chunk: input.defining_chunk,
                    input_index: input.input_index.try_into()?,
                })
            })
            .collect::<Result<_>>()?,
        initializer_call_effect_inputs: dependencies
            .initializer_call_effect_inputs
            .into_iter()
            .map(|input| {
                Ok(InitializerCallEffectInputRef {
                    defining_chunk: input.defining_chunk,
                    input_index: input.input_index.try_into()?,
                })
            })
            .collect::<Result<_>>()?,
        imported_primitive_array_spread_inputs: dependencies
            .imported_primitive_array_spread_inputs
            .into_iter()
            .map(|input| {
                Ok(ImportedPrimitiveArraySpreadInputRef {
                    defining_chunk: input.defining_chunk,
                    input_index: input.input_index.try_into()?,
                })
            })
            .collect::<Result<_>>()?,
        descriptor_copy_authors: dependencies
            .descriptor_copy_authors
            .into_iter()
            .map(|input| DescriptorCopyAuthorRef {
                defining_module: input.defining_module,
                export: input.export,
                spec_location: input.spec_location,
            })
            .collect(),
        argument_inputs: dependencies
            .argument_inputs
            .into_iter()
            .map(|input| {
                Ok(ArgumentInputRef {
                    defining_chunk: input.defining_chunk,
                    input_index: input.input_index.try_into()?,
                })
            })
            .collect::<Result<_>>()?,
        host_behavior: dependencies
            .host_behavior
            .into_iter()
            .map(decode_host_behavior)
            .collect::<Result<_>>()?,
        program_premises: dependencies
            .program_premises
            .into_iter()
            .map(proto::ProgramPremiseKey::try_from)
            .collect::<std::result::Result<_, _>>()?,
        no_accessor_install_callables: dependencies
            .no_accessor_install_callables
            .into_iter()
            .map(|input| {
                Ok(NoAccessorInstallRef {
                    defining_chunk: input.defining_chunk,
                    input_index: input.input_index.try_into()?,
                })
            })
            .collect::<Result<_>>()?,
    })
}

fn encode_host_behavior(key: &HostBehaviorKey) -> proto::HostBehaviorKey {
    use proto::HostBehaviorFlag;
    use proto::host_behavior_key::Key;
    proto::HostBehaviorKey {
        key: Some(match key {
            HostBehaviorKey::LoaderResultForEvaluatingModule => {
                Key::Flag(HostBehaviorFlag::LoaderResultForEvaluatingModule as i32)
            }
            HostBehaviorKey::SynchronousLoadRunsAppCallbacks => {
                Key::Flag(HostBehaviorFlag::SynchronousLoadRunsAppCallbacks as i32)
            }
            HostBehaviorKey::SynchronousLoadDrainsMicrotasks => {
                Key::Flag(HostBehaviorFlag::SynchronousLoadDrainsMicrotasks as i32)
            }
            HostBehaviorKey::PredefinedGlobals => {
                Key::Flag(HostBehaviorFlag::PredefinedGlobals as i32)
            }
            HostBehaviorKey::ReentryFreeBuiltinModule(module) => Key::BuiltinModule(*module as i32),
            HostBehaviorKey::ReentryFreeBunModule(module) => Key::BunModule(*module as i32),
            HostBehaviorKey::ReentryFreeEmbeddedLoader(loader) => {
                Key::EmbeddedLoader(*loader as i32)
            }
            HostBehaviorKey::ReentryFreeOperation(operation) => Key::Operation(*operation as i32),
        }),
    }
}

fn decode_host_behavior(key: proto::HostBehaviorKey) -> Result<HostBehaviorKey> {
    use proto::HostBehaviorFlag;
    use proto::host_behavior_key::Key;
    Ok(match key.key.context("missing saved host behavior key")? {
        Key::Flag(flag) => match HostBehaviorFlag::try_from(flag)? {
            HostBehaviorFlag::LoaderResultForEvaluatingModule => {
                HostBehaviorKey::LoaderResultForEvaluatingModule
            }
            HostBehaviorFlag::SynchronousLoadRunsAppCallbacks => {
                HostBehaviorKey::SynchronousLoadRunsAppCallbacks
            }
            HostBehaviorFlag::SynchronousLoadDrainsMicrotasks => {
                HostBehaviorKey::SynchronousLoadDrainsMicrotasks
            }
            HostBehaviorFlag::PredefinedGlobals => HostBehaviorKey::PredefinedGlobals,
        },
        Key::BuiltinModule(module) => {
            HostBehaviorKey::ReentryFreeBuiltinModule(spec::NodeBuiltinModule::try_from(module)?)
        }
        Key::BunModule(module) => {
            HostBehaviorKey::ReentryFreeBunModule(spec::BunModule::try_from(module)?)
        }
        Key::EmbeddedLoader(loader) => {
            HostBehaviorKey::ReentryFreeEmbeddedLoader(spec::EmbeddedLoader::try_from(loader)?)
        }
        Key::Operation(operation) => {
            HostBehaviorKey::ReentryFreeOperation(spec::ReentryFreeOperation::try_from(operation)?)
        }
    })
}
