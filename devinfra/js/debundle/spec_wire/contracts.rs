use super::required;
use super::selectors::{decode_source_match_claim, encode_source_match_claim};
use anyhow::{Context, Result};
use spec_wire_proto::debundle::spec as pb;

pub fn encode_value_input_contract(
    value: &spec::input_contracts::ValueInputContract,
) -> pb::ValueInputContract {
    pb::ValueInputContract {
        source_match: Some(encode_source_match_claim(&value.source_match)),
        value: Some(encode_captured_value_contract(&value.value)),
        audited_source_sha256: Some(value.audited_source_sha256.clone()),
        expected_export: value.expected_export.clone(),
        note: value.note.clone(),
    }
}

pub fn decode_value_input_contract(
    v: pb::ValueInputContract,
) -> Result<spec::input_contracts::ValueInputContract> {
    Ok(spec::input_contracts::ValueInputContract {
        source_match: decode_source_match_claim(required(v.source_match)?)?,
        value: decode_captured_value_contract(required(v.value)?)?,
        audited_source_sha256: required(v.audited_source_sha256)?,
        expected_export: v.expected_export,
        note: v.note,
    })
}

pub fn encode_captured_value_contract(
    value: &spec::input_contracts::CapturedValueContract,
) -> pb::CapturedValueContract {
    pb::CapturedValueContract {
        kind: Some(value.kind as i32),
    }
}

pub fn decode_captured_value_contract(
    v: pb::CapturedValueContract,
) -> Result<spec::input_contracts::CapturedValueContract> {
    Ok(spec::input_contracts::CapturedValueContract {
        kind: pb::CapturedValueKind::try_from(
            v.kind.context("missing required projection field")?,
        )?,
    })
}

pub fn encode_native_operation_input_contract(
    value: &spec::input_contracts::NativeOperationInputContract,
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
) -> Result<spec::input_contracts::NativeOperationInputContract> {
    Ok(spec::input_contracts::NativeOperationInputContract {
        selector: decode_source_unit_selector(required(v.selector)?)?,
        operation: pb::NativeOperationKind::try_from(
            v.operation.context("missing required projection field")?,
        )?,
        audited_source_sha256: required(v.audited_source_sha256)?,
        note: v.note,
    })
}

pub fn encode_source_unit_selector(
    value: &spec::input_contracts::SourceUnitSelector,
) -> pb::SourceUnitSelector {
    use pb::source_unit_selector::Value;
    pb::SourceUnitSelector {
        value: Some(match value {
            spec::input_contracts::SourceUnitSelector::SourceMatch(v) => {
                Value::SourceMatch(encode_source_match_claim(v))
            }
            spec::input_contracts::SourceUnitSelector::ResolvedMember(v) => {
                Value::ResolvedMember(v.clone())
            }
        }),
    }
}

pub fn decode_source_unit_selector(
    v: pb::SourceUnitSelector,
) -> Result<spec::input_contracts::SourceUnitSelector> {
    use pb::source_unit_selector::Value;
    Ok(match v.value.context("missing source unit selector")? {
        Value::SourceMatch(v) => {
            spec::input_contracts::SourceUnitSelector::SourceMatch(decode_source_match_claim(v)?)
        }
        Value::ResolvedMember(v) => spec::input_contracts::SourceUnitSelector::ResolvedMember(v),
    })
}

pub fn encode_initializer_call_effect_input_contract(
    value: &spec::input_contracts::InitializerCallEffectInputContract,
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
) -> Result<spec::input_contracts::InitializerCallEffectInputContract> {
    Ok(spec::input_contracts::InitializerCallEffectInputContract {
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
    value: &spec::input_contracts::InitializerCallEffectiveReplacement,
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
) -> Result<spec::input_contracts::InitializerCallEffectiveReplacement> {
    Ok(spec::input_contracts::InitializerCallEffectiveReplacement {
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
    value: &spec::input_contracts::ImportedPrimitiveArraySpreadInputContract,
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
) -> Result<spec::input_contracts::ImportedPrimitiveArraySpreadInputContract> {
    Ok(
        spec::input_contracts::ImportedPrimitiveArraySpreadInputContract {
            source_match: decode_source_match_claim(required(v.source_match)?)?,
            operands: v
                .operands
                .into_iter()
                .map(decode_imported_primitive_array_operand_contract)
                .collect::<Result<_>>()?,
            audited_source_sha256: required(v.audited_source_sha256)?,
            lifetime: decode_imported_primitive_array_lifetime_contract(required(v.lifetime)?)?,
            note: v.note,
        },
    )
}

pub fn encode_imported_primitive_array_operand_contract(
    value: &spec::input_contracts::ImportedPrimitiveArrayOperandContract,
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
) -> Result<spec::input_contracts::ImportedPrimitiveArrayOperandContract> {
    Ok(
        spec::input_contracts::ImportedPrimitiveArrayOperandContract {
            selector_local: required(v.selector_local)?,
            provider_chunk: required(v.provider_chunk)?,
            provider_source_match: decode_source_match_claim(required(v.provider_source_match)?)?,
            audited_provider_source_sha256: required(v.audited_provider_source_sha256)?,
        },
    )
}

pub fn encode_imported_primitive_array_lifetime_contract(
    value: &spec::input_contracts::ImportedPrimitiveArrayLifetimeContract,
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
) -> Result<spec::input_contracts::ImportedPrimitiveArrayLifetimeContract> {
    Ok(
        spec::input_contracts::ImportedPrimitiveArrayLifetimeContract {
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
        },
    )
}

pub fn encode_commuting_statements_contract(
    value: &spec::input_contracts::CommutingStatementsContract,
) -> pb::CommutingStatementsContract {
    pb::CommutingStatementsContract {
        a: Some(encode_source_unit_selector(&value.a)),
        b: Some(encode_source_unit_selector(&value.b)),
        note: value.note.clone(),
    }
}

pub fn decode_commuting_statements_contract(
    v: pb::CommutingStatementsContract,
) -> Result<spec::input_contracts::CommutingStatementsContract> {
    Ok(spec::input_contracts::CommutingStatementsContract {
        a: decode_source_unit_selector(required(v.a)?)?,
        b: decode_source_unit_selector(required(v.b)?)?,
        note: v.note,
    })
}

pub fn encode_native_call_identity_input_contract(
    value: &spec::input_contracts::NativeCallIdentityInputContract,
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
) -> Result<spec::input_contracts::NativeCallIdentityInputContract> {
    Ok(spec::input_contracts::NativeCallIdentityInputContract {
        source_match: decode_source_match_claim(required(v.source_match)?)?,
        callee: pb::NativeCallIdentityKind::try_from(
            v.callee.context("missing required projection field")?,
        )?,
        audited_source_sha256: required(v.audited_source_sha256)?,
        note: v.note,
    })
}

pub fn encode_argument_input_contract(
    value: &spec::input_contracts::ArgumentInputContract,
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
) -> Result<spec::input_contracts::ArgumentInputContract> {
    Ok(spec::input_contracts::ArgumentInputContract {
        source_match: decode_source_match_claim(required(v.source_match)?)?,
        parameter_index: usize::try_from(required(v.parameter_index)?)?,
        exact_arity: usize::try_from(required(v.exact_arity)?)?,
        audited_source_sha256: required(v.audited_source_sha256)?,
        note: v.note,
    })
}

pub fn encode_no_accessor_install_callable_contract(
    value: &spec::input_contracts::NoAccessorInstallCallableContract,
) -> pb::NoAccessorInstallCallableContract {
    pb::NoAccessorInstallCallableContract {
        source_match: Some(encode_source_match_claim(&value.source_match)),
        note: value.note.clone(),
    }
}

pub fn decode_no_accessor_install_callable_contract(
    v: pb::NoAccessorInstallCallableContract,
) -> Result<spec::input_contracts::NoAccessorInstallCallableContract> {
    Ok(spec::input_contracts::NoAccessorInstallCallableContract {
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
