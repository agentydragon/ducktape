//! Module-YAML reads and edits for the name-binding rewrite: claims, annotations, member removal.

use std::collections::BTreeMap;

use anyhow::{Result, bail};
use serde_yaml::Value;

use crate::group_synthesis::SynthesizedSelectorGroup;
use crate::name_binding_rewrite::NameBindingMember;

#[derive(Debug, Clone)]
pub(crate) struct MigratedSourceBinding {
    pub(crate) local: String,
    pub(crate) name: String,
}

pub(crate) fn add_synthesized_member_annotations(
    module: &str,
    member: &NameBindingMember,
    annotations: &mut serde_yaml::Mapping,
) -> Result<()> {
    for (field, value) in &member.annotation_fields {
        merge_annotation_field(
            module,
            &format!("members[{}].{field}", member.member_index),
            annotations,
            &member.export_name,
            field,
            value,
        )?;
    }
    Ok(())
}

pub(crate) fn synthesized_member_annotation_fields(member: &Value) -> Vec<(&'static str, Value)> {
    let Some(member) = member.as_mapping() else {
        return Vec::new();
    };
    [
        "purity",
        "effect",
        "pure_members",
        "no_sync_callback_members",
        "comment",
        "note",
    ]
    .into_iter()
    .filter_map(|field| member.get(yk(field)).map(|value| (field, value.clone())))
    .collect()
}

pub(crate) fn ensure_optional_sequence(
    root: &serde_yaml::Mapping,
    key: &str,
    module: &str,
) -> Result<()> {
    if let Some(value) = root.get(yk(key))
        && !matches!(value, Value::Sequence(_))
    {
        bail!("{module}: {key} exists but is not a sequence");
    }
    Ok(())
}

pub(crate) fn existing_annotations_mapping(
    root: &serde_yaml::Mapping,
    module: &str,
) -> Result<serde_yaml::Mapping> {
    match root.get(yk("annotations")) {
        None => Ok(serde_yaml::Mapping::new()),
        Some(Value::Mapping(mapping)) => Ok(mapping.clone()),
        Some(_) => bail!("{module}: annotations exists but is not a mapping"),
    }
}

fn merge_annotation_field(
    module: &str,
    origin: &str,
    annotations: &mut serde_yaml::Mapping,
    export_name: &str,
    field: &str,
    value: &Value,
) -> Result<()> {
    if annotation_field_is_empty(field, value) {
        return Ok(());
    }
    validate_annotation_field_value(module, origin, field, value)?;
    let annotation = annotations
        .entry(Value::String(export_name.to_string()))
        .or_insert_with(|| Value::Mapping(serde_yaml::Mapping::new()));
    let Value::Mapping(annotation_mapping) = annotation else {
        bail!("{module}: annotations.{export_name} exists but is not a mapping");
    };
    match annotation_mapping.get(yk(field)) {
        Some(existing) if existing == value => Ok(()),
        Some(_) => bail!("{module}: {origin} conflicts with annotations.{export_name}.{field}"),
        None => {
            annotation_mapping.insert(yk(field), value.clone());
            Ok(())
        }
    }
}

fn annotation_field_is_empty(field: &str, value: &Value) -> bool {
    match (field, value) {
        (_, Value::Null) => true,
        ("purity" | "effect", Value::String(value)) => value == "default",
        ("pure_members" | "no_sync_callback_members", Value::Sequence(values)) => values.is_empty(),
        _ => false,
    }
}

fn validate_annotation_field_value(
    module: &str,
    origin: &str,
    field: &str,
    value: &Value,
) -> Result<()> {
    match field {
        "comment" | "note" | "purity" | "effect" => {
            if !matches!(value, Value::String(_)) {
                bail!("{module}: {origin}.{field} must be a string");
            }
        }
        "pure_members" | "no_sync_callback_members" => {
            let Value::Sequence(values) = value else {
                bail!("{module}: {origin}.{field} must be a string list");
            };
            for item in values {
                if !matches!(item, Value::String(_)) {
                    bail!("{module}: {origin}.{field} entries must be strings");
                }
            }
        }
        _ => bail!("{module}: unsupported annotation field `{field}`"),
    }
    Ok(())
}

pub(crate) fn source_match_claim_value(
    match_source: &str,
    bindings: Vec<MigratedSourceBinding>,
) -> Value {
    let mut claim = serde_yaml::Mapping::new();
    claim.insert(yk("match"), Value::String(match_source.to_string()));
    claim.insert(
        yk("bindings"),
        Value::Sequence(
            bindings
                .into_iter()
                .map(source_match_binding_value)
                .collect(),
        ),
    );
    Value::Mapping(claim)
}

fn source_match_binding_value(binding: MigratedSourceBinding) -> Value {
    if binding.local == binding.name {
        return Value::String(binding.local);
    }
    let mut detail = serde_yaml::Mapping::new();
    detail.insert(yk("local"), Value::String(binding.local));
    detail.insert(yk("name"), Value::String(binding.name));
    Value::Mapping(detail)
}

pub(crate) fn synthesized_source_match_claim_value(
    synthesized: &SynthesizedSelectorGroup,
) -> Value {
    source_match_claim_value(
        &synthesized.match_source,
        synthesized
            .target_bindings
            .iter()
            .map(|target| MigratedSourceBinding {
                local: target.selector_local.clone(),
                name: target.export_name.clone(),
            })
            .collect(),
    )
}

pub(crate) fn apply_member_replacements(
    members: &mut Vec<Value>,
    replacements: BTreeMap<usize, Option<Value>>,
) {
    let mut next = Vec::with_capacity(members.len());
    for (idx, member) in std::mem::take(members).into_iter().enumerate() {
        match replacements.get(&idx) {
            Some(Some(replacement)) => next.push(replacement.clone()),
            Some(None) => {}
            None => next.push(member),
        }
    }
    *members = next;
}

pub(crate) fn mapping_get<'a>(value: &'a Value, key: &str) -> Option<&'a Value> {
    let Value::Mapping(mapping) = value else {
        return None;
    };
    mapping.get(yk(key))
}

pub(crate) fn mapping_get_path<'a>(value: &'a Value, path: &[&str]) -> Option<&'a Value> {
    let mut current = value;
    for key in path {
        let Value::Mapping(mapping) = current else {
            return None;
        };
        current = mapping.get(yk(key))?;
    }
    Some(current)
}

pub(crate) fn value_as_string(value: &Value) -> Option<String> {
    match value {
        Value::String(value) => Some(value.clone()),
        _ => None,
    }
}

pub(crate) fn yk(key: &str) -> Value {
    Value::String(key.to_string())
}
