//! Lowering of each selector atom to cached domain restrictions and allowed-tuple rows.

use std::collections::{BTreeMap, BTreeSet, btree_map::Entry};

use selector_constraint_backend::{
    AllowedTupleRowsId, BackendValueId, CompiledSelectorProblemBuilder,
    CompiledSelectorProblemError, ConstraintVariableId, SharedVariableDomainId,
};
use selector_ir::{
    SelectorAtom, SelectorProjectedValue, SelectorVariableId, SourceUnitTerm, StringTerm,
    VariableDomain,
};
use source_identity::SourceUnitId;

use super::fact_domains::{FactDomains, SourceUnitStringIndex};
use super::{CompiledSelectorProblemBuildError, model_variable};

pub(super) fn lower_atom_constraint(
    atom: &SelectorAtom,
    domains: &FactDomains,
    variables: &[ConstraintVariableId],
    model: &mut CompiledSelectorProblemBuilder,
    support_cache: &mut EncodedSupportCache,
) -> Result<(), CompiledSelectorProblemBuildError> {
    match atom {
        SelectorAtom::SourceUnitKind {
            source_unit,
            statement_kind,
        } => add_cached_source_unit_string_indexed_allowed_tuples(
            model,
            variables,
            source_unit,
            statement_kind,
            &domains.source_unit_kinds_index,
            "source_unit_kind",
            support_cache,
        ),
        SelectorAtom::SourceUnitDeclaresBinding {
            source_unit,
            binding,
        } => add_cached_source_unit_string_indexed_allowed_tuples(
            model,
            variables,
            source_unit,
            binding,
            &domains.declared_bindings_index,
            "declared_binding",
            support_cache,
        ),
        SelectorAtom::ProjectedAllowedTuples {
            variables: projected_variables,
            rows,
        } => add_projected_allowed_tuples(model, variables, projected_variables, rows),
        SelectorAtom::SourceUnitNotIn {
            source_unit,
            excluded,
        } => lower_source_unit_not_in(source_unit, excluded, variables, model),
        SelectorAtom::SourceUnitReferencesSourceUnit {
            source_unit,
            referenced,
        } => add_cached_source_unit_pair_allowed_tuples(
            model,
            variables,
            source_unit,
            referenced,
            &domains.references_unit,
            "references_source_unit".to_string(),
            support_cache,
        ),
        SelectorAtom::ReadsMember {
            source_unit,
            member,
        } => add_cached_source_unit_string_allowed_tuples(
            model,
            variables,
            source_unit,
            member,
            &domains.member_reads,
            "member_reads".to_string(),
            support_cache,
        ),
        SelectorAtom::ReadsMemberOfSourceUnit {
            source_unit,
            object,
            member,
        } => {
            let (facts, relation_key) = reads_member_of_source_unit_support(domains, member)?;
            add_cached_source_unit_pair_allowed_tuples(
                model,
                variables,
                source_unit,
                object,
                facts,
                relation_key,
                support_cache,
            )
        }
        SelectorAtom::ConsumesModuleMember {
            source_unit,
            module,
            member,
        } => {
            let (facts, relation_key) = module_member_use_support(domains, module, member)?;
            add_cached_source_unit_allowed_tuples(
                model,
                variables,
                source_unit,
                facts,
                relation_key,
                support_cache,
            )
        }
        SelectorAtom::PassedToCall {
            source_unit,
            callee_member,
            arg_index,
        } => {
            let (facts, relation_key) = call_argument_support(domains, callee_member, *arg_index)?;
            add_cached_source_unit_allowed_tuples(
                model,
                variables,
                source_unit,
                facts,
                relation_key,
                support_cache,
            )
        }
        SelectorAtom::PassedToCallOfSourceUnit {
            source_unit,
            callee_object,
            callee_member,
            arg_index,
        } => {
            let (facts, relation_key) =
                call_argument_from_source_unit_support(domains, callee_member, *arg_index)?;
            add_cached_source_unit_pair_allowed_tuples(
                model,
                variables,
                source_unit,
                callee_object,
                facts,
                relation_key,
                support_cache,
            )
        }
        SelectorAtom::MakesDecorateCallForSourceUnit {
            source_unit,
            class_anchor,
            member,
        } => {
            let (facts, relation_key) = decorate_call_support(domains, member)?;
            add_cached_source_unit_pair_allowed_tuples(
                model,
                variables,
                source_unit,
                class_anchor,
                facts,
                relation_key,
                support_cache,
            )
        }
        SelectorAtom::IntrinsicAlias {
            source_unit,
            property,
            referenced_by,
        } => {
            let (facts, relation_key) = intrinsic_alias_support(domains, property)?;
            add_cached_source_unit_pair_allowed_tuples(
                model,
                variables,
                source_unit,
                referenced_by,
                facts,
                relation_key,
                support_cache,
            )
        }
    }
}

fn lower_source_unit_not_in(
    source_unit: &SourceUnitTerm,
    excluded: &[SourceUnitId],
    variables: &[ConstraintVariableId],
    model: &mut CompiledSelectorProblemBuilder,
) -> Result<(), CompiledSelectorProblemBuildError> {
    match source_unit {
        SourceUnitTerm::Var { id } => {
            let variable = model_variable(variables, *id)?;
            let encoded = excluded
                .iter()
                .map(|source_unit| model.intern_source_unit(*source_unit))
                .collect::<Result<Vec<_>, _>>()?;
            model.exclude_variable_encoded_values(variable, encoded)?;
            Ok(())
        }
        SourceUnitTerm::Const { source_unit } => {
            if excluded.contains(source_unit) {
                Err(
                    CompiledSelectorProblemBuildError::ConstantOnlyAtomUnsatisfied {
                        atom: format!("source_unit_not_in({source_unit:?})"),
                    },
                )
            } else {
                Ok(())
            }
        }
    }
}

/// A relation atom's fact support and the cache key its lowered constraint is shared under.
/// Each `*_support` function resolves an atom's constant arguments to one.
type RelationSupport<'a, T> = (&'a BTreeSet<T>, String);

fn reads_member_of_source_unit_support<'a>(
    domains: &'a FactDomains,
    member: &StringTerm,
) -> Result<RelationSupport<'a, (SourceUnitId, SourceUnitId)>, CompiledSelectorProblemBuildError> {
    let member = required_string_term_const(member, "reads_member_of_source_unit.member")?;
    let facts = domains
        .relation_supports
        .reads_member_of_source_unit(&member);
    Ok((facts, format!("reads_member_of_unit:{member}")))
}

fn module_member_use_support<'a>(
    domains: &'a FactDomains,
    module: &StringTerm,
    member: &StringTerm,
) -> Result<RelationSupport<'a, SourceUnitId>, CompiledSelectorProblemBuildError> {
    let module = required_string_term_const(module, "consumes_module_member.module")?;
    let member = required_string_term_const(member, "consumes_module_member.member")?;
    let facts = domains
        .relation_supports
        .module_member_uses(&module, &member);
    Ok((facts, format!("module_member_uses:{module}:{member}")))
}

fn call_argument_support<'a>(
    domains: &'a FactDomains,
    callee_member: &StringTerm,
    arg_index: Option<u32>,
) -> Result<RelationSupport<'a, SourceUnitId>, CompiledSelectorProblemBuildError> {
    let callee_member = required_string_term_const(callee_member, "passed_to_call.callee_member")?;
    let facts = domains
        .relation_supports
        .call_arguments(&callee_member, arg_index);
    Ok((
        facts,
        format!("call_arguments:{callee_member}:{arg_index:?}"),
    ))
}

fn call_argument_from_source_unit_support<'a>(
    domains: &'a FactDomains,
    callee_member: &StringTerm,
    arg_index: Option<u32>,
) -> Result<RelationSupport<'a, (SourceUnitId, SourceUnitId)>, CompiledSelectorProblemBuildError> {
    let callee_member =
        required_string_term_const(callee_member, "passed_to_call_of_source_unit.callee_member")?;
    let facts = domains
        .relation_supports
        .call_arguments_from_source_unit(&callee_member, arg_index);
    Ok((
        facts,
        format!("call_arguments_from_unit:{callee_member}:{arg_index:?}"),
    ))
}

fn decorate_call_support<'a>(
    domains: &'a FactDomains,
    member: &Option<StringTerm>,
) -> Result<RelationSupport<'a, (SourceUnitId, SourceUnitId)>, CompiledSelectorProblemBuildError> {
    let member = optional_string_term_const(member)?;
    let facts = domains
        .relation_supports
        .makes_decorate_call_for_source_unit(member.as_deref());
    Ok((facts, format!("makes_decorate_call_for_unit:{member:?}")))
}

fn intrinsic_alias_support<'a>(
    domains: &'a FactDomains,
    property: &StringTerm,
) -> Result<RelationSupport<'a, (SourceUnitId, SourceUnitId)>, CompiledSelectorProblemBuildError> {
    let property = required_string_term_const(property, "intrinsic_alias.property")?;
    let facts = domains
        .relation_supports
        .intrinsic_alias_referenced_by(&property);
    Ok((facts, format!("intrinsic_alias_referenced_by:{property}")))
}

#[derive(Debug, Default)]
pub(super) struct EncodedSupportCache {
    source_unit_unary: BTreeMap<String, SharedVariableDomainId>,
    string_unary: BTreeMap<String, SharedVariableDomainId>,
    source_unit_string_binary_by_key: BTreeMap<String, AllowedTupleRowsId>,
    source_unit_pair_binary: BTreeMap<String, AllowedTupleRowsId>,
    source_unit_string_unary: BTreeMap<(&'static str, String), SharedVariableDomainId>,
    source_unit_string_binary: BTreeMap<&'static str, AllowedTupleRowsId>,
}

fn restrict_string_variable_to_candidates<'a>(
    model: &mut CompiledSelectorProblemBuilder,
    variable: ConstraintVariableId,
    candidates: impl IntoIterator<Item = &'a str>,
) -> Result<(), CompiledSelectorProblemBuildError> {
    let values = candidates
        .into_iter()
        .map(|value| model.intern_string(value))
        .collect::<Result<Vec<_>, _>>()?;
    model
        .restrict_variable_to_encoded_values(variable, values)
        .map_err(Into::into)
}

/// Restrict one variable to an interned support domain. The model reports its
/// own error type and every caller here works in build errors.
fn restrict_variable_to_domain(
    model: &mut CompiledSelectorProblemBuilder,
    variable: ConstraintVariableId,
    domain_id: SharedVariableDomainId,
) -> Result<(), CompiledSelectorProblemBuildError> {
    model
        .restrict_variable_to_shared_sparse_domain(variable, domain_id)
        .map_err(Into::into)
}

fn cached_source_unit_domain(
    model: &mut CompiledSelectorProblemBuilder,
    support_cache: &mut EncodedSupportCache,
    key: String,
    source_units: impl IntoIterator<Item = SourceUnitId>,
) -> Result<SharedVariableDomainId, CompiledSelectorProblemBuildError> {
    if let Some(domain_id) = support_cache.source_unit_unary.get(&key) {
        return Ok(*domain_id);
    }
    let values = source_units
        .into_iter()
        .map(|source_unit| model.intern_source_unit(source_unit))
        .collect::<Result<Vec<_>, _>>()?;
    let domain_id =
        model.intern_shared_sparse_variable_domain(VariableDomain::SourceUnit, values)?;
    support_cache.source_unit_unary.insert(key, domain_id);
    Ok(domain_id)
}

fn cached_string_domain<'a>(
    model: &mut CompiledSelectorProblemBuilder,
    support_cache: &mut EncodedSupportCache,
    key: String,
    strings: impl IntoIterator<Item = &'a str>,
) -> Result<SharedVariableDomainId, CompiledSelectorProblemBuildError> {
    if let Some(domain_id) = support_cache.string_unary.get(&key) {
        return Ok(*domain_id);
    }
    let values = strings
        .into_iter()
        .map(|value| model.intern_string(value))
        .collect::<Result<Vec<_>, _>>()?;
    let domain_id = model.intern_shared_sparse_variable_domain(VariableDomain::String, values)?;
    support_cache.string_unary.insert(key, domain_id);
    Ok(domain_id)
}

fn add_cached_source_unit_string_indexed_allowed_tuples(
    model: &mut CompiledSelectorProblemBuilder,
    variables: &[ConstraintVariableId],
    source_unit: &SourceUnitTerm,
    string: &StringTerm,
    index: &SourceUnitStringIndex,
    relation: &'static str,
    support_cache: &mut EncodedSupportCache,
) -> Result<(), CompiledSelectorProblemBuildError> {
    match (source_unit, string) {
        (SourceUnitTerm::Const { source_unit }, StringTerm::Const { value }) => index
            .contains(*source_unit, value)
            .then_some(())
            .ok_or_else(
                || CompiledSelectorProblemBuildError::ConstantOnlyAtomUnsatisfied {
                    atom: format!("source_unit/string fact {source_unit:?} {value:?}"),
                },
            ),
        (SourceUnitTerm::Var { id }, StringTerm::Const { value }) => {
            let variable = model_variable(variables, *id)?;
            let key = (relation, value.clone());
            let domain_id =
                if let Some(domain_id) = support_cache.source_unit_string_unary.get(&key) {
                    *domain_id
                } else {
                    let values = index
                        .source_units_by_value
                        .get(value)
                        .into_iter()
                        .flatten()
                        .map(|source_unit| model.intern_source_unit(*source_unit))
                        .collect::<Result<Vec<_>, _>>()?;
                    let domain_id = model
                        .intern_shared_sparse_variable_domain(VariableDomain::SourceUnit, values)?;
                    support_cache
                        .source_unit_string_unary
                        .insert(key, domain_id);
                    domain_id
                };
            restrict_variable_to_domain(model, variable, domain_id)
        }
        (SourceUnitTerm::Const { source_unit }, StringTerm::Var { id }) => {
            let variable = model_variable(variables, *id)?;
            restrict_string_variable_to_candidates(
                model,
                variable,
                index
                    .values_by_unit
                    .get(source_unit)
                    .into_iter()
                    .flatten()
                    .map(String::as_str),
            )
        }
        (SourceUnitTerm::Var { id: source_unit_id }, StringTerm::Var { id: string_id }) => {
            // `SelectorProgram::validate` types source unit terms as source unit variables
            // and string terms as string variables: the two are distinct.
            add_source_unit_string_rows(
                model,
                [
                    model_variable(variables, *source_unit_id)?,
                    model_variable(variables, *string_id)?,
                ],
                support_cache.source_unit_string_binary.entry(relation),
                index.rows(),
            )
        }
    }
}

fn add_cached_source_unit_string_allowed_tuples(
    model: &mut CompiledSelectorProblemBuilder,
    variables: &[ConstraintVariableId],
    source_unit: &SourceUnitTerm,
    string: &StringTerm,
    facts: &BTreeSet<(SourceUnitId, String)>,
    relation_key: String,
    support_cache: &mut EncodedSupportCache,
) -> Result<(), CompiledSelectorProblemBuildError> {
    match (source_unit, string) {
        (SourceUnitTerm::Const { source_unit }, StringTerm::Const { value }) => facts
            .iter()
            .any(|(fact_source_unit, fact_value)| {
                fact_source_unit == source_unit && fact_value == value
            })
            .then_some(())
            .ok_or_else(
                || CompiledSelectorProblemBuildError::ConstantOnlyAtomUnsatisfied {
                    atom: format!("source_unit/string fact {source_unit:?} {value:?}"),
                },
            ),
        (SourceUnitTerm::Var { id }, StringTerm::Const { value }) => {
            let variable = model_variable(variables, *id)?;
            let domain_id = cached_source_unit_domain(
                model,
                support_cache,
                format!("{relation_key}:source_unit-by-string:{value}"),
                facts.iter().filter_map(|(fact_source_unit, fact_value)| {
                    (fact_value == value).then_some(*fact_source_unit)
                }),
            )?;
            restrict_variable_to_domain(model, variable, domain_id)
        }
        (SourceUnitTerm::Const { source_unit }, StringTerm::Var { id }) => {
            let variable = model_variable(variables, *id)?;
            let domain_id = cached_string_domain(
                model,
                support_cache,
                format!("{relation_key}:string-by-source-unit:{}", source_unit.0),
                facts.iter().filter_map(|(fact_source_unit, fact_value)| {
                    (fact_source_unit == source_unit).then_some(fact_value.as_str())
                }),
            )?;
            restrict_variable_to_domain(model, variable, domain_id)
        }
        (SourceUnitTerm::Var { id: source_unit_id }, StringTerm::Var { id: string_id }) => {
            add_source_unit_string_rows(
                model,
                [
                    model_variable(variables, *source_unit_id)?,
                    model_variable(variables, *string_id)?,
                ],
                support_cache
                    .source_unit_string_binary_by_key
                    .entry(relation_key),
                facts.iter().map(|(unit, value)| (*unit, value.as_str())),
            )
        }
    }
}

fn add_source_unit_string_rows<'a, K: Ord>(
    model: &mut CompiledSelectorProblemBuilder,
    variables: [ConstraintVariableId; 2],
    cached: Entry<'_, K, AllowedTupleRowsId>,
    rows: impl Iterator<Item = (SourceUnitId, &'a str)>,
) -> Result<(), CompiledSelectorProblemBuildError> {
    let row_set = match cached {
        Entry::Occupied(entry) => *entry.get(),
        Entry::Vacant(entry) => {
            let tuples = rows
                .map(|(unit, value)| {
                    Ok((model.intern_source_unit(unit)?, model.intern_string(value)?))
                })
                .collect::<Result<Vec<_>, CompiledSelectorProblemError>>()?;
            let id = model.intern_encoded_allowed_binary_row_set(
                variables,
                [VariableDomain::SourceUnit, VariableDomain::String],
                tuples,
            )?;
            *entry.insert(id)
        }
    };
    model.add_encoded_allowed_row_set(variables.to_vec(), row_set)?;
    Ok(())
}

fn add_cached_source_unit_allowed_tuples(
    model: &mut CompiledSelectorProblemBuilder,
    variables: &[ConstraintVariableId],
    source_unit: &SourceUnitTerm,
    facts: &BTreeSet<SourceUnitId>,
    relation_key: String,
    support_cache: &mut EncodedSupportCache,
) -> Result<(), CompiledSelectorProblemBuildError> {
    match source_unit {
        SourceUnitTerm::Const { source_unit } => {
            facts.contains(source_unit).then_some(()).ok_or_else(|| {
                CompiledSelectorProblemBuildError::ConstantOnlyAtomUnsatisfied {
                    atom: format!("source unit fact {source_unit:?}"),
                }
            })
        }
        SourceUnitTerm::Var { id } => {
            let variable = model_variable(variables, *id)?;
            let domain_id = cached_source_unit_domain(
                model,
                support_cache,
                relation_key,
                facts.iter().copied(),
            )?;
            restrict_variable_to_domain(model, variable, domain_id)
        }
    }
}

fn add_cached_source_unit_pair_allowed_tuples(
    model: &mut CompiledSelectorProblemBuilder,
    variables: &[ConstraintVariableId],
    left: &SourceUnitTerm,
    right: &SourceUnitTerm,
    facts: &BTreeSet<(SourceUnitId, SourceUnitId)>,
    relation_key: String,
    support_cache: &mut EncodedSupportCache,
) -> Result<(), CompiledSelectorProblemBuildError> {
    match (left, right) {
        (
            SourceUnitTerm::Const { source_unit: left },
            SourceUnitTerm::Const { source_unit: right },
        ) => facts
            .contains(&(*left, *right))
            .then_some(())
            .ok_or_else(
                || CompiledSelectorProblemBuildError::ConstantOnlyAtomUnsatisfied {
                    atom: format!("source unit pair fact {left:?} {right:?}"),
                },
            ),
        (SourceUnitTerm::Var { id }, SourceUnitTerm::Const { source_unit: right }) => {
            let variable = model_variable(variables, *id)?;
            let domain_id = cached_source_unit_domain(
                model,
                support_cache,
                format!("{relation_key}:left-by-right:{}", right.0),
                facts.iter().filter_map(|(fact_left, fact_right)| {
                    (fact_right == right).then_some(*fact_left)
                }),
            )?;
            restrict_variable_to_domain(model, variable, domain_id)
        }
        (SourceUnitTerm::Const { source_unit: left }, SourceUnitTerm::Var { id }) => {
            let variable = model_variable(variables, *id)?;
            let domain_id = cached_source_unit_domain(
                model,
                support_cache,
                format!("{relation_key}:right-by-left:{}", left.0),
                facts.iter().filter_map(|(fact_left, fact_right)| {
                    (fact_left == left).then_some(*fact_right)
                }),
            )?;
            restrict_variable_to_domain(model, variable, domain_id)
        }
        (SourceUnitTerm::Var { id: left_id }, SourceUnitTerm::Var { id: right_id }) => {
            let constraint_variables = [
                model_variable(variables, *left_id)?,
                model_variable(variables, *right_id)?,
            ];
            if constraint_variables[0] == constraint_variables[1] {
                let domain_id = cached_source_unit_domain(
                    model,
                    support_cache,
                    format!("{relation_key}:same-variable"),
                    facts
                        .iter()
                        .filter_map(|(left, right)| (left == right).then_some(*left)),
                )?;
                return model
                    .restrict_variable_to_shared_sparse_domain(constraint_variables[0], domain_id)
                    .map_err(Into::into);
            }
            let row_set =
                if let Some(row_set) = support_cache.source_unit_pair_binary.get(&relation_key) {
                    *row_set
                } else {
                    let tuples = facts
                        .iter()
                        .map(|(fact_left, fact_right)| {
                            Ok((
                                model.intern_source_unit(*fact_left)?,
                                model.intern_source_unit(*fact_right)?,
                            ))
                        })
                        .collect::<Result<Vec<_>, CompiledSelectorProblemError>>()?;
                    let row_set = model.intern_encoded_allowed_binary_row_set(
                        constraint_variables,
                        [VariableDomain::SourceUnit, VariableDomain::SourceUnit],
                        tuples,
                    )?;
                    support_cache
                        .source_unit_pair_binary
                        .insert(relation_key, row_set);
                    row_set
                };
            model
                .add_encoded_allowed_row_set(constraint_variables.to_vec(), row_set)
                .map(|_| ())
                .map_err(Into::into)
        }
    }
}

fn add_projected_allowed_tuples(
    model: &mut CompiledSelectorProblemBuilder,
    variables: &[ConstraintVariableId],
    projected_variables: &[SelectorVariableId],
    rows: &[Vec<SelectorProjectedValue>],
) -> Result<(), CompiledSelectorProblemBuildError> {
    let constraint_variables = projected_variables
        .iter()
        .map(|variable| model_variable(variables, *variable))
        .collect::<Result<Vec<_>, _>>()?;
    let tuples = rows.to_vec();
    if let [row] = tuples.as_slice() {
        for (variable, value) in constraint_variables.iter().zip(row) {
            let value = intern_constraint_value(model, value.clone())?;
            model.restrict_variable_to_encoded_values(*variable, [value])?;
        }
        return Ok(());
    }
    let mut column_values = vec![Vec::new(); constraint_variables.len()];
    for row in &tuples {
        for (column, value) in row.iter().enumerate() {
            column_values[column].push(intern_constraint_value(model, value.clone())?);
        }
    }
    for (variable, values) in constraint_variables.iter().zip(column_values) {
        model.restrict_variable_to_encoded_values(*variable, values)?;
    }
    model
        .add_allowed_tuples(constraint_variables, tuples)
        .map(|_| ())
        .map_err(Into::into)
}

fn intern_constraint_value(
    model: &mut CompiledSelectorProblemBuilder,
    value: SelectorProjectedValue,
) -> Result<BackendValueId, CompiledSelectorProblemError> {
    match value {
        SelectorProjectedValue::SourceUnit(value) => model.intern_source_unit(value),
        SelectorProjectedValue::String(value) => model.intern_string(&value),
    }
}

fn optional_string_term_const(
    term: &Option<StringTerm>,
) -> Result<Option<String>, CompiledSelectorProblemBuildError> {
    match term {
        Some(StringTerm::Const { value }) => Ok(Some(value.clone())),
        Some(StringTerm::Var { .. }) => Err(CompiledSelectorProblemBuildError::UnsupportedAtom {
            atom: "selector relation currently requires a constant optional string".to_string(),
        }),
        None => Ok(None),
    }
}

fn required_string_term_const(
    term: &StringTerm,
    context: &'static str,
) -> Result<String, CompiledSelectorProblemBuildError> {
    match term {
        StringTerm::Const { value } => Ok(value.clone()),
        StringTerm::Var { .. } => Err(CompiledSelectorProblemBuildError::UnsupportedAtom {
            atom: format!("{context} currently requires a constant string"),
        }),
    }
}
