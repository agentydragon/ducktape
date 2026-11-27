use super::required;
use anyhow::{Context, Result, ensure};
use spec_wire_proto::debundle::spec as pb;
use std::collections::{BTreeMap, BTreeSet};

pub fn encode_logical_module(value: &spec::LogicalModule) -> pb::LogicalModule {
    pb::LogicalModule {
        members: value.members.iter().map(encode_member).collect(),
        source_matches: value
            .source_matches
            .iter()
            .map(encode_source_match_claim)
            .collect(),
        annotations: encode_binding_annotation_map(&value.annotations),
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
        annotations: decode_binding_annotation_map(v.annotations)?,
        anonymous_statements: v
            .anonymous_statements
            .into_iter()
            .map(decode_anonymous_statement)
            .collect::<Result<_>>()?,
        comment: v.comment,
        note: v.note,
    })
}

pub fn encode_member(value: &spec::member_selectors::Member) -> pb::Member {
    pb::Member {
        name: value.name.clone(),
        selector: Some(encode_raw_member_selector(&value.selector)),
    }
}

pub fn decode_member(v: pb::Member) -> Result<spec::member_selectors::Member> {
    Ok(spec::member_selectors::Member {
        name: v.name,
        selector: decode_raw_member_selector(required(v.selector)?)?,
    })
}

pub fn encode_raw_member_selector(
    value: &spec::member_selectors::RawMemberSelector,
) -> pb::RawMemberSelector {
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

pub fn decode_raw_member_selector(
    v: pb::RawMemberSelector,
) -> Result<spec::member_selectors::RawMemberSelector> {
    Ok(spec::member_selectors::RawMemberSelector {
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

pub fn encode_binding_selector(
    value: &spec::member_selectors::BindingSelector,
) -> pb::BindingSelector {
    pb::BindingSelector {
        name: Some(value.name.clone()),
        kind: value.kind.map(|kind| kind as i32),
    }
}

pub fn decode_binding_selector(
    v: pb::BindingSelector,
) -> Result<spec::member_selectors::BindingSelector> {
    Ok(spec::member_selectors::BindingSelector {
        name: required(v.name)?,
        kind: v.kind.map(pb::BindingSourceKind::try_from).transpose()?,
    })
}

pub fn encode_cross_ref_selector(
    value: &spec::member_selectors::CrossRefSelector,
) -> pb::CrossRefSelector {
    pb::CrossRefSelector {
        references: Some(value.references.clone()),
        kind: value.kind.map(|kind| kind as i32),
    }
}

pub fn decode_cross_ref_selector(
    v: pb::CrossRefSelector,
) -> Result<spec::member_selectors::CrossRefSelector> {
    Ok(spec::member_selectors::CrossRefSelector {
        references: required(v.references)?,
        kind: v.kind.map(pb::BindingSourceKind::try_from).transpose()?,
    })
}

pub fn encode_reads_member_selector(
    value: &spec::member_selectors::ReadsMemberSelector,
) -> pb::ReadsMemberSelector {
    pb::ReadsMemberSelector {
        member: Some(value.member.clone()),
        object: value.object.clone(),
        kind: value.kind.map(|kind| kind as i32),
    }
}

pub fn decode_reads_member_selector(
    v: pb::ReadsMemberSelector,
) -> Result<spec::member_selectors::ReadsMemberSelector> {
    Ok(spec::member_selectors::ReadsMemberSelector {
        member: required(v.member)?,
        object: v.object,
        kind: v.kind.map(pb::BindingSourceKind::try_from).transpose()?,
    })
}

pub fn encode_member_of_module_selector(
    value: &spec::member_selectors::MemberOfModuleSelector,
) -> pb::MemberOfModuleSelector {
    pb::MemberOfModuleSelector {
        module: Some(value.module.clone()),
        member: Some(value.member.clone()),
        kind: value.kind.map(|kind| kind as i32),
    }
}

pub fn decode_member_of_module_selector(
    v: pb::MemberOfModuleSelector,
) -> Result<spec::member_selectors::MemberOfModuleSelector> {
    Ok(spec::member_selectors::MemberOfModuleSelector {
        module: required(v.module)?,
        member: required(v.member)?,
        kind: v.kind.map(pb::BindingSourceKind::try_from).transpose()?,
    })
}

pub fn encode_passed_to_call_selector(
    value: &spec::member_selectors::PassedToCallSelector,
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
) -> Result<spec::member_selectors::PassedToCallSelector> {
    Ok(spec::member_selectors::PassedToCallSelector {
        callee_member: required(v.callee_member)?,
        object: v.object,
        arg_index: v.arg_index.map(usize::try_from).transpose()?,
        kind: v.kind.map(pb::BindingSourceKind::try_from).transpose()?,
    })
}

pub fn encode_makes_decorate_call_selector(
    value: &spec::member_selectors::MakesDecorateCallSelector,
) -> pb::MakesDecorateCallSelector {
    pb::MakesDecorateCallSelector {
        class: Some(value.class.clone()),
        member: value.member.clone(),
        kind: value.kind.map(|kind| kind as i32),
    }
}

pub fn decode_makes_decorate_call_selector(
    v: pb::MakesDecorateCallSelector,
) -> Result<spec::member_selectors::MakesDecorateCallSelector> {
    Ok(spec::member_selectors::MakesDecorateCallSelector {
        class: required(v.class)?,
        member: v.member,
        kind: v.kind.map(pb::BindingSourceKind::try_from).transpose()?,
    })
}

pub fn encode_intrinsic_alias_selector(
    value: &spec::member_selectors::IntrinsicAliasSelector,
) -> pb::IntrinsicAliasSelector {
    pb::IntrinsicAliasSelector {
        property: Some(value.property.clone()),
        referenced_by: Some(value.referenced_by.clone()),
    }
}

pub fn decode_intrinsic_alias_selector(
    v: pb::IntrinsicAliasSelector,
) -> Result<spec::member_selectors::IntrinsicAliasSelector> {
    Ok(spec::member_selectors::IntrinsicAliasSelector {
        property: required(v.property)?,
        referenced_by: required(v.referenced_by)?,
    })
}

pub fn encode_source_match_claim(
    value: &spec::annotations::SourceMatchClaim,
) -> pb::SourceMatchClaim {
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

pub fn decode_source_match_claim(
    v: pb::SourceMatchClaim,
) -> Result<spec::annotations::SourceMatchClaim> {
    Ok(spec::annotations::SourceMatchClaim {
        identifiers: pb::SourceMatchIdentifierMode::try_from(required(v.identifiers)?)?,
        match_source: required(v.match_source)?,
        bindings: v
            .bindings
            .into_iter()
            .map(decode_source_match_binding)
            .collect::<Result<_>>()?,
        note: v.note,
    })
}

pub fn encode_source_match_binding(
    value: &spec::annotations::SourceMatchBinding,
) -> pb::SourceMatchBinding {
    use pb::source_match_binding::Value;
    pb::SourceMatchBinding {
        value: Some(match value {
            spec::annotations::SourceMatchBinding::Local(v) => Value::Local(v.clone()),
            spec::annotations::SourceMatchBinding::Detailed(v) => Value::Detailed(v.clone()),
        }),
    }
}

pub fn decode_source_match_binding(
    v: pb::SourceMatchBinding,
) -> Result<spec::annotations::SourceMatchBinding> {
    use pb::source_match_binding::Value;
    Ok(match v.value.context("missing source match binding")? {
        Value::Local(v) => spec::annotations::SourceMatchBinding::Local(v),
        Value::Detailed(v) => spec::annotations::SourceMatchBinding::Detailed(v),
    })
}

/// Every wire message that carries binding annotations stores the same map, and
/// turning it into or out of the wire form is the same one step per entry.
pub fn encode_binding_annotation_map<C>(
    values: &BTreeMap<String, spec::annotations::BindingAnnotation>,
) -> C
where
    C: FromIterator<(String, pb::BindingAnnotation)>,
{
    values
        .iter()
        .map(|(key, annotation)| (key.clone(), encode_binding_annotation(annotation)))
        .collect()
}

/// The decode side of `encode_binding_annotation_map`: one fallible entry codec
/// over the wire map, collected into whichever spec map the message decodes to.
pub fn decode_binding_annotation_map<R>(
    values: impl IntoIterator<Item = (String, pb::BindingAnnotation)>,
) -> Result<R>
where
    R: FromIterator<(String, spec::annotations::BindingAnnotation)>,
{
    values
        .into_iter()
        .map(|(key, annotation)| Ok((key, decode_binding_annotation(annotation)?)))
        .collect()
}

pub fn encode_binding_annotation(
    value: &spec::annotations::BindingAnnotation,
) -> pb::BindingAnnotation {
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
        pure_members: value.pure_members.clone(),
        no_sync_callback_members: value.no_sync_callback_members.clone(),
        comment: value.comment.clone(),
        note: value.note.clone(),
    }
}

pub fn decode_binding_annotation(
    v: pb::BindingAnnotation,
) -> Result<spec::annotations::BindingAnnotation> {
    Ok(spec::annotations::BindingAnnotation {
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

pub fn encode_anonymous_statement(
    value: &spec::annotations::AnonymousStatement,
) -> pb::AnonymousStatement {
    pb::AnonymousStatement {
        match_source: value.match_source.clone(),
        source_match: value.source_match.as_ref().map(encode_source_match),
        no_reentry_call: value.no_reentry_call.as_ref().map(encode_no_reentry_call),
        note: value.note.clone(),
        comment: value.comment.clone(),
    }
}

pub fn decode_anonymous_statement(
    v: pb::AnonymousStatement,
) -> Result<spec::annotations::AnonymousStatement> {
    Ok(spec::annotations::AnonymousStatement {
        match_source: v.match_source,
        source_match: v.source_match.map(decode_source_match).transpose()?,
        no_reentry_call: v.no_reentry_call.map(decode_no_reentry_call).transpose()?,
        note: v.note,
        comment: v.comment,
    })
}

pub fn encode_source_match(value: &spec::annotations::SourceMatch) -> pb::SourceMatch {
    pb::SourceMatch {
        identifiers: Some(value.identifiers as i32),
        target_binding: value.target_binding.clone(),
        match_source: Some(value.match_source.clone()),
    }
}

pub fn decode_source_match(v: pb::SourceMatch) -> Result<spec::annotations::SourceMatch> {
    Ok(spec::annotations::SourceMatch {
        identifiers: pb::SourceMatchIdentifierMode::try_from(required(v.identifiers)?)?,
        target_binding: v.target_binding,
        match_source: required(v.match_source)?,
    })
}

pub fn encode_no_reentry_call(value: &spec::annotations::NoReentryCall) -> pb::NoReentryCall {
    pb::NoReentryCall {
        callback_arguments: value
            .callback_arguments
            .iter()
            .map(|index| *index as u64)
            .collect(),
    }
}

pub fn decode_no_reentry_call(v: pb::NoReentryCall) -> Result<spec::annotations::NoReentryCall> {
    let result = spec::annotations::NoReentryCall {
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
