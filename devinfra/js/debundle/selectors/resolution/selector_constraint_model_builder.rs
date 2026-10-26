//! Lowering from selector IR plus facts into a compact finite-domain problem.

use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;

use analysis::{SourceUnitId, StatementOrdinal};
use selector_constraint_backend::{
    AllowedTupleRowsId, BackendValueId, BindingProjection, CompiledSelectorProblem,
    CompiledSelectorProblemBuilder, CompiledSelectorProblemError, ConstraintValue,
    ConstraintVariableId, SharedVariableDomainId, TargetBindingProjection,
};
use selector_ir::{
    SelectorAtom, SelectorFact, SelectorFactStore, SelectorProgram, SelectorProgramError,
    SelectorProjectedValue, SelectorVariableId, SourceUnitTerm, StringTerm, VariableDomain,
};

pub fn compile_selector_problem(
    program: &SelectorProgram,
    facts: &SelectorFactStore,
) -> Result<CompiledSelectorProblem, CompiledSelectorProblemBuildError> {
    program
        .validate()
        .map_err(CompiledSelectorProblemBuildError::InvalidProgram)?;

    let mut domains = FactDomains::from_program_and_facts(program, facts);

    domains.discard_unneeded_raw_relations();
    let target_binding_projections = TargetBindingProjections::from_program(program)?;

    let mut model = CompiledSelectorProblemBuilder::default();
    for domain in [VariableDomain::SourceUnit, VariableDomain::String] {
        model.add_full_domain_values(domain, domains.values_for(domain))?;
    }
    domains.discard_full_domain_source_sets();
    let mut variables = Vec::with_capacity(program.variables.len());
    for variable in &program.variables {
        variables.push(model.add_variable(variable.domain, variable.debug_name.clone()));
    }

    for target in &program.targets {
        let source_unit_variable = model_variable(&variables, target.source_unit)?;
        let binding_projection =
            match target_binding_projections.binding_projection(target.source_unit) {
                Some(SourceBindingProjection::Const(binding)) => {
                    Some(TargetBindingProjection::Const(binding.clone()))
                }
                Some(SourceBindingProjection::Variable(binding)) => Some(
                    TargetBindingProjection::Variable(model_variable(&variables, *binding)?),
                ),
                None => None,
            };
        model.add_target_projection(target.id, source_unit_variable, binding_projection)?;
    }

    let mut support_cache = EncodedSupportCache::default();
    for atom in &program.atoms {
        lower_atom_constraint(atom, &domains, &variables, &mut model, &mut support_cache)?;
        if model.known_unsat() {
            break;
        }
    }

    if !model.known_unsat() {
        model.simplify_allowed_tuples_against_current_domains()?;
    }

    if !model.known_unsat() {
        for targets in &program.all_different {
            model.require_target_all_different(targets.clone())?;
        }
    }

    model
        .finish()
        .map_err(CompiledSelectorProblemBuildError::from)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CompiledSelectorProblemBuildError {
    InvalidProgram(SelectorProgramError),
    InvalidModel(CompiledSelectorProblemError),
    UnknownSelectorVariable {
        variable: SelectorVariableId,
    },
    UnsupportedAtom {
        atom: String,
    },
    ConstantOnlyAtomUnsatisfied {
        atom: String,
    },
    ConflictingTargetBindingProjection {
        source_unit: SelectorVariableId,
        existing: SourceBindingProjection,
        actual: SourceBindingProjection,
    },
}

impl fmt::Display for CompiledSelectorProblemBuildError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidProgram(err) => write!(f, "invalid selector program: {err}"),
            Self::InvalidModel(err) => write!(f, "invalid compiled selector problem: {err}"),
            Self::UnknownSelectorVariable { variable } => {
                write!(f, "selector variable {variable:?} has no model variable")
            }
            Self::UnsupportedAtom { atom } => {
                write!(
                    f,
                    "selector atom is not supported by the compiled selector problem builder: {atom}"
                )
            }
            Self::ConstantOnlyAtomUnsatisfied { atom } => {
                write!(
                    f,
                    "constant-only selector atom has no matching fact: {atom}"
                )
            }
            Self::ConflictingTargetBindingProjection {
                source_unit,
                existing,
                actual,
            } => write!(
                f,
                "selector source unit variable {source_unit:?} has conflicting target binding projections: {existing:?} vs {actual:?}"
            ),
        }
    }
}

impl Error for CompiledSelectorProblemBuildError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::InvalidProgram(err) => Some(err),
            Self::InvalidModel(err) => Some(err),
            Self::UnknownSelectorVariable { .. }
            | Self::UnsupportedAtom { .. }
            | Self::ConstantOnlyAtomUnsatisfied { .. }
            | Self::ConflictingTargetBindingProjection { .. } => None,
        }
    }
}

impl From<CompiledSelectorProblemError> for CompiledSelectorProblemBuildError {
    fn from(err: CompiledSelectorProblemError) -> Self {
        Self::InvalidModel(err)
    }
}

fn model_variable(
    variables: &[ConstraintVariableId],
    variable: SelectorVariableId,
) -> Result<ConstraintVariableId, CompiledSelectorProblemBuildError> {
    variables
        .get(variable.0)
        .copied()
        .ok_or(CompiledSelectorProblemBuildError::UnknownSelectorVariable { variable })
}

fn lower_atom_constraint(
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
        SelectorAtom::SourceUnitAliasesSourceUnit {
            source_unit,
            aliased,
        } => add_cached_source_unit_pair_allowed_tuples(
            model,
            variables,
            source_unit,
            aliased,
            &domains.aliases_unit,
            "aliases_source_unit".to_string(),
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
            let member = required_string_term_const(member, "reads_member_of_source_unit.member")?;
            let facts = domains
                .relation_supports
                .reads_member_of_source_unit(&member);
            add_cached_source_unit_pair_allowed_tuples(
                model,
                variables,
                source_unit,
                object,
                facts,
                format!("reads_member_of_unit:{member}"),
                support_cache,
            )
        }
        SelectorAtom::ConsumesModuleMember {
            source_unit,
            module,
            member,
        } => {
            let module = required_string_term_const(module, "consumes_module_member.module")?;
            let member = required_string_term_const(member, "consumes_module_member.member")?;
            let facts = domains
                .relation_supports
                .module_member_uses(&module, &member);
            add_cached_source_unit_allowed_tuples(
                model,
                variables,
                source_unit,
                facts,
                format!("module_member_uses:{module}:{member}"),
                support_cache,
            )
        }
        SelectorAtom::PassedToCall {
            source_unit,
            callee_member,
            arg_index,
        } => {
            let callee_member =
                required_string_term_const(callee_member, "passed_to_call.callee_member")?;
            let facts = domains
                .relation_supports
                .call_arguments(&callee_member, *arg_index);
            add_cached_source_unit_allowed_tuples(
                model,
                variables,
                source_unit,
                facts,
                format!("call_arguments:{callee_member}:{arg_index:?}"),
                support_cache,
            )
        }
        SelectorAtom::PassedToCallOfSourceUnit {
            source_unit,
            callee_object,
            callee_member,
            arg_index,
        } => {
            let callee_member = required_string_term_const(
                callee_member,
                "passed_to_call_of_source_unit.callee_member",
            )?;
            let facts = domains
                .relation_supports
                .call_arguments_from_source_unit(&callee_member, *arg_index);
            add_cached_source_unit_pair_allowed_tuples(
                model,
                variables,
                source_unit,
                callee_object,
                facts,
                format!("call_arguments_from_unit:{callee_member}:{arg_index:?}"),
                support_cache,
            )
        }
        SelectorAtom::MakesDecorateCallForSourceUnit {
            source_unit,
            class_anchor,
            member,
        } => {
            let member = optional_string_term_const(member)?;
            let facts = domains
                .relation_supports
                .makes_decorate_call_for_source_unit(member.as_deref());
            add_cached_source_unit_pair_allowed_tuples(
                model,
                variables,
                source_unit,
                class_anchor,
                facts,
                format!("makes_decorate_call_for_unit:{member:?}"),
                support_cache,
            )
        }
        SelectorAtom::IntrinsicAlias {
            source_unit,
            property,
            referenced_by,
        } => {
            let property = required_string_term_const(property, "intrinsic_alias.property")?;
            let facts = domains
                .relation_supports
                .intrinsic_alias_referenced_by(&property);
            add_cached_source_unit_pair_allowed_tuples(
                model,
                variables,
                source_unit,
                referenced_by,
                facts,
                format!("intrinsic_alias_referenced_by:{property}"),
                support_cache,
            )
        }
    }
}

#[derive(Debug, Default)]
struct EncodedSupportCache {
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
            model
                .restrict_variable_to_shared_sparse_domain(variable, domain_id)
                .map_err(Into::into)
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
            let constraint_variables = [
                model_variable(variables, *source_unit_id)?,
                model_variable(variables, *string_id)?,
            ];
            let row_set =
                if let Some(row_set) = support_cache.source_unit_string_binary.get(relation) {
                    *row_set
                } else {
                    let tuples = index
                        .rows
                        .iter()
                        .map(|(fact_source_unit, fact_string)| {
                            Ok((
                                model.intern_source_unit(*fact_source_unit)?,
                                model.intern_string(fact_string)?,
                            ))
                        })
                        .collect::<Result<Vec<_>, CompiledSelectorProblemError>>()?;
                    let row_set = model.intern_encoded_allowed_binary_row_set(
                        constraint_variables,
                        [VariableDomain::SourceUnit, VariableDomain::String],
                        tuples,
                    )?;
                    support_cache
                        .source_unit_string_binary
                        .insert(relation, row_set);
                    row_set
                };
            model
                .add_encoded_allowed_row_set(constraint_variables.to_vec(), row_set)
                .map(|_| ())
                .map_err(Into::into)
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
            model
                .restrict_variable_to_shared_sparse_domain(variable, domain_id)
                .map_err(Into::into)
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
            model
                .restrict_variable_to_shared_sparse_domain(variable, domain_id)
                .map_err(Into::into)
        }
        (SourceUnitTerm::Var { id: source_unit_id }, StringTerm::Var { id: string_id }) => {
            let constraint_variables = [
                model_variable(variables, *source_unit_id)?,
                model_variable(variables, *string_id)?,
            ];
            let row_set = if let Some(row_set) = support_cache
                .source_unit_string_binary_by_key
                .get(&relation_key)
            {
                *row_set
            } else {
                let tuples = facts
                    .iter()
                    .map(|(fact_source_unit, fact_string)| {
                        Ok((
                            model.intern_source_unit(*fact_source_unit)?,
                            model.intern_string(fact_string)?,
                        ))
                    })
                    .collect::<Result<Vec<_>, CompiledSelectorProblemError>>()?;
                let row_set = model.intern_encoded_allowed_binary_row_set(
                    constraint_variables,
                    [VariableDomain::SourceUnit, VariableDomain::String],
                    tuples,
                )?;
                support_cache
                    .source_unit_string_binary_by_key
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
            model
                .restrict_variable_to_shared_sparse_domain(variable, domain_id)
                .map_err(Into::into)
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
            model
                .restrict_variable_to_shared_sparse_domain(variable, domain_id)
                .map_err(Into::into)
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
            model
                .restrict_variable_to_shared_sparse_domain(variable, domain_id)
                .map_err(Into::into)
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
    let tuples = rows
        .iter()
        .map(|row| row.iter().cloned().map(projected_value).collect())
        .collect::<Vec<Vec<_>>>();
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
    value: ConstraintValue,
) -> Result<BackendValueId, CompiledSelectorProblemError> {
    match value {
        ConstraintValue::SourceUnit(value) => model.intern_source_unit(value),
        ConstraintValue::String(value) => model.intern_string(&value),
    }
}

fn projected_value(value: SelectorProjectedValue) -> ConstraintValue {
    match value {
        SelectorProjectedValue::SourceUnit(value) => ConstraintValue::SourceUnit(value),
        SelectorProjectedValue::String(value) => ConstraintValue::String(value),
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

/// Binding projection before selector variables are lowered to solver variables.
pub type SourceBindingProjection = BindingProjection<SelectorVariableId>;

#[derive(Debug, Default)]
struct TargetBindingProjections {
    by_unit: BTreeMap<SelectorVariableId, SourceBindingProjection>,
}

impl TargetBindingProjections {
    fn from_program(program: &SelectorProgram) -> Result<Self, CompiledSelectorProblemBuildError> {
        let mut projections = Self::default();
        for atom in &program.atoms {
            match atom {
                SelectorAtom::SourceUnitDeclaresBinding {
                    source_unit: SourceUnitTerm::Var { id: source_unit },
                    binding: StringTerm::Const { value },
                } => projections
                    .insert(*source_unit, SourceBindingProjection::Const(value.clone()))?,
                SelectorAtom::SourceUnitDeclaresBinding {
                    source_unit: SourceUnitTerm::Var { id: source_unit },
                    binding: StringTerm::Var { id: binding },
                } => {
                    projections.insert(*source_unit, SourceBindingProjection::Variable(*binding))?
                }
                _ => {}
            }
        }
        Ok(projections)
    }

    fn insert(
        &mut self,
        source_unit: SelectorVariableId,
        binding: SourceBindingProjection,
    ) -> Result<(), CompiledSelectorProblemBuildError> {
        match self.by_unit.get(&source_unit) {
            Some(existing) if existing != &binding => Err(
                CompiledSelectorProblemBuildError::ConflictingTargetBindingProjection {
                    source_unit,
                    existing: existing.clone(),
                    actual: binding,
                },
            ),
            Some(_) => Ok(()),
            None => {
                self.by_unit.insert(source_unit, binding);
                Ok(())
            }
        }
    }

    fn binding_projection(
        &self,
        source_unit: SelectorVariableId,
    ) -> Option<&SourceBindingProjection> {
        self.by_unit.get(&source_unit)
    }
}

#[derive(Debug, Default)]
struct SourceUnitStringIndex {
    source_units_by_value: BTreeMap<String, Vec<SourceUnitId>>,
    values_by_unit: BTreeMap<SourceUnitId, Vec<String>>,
    rows: Vec<(SourceUnitId, String)>,
}

impl SourceUnitStringIndex {
    fn from_facts(facts: &BTreeSet<(SourceUnitId, String)>) -> Self {
        let mut index = Self::default();
        for (source_unit, value) in facts {
            index
                .source_units_by_value
                .entry(value.clone())
                .or_default()
                .push(*source_unit);
            index
                .values_by_unit
                .entry(*source_unit)
                .or_default()
                .push(value.clone());
            index.rows.push((*source_unit, value.clone()));
        }
        for source_units in index.source_units_by_value.values_mut() {
            source_units.sort_unstable();
            source_units.dedup();
        }
        for values in index.values_by_unit.values_mut() {
            values.sort();
            values.dedup();
        }
        index.rows.sort();
        index.rows.dedup();
        index
    }

    fn contains(&self, source_unit: SourceUnitId, value: &str) -> bool {
        self.values_by_unit.get(&source_unit).is_some_and(|values| {
            values
                .binary_search_by(|candidate| candidate.as_str().cmp(value))
                .is_ok()
        })
    }
}

#[derive(Debug, Default)]
struct RelationSupportCache {
    empty_source_unit_support: BTreeSet<SourceUnitId>,
    empty_source_unit_pair_support: BTreeSet<(SourceUnitId, SourceUnitId)>,
    module_member_uses_by_module_member: BTreeMap<(String, String), BTreeSet<SourceUnitId>>,
    reads_member_of_source_unit_by_member: BTreeMap<String, BTreeSet<(SourceUnitId, SourceUnitId)>>,
    call_arguments_by_member: BTreeMap<String, BTreeSet<SourceUnitId>>,
    call_arguments_by_member_arg_index: BTreeMap<String, BTreeMap<usize, BTreeSet<SourceUnitId>>>,
    call_arguments_from_source_unit_by_member:
        BTreeMap<String, BTreeSet<(SourceUnitId, SourceUnitId)>>,
    call_arguments_from_source_unit_by_member_arg_index:
        BTreeMap<String, BTreeMap<usize, BTreeSet<(SourceUnitId, SourceUnitId)>>>,
    makes_decorate_call_for_source_unit_all: BTreeSet<(SourceUnitId, SourceUnitId)>,
    makes_decorate_call_for_source_unit_by_member:
        BTreeMap<String, BTreeSet<(SourceUnitId, SourceUnitId)>>,
    intrinsic_alias_referenced_by_property:
        BTreeMap<String, BTreeSet<(SourceUnitId, SourceUnitId)>>,
}

impl RelationSupportCache {
    fn from_domains(domains: &FactDomains) -> Self {
        let mut cache = Self::default();

        for (source_unit, module, member) in &domains.module_member_uses {
            cache
                .module_member_uses_by_module_member
                .entry((module.clone(), member.clone()))
                .or_default()
                .insert(*source_unit);
        }

        for (source_unit, object, member) in &domains.reads_member_of_unit {
            cache
                .reads_member_of_source_unit_by_member
                .entry(member.clone())
                .or_default()
                .insert((*source_unit, *object));
        }

        for (source_unit, callee_member, arg_index) in &domains.call_arguments {
            cache
                .call_arguments_by_member
                .entry(callee_member.clone())
                .or_default()
                .insert(*source_unit);
            cache
                .call_arguments_by_member_arg_index
                .entry(callee_member.clone())
                .or_default()
                .entry(*arg_index)
                .or_default()
                .insert(*source_unit);
        }

        for (source_unit, callee_object, callee_member, arg_index) in
            &domains.call_arguments_from_unit
        {
            let support = (*source_unit, *callee_object);
            cache
                .call_arguments_from_source_unit_by_member
                .entry(callee_member.clone())
                .or_default()
                .insert(support);
            cache
                .call_arguments_from_source_unit_by_member_arg_index
                .entry(callee_member.clone())
                .or_default()
                .entry(*arg_index)
                .or_default()
                .insert(support);
        }

        for (source_unit, class_anchor, member) in &domains.makes_decorate_call_for_unit {
            let support = (*source_unit, *class_anchor);
            cache
                .makes_decorate_call_for_source_unit_all
                .insert(support);
            if let Some(member) = member {
                cache
                    .makes_decorate_call_for_source_unit_by_member
                    .entry(member.clone())
                    .or_default()
                    .insert(support);
            }
        }

        for (source_unit, property, referenced_by) in &domains.intrinsic_alias_referenced_by {
            cache
                .intrinsic_alias_referenced_by_property
                .entry(property.clone())
                .or_default()
                .insert((*source_unit, *referenced_by));
        }

        cache
    }

    fn module_member_uses(&self, module: &str, member: &str) -> &BTreeSet<SourceUnitId> {
        self.module_member_uses_by_module_member
            .get(&(module.to_string(), member.to_string()))
            .unwrap_or(&self.empty_source_unit_support)
    }

    fn reads_member_of_source_unit(&self, member: &str) -> &BTreeSet<(SourceUnitId, SourceUnitId)> {
        self.reads_member_of_source_unit_by_member
            .get(member)
            .unwrap_or(&self.empty_source_unit_pair_support)
    }

    fn call_arguments(
        &self,
        callee_member: &str,
        arg_index: Option<u32>,
    ) -> &BTreeSet<SourceUnitId> {
        match arg_index {
            Some(arg_index) => self
                .call_arguments_by_member_arg_index
                .get(callee_member)
                .and_then(|by_arg_index| {
                    usize::try_from(arg_index)
                        .ok()
                        .and_then(|arg_index| by_arg_index.get(&arg_index))
                })
                .unwrap_or(&self.empty_source_unit_support),
            None => self
                .call_arguments_by_member
                .get(callee_member)
                .unwrap_or(&self.empty_source_unit_support),
        }
    }

    fn call_arguments_from_source_unit(
        &self,
        callee_member: &str,
        arg_index: Option<u32>,
    ) -> &BTreeSet<(SourceUnitId, SourceUnitId)> {
        match arg_index {
            Some(arg_index) => self
                .call_arguments_from_source_unit_by_member_arg_index
                .get(callee_member)
                .and_then(|by_arg_index| {
                    usize::try_from(arg_index)
                        .ok()
                        .and_then(|arg_index| by_arg_index.get(&arg_index))
                })
                .unwrap_or(&self.empty_source_unit_pair_support),
            None => self
                .call_arguments_from_source_unit_by_member
                .get(callee_member)
                .unwrap_or(&self.empty_source_unit_pair_support),
        }
    }

    fn makes_decorate_call_for_source_unit(
        &self,
        member: Option<&str>,
    ) -> &BTreeSet<(SourceUnitId, SourceUnitId)> {
        match member {
            Some(member) => self
                .makes_decorate_call_for_source_unit_by_member
                .get(member)
                .unwrap_or(&self.empty_source_unit_pair_support),
            None => &self.makes_decorate_call_for_source_unit_all,
        }
    }

    fn intrinsic_alias_referenced_by(
        &self,
        property: &str,
    ) -> &BTreeSet<(SourceUnitId, SourceUnitId)> {
        self.intrinsic_alias_referenced_by_property
            .get(property)
            .unwrap_or(&self.empty_source_unit_pair_support)
    }
}

#[derive(Debug, Default)]
struct DerivedFactRequirements {
    references_unit: bool,
    aliases_unit: bool,
    member_reads: bool,
    reads_member_of_unit: bool,
    module_member_uses: bool,
    call_arguments: bool,
    call_arguments_from_unit: bool,
    makes_decorate_call_for_unit: bool,
    intrinsic_alias_referenced_by: bool,
}

impl DerivedFactRequirements {
    fn from_program(program: &SelectorProgram) -> Self {
        let mut requirements = Self::default();
        for atom in &program.atoms {
            match atom {
                SelectorAtom::SourceUnitReferencesSourceUnit { .. } => {
                    requirements.references_unit = true;
                }
                SelectorAtom::SourceUnitAliasesSourceUnit { .. } => {
                    requirements.aliases_unit = true;
                }
                SelectorAtom::ReadsMember { .. } => {
                    requirements.member_reads = true;
                }
                SelectorAtom::ReadsMemberOfSourceUnit { .. } => {
                    requirements.reads_member_of_unit = true;
                }
                SelectorAtom::ConsumesModuleMember { .. } => {
                    requirements.module_member_uses = true;
                }
                SelectorAtom::PassedToCall { .. } => {
                    requirements.call_arguments = true;
                }
                SelectorAtom::PassedToCallOfSourceUnit { .. } => {
                    requirements.call_arguments_from_unit = true;
                }
                SelectorAtom::MakesDecorateCallForSourceUnit { .. } => {
                    requirements.makes_decorate_call_for_unit = true;
                }
                SelectorAtom::IntrinsicAlias { .. } => {
                    requirements.intrinsic_alias_referenced_by = true;
                }
                SelectorAtom::SourceUnitKind { .. }
                | SelectorAtom::SourceUnitDeclaresBinding { .. }
                | SelectorAtom::ProjectedAllowedTuples { .. } => {}
            }
        }
        requirements
    }
}

#[derive(Debug, Default)]
struct FactDomains {
    source_units: BTreeSet<SourceUnitId>,
    strings: BTreeSet<String>,
    source_unit_kinds: BTreeSet<(SourceUnitId, String)>,
    source_unit_statement_ordinals: BTreeSet<(SourceUnitId, StatementOrdinal)>,
    declared_bindings: BTreeSet<(SourceUnitId, String)>,
    raw_source_unit_references_binding: BTreeSet<(SourceUnitId, String, String)>,
    references_unit: BTreeSet<(SourceUnitId, SourceUnitId)>,
    aliases_unit: BTreeSet<(SourceUnitId, SourceUnitId)>,
    raw_member_reads: BTreeSet<(StatementOrdinal, Option<String>, String)>,
    member_reads: BTreeSet<(SourceUnitId, String)>,
    reads_member_of_unit: BTreeSet<(SourceUnitId, SourceUnitId, String)>,
    raw_module_member_uses: BTreeSet<(StatementOrdinal, String, String)>,
    module_member_uses: BTreeSet<(SourceUnitId, String, String)>,
    raw_call_arguments: BTreeSet<(String, Option<String>, String, usize)>,
    call_arguments: BTreeSet<(SourceUnitId, String, usize)>,
    call_arguments_from_unit: BTreeSet<(SourceUnitId, SourceUnitId, String, usize)>,
    decorate_calls: BTreeSet<(String, String, Option<String>)>,
    makes_decorate_call_for_unit: BTreeSet<(SourceUnitId, SourceUnitId, Option<String>)>,
    intrinsic_aliases: BTreeSet<(String, String)>,
    intrinsic_alias_referenced_by: BTreeSet<(SourceUnitId, String, SourceUnitId)>,
    source_unit_kinds_index: SourceUnitStringIndex,
    declared_bindings_index: SourceUnitStringIndex,
    relation_supports: RelationSupportCache,
}

impl FactDomains {
    fn from_program_and_facts(program: &SelectorProgram, facts: &SelectorFactStore) -> Self {
        let mut domains = Self::default();
        let requirements = DerivedFactRequirements::from_program(program);
        domains.add_facts(facts);
        domains.add_derived_facts(&requirements);
        domains.add_program_constants(program);
        domains.build_lookup_indexes();
        domains
    }

    fn values_for(&self, domain: VariableDomain) -> Vec<ConstraintValue> {
        match domain {
            VariableDomain::SourceUnit => self
                .source_units
                .iter()
                .copied()
                .map(ConstraintValue::SourceUnit)
                .collect(),
            VariableDomain::String => self
                .strings
                .iter()
                .cloned()
                .map(ConstraintValue::String)
                .collect(),
        }
    }

    fn add_facts(&mut self, facts: &SelectorFactStore) {
        for fact in &facts.facts {
            match fact {
                SelectorFact::SourceUnit {
                    source_unit,
                    statement_ordinal,
                    statement_kind,
                    ..
                } => {
                    self.add_source_unit(*source_unit);
                    self.add_string(statement_kind);
                    self.source_unit_kinds
                        .insert((*source_unit, statement_kind.clone()));
                    self.source_unit_statement_ordinals
                        .insert((*source_unit, *statement_ordinal));
                }
                SelectorFact::DeclaredBinding {
                    source_unit,
                    binding,
                    ..
                } => {
                    self.add_source_unit(*source_unit);
                    self.add_string(binding);
                    self.declared_bindings
                        .insert((*source_unit, binding.clone()));
                }
                SelectorFact::SourceUnitReferencesBinding {
                    source_unit,
                    binding,
                    edge_kind,
                    ..
                } => {
                    self.add_source_unit(*source_unit);
                    self.add_string(binding);
                    self.add_string(edge_kind);
                    self.raw_source_unit_references_binding.insert((
                        *source_unit,
                        binding.clone(),
                        edge_kind.clone(),
                    ));
                }
                SelectorFact::MemberRead {
                    statement_ordinal,
                    object,
                    member,
                    ..
                } => {
                    if let Some(object) = object {
                        self.add_string(object);
                    }
                    self.add_string(member);
                    self.raw_member_reads.insert((
                        *statement_ordinal,
                        object.clone(),
                        member.clone(),
                    ));
                }
                SelectorFact::ModuleMemberUse {
                    statement_ordinal,
                    module,
                    member,
                    ..
                } => {
                    self.add_string(module);
                    self.add_string(member);
                    self.raw_module_member_uses.insert((
                        *statement_ordinal,
                        module.clone(),
                        member.clone(),
                    ));
                }
                SelectorFact::CallArgumentUse {
                    argument,
                    callee_object,
                    callee_member,
                    arg_index,
                    ..
                } => {
                    self.add_string(argument);
                    if let Some(callee_object) = callee_object {
                        self.add_string(callee_object);
                    }
                    self.add_string(callee_member);
                    self.raw_call_arguments.insert((
                        argument.clone(),
                        callee_object.clone(),
                        callee_member.clone(),
                        *arg_index,
                    ));
                }
                SelectorFact::DecorateCallUse {
                    callee,
                    class_anchor,
                    member,
                    ..
                } => {
                    self.add_string(callee);
                    self.add_string(class_anchor);
                    if let Some(member) = member {
                        self.add_string(member);
                    }
                    self.decorate_calls.insert((
                        callee.clone(),
                        class_anchor.clone(),
                        member.clone(),
                    ));
                }
                SelectorFact::IntrinsicAliasUse {
                    binding, property, ..
                } => {
                    self.add_string(binding);
                    self.add_string(property);
                    self.intrinsic_aliases
                        .insert((binding.clone(), property.clone()));
                }
            }
        }
    }

    fn build_lookup_indexes(&mut self) {
        self.source_unit_kinds_index = SourceUnitStringIndex::from_facts(&self.source_unit_kinds);
        self.declared_bindings_index = SourceUnitStringIndex::from_facts(&self.declared_bindings);
        self.relation_supports = RelationSupportCache::from_domains(self);
    }

    fn discard_unneeded_raw_relations(&mut self) {
        self.raw_source_unit_references_binding.clear();
        self.raw_member_reads.clear();
        self.raw_module_member_uses.clear();
        self.raw_call_arguments.clear();
        self.decorate_calls.clear();
        self.intrinsic_aliases.clear();
    }

    fn discard_full_domain_source_sets(&mut self) {
        self.source_units.clear();
        self.strings.clear();
    }

    fn add_derived_facts(&mut self, requirements: &DerivedFactRequirements) {
        let mut source_units_with_declarations = BTreeSet::new();
        let mut source_units_by_binding: BTreeMap<String, BTreeSet<SourceUnitId>> = BTreeMap::new();
        for (source_unit, binding) in &self.declared_bindings {
            source_units_with_declarations.insert(*source_unit);
            source_units_by_binding
                .entry(binding.clone())
                .or_default()
                .insert(*source_unit);
        }

        if requirements.references_unit {
            for (source_unit, binding, _edge_kind) in &self.raw_source_unit_references_binding {
                if !source_units_with_declarations.contains(source_unit) {
                    continue;
                }
                if let Some(referenced_source_units) = source_units_by_binding.get(binding) {
                    self.references_unit.extend(
                        referenced_source_units
                            .iter()
                            .map(|referenced| (*source_unit, *referenced)),
                    );
                }
            }
        }

        if requirements.aliases_unit {
            let var_decl_source_units = self
                .source_unit_kinds
                .iter()
                .filter_map(|(source_unit, kind)| (kind == "var_decl").then_some(*source_unit))
                .collect::<BTreeSet<_>>();
            for (source_unit, binding, edge_kind) in &self.raw_source_unit_references_binding {
                if (edge_kind != "eager_use" && edge_kind != "hoisted_use")
                    || !var_decl_source_units.contains(source_unit)
                    || !source_units_with_declarations.contains(source_unit)
                {
                    continue;
                }
                if let Some(aliased_source_units) = source_units_by_binding.get(binding) {
                    self.aliases_unit.extend(
                        aliased_source_units
                            .iter()
                            .map(|aliased| (*source_unit, *aliased)),
                    );
                }
            }
        }

        let needs_source_unit_by_ordinal = requirements.member_reads
            || requirements.reads_member_of_unit
            || requirements.module_member_uses;
        let source_unit_by_ordinal = needs_source_unit_by_ordinal.then(|| {
            self.source_unit_statement_ordinals
                .iter()
                .map(|(source_unit, ordinal)| (*ordinal, *source_unit))
                .collect::<BTreeMap<_, _>>()
        });

        // Reads of `object.member` by a declaring source unit, which
        // `reads_member_of_unit` joins to the source units declaring `object`.
        let mut member_reads_from_binding = BTreeSet::new();
        if requirements.member_reads || requirements.reads_member_of_unit {
            let source_unit_by_ordinal = source_unit_by_ordinal
                .as_ref()
                .expect("source unit ordinal index should be built for member reads");
            for (statement_ordinal, object, member) in &self.raw_member_reads {
                let Some(source_unit) = source_unit_by_ordinal.get(statement_ordinal) else {
                    continue;
                };
                if !source_units_with_declarations.contains(source_unit) {
                    continue;
                }
                if requirements.member_reads {
                    self.member_reads.insert((*source_unit, member.clone()));
                }
                if requirements.reads_member_of_unit
                    && let Some(object) = object
                {
                    member_reads_from_binding.insert((
                        *source_unit,
                        object.clone(),
                        member.clone(),
                    ));
                }
            }
        }

        if requirements.reads_member_of_unit {
            for (source_unit, object_binding, member) in &member_reads_from_binding {
                if let Some(object_source_units) = source_units_by_binding.get(object_binding) {
                    self.reads_member_of_unit
                        .extend(object_source_units.iter().map(|object_source_unit| {
                            (*source_unit, *object_source_unit, member.clone())
                        }));
                }
            }
        }

        if requirements.module_member_uses {
            let source_unit_by_ordinal = source_unit_by_ordinal
                .as_ref()
                .expect("source unit ordinal index should be built for module member uses");
            for (statement_ordinal, module, member) in &self.raw_module_member_uses {
                let Some(source_unit) = source_unit_by_ordinal.get(statement_ordinal) else {
                    continue;
                };
                if !source_units_with_declarations.contains(source_unit) {
                    continue;
                }
                self.module_member_uses
                    .insert((*source_unit, module.clone(), member.clone()));
            }
        }

        if requirements.call_arguments || requirements.call_arguments_from_unit {
            for (argument, callee_object, callee_member, arg_index) in &self.raw_call_arguments {
                let Some(argument_source_units) = source_units_by_binding.get(argument) else {
                    continue;
                };
                for source_unit in argument_source_units {
                    if requirements.call_arguments {
                        self.call_arguments.insert((
                            *source_unit,
                            callee_member.clone(),
                            *arg_index,
                        ));
                    }
                    if requirements.call_arguments_from_unit
                        && let Some(callee_object) = callee_object
                        && let Some(callee_object_source_units) =
                            source_units_by_binding.get(callee_object)
                    {
                        self.call_arguments_from_unit.extend(
                            callee_object_source_units
                                .iter()
                                .map(|callee_object_source_unit| {
                                    (
                                        *source_unit,
                                        *callee_object_source_unit,
                                        callee_member.clone(),
                                        *arg_index,
                                    )
                                }),
                        );
                    }
                }
            }
        }

        if requirements.makes_decorate_call_for_unit {
            for (callee, class_anchor, member) in &self.decorate_calls {
                let Some(callee_source_units) = source_units_by_binding.get(callee) else {
                    continue;
                };
                for source_unit in callee_source_units {
                    if let Some(class_source_units) = source_units_by_binding.get(class_anchor) {
                        self.makes_decorate_call_for_unit
                            .extend(class_source_units.iter().map(|class_source_unit| {
                                (*source_unit, *class_source_unit, member.clone())
                            }));
                    }
                }
            }
        }

        if requirements.intrinsic_alias_referenced_by {
            let mut raw_referencers_by_binding: BTreeMap<&str, Vec<SourceUnitId>> = BTreeMap::new();
            for (referencer, binding, _edge_kind) in &self.raw_source_unit_references_binding {
                raw_referencers_by_binding
                    .entry(binding.as_str())
                    .or_default()
                    .push(*referencer);
            }
            for (binding, property) in &self.intrinsic_aliases {
                let Some(alias_source_units) = source_units_by_binding.get(binding) else {
                    continue;
                };
                if let Some(referencers) = raw_referencers_by_binding.get(binding.as_str()) {
                    self.intrinsic_alias_referenced_by
                        .extend(alias_source_units.iter().flat_map(|alias_source_unit| {
                            referencers.iter().map(|referencer| {
                                (*alias_source_unit, property.clone(), *referencer)
                            })
                        }));
                }
            }
        }
    }

    fn add_program_constants(&mut self, program: &SelectorProgram) {
        for atom in &program.atoms {
            self.add_atom_constants(atom);
        }
    }

    fn add_atom_constants(&mut self, atom: &SelectorAtom) {
        match atom {
            SelectorAtom::SourceUnitKind {
                source_unit,
                statement_kind,
            } => {
                self.add_source_unit_term(source_unit);
                self.add_string_term(statement_kind);
            }
            SelectorAtom::SourceUnitDeclaresBinding {
                source_unit,
                binding,
            } => {
                self.add_source_unit_term(source_unit);
                self.add_string_term(binding);
            }
            SelectorAtom::ProjectedAllowedTuples { rows, .. } => {
                for row in rows {
                    for value in row {
                        self.add_projected_value(value);
                    }
                }
            }
            SelectorAtom::SourceUnitReferencesSourceUnit {
                source_unit,
                referenced,
            }
            | SelectorAtom::SourceUnitAliasesSourceUnit {
                source_unit,
                aliased: referenced,
            } => {
                self.add_source_unit_term(source_unit);
                self.add_source_unit_term(referenced);
            }
            SelectorAtom::ReadsMember {
                source_unit,
                member,
            } => {
                self.add_source_unit_term(source_unit);
                self.add_string_term(member);
            }
            SelectorAtom::ReadsMemberOfSourceUnit {
                source_unit,
                object,
                member,
            } => {
                self.add_source_unit_term(source_unit);
                self.add_source_unit_term(object);
                self.add_string_term(member);
            }
            SelectorAtom::ConsumesModuleMember {
                source_unit,
                module,
                member,
            } => {
                self.add_source_unit_term(source_unit);
                self.add_string_term(module);
                self.add_string_term(member);
            }
            SelectorAtom::PassedToCall {
                source_unit,
                callee_member,
                ..
            } => {
                self.add_source_unit_term(source_unit);
                self.add_string_term(callee_member);
            }
            SelectorAtom::PassedToCallOfSourceUnit {
                source_unit,
                callee_object,
                callee_member,
                ..
            } => {
                self.add_source_unit_term(source_unit);
                self.add_source_unit_term(callee_object);
                self.add_string_term(callee_member);
            }
            SelectorAtom::MakesDecorateCallForSourceUnit {
                source_unit,
                class_anchor,
                member,
            } => {
                self.add_source_unit_term(source_unit);
                self.add_source_unit_term(class_anchor);
                if let Some(member) = member {
                    self.add_string_term(member);
                }
            }
            SelectorAtom::IntrinsicAlias {
                source_unit,
                property,
                referenced_by,
            } => {
                self.add_source_unit_term(source_unit);
                self.add_string_term(property);
                self.add_source_unit_term(referenced_by);
            }
        }
    }

    fn add_source_unit_term(&mut self, term: &SourceUnitTerm) {
        if let SourceUnitTerm::Const { source_unit } = term {
            self.add_source_unit(*source_unit);
        }
    }

    fn add_string_term(&mut self, term: &StringTerm) {
        if let StringTerm::Const { value } = term {
            self.add_string(value);
        }
    }

    fn add_projected_value(&mut self, value: &SelectorProjectedValue) {
        match value {
            SelectorProjectedValue::SourceUnit(value) => self.add_source_unit(*value),
            SelectorProjectedValue::String(value) => self.add_string(value),
        }
    }

    fn add_source_unit(&mut self, source_unit: SourceUnitId) {
        self.source_units.insert(source_unit);
    }

    fn add_string(&mut self, value: &str) {
        self.strings.insert(value.to_string());
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use analysis::SourceUnitId;
    use selector_constraint_backend::{AllowedTupleConstraintId, BackendValueId, ConstraintValue};
    use selector_ir::ClaimKind;
    use selector_test_fixtures::{
        broad_specific_targets, declared_binding, member_read, module_member_use, source_unit_fact,
    };

    #[derive(Debug, Clone, PartialEq, Eq)]
    struct AllowedTupleConstraint {
        id: AllowedTupleConstraintId,
        variables: Vec<ConstraintVariableId>,
        tuples: Vec<Vec<ConstraintValue>>,
    }

    fn source_unit(value: usize) -> ConstraintValue {
        ConstraintValue::SourceUnit(SourceUnitId(value))
    }

    fn string(value: &str) -> ConstraintValue {
        ConstraintValue::String(value.to_string())
    }

    fn source_unit_reference(source_unit: usize, binding: &str, edge_kind: &str) -> SelectorFact {
        SelectorFact::SourceUnitReferencesBinding {
            source_unit: SourceUnitId(source_unit),
            binding: binding.to_string(),
            edge_kind: edge_kind.to_string(),
        }
    }

    fn decorate_call(callee: &str, class_anchor: &str, member: Option<&str>) -> SelectorFact {
        SelectorFact::DecorateCallUse {
            callee: callee.to_string(),
            class_anchor: class_anchor.to_string(),
            member: member.map(str::to_string),
        }
    }

    fn intrinsic_alias(binding: &str, property: &str) -> SelectorFact {
        SelectorFact::IntrinsicAliasUse {
            binding: binding.to_string(),
            property: property.to_string(),
        }
    }

    fn fact_store(facts: Vec<SelectorFact>) -> SelectorFactStore {
        SelectorFactStore { facts }
    }

    fn allowed_tuples_for(
        model: &CompiledSelectorProblem,
        variables: &[ConstraintVariableId],
    ) -> AllowedTupleConstraint {
        let constraint = model
            .allowed_tuples
            .iter()
            .find(|constraint| constraint.variables == variables)
            .unwrap();
        AllowedTupleConstraint {
            id: constraint.id,
            variables: constraint.variables.clone(),
            tuples: model
                .allowed_tuple_rows(constraint)
                .iter()
                .map(|tuple| decode_tuple(model, &constraint.variables, tuple))
                .collect(),
        }
    }

    fn satisfying_tuples_for(
        model: &CompiledSelectorProblem,
        variables: &[ConstraintVariableId],
    ) -> Vec<Vec<ConstraintValue>> {
        let mut rows = BTreeSet::new();
        let mut assignment = BTreeMap::new();
        collect_satisfying_tuples(model, variables, 0, &mut assignment, &mut rows);
        rows.into_iter().collect()
    }

    fn collect_satisfying_tuples(
        model: &CompiledSelectorProblem,
        variables: &[ConstraintVariableId],
        variable_index: usize,
        assignment: &mut BTreeMap<ConstraintVariableId, BackendValueId>,
        rows: &mut BTreeSet<Vec<ConstraintValue>>,
    ) {
        let Some(variable) = model.variables.get(variable_index) else {
            if model_constraints_satisfied(model, assignment)
                && let Some(row) = variables
                    .iter()
                    .map(|variable| {
                        assignment
                            .get(variable)
                            .map(|value| decode_variable_value(model, *variable, *value))
                    })
                    .collect::<Option<Vec<_>>>()
            {
                rows.insert(row);
            }
            return;
        };

        for value in model.variable_domain_values(variable) {
            assignment.insert(variable.id, value);
            collect_satisfying_tuples(model, variables, variable_index + 1, assignment, rows);
        }
        assignment.remove(&variable.id);
    }

    fn model_constraints_satisfied(
        model: &CompiledSelectorProblem,
        assignment: &BTreeMap<ConstraintVariableId, BackendValueId>,
    ) -> bool {
        model.allowed_tuples.iter().all(|constraint| {
            constraint
                .variables
                .iter()
                .map(|variable| assignment.get(variable).copied())
                .collect::<Option<Vec<_>>>()
                .is_some_and(|row| model.allowed_tuple_rows(constraint).contains(&row))
        }) && model.all_different.iter().all(|constraint| {
            let mut seen = BTreeSet::new();
            constraint.variables.iter().all(|variable| {
                assignment
                    .get(variable)
                    .is_some_and(|value| seen.insert(*value))
            })
        })
    }

    fn decode_tuple(
        model: &CompiledSelectorProblem,
        variables: &[ConstraintVariableId],
        values: &[BackendValueId],
    ) -> Vec<ConstraintValue> {
        variables
            .iter()
            .zip(values.iter())
            .map(|(variable, value)| decode_variable_value(model, *variable, *value))
            .collect()
    }

    fn decode_variable_value(
        model: &CompiledSelectorProblem,
        variable: ConstraintVariableId,
        value: BackendValueId,
    ) -> ConstraintValue {
        let variable = &model.variables[variable.0];
        model
            .decode_value(variable.domain, value)
            .expect("test fixture assigns values from the variable domain")
    }

    fn decoded_variable_domain(
        model: &CompiledSelectorProblem,
        variable: ConstraintVariableId,
    ) -> Vec<ConstraintValue> {
        model
            .variable_domain_values(&model.variables[variable.0])
            .iter()
            .map(|value| decode_variable_value(model, variable, *value))
            .collect()
    }

    #[test]
    fn duplicate_variables_in_allowed_tuple_atoms_are_merged() {
        let mut program = SelectorProgram::default();
        let source_unit_var =
            program.add_variable(VariableDomain::SourceUnit, Some("source unit".to_string()));
        program.add_atom(SelectorAtom::SourceUnitReferencesSourceUnit {
            source_unit: SourceUnitTerm::Var {
                id: source_unit_var,
            },
            referenced: SourceUnitTerm::Var {
                id: source_unit_var,
            },
        });

        let facts = fact_store(vec![
            source_unit_fact(10, 0, "var"),
            source_unit_fact(20, 1, "var"),
            source_unit_fact(30, 2, "var"),
            declared_binding(10, "a"),
            declared_binding(20, "b"),
            declared_binding(30, "c"),
            source_unit_reference(10, "a", "eager_use"),
            source_unit_reference(10, "b", "eager_use"),
            source_unit_reference(20, "b", "eager_use"),
            source_unit_reference(30, "a", "eager_use"),
        ]);

        let model = compile_selector_problem(&program, &facts).unwrap();

        assert_eq!(
            decoded_variable_domain(&model, ConstraintVariableId(0)),
            vec![source_unit(10), source_unit(20)]
        );
    }

    #[test]
    fn single_row_projected_allowed_tuples_restricts_domains_without_table() {
        let mut program = SelectorProgram::default();
        let source_unit_var =
            program.add_variable(VariableDomain::SourceUnit, Some("source unit".to_string()));
        let binding_var = program.add_variable(VariableDomain::String, Some("binding".to_string()));
        program.add_atom(SelectorAtom::ProjectedAllowedTuples {
            variables: vec![source_unit_var, binding_var],
            rows: vec![vec![
                SelectorProjectedValue::SourceUnit(SourceUnitId(7)),
                SelectorProjectedValue::String("actual".to_string()),
            ]],
        });

        let model = compile_selector_problem(&program, &fact_store(vec![])).unwrap();

        assert_eq!(model.allowed_tuples, vec![]);
        assert_eq!(
            decoded_variable_domain(&model, ConstraintVariableId(0)),
            vec![source_unit(7)]
        );
        assert_eq!(
            decoded_variable_domain(&model, ConstraintVariableId(1)),
            vec![string("actual")]
        );
    }

    #[test]
    fn multi_row_projected_allowed_tuples_preserves_correlation() {
        let mut program = SelectorProgram::default();
        let source_unit_var =
            program.add_variable(VariableDomain::SourceUnit, Some("source unit".to_string()));
        let binding_var = program.add_variable(VariableDomain::String, Some("binding".to_string()));
        program.add_atom(SelectorAtom::ProjectedAllowedTuples {
            variables: vec![source_unit_var, binding_var],
            rows: vec![
                vec![
                    SelectorProjectedValue::SourceUnit(SourceUnitId(7)),
                    SelectorProjectedValue::String("actual".to_string()),
                ],
                vec![
                    SelectorProjectedValue::SourceUnit(SourceUnitId(8)),
                    SelectorProjectedValue::String("other".to_string()),
                ],
            ],
        });

        let model = compile_selector_problem(&program, &fact_store(vec![])).unwrap();

        assert_eq!(
            decoded_variable_domain(&model, ConstraintVariableId(0)),
            vec![source_unit(7), source_unit(8)]
        );
        assert_eq!(
            decoded_variable_domain(&model, ConstraintVariableId(1)),
            vec![string("actual"), string("other")]
        );
        assert_eq!(
            satisfying_tuples_for(&model, &[ConstraintVariableId(0), ConstraintVariableId(1)]),
            vec![
                vec![source_unit(7), string("actual")],
                vec![source_unit(8), string("other")]
            ]
        );
    }

    #[test]
    fn target_injectivity_prunes_fixed_values_from_broad_targets() {
        let (program, facts, broad_target, strict_target) = broad_specific_targets();

        let model = compile_selector_problem(&program, &facts).unwrap();

        let source_unit_domain = |target| {
            let projection = model
                .target_projections
                .iter()
                .find(|projection| projection.target == target)
                .unwrap();
            decoded_variable_domain(&model, projection.source_unit_variable)
        };
        // The strict target can only be source unit 20, so injectivity removes source unit 20
        // from the broad target's domain and nothing is left to be all-different.
        assert_eq!(source_unit_domain(broad_target), vec![source_unit(10)]);
        assert_eq!(source_unit_domain(strict_target), vec![source_unit(20)]);
        assert!(model.all_different.is_empty());
    }

    #[test]
    fn target_binding_projection_preserves_binding_variable() {
        let mut program = SelectorProgram::default();
        let source_unit_var =
            program.add_variable(VariableDomain::SourceUnit, Some("source unit".to_string()));
        let binding_var = program.add_variable(VariableDomain::String, Some("binding".to_string()));
        let target = program.add_target(
            source_unit_var,
            "module",
            ClaimKind::Binding { export_name: None },
        );
        program.add_atom(SelectorAtom::SourceUnitDeclaresBinding {
            source_unit: SourceUnitTerm::Var {
                id: source_unit_var,
            },
            binding: StringTerm::Var { id: binding_var },
        });

        let facts = fact_store(vec![
            source_unit_fact(7, 0, "var"),
            source_unit_fact(8, 1, "var"),
            declared_binding(7, "actual"),
            declared_binding(8, "other"),
        ]);

        let model = compile_selector_problem(&program, &facts).unwrap();

        assert_eq!(model.target_projections.len(), 1);
        assert_eq!(model.target_projections[0].target, target);
        assert_eq!(
            model.target_projections[0].source_unit_variable,
            ConstraintVariableId(0)
        );
        assert_eq!(
            model.target_projections[0].binding_projection,
            Some(TargetBindingProjection::Variable(ConstraintVariableId(1)))
        );
        assert_eq!(
            allowed_tuples_for(&model, &[ConstraintVariableId(0), ConstraintVariableId(1)]),
            AllowedTupleConstraint {
                id: AllowedTupleConstraintId(0),
                variables: vec![ConstraintVariableId(0), ConstraintVariableId(1)],
                tuples: vec![
                    vec![source_unit(7), string("actual")],
                    vec![source_unit(8), string("other")],
                ],
            }
        );
    }

    #[test]
    fn relational_atoms_lower_to_allowed_tuple_constraints() {
        let mut program = SelectorProgram::default();
        let referenced_source_unit = program.add_variable(
            VariableDomain::SourceUnit,
            Some("referenced_source_unit".to_string()),
        );
        let aliased_source_unit = program.add_variable(
            VariableDomain::SourceUnit,
            Some("aliased_source_unit".to_string()),
        );
        let reader_source_unit = program.add_variable(
            VariableDomain::SourceUnit,
            Some("reader_source_unit".to_string()),
        );
        let object_source_unit = program.add_variable(
            VariableDomain::SourceUnit,
            Some("object_source_unit".to_string()),
        );
        let class_source_unit = program.add_variable(
            VariableDomain::SourceUnit,
            Some("class_source_unit".to_string()),
        );
        let intrinsic_referencer = program.add_variable(
            VariableDomain::SourceUnit,
            Some("intrinsic_referencer".to_string()),
        );
        let read_member =
            program.add_variable(VariableDomain::String, Some("read_member".to_string()));
        let module_consumer = program.add_variable(
            VariableDomain::SourceUnit,
            Some("module_consumer".to_string()),
        );

        program.add_atom(SelectorAtom::SourceUnitReferencesSourceUnit {
            source_unit: SourceUnitTerm::Const {
                source_unit: SourceUnitId(20),
            },
            referenced: SourceUnitTerm::Var {
                id: referenced_source_unit,
            },
        });
        program.add_atom(SelectorAtom::SourceUnitAliasesSourceUnit {
            source_unit: SourceUnitTerm::Const {
                source_unit: SourceUnitId(30),
            },
            aliased: SourceUnitTerm::Var {
                id: aliased_source_unit,
            },
        });
        program.add_atom(SelectorAtom::ReadsMember {
            source_unit: SourceUnitTerm::Var {
                id: reader_source_unit,
            },
            member: StringTerm::Var { id: read_member },
        });
        program.add_atom(SelectorAtom::ReadsMemberOfSourceUnit {
            source_unit: SourceUnitTerm::Const {
                source_unit: SourceUnitId(40),
            },
            object: SourceUnitTerm::Var {
                id: object_source_unit,
            },
            member: StringTerm::Const {
                value: "value".to_string(),
            },
        });
        program.add_atom(SelectorAtom::ConsumesModuleMember {
            source_unit: SourceUnitTerm::Var {
                id: module_consumer,
            },
            module: StringTerm::Const {
                value: "./accessors".to_string(),
            },
            member: StringTerm::Const {
                value: "Widget".to_string(),
            },
        });
        program.add_atom(SelectorAtom::MakesDecorateCallForSourceUnit {
            source_unit: SourceUnitTerm::Const {
                source_unit: SourceUnitId(60),
            },
            class_anchor: SourceUnitTerm::Var {
                id: class_source_unit,
            },
            member: Some(StringTerm::Const {
                value: "field".to_string(),
            }),
        });
        program.add_atom(SelectorAtom::IntrinsicAlias {
            source_unit: SourceUnitTerm::Const {
                source_unit: SourceUnitId(80),
            },
            property: StringTerm::Const {
                value: "defineProperty".to_string(),
            },
            referenced_by: SourceUnitTerm::Var {
                id: intrinsic_referencer,
            },
        });

        let facts = fact_store(vec![
            source_unit_fact(10, 0, "function"),
            declared_binding(10, "target"),
            source_unit_fact(20, 1, "function"),
            declared_binding(20, "referrer"),
            source_unit_reference(20, "target", "read"),
            source_unit_fact(30, 2, "var_decl"),
            declared_binding(30, "aliasStatement"),
            source_unit_reference(30, "target", "eager_use"),
            source_unit_fact(40, 3, "function"),
            declared_binding(40, "reader"),
            member_read(3, None, "size"),
            member_read(3, Some("objectBinding"), "value"),
            source_unit_fact(50, 4, "var_decl"),
            declared_binding(50, "objectBinding"),
            source_unit_fact(60, 5, "function"),
            declared_binding(60, "decorate"),
            source_unit_fact(65, 55, "function"),
            declared_binding(65, "moduleConsumer"),
            module_member_use(55, "./accessors", "Widget"),
            source_unit_fact(66, 56, "function"),
            declared_binding(66, "otherModuleConsumer"),
            module_member_use(56, "./accessors", "Other"),
            // Same member of a different module: not a consumer of `./accessors`.
            source_unit_fact(67, 57, "function"),
            declared_binding(67, "otherSourceConsumer"),
            module_member_use(57, "./other", "Widget"),
            source_unit_fact(70, 6, "class"),
            declared_binding(70, "Class"),
            decorate_call("decorate", "Class", Some("field")),
            source_unit_fact(80, 7, "var_decl"),
            declared_binding(80, "define"),
            intrinsic_alias("define", "defineProperty"),
            source_unit_fact(90, 8, "function"),
            declared_binding(90, "aliasUser"),
            source_unit_reference(90, "define", "read"),
        ]);

        let model = compile_selector_problem(&program, &facts).unwrap();

        assert_eq!(
            decoded_variable_domain(&model, ConstraintVariableId(0)),
            vec![source_unit(10)]
        );
        assert_eq!(
            decoded_variable_domain(&model, ConstraintVariableId(1)),
            vec![source_unit(10)]
        );
        assert_eq!(
            satisfying_tuples_for(&model, &[ConstraintVariableId(2), ConstraintVariableId(6)]),
            vec![
                vec![source_unit(40), string("size")],
                vec![source_unit(40), string("value")],
            ]
        );
        assert_eq!(
            decoded_variable_domain(&model, ConstraintVariableId(3)),
            vec![source_unit(50)]
        );
        assert_eq!(
            decoded_variable_domain(&model, ConstraintVariableId(4)),
            vec![source_unit(70)]
        );
        assert_eq!(
            decoded_variable_domain(&model, ConstraintVariableId(5)),
            vec![source_unit(90)]
        );
        assert_eq!(
            decoded_variable_domain(&model, ConstraintVariableId(7)),
            vec![source_unit(65)]
        );
    }

    #[test]
    fn unsupported_atoms_fail_closed() {
        let mut program = SelectorProgram::default();
        let source_unit_var =
            program.add_variable(VariableDomain::SourceUnit, Some("source unit".to_string()));
        let member_var = program.add_variable(VariableDomain::String, Some("member".to_string()));
        program.add_atom(SelectorAtom::PassedToCall {
            source_unit: SourceUnitTerm::Var {
                id: source_unit_var,
            },
            callee_member: StringTerm::Var { id: member_var },
            arg_index: None,
        });

        let facts = fact_store(vec![source_unit_fact(10, 0, "var")]);

        let err = compile_selector_problem(&program, &facts).unwrap_err();
        assert!(matches!(
            err,
            CompiledSelectorProblemBuildError::UnsupportedAtom { .. }
        ));
    }

    #[test]
    fn unsatisfied_constant_only_atoms_fail_closed() {
        let mut program = SelectorProgram::default();
        program.add_atom(SelectorAtom::SourceUnitDeclaresBinding {
            source_unit: SourceUnitTerm::Const {
                source_unit: SourceUnitId(10),
            },
            binding: StringTerm::Const {
                value: "missing".to_string(),
            },
        });

        let facts = fact_store(vec![source_unit_fact(10, 0, "var")]);

        let err = compile_selector_problem(&program, &facts).unwrap_err();
        assert!(matches!(
            err,
            CompiledSelectorProblemBuildError::ConstantOnlyAtomUnsatisfied { .. }
        ));
    }
}
