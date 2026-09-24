//! Lowering from selector IR plus facts into a compact finite-domain problem.

use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;
use std::time::Instant;

use analysis::{OwnerId, StatementOrdinal};
use selector_constraint_backend::{
    AllDifferentReason, AllowedTupleRowsId, BackendValueId, CompiledSelectorProblem,
    CompiledSelectorProblemBuilder, CompiledSelectorProblemError, ConstraintValue,
    ConstraintVariableId, SharedVariableDomainId, TargetBindingProjection,
};
use selector_ir::{
    ClaimKind, OwnerTerm, SelectorAtom, SelectorFact, SelectorFactStore, SelectorProgram,
    SelectorProgramError, SelectorProjectedValue, SelectorVariableId, StringTerm, VariableDomain,
};

pub fn compile_selector_problem(
    program: &SelectorProgram,
    facts: &SelectorFactStore,
) -> Result<CompiledSelectorProblem, CompiledSelectorProblemBuildError> {
    Ok(compile_selector_problem_with_summary(program, facts)?.problem)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompiledSelectorProblemWithSummary {
    pub problem: CompiledSelectorProblem,
    pub summary: SelectorModelBuildSummary,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SelectorModelBuildSummary {
    pub domain_value_counts: BTreeMap<&'static str, usize>,
    pub stored_relation_counts: BTreeMap<&'static str, usize>,
    pub derived_relation_counts: BTreeMap<&'static str, usize>,
    pub timings_ms: BTreeMap<&'static str, u128>,
}

pub fn compile_selector_problem_with_summary(
    program: &SelectorProgram,
    facts: &SelectorFactStore,
) -> Result<CompiledSelectorProblemWithSummary, CompiledSelectorProblemBuildError> {
    let total_start = Instant::now();

    let validate_start = Instant::now();
    program
        .validate()
        .map_err(CompiledSelectorProblemBuildError::InvalidProgram)?;
    let validate_ms = validate_start.elapsed().as_millis();

    let fact_domains_start = Instant::now();
    let mut domains = FactDomains::from_program_and_facts(program, facts);
    let fact_domains_ms = fact_domains_start.elapsed().as_millis();

    let domain_summary_start = Instant::now();
    let mut summary = domains.summary();
    let domain_summary_ms = domain_summary_start.elapsed().as_millis();

    let setup_start = Instant::now();
    domains.discard_unneeded_raw_relations();
    let target_binding_projections = TargetBindingProjections::from_program(program)?;

    let mut model = CompiledSelectorProblemBuilder::default();
    for domain in [VariableDomain::Owner, VariableDomain::String] {
        model.add_full_domain_values(domain, domains.values_for(domain))?;
    }
    domains.discard_full_domain_source_sets();
    let mut variables = Vec::with_capacity(program.variables.len());
    for variable in &program.variables {
        variables.push(model.add_variable(
            variable.id,
            variable.domain,
            variable.debug_name.clone(),
        )?);
    }

    for target in &program.targets {
        let owner_variable = model_variable(&variables, target.owner)?;
        let binding_projection = match target_binding_projections.binding_projection(target.owner) {
            Some(SourceBindingProjection::Const(binding)) => {
                Some(TargetBindingProjection::Const(binding.clone()))
            }
            Some(SourceBindingProjection::Var(binding)) => Some(TargetBindingProjection::Variable(
                model_variable(&variables, *binding)?,
            )),
            None => None,
        };
        model.add_target_projection(target.id, owner_variable, binding_projection)?;
    }
    let variables_and_targets_ms = setup_start.elapsed().as_millis();

    let atom_lowering_start = Instant::now();
    let mut support_cache = EncodedSupportCache::default();
    for atom in &program.atoms {
        lower_atom_constraint(atom, &domains, &variables, &mut model, &mut support_cache)?;
        if model.known_unsat_reason().is_some() {
            break;
        }
    }
    let atom_lowering_ms = atom_lowering_start.elapsed().as_millis();

    let allowed_tuple_simplification_start = Instant::now();
    if model.known_unsat_reason().is_none() {
        model.simplify_allowed_tuples_against_current_domains()?;
    }
    let allowed_tuple_simplification_ms = allowed_tuple_simplification_start.elapsed().as_millis();

    let all_different_start = Instant::now();
    if model.known_unsat_reason().is_none() {
        for targets in &program.all_different {
            model.require_target_all_different(targets.clone())?;
        }
        for variable_set in &program.all_different_variables {
            model.add_all_different(
                variable_set
                    .variables
                    .iter()
                    .map(|variable| model_variable(&variables, *variable))
                    .collect::<Result<Vec<_>, _>>()?,
                AllDifferentReason::SelectorSemantics {
                    label: variable_set.label.clone(),
                },
            )?;
        }
    }
    let all_different_ms = all_different_start.elapsed().as_millis();

    let finish_start = Instant::now();
    let problem = model
        .finish()
        .map_err(CompiledSelectorProblemBuildError::from)?;
    let finish_ms = finish_start.elapsed().as_millis();
    summary.timings_ms = BTreeMap::from([
        ("validate", validate_ms),
        ("fact_domains", fact_domains_ms),
        ("domain_summary", domain_summary_ms),
        ("variables_and_targets", variables_and_targets_ms),
        ("atom_lowering", atom_lowering_ms),
        (
            "allowed_tuple_simplification",
            allowed_tuple_simplification_ms,
        ),
        ("all_different", all_different_ms),
        ("finish", finish_ms),
        ("total", total_start.elapsed().as_millis()),
    ]);
    Ok(CompiledSelectorProblemWithSummary { problem, summary })
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
        owner: SelectorVariableId,
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
                owner,
                existing,
                actual,
            } => write!(
                f,
                "selector owner variable {owner:?} has conflicting target binding projections: {existing:?} vs {actual:?}"
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
        SelectorAtom::OwnerKind {
            owner,
            statement_kind,
        } => add_cached_owner_string_indexed_allowed_tuples(
            model,
            variables,
            owner,
            statement_kind,
            &domains.owner_kinds_index,
            "owner_kind",
            support_cache,
        ),
        SelectorAtom::OwnerDeclaresBinding { owner, binding } => {
            add_cached_owner_string_indexed_allowed_tuples(
                model,
                variables,
                owner,
                binding,
                &domains.declared_bindings_index,
                "declared_binding",
                support_cache,
            )
        }
        SelectorAtom::ProjectedAllowedTuples {
            variables: projected_variables,
            rows,
            reason: _,
        } => add_projected_allowed_tuples(model, variables, projected_variables, rows),
        SelectorAtom::OwnerExportName { owner, export_name } => {
            add_cached_owner_string_indexed_allowed_tuples(
                model,
                variables,
                owner,
                export_name,
                &domains.export_names_index,
                "export_name",
                support_cache,
            )
        }
        SelectorAtom::OwnerReferencesBinding {
            owner,
            binding,
            edge_kind,
        } => {
            let edge_kind = optional_string_term_const(edge_kind)?;
            let facts = domains
                .relation_supports
                .owner_references_binding(edge_kind.as_deref());
            add_cached_owner_string_allowed_tuples(
                model,
                variables,
                owner,
                binding,
                facts,
                format!("owner_references_binding:{edge_kind:?}"),
                support_cache,
            )
        }
        SelectorAtom::OwnerReferencesOwner { owner, referenced } => {
            add_cached_owner_owner_allowed_tuples(
                model,
                variables,
                owner,
                referenced,
                &domains.references_owner,
                "references_owner".to_string(),
                support_cache,
            )
        }
        SelectorAtom::OwnerAliasesOwner { owner, aliased } => {
            add_cached_owner_owner_allowed_tuples(
                model,
                variables,
                owner,
                aliased,
                &domains.aliases_owner,
                "aliases_owner".to_string(),
                support_cache,
            )
        }
        SelectorAtom::ReadsMember {
            owner,
            object: None,
            member,
        } => add_cached_owner_string_allowed_tuples(
            model,
            variables,
            owner,
            member,
            &domains.member_reads,
            "member_reads".to_string(),
            support_cache,
        ),
        SelectorAtom::ReadsMember {
            owner,
            object: Some(object),
            member,
        } => {
            let object = required_string_term_const(object, "reads_member.object")?;
            let facts = domains.relation_supports.member_reads_from_binding(&object);
            add_cached_owner_string_allowed_tuples(
                model,
                variables,
                owner,
                member,
                facts,
                format!("member_reads_from_binding:{object}"),
                support_cache,
            )
        }
        SelectorAtom::ReadsMemberOfOwner {
            owner,
            object,
            member,
        } => {
            let member = required_string_term_const(member, "reads_member_of_owner.member")?;
            let facts = domains.relation_supports.reads_member_of_owner(&member);
            add_cached_owner_owner_allowed_tuples(
                model,
                variables,
                owner,
                object,
                facts,
                format!("reads_member_of_owner:{member}"),
                support_cache,
            )
        }
        SelectorAtom::ConsumesModuleMember {
            owner,
            module,
            member,
        } => {
            let module = required_string_term_const(module, "consumes_module_member.module")?;
            let member = required_string_term_const(member, "consumes_module_member.member")?;
            let facts = domains
                .relation_supports
                .module_member_uses(&module, &member);
            add_cached_owner_allowed_tuples(
                model,
                variables,
                owner,
                facts,
                format!("module_member_uses:{module}:{member}"),
                support_cache,
            )
        }
        SelectorAtom::PassedToCall {
            owner,
            callee_object: None,
            callee_member,
            arg_index,
        } => {
            let callee_member =
                required_string_term_const(callee_member, "passed_to_call.callee_member")?;
            let facts = domains
                .relation_supports
                .call_arguments(&callee_member, *arg_index);
            add_cached_owner_allowed_tuples(
                model,
                variables,
                owner,
                facts,
                format!("call_arguments:{callee_member}:{arg_index:?}"),
                support_cache,
            )
        }
        SelectorAtom::PassedToCall {
            owner,
            callee_object: Some(callee_object),
            callee_member,
            arg_index,
        } => {
            let callee_object =
                required_string_term_const(callee_object, "passed_to_call.callee_object")?;
            let callee_member =
                required_string_term_const(callee_member, "passed_to_call.callee_member")?;
            let facts = domains.relation_supports.call_arguments_from_binding(
                &callee_object,
                &callee_member,
                *arg_index,
            );
            add_cached_owner_allowed_tuples(
                model,
                variables,
                owner,
                facts,
                format!(
                    "call_arguments_from_binding:{callee_object}:{callee_member}:{arg_index:?}"
                ),
                support_cache,
            )
        }
        SelectorAtom::PassedToCallOfOwner {
            owner,
            callee_object,
            callee_member,
            arg_index,
        } => {
            let callee_member =
                required_string_term_const(callee_member, "passed_to_call_of_owner.callee_member")?;
            let facts = domains
                .relation_supports
                .call_arguments_from_owner(&callee_member, *arg_index);
            add_cached_owner_owner_allowed_tuples(
                model,
                variables,
                owner,
                callee_object,
                facts,
                format!("call_arguments_from_owner:{callee_member}:{arg_index:?}"),
                support_cache,
            )
        }
        SelectorAtom::MakesDecorateCall {
            owner,
            class_anchor,
            member,
        } => {
            let class_anchor =
                required_string_term_const(class_anchor, "makes_decorate_call.class_anchor")?;
            let member = optional_string_term_const(member)?;
            let facts = domains
                .relation_supports
                .makes_decorate_call_for_binding(&class_anchor, member.as_deref());
            add_cached_owner_allowed_tuples(
                model,
                variables,
                owner,
                facts,
                format!("makes_decorate_call_for_binding:{class_anchor}:{member:?}"),
                support_cache,
            )
        }
        SelectorAtom::MakesDecorateCallForOwner {
            owner,
            class_anchor,
            member,
        } => {
            let member = optional_string_term_const(member)?;
            let facts = domains
                .relation_supports
                .makes_decorate_call_for_owner(member.as_deref());
            add_cached_owner_owner_allowed_tuples(
                model,
                variables,
                owner,
                class_anchor,
                facts,
                format!("makes_decorate_call_for_owner:{member:?}"),
                support_cache,
            )
        }
        SelectorAtom::IntrinsicAlias {
            owner,
            property,
            referenced_by,
        } => {
            let property = required_string_term_const(property, "intrinsic_alias.property")?;
            let facts = domains
                .relation_supports
                .intrinsic_alias_referenced_by(&property);
            add_cached_owner_owner_allowed_tuples(
                model,
                variables,
                owner,
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
    owner_unary: BTreeMap<String, SharedVariableDomainId>,
    string_unary: BTreeMap<String, SharedVariableDomainId>,
    owner_string_binary_by_key: BTreeMap<String, AllowedTupleRowsId>,
    owner_owner_binary: BTreeMap<String, AllowedTupleRowsId>,
    owner_string_unary: BTreeMap<(&'static str, String), SharedVariableDomainId>,
    owner_string_binary: BTreeMap<&'static str, AllowedTupleRowsId>,
}

fn restrict_owner_variable_to_candidates(
    model: &mut CompiledSelectorProblemBuilder,
    variable: ConstraintVariableId,
    candidates: impl IntoIterator<Item = OwnerId>,
) -> Result<(), CompiledSelectorProblemBuildError> {
    let values = candidates
        .into_iter()
        .map(|owner| model.intern_owner(owner))
        .collect::<Result<Vec<_>, _>>()?;
    model
        .restrict_variable_to_encoded_values(variable, values)
        .map_err(Into::into)
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

fn cached_owner_domain(
    model: &mut CompiledSelectorProblemBuilder,
    support_cache: &mut EncodedSupportCache,
    key: String,
    owners: impl IntoIterator<Item = OwnerId>,
) -> Result<SharedVariableDomainId, CompiledSelectorProblemBuildError> {
    if let Some(domain_id) = support_cache.owner_unary.get(&key) {
        return Ok(*domain_id);
    }
    let values = owners
        .into_iter()
        .map(|owner| model.intern_owner(owner))
        .collect::<Result<Vec<_>, _>>()?;
    let domain_id = model.intern_shared_sparse_variable_domain(VariableDomain::Owner, values)?;
    support_cache.owner_unary.insert(key, domain_id);
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

fn add_owner_string_indexed_allowed_tuples(
    model: &mut CompiledSelectorProblemBuilder,
    variables: &[ConstraintVariableId],
    owner: &OwnerTerm,
    string: &StringTerm,
    index: &OwnerStringIndex,
) -> Result<(), CompiledSelectorProblemBuildError> {
    match (owner, string) {
        (OwnerTerm::Const { owner }, StringTerm::Const { value }) => {
            index.contains(*owner, value).then_some(()).ok_or_else(|| {
                CompiledSelectorProblemBuildError::ConstantOnlyAtomUnsatisfied {
                    atom: format!("owner/string fact {owner:?} {value:?}"),
                }
            })
        }
        (OwnerTerm::Var { id }, StringTerm::Const { value }) => {
            let variable = model_variable(variables, *id)?;
            restrict_owner_variable_to_candidates(
                model,
                variable,
                index
                    .owners_by_value
                    .get(value)
                    .into_iter()
                    .flatten()
                    .copied(),
            )
        }
        (OwnerTerm::Const { owner }, StringTerm::Var { id }) => {
            let variable = model_variable(variables, *id)?;
            restrict_string_variable_to_candidates(
                model,
                variable,
                index
                    .values_by_owner
                    .get(owner)
                    .into_iter()
                    .flatten()
                    .map(String::as_str),
            )
        }
        (OwnerTerm::Var { id: owner_id }, StringTerm::Var { id: string_id }) => {
            let constraint_variables = [
                model_variable(variables, *owner_id)?,
                model_variable(variables, *string_id)?,
            ];
            let tuples = index
                .rows
                .iter()
                .map(|(fact_owner, fact_string)| {
                    Ok((
                        model.intern_owner(*fact_owner)?,
                        model.intern_string(fact_string)?,
                    ))
                })
                .collect::<Result<Vec<_>, CompiledSelectorProblemError>>()?;
            add_encoded_allowed_binary_tuple_set(model, constraint_variables, tuples)
        }
    }
}

fn add_cached_owner_string_indexed_allowed_tuples(
    model: &mut CompiledSelectorProblemBuilder,
    variables: &[ConstraintVariableId],
    owner: &OwnerTerm,
    string: &StringTerm,
    index: &OwnerStringIndex,
    relation: &'static str,
    support_cache: &mut EncodedSupportCache,
) -> Result<(), CompiledSelectorProblemBuildError> {
    match (owner, string) {
        (OwnerTerm::Var { id }, StringTerm::Const { value }) => {
            let variable = model_variable(variables, *id)?;
            let key = (relation, value.clone());
            let domain_id = if let Some(domain_id) = support_cache.owner_string_unary.get(&key) {
                *domain_id
            } else {
                let values = index
                    .owners_by_value
                    .get(value)
                    .into_iter()
                    .flatten()
                    .map(|owner| model.intern_owner(*owner))
                    .collect::<Result<Vec<_>, _>>()?;
                let domain_id =
                    model.intern_shared_sparse_variable_domain(VariableDomain::Owner, values)?;
                support_cache.owner_string_unary.insert(key, domain_id);
                domain_id
            };
            model
                .restrict_variable_to_shared_sparse_domain(variable, domain_id)
                .map_err(Into::into)
        }
        (OwnerTerm::Var { id: owner_id }, StringTerm::Var { id: string_id }) => {
            let constraint_variables = [
                model_variable(variables, *owner_id)?,
                model_variable(variables, *string_id)?,
            ];
            if constraint_variables[0] == constraint_variables[1] {
                return add_owner_string_indexed_allowed_tuples(
                    model, variables, owner, string, index,
                );
            }
            let row_set = if let Some(row_set) = support_cache.owner_string_binary.get(relation) {
                *row_set
            } else {
                let tuples = index
                    .rows
                    .iter()
                    .map(|(fact_owner, fact_string)| {
                        Ok((
                            model.intern_owner(*fact_owner)?,
                            model.intern_string(fact_string)?,
                        ))
                    })
                    .collect::<Result<Vec<_>, CompiledSelectorProblemError>>()?;
                let row_set = model.intern_encoded_allowed_binary_row_set(
                    constraint_variables,
                    [VariableDomain::Owner, VariableDomain::String],
                    tuples,
                )?;
                support_cache.owner_string_binary.insert(relation, row_set);
                row_set
            };
            model
                .add_encoded_allowed_row_set(constraint_variables.to_vec(), row_set)
                .map(|_| ())
                .map_err(Into::into)
        }
        _ => add_owner_string_indexed_allowed_tuples(model, variables, owner, string, index),
    }
}

fn add_cached_owner_string_allowed_tuples(
    model: &mut CompiledSelectorProblemBuilder,
    variables: &[ConstraintVariableId],
    owner: &OwnerTerm,
    string: &StringTerm,
    facts: &BTreeSet<(OwnerId, String)>,
    relation_key: String,
    support_cache: &mut EncodedSupportCache,
) -> Result<(), CompiledSelectorProblemBuildError> {
    match (owner, string) {
        (OwnerTerm::Const { owner }, StringTerm::Const { value }) => facts
            .iter()
            .any(|(fact_owner, fact_value)| fact_owner == owner && fact_value == value)
            .then_some(())
            .ok_or_else(
                || CompiledSelectorProblemBuildError::ConstantOnlyAtomUnsatisfied {
                    atom: format!("owner/string fact {owner:?} {value:?}"),
                },
            ),
        (OwnerTerm::Var { id }, StringTerm::Const { value }) => {
            let variable = model_variable(variables, *id)?;
            let domain_id = cached_owner_domain(
                model,
                support_cache,
                format!("{relation_key}:owner-by-string:{value}"),
                facts.iter().filter_map(|(fact_owner, fact_value)| {
                    (fact_value == value).then_some(*fact_owner)
                }),
            )?;
            model
                .restrict_variable_to_shared_sparse_domain(variable, domain_id)
                .map_err(Into::into)
        }
        (OwnerTerm::Const { owner }, StringTerm::Var { id }) => {
            let variable = model_variable(variables, *id)?;
            let domain_id = cached_string_domain(
                model,
                support_cache,
                format!("{relation_key}:string-by-owner:{}", owner.0),
                facts.iter().filter_map(|(fact_owner, fact_value)| {
                    (fact_owner == owner).then_some(fact_value.as_str())
                }),
            )?;
            model
                .restrict_variable_to_shared_sparse_domain(variable, domain_id)
                .map_err(Into::into)
        }
        (OwnerTerm::Var { id: owner_id }, StringTerm::Var { id: string_id }) => {
            let constraint_variables = [
                model_variable(variables, *owner_id)?,
                model_variable(variables, *string_id)?,
            ];
            let row_set = if let Some(row_set) =
                support_cache.owner_string_binary_by_key.get(&relation_key)
            {
                *row_set
            } else {
                let tuples = facts
                    .iter()
                    .map(|(fact_owner, fact_string)| {
                        Ok((
                            model.intern_owner(*fact_owner)?,
                            model.intern_string(fact_string)?,
                        ))
                    })
                    .collect::<Result<Vec<_>, CompiledSelectorProblemError>>()?;
                let row_set = model.intern_encoded_allowed_binary_row_set(
                    constraint_variables,
                    [VariableDomain::Owner, VariableDomain::String],
                    tuples,
                )?;
                support_cache
                    .owner_string_binary_by_key
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

fn add_cached_owner_allowed_tuples(
    model: &mut CompiledSelectorProblemBuilder,
    variables: &[ConstraintVariableId],
    owner: &OwnerTerm,
    facts: &BTreeSet<OwnerId>,
    relation_key: String,
    support_cache: &mut EncodedSupportCache,
) -> Result<(), CompiledSelectorProblemBuildError> {
    match owner {
        OwnerTerm::Const { owner } => facts.contains(owner).then_some(()).ok_or_else(|| {
            CompiledSelectorProblemBuildError::ConstantOnlyAtomUnsatisfied {
                atom: format!("owner fact {owner:?}"),
            }
        }),
        OwnerTerm::Var { id } => {
            let variable = model_variable(variables, *id)?;
            let domain_id =
                cached_owner_domain(model, support_cache, relation_key, facts.iter().copied())?;
            model
                .restrict_variable_to_shared_sparse_domain(variable, domain_id)
                .map_err(Into::into)
        }
    }
}

fn add_cached_owner_owner_allowed_tuples(
    model: &mut CompiledSelectorProblemBuilder,
    variables: &[ConstraintVariableId],
    left: &OwnerTerm,
    right: &OwnerTerm,
    facts: &BTreeSet<(OwnerId, OwnerId)>,
    relation_key: String,
    support_cache: &mut EncodedSupportCache,
) -> Result<(), CompiledSelectorProblemBuildError> {
    match (left, right) {
        (OwnerTerm::Const { owner: left }, OwnerTerm::Const { owner: right }) => facts
            .contains(&(*left, *right))
            .then_some(())
            .ok_or_else(
                || CompiledSelectorProblemBuildError::ConstantOnlyAtomUnsatisfied {
                    atom: format!("owner/owner fact {left:?} {right:?}"),
                },
            ),
        (OwnerTerm::Var { id }, OwnerTerm::Const { owner: right }) => {
            let variable = model_variable(variables, *id)?;
            let domain_id = cached_owner_domain(
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
        (OwnerTerm::Const { owner: left }, OwnerTerm::Var { id }) => {
            let variable = model_variable(variables, *id)?;
            let domain_id = cached_owner_domain(
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
        (OwnerTerm::Var { id: left_id }, OwnerTerm::Var { id: right_id }) => {
            let constraint_variables = [
                model_variable(variables, *left_id)?,
                model_variable(variables, *right_id)?,
            ];
            if constraint_variables[0] == constraint_variables[1] {
                let domain_id = cached_owner_domain(
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
            let row_set = if let Some(row_set) = support_cache.owner_owner_binary.get(&relation_key)
            {
                *row_set
            } else {
                let tuples = facts
                    .iter()
                    .map(|(fact_left, fact_right)| {
                        Ok((
                            model.intern_owner(*fact_left)?,
                            model.intern_owner(*fact_right)?,
                        ))
                    })
                    .collect::<Result<Vec<_>, CompiledSelectorProblemError>>()?;
                let row_set = model.intern_encoded_allowed_binary_row_set(
                    constraint_variables,
                    [VariableDomain::Owner, VariableDomain::Owner],
                    tuples,
                )?;
                support_cache
                    .owner_owner_binary
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
        ConstraintValue::Owner(value) => model.intern_owner(value),
        ConstraintValue::String(value) => model.intern_string(&value),
    }
}

fn projected_value(value: SelectorProjectedValue) -> ConstraintValue {
    match value {
        SelectorProjectedValue::Owner(value) => ConstraintValue::Owner(value),
        SelectorProjectedValue::String(value) => ConstraintValue::String(value),
    }
}

fn add_encoded_allowed_binary_tuple_set(
    model: &mut CompiledSelectorProblemBuilder,
    variables: [ConstraintVariableId; 2],
    tuples: Vec<(BackendValueId, BackendValueId)>,
) -> Result<(), CompiledSelectorProblemBuildError> {
    if variables[0] == variables[1] {
        return model
            .restrict_variable_to_encoded_values(
                variables[0],
                tuples
                    .into_iter()
                    .filter_map(|(left, right)| (left == right).then_some(left)),
            )
            .map_err(Into::into);
    }
    model
        .add_encoded_allowed_binary_tuples(variables, tuples)
        .map(|_| ())
        .map_err(Into::into)
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

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SourceBindingProjection {
    Const(String),
    Var(SelectorVariableId),
}

#[derive(Debug, Default)]
struct TargetBindingProjections {
    by_owner: BTreeMap<SelectorVariableId, SourceBindingProjection>,
}

impl TargetBindingProjections {
    fn from_program(program: &SelectorProgram) -> Result<Self, CompiledSelectorProblemBuildError> {
        let mut projections = Self::default();
        for atom in &program.atoms {
            match atom {
                SelectorAtom::OwnerDeclaresBinding {
                    owner: OwnerTerm::Var { id: owner },
                    binding: StringTerm::Const { value },
                } => projections.insert(*owner, SourceBindingProjection::Const(value.clone()))?,
                SelectorAtom::OwnerDeclaresBinding {
                    owner: OwnerTerm::Var { id: owner },
                    binding: StringTerm::Var { id: binding },
                } => projections.insert(*owner, SourceBindingProjection::Var(*binding))?,
                _ => {}
            }
        }
        Ok(projections)
    }

    fn insert(
        &mut self,
        owner: SelectorVariableId,
        binding: SourceBindingProjection,
    ) -> Result<(), CompiledSelectorProblemBuildError> {
        match self.by_owner.get(&owner) {
            Some(existing) if existing != &binding => Err(
                CompiledSelectorProblemBuildError::ConflictingTargetBindingProjection {
                    owner,
                    existing: existing.clone(),
                    actual: binding,
                },
            ),
            Some(_) => Ok(()),
            None => {
                self.by_owner.insert(owner, binding);
                Ok(())
            }
        }
    }

    fn binding_projection(&self, owner: SelectorVariableId) -> Option<&SourceBindingProjection> {
        self.by_owner.get(&owner)
    }
}

#[derive(Debug, Default)]
struct OwnerStringIndex {
    owners_by_value: BTreeMap<String, Vec<OwnerId>>,
    values_by_owner: BTreeMap<OwnerId, Vec<String>>,
    rows: Vec<(OwnerId, String)>,
}

impl OwnerStringIndex {
    fn from_facts(facts: &BTreeSet<(OwnerId, String)>) -> Self {
        let mut index = Self::default();
        for (owner, value) in facts {
            index
                .owners_by_value
                .entry(value.clone())
                .or_default()
                .push(*owner);
            index
                .values_by_owner
                .entry(*owner)
                .or_default()
                .push(value.clone());
            index.rows.push((*owner, value.clone()));
        }
        for owners in index.owners_by_value.values_mut() {
            owners.sort_unstable();
            owners.dedup();
        }
        for values in index.values_by_owner.values_mut() {
            values.sort();
            values.dedup();
        }
        index.rows.sort();
        index.rows.dedup();
        index
    }

    fn contains(&self, owner: OwnerId, value: &str) -> bool {
        self.values_by_owner.get(&owner).is_some_and(|values| {
            values
                .binary_search_by(|candidate| candidate.as_str().cmp(value))
                .is_ok()
        })
    }
}

#[derive(Debug, Default)]
struct RelationSupportCache {
    empty_owner_support: BTreeSet<OwnerId>,
    empty_owner_string_support: BTreeSet<(OwnerId, String)>,
    empty_owner_owner_support: BTreeSet<(OwnerId, OwnerId)>,
    owner_references_binding_all: BTreeSet<(OwnerId, String)>,
    owner_references_binding_by_edge_kind: BTreeMap<String, BTreeSet<(OwnerId, String)>>,
    module_member_uses_by_module_member: BTreeMap<(String, String), BTreeSet<OwnerId>>,
    member_reads_from_binding_by_object: BTreeMap<String, BTreeSet<(OwnerId, String)>>,
    reads_member_of_owner_by_member: BTreeMap<String, BTreeSet<(OwnerId, OwnerId)>>,
    call_arguments_by_member: BTreeMap<String, BTreeSet<OwnerId>>,
    call_arguments_by_member_arg_index: BTreeMap<String, BTreeMap<usize, BTreeSet<OwnerId>>>,
    call_arguments_from_binding_by_object_member:
        BTreeMap<String, BTreeMap<String, BTreeSet<OwnerId>>>,
    call_arguments_from_binding_by_object_member_arg_index:
        BTreeMap<String, BTreeMap<String, BTreeMap<usize, BTreeSet<OwnerId>>>>,
    call_arguments_from_owner_by_member: BTreeMap<String, BTreeSet<(OwnerId, OwnerId)>>,
    call_arguments_from_owner_by_member_arg_index:
        BTreeMap<String, BTreeMap<usize, BTreeSet<(OwnerId, OwnerId)>>>,
    makes_decorate_call_for_binding_by_class_anchor: BTreeMap<String, BTreeSet<OwnerId>>,
    makes_decorate_call_for_binding_by_class_anchor_member:
        BTreeMap<String, BTreeMap<String, BTreeSet<OwnerId>>>,
    makes_decorate_call_for_owner_all: BTreeSet<(OwnerId, OwnerId)>,
    makes_decorate_call_for_owner_by_member: BTreeMap<String, BTreeSet<(OwnerId, OwnerId)>>,
    intrinsic_alias_referenced_by_property: BTreeMap<String, BTreeSet<(OwnerId, OwnerId)>>,
}

impl RelationSupportCache {
    fn from_domains(domains: &FactDomains) -> Self {
        let mut cache = Self::default();

        for (owner, binding, edge_kind) in &domains.owner_references_binding {
            let support = (*owner, binding.clone());
            cache.owner_references_binding_all.insert(support.clone());
            cache
                .owner_references_binding_by_edge_kind
                .entry(edge_kind.clone())
                .or_default()
                .insert(support);
        }

        for (owner, object, member) in &domains.member_reads_from_binding {
            cache
                .member_reads_from_binding_by_object
                .entry(object.clone())
                .or_default()
                .insert((*owner, member.clone()));
        }

        for (owner, module, member) in &domains.module_member_uses {
            cache
                .module_member_uses_by_module_member
                .entry((module.clone(), member.clone()))
                .or_default()
                .insert(*owner);
        }

        for (owner, object, member) in &domains.reads_member_of_owner {
            cache
                .reads_member_of_owner_by_member
                .entry(member.clone())
                .or_default()
                .insert((*owner, *object));
        }

        for (owner, callee_member, arg_index) in &domains.call_arguments {
            cache
                .call_arguments_by_member
                .entry(callee_member.clone())
                .or_default()
                .insert(*owner);
            cache
                .call_arguments_by_member_arg_index
                .entry(callee_member.clone())
                .or_default()
                .entry(*arg_index)
                .or_default()
                .insert(*owner);
        }

        for (owner, callee_object, callee_member, arg_index) in &domains.call_arguments_from_binding
        {
            cache
                .call_arguments_from_binding_by_object_member
                .entry(callee_object.clone())
                .or_default()
                .entry(callee_member.clone())
                .or_default()
                .insert(*owner);
            cache
                .call_arguments_from_binding_by_object_member_arg_index
                .entry(callee_object.clone())
                .or_default()
                .entry(callee_member.clone())
                .or_default()
                .entry(*arg_index)
                .or_default()
                .insert(*owner);
        }

        for (owner, callee_object, callee_member, arg_index) in &domains.call_arguments_from_owner {
            let support = (*owner, *callee_object);
            cache
                .call_arguments_from_owner_by_member
                .entry(callee_member.clone())
                .or_default()
                .insert(support);
            cache
                .call_arguments_from_owner_by_member_arg_index
                .entry(callee_member.clone())
                .or_default()
                .entry(*arg_index)
                .or_default()
                .insert(support);
        }

        for (owner, class_anchor, member) in &domains.makes_decorate_call_for_binding {
            cache
                .makes_decorate_call_for_binding_by_class_anchor
                .entry(class_anchor.clone())
                .or_default()
                .insert(*owner);
            if let Some(member) = member {
                cache
                    .makes_decorate_call_for_binding_by_class_anchor_member
                    .entry(class_anchor.clone())
                    .or_default()
                    .entry(member.clone())
                    .or_default()
                    .insert(*owner);
            }
        }

        for (owner, class_anchor, member) in &domains.makes_decorate_call_for_owner {
            let support = (*owner, *class_anchor);
            cache.makes_decorate_call_for_owner_all.insert(support);
            if let Some(member) = member {
                cache
                    .makes_decorate_call_for_owner_by_member
                    .entry(member.clone())
                    .or_default()
                    .insert(support);
            }
        }

        for (owner, property, referenced_by) in &domains.intrinsic_alias_referenced_by {
            cache
                .intrinsic_alias_referenced_by_property
                .entry(property.clone())
                .or_default()
                .insert((*owner, *referenced_by));
        }

        cache
    }

    fn owner_references_binding(&self, edge_kind: Option<&str>) -> &BTreeSet<(OwnerId, String)> {
        match edge_kind {
            Some(edge_kind) => self
                .owner_references_binding_by_edge_kind
                .get(edge_kind)
                .unwrap_or(&self.empty_owner_string_support),
            None => &self.owner_references_binding_all,
        }
    }

    fn member_reads_from_binding(&self, object: &str) -> &BTreeSet<(OwnerId, String)> {
        self.member_reads_from_binding_by_object
            .get(object)
            .unwrap_or(&self.empty_owner_string_support)
    }

    fn module_member_uses(&self, module: &str, member: &str) -> &BTreeSet<OwnerId> {
        self.module_member_uses_by_module_member
            .get(&(module.to_string(), member.to_string()))
            .unwrap_or(&self.empty_owner_support)
    }

    fn reads_member_of_owner(&self, member: &str) -> &BTreeSet<(OwnerId, OwnerId)> {
        self.reads_member_of_owner_by_member
            .get(member)
            .unwrap_or(&self.empty_owner_owner_support)
    }

    fn call_arguments(&self, callee_member: &str, arg_index: Option<u32>) -> &BTreeSet<OwnerId> {
        match arg_index {
            Some(arg_index) => self
                .call_arguments_by_member_arg_index
                .get(callee_member)
                .and_then(|by_arg_index| {
                    usize::try_from(arg_index)
                        .ok()
                        .and_then(|arg_index| by_arg_index.get(&arg_index))
                })
                .unwrap_or(&self.empty_owner_support),
            None => self
                .call_arguments_by_member
                .get(callee_member)
                .unwrap_or(&self.empty_owner_support),
        }
    }

    fn call_arguments_from_binding(
        &self,
        callee_object: &str,
        callee_member: &str,
        arg_index: Option<u32>,
    ) -> &BTreeSet<OwnerId> {
        match arg_index {
            Some(arg_index) => self
                .call_arguments_from_binding_by_object_member_arg_index
                .get(callee_object)
                .and_then(|by_member| by_member.get(callee_member))
                .and_then(|by_arg_index| {
                    usize::try_from(arg_index)
                        .ok()
                        .and_then(|arg_index| by_arg_index.get(&arg_index))
                })
                .unwrap_or(&self.empty_owner_support),
            None => self
                .call_arguments_from_binding_by_object_member
                .get(callee_object)
                .and_then(|by_member| by_member.get(callee_member))
                .unwrap_or(&self.empty_owner_support),
        }
    }

    fn call_arguments_from_owner(
        &self,
        callee_member: &str,
        arg_index: Option<u32>,
    ) -> &BTreeSet<(OwnerId, OwnerId)> {
        match arg_index {
            Some(arg_index) => self
                .call_arguments_from_owner_by_member_arg_index
                .get(callee_member)
                .and_then(|by_arg_index| {
                    usize::try_from(arg_index)
                        .ok()
                        .and_then(|arg_index| by_arg_index.get(&arg_index))
                })
                .unwrap_or(&self.empty_owner_owner_support),
            None => self
                .call_arguments_from_owner_by_member
                .get(callee_member)
                .unwrap_or(&self.empty_owner_owner_support),
        }
    }

    fn makes_decorate_call_for_binding(
        &self,
        class_anchor: &str,
        member: Option<&str>,
    ) -> &BTreeSet<OwnerId> {
        match member {
            Some(member) => self
                .makes_decorate_call_for_binding_by_class_anchor_member
                .get(class_anchor)
                .and_then(|by_member| by_member.get(member))
                .unwrap_or(&self.empty_owner_support),
            None => self
                .makes_decorate_call_for_binding_by_class_anchor
                .get(class_anchor)
                .unwrap_or(&self.empty_owner_support),
        }
    }

    fn makes_decorate_call_for_owner(&self, member: Option<&str>) -> &BTreeSet<(OwnerId, OwnerId)> {
        match member {
            Some(member) => self
                .makes_decorate_call_for_owner_by_member
                .get(member)
                .unwrap_or(&self.empty_owner_owner_support),
            None => &self.makes_decorate_call_for_owner_all,
        }
    }

    fn intrinsic_alias_referenced_by(&self, property: &str) -> &BTreeSet<(OwnerId, OwnerId)> {
        self.intrinsic_alias_referenced_by_property
            .get(property)
            .unwrap_or(&self.empty_owner_owner_support)
    }
}

#[derive(Debug, Default)]
struct DerivedFactRequirements {
    owner_references_binding: bool,
    references_owner: bool,
    aliases_owner: bool,
    member_reads: bool,
    member_reads_from_binding: bool,
    reads_member_of_owner: bool,
    module_member_uses: bool,
    call_arguments: bool,
    call_arguments_from_binding: bool,
    call_arguments_from_owner: bool,
    makes_decorate_call_for_binding: bool,
    makes_decorate_call_for_owner: bool,
    intrinsic_alias_referenced_by: bool,
}

impl DerivedFactRequirements {
    fn from_program(program: &SelectorProgram) -> Self {
        let mut requirements = Self::default();
        for atom in &program.atoms {
            match atom {
                SelectorAtom::OwnerReferencesBinding { .. } => {
                    requirements.owner_references_binding = true;
                }
                SelectorAtom::OwnerReferencesOwner { .. } => {
                    requirements.references_owner = true;
                }
                SelectorAtom::OwnerAliasesOwner { .. } => {
                    requirements.aliases_owner = true;
                }
                SelectorAtom::ReadsMember { object: None, .. } => {
                    requirements.member_reads = true;
                }
                SelectorAtom::ReadsMember {
                    object: Some(_), ..
                } => {
                    requirements.member_reads_from_binding = true;
                }
                SelectorAtom::ReadsMemberOfOwner { .. } => {
                    requirements.member_reads_from_binding = true;
                    requirements.reads_member_of_owner = true;
                }
                SelectorAtom::ConsumesModuleMember { .. } => {
                    requirements.module_member_uses = true;
                }
                SelectorAtom::PassedToCall {
                    callee_object: None,
                    ..
                } => {
                    requirements.call_arguments = true;
                }
                SelectorAtom::PassedToCall {
                    callee_object: Some(_),
                    ..
                } => {
                    requirements.call_arguments_from_binding = true;
                }
                SelectorAtom::PassedToCallOfOwner { .. } => {
                    requirements.call_arguments_from_owner = true;
                }
                SelectorAtom::MakesDecorateCall { .. } => {
                    requirements.makes_decorate_call_for_binding = true;
                }
                SelectorAtom::MakesDecorateCallForOwner { .. } => {
                    requirements.makes_decorate_call_for_owner = true;
                }
                SelectorAtom::IntrinsicAlias { .. } => {
                    requirements.intrinsic_alias_referenced_by = true;
                }
                SelectorAtom::OwnerKind { .. }
                | SelectorAtom::OwnerDeclaresBinding { .. }
                | SelectorAtom::ProjectedAllowedTuples { .. }
                | SelectorAtom::OwnerExportName { .. } => {}
            }
        }
        requirements
    }
}

#[derive(Debug, Default)]
struct FactDomains {
    owners: BTreeSet<OwnerId>,
    strings: BTreeSet<String>,
    owner_kinds: BTreeSet<(OwnerId, String)>,
    owner_statement_ordinals: BTreeSet<(OwnerId, StatementOrdinal)>,
    declared_bindings: BTreeSet<(OwnerId, String)>,
    export_names: BTreeSet<(OwnerId, String)>,
    raw_owner_references_binding: BTreeSet<(OwnerId, String, String)>,
    owner_references_binding: BTreeSet<(OwnerId, String, String)>,
    references_owner: BTreeSet<(OwnerId, OwnerId)>,
    aliases_owner: BTreeSet<(OwnerId, OwnerId)>,
    raw_member_reads: BTreeSet<(StatementOrdinal, Option<String>, String)>,
    member_reads: BTreeSet<(OwnerId, String)>,
    member_reads_from_binding: BTreeSet<(OwnerId, String, String)>,
    reads_member_of_owner: BTreeSet<(OwnerId, OwnerId, String)>,
    raw_module_member_uses: BTreeSet<(StatementOrdinal, String, String)>,
    module_member_uses: BTreeSet<(OwnerId, String, String)>,
    raw_call_arguments: BTreeSet<(String, Option<String>, String, usize)>,
    call_arguments: BTreeSet<(OwnerId, String, usize)>,
    call_arguments_from_binding: BTreeSet<(OwnerId, String, String, usize)>,
    call_arguments_from_owner: BTreeSet<(OwnerId, OwnerId, String, usize)>,
    decorate_calls: BTreeSet<(String, String, Option<String>)>,
    makes_decorate_call_for_binding: BTreeSet<(OwnerId, String, Option<String>)>,
    makes_decorate_call_for_owner: BTreeSet<(OwnerId, OwnerId, Option<String>)>,
    intrinsic_aliases: BTreeSet<(String, String)>,
    intrinsic_alias_referenced_by: BTreeSet<(OwnerId, String, OwnerId)>,
    owner_kinds_index: OwnerStringIndex,
    declared_bindings_index: OwnerStringIndex,
    export_names_index: OwnerStringIndex,
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

    fn summary(&self) -> SelectorModelBuildSummary {
        SelectorModelBuildSummary {
            domain_value_counts: BTreeMap::from([
                ("owner", self.owners.len()),
                ("string", self.strings.len()),
            ]),
            stored_relation_counts: BTreeMap::from([
                ("owner_kind", self.owner_kinds.len()),
                (
                    "owner_statement_ordinal",
                    self.owner_statement_ordinals.len(),
                ),
                ("declared_binding", self.declared_bindings.len()),
                ("export_name", self.export_names.len()),
                (
                    "raw_owner_references_binding",
                    self.raw_owner_references_binding.len(),
                ),
                (
                    "owner_references_binding",
                    self.owner_references_binding.len(),
                ),
                ("references_owner", self.references_owner.len()),
                ("aliases_owner", self.aliases_owner.len()),
                ("raw_member_read", self.raw_member_reads.len()),
                ("member_read", self.member_reads.len()),
                (
                    "member_read_from_binding",
                    self.member_reads_from_binding.len(),
                ),
                ("reads_member_of_owner", self.reads_member_of_owner.len()),
                ("raw_module_member_use", self.raw_module_member_uses.len()),
                ("module_member_use", self.module_member_uses.len()),
                ("raw_call_argument", self.raw_call_arguments.len()),
                ("call_argument", self.call_arguments.len()),
                (
                    "call_argument_from_binding",
                    self.call_arguments_from_binding.len(),
                ),
                (
                    "call_argument_from_owner",
                    self.call_arguments_from_owner.len(),
                ),
                ("decorate_call", self.decorate_calls.len()),
                (
                    "makes_decorate_call_for_binding",
                    self.makes_decorate_call_for_binding.len(),
                ),
                (
                    "makes_decorate_call_for_owner",
                    self.makes_decorate_call_for_owner.len(),
                ),
                ("intrinsic_alias", self.intrinsic_aliases.len()),
                (
                    "intrinsic_alias_referenced_by",
                    self.intrinsic_alias_referenced_by.len(),
                ),
            ]),
            derived_relation_counts: BTreeMap::from([
                (
                    "owner_references_binding",
                    self.owner_references_binding.len(),
                ),
                ("references_owner", self.references_owner.len()),
                ("aliases_owner", self.aliases_owner.len()),
                ("member_read", self.member_reads.len()),
                (
                    "member_read_from_binding",
                    self.member_reads_from_binding.len(),
                ),
                ("reads_member_of_owner", self.reads_member_of_owner.len()),
                ("module_member_use", self.module_member_uses.len()),
                ("call_argument", self.call_arguments.len()),
                (
                    "call_argument_from_binding",
                    self.call_arguments_from_binding.len(),
                ),
                (
                    "call_argument_from_owner",
                    self.call_arguments_from_owner.len(),
                ),
                (
                    "makes_decorate_call_for_binding",
                    self.makes_decorate_call_for_binding.len(),
                ),
                (
                    "makes_decorate_call_for_owner",
                    self.makes_decorate_call_for_owner.len(),
                ),
                (
                    "intrinsic_alias_referenced_by",
                    self.intrinsic_alias_referenced_by.len(),
                ),
            ]),
            timings_ms: BTreeMap::new(),
        }
    }

    fn values_for(&self, domain: VariableDomain) -> Vec<ConstraintValue> {
        match domain {
            VariableDomain::Owner => self
                .owners
                .iter()
                .copied()
                .map(ConstraintValue::Owner)
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
                SelectorFact::Owner {
                    owner,
                    statement_ordinal,
                    statement_kind,
                    ..
                } => {
                    self.add_owner(*owner);
                    self.add_string(statement_kind);
                    self.owner_kinds.insert((*owner, statement_kind.clone()));
                    self.owner_statement_ordinals
                        .insert((*owner, *statement_ordinal));
                }
                SelectorFact::DeclaredBinding {
                    owner,
                    binding,
                    export_name,
                    ..
                } => {
                    self.add_owner(*owner);
                    self.add_string(binding);
                    self.declared_bindings.insert((*owner, binding.clone()));
                    if let Some(export_name) = export_name {
                        self.add_string(export_name);
                        self.export_names.insert((*owner, export_name.clone()));
                    }
                }
                SelectorFact::OwnerReferencesBinding {
                    owner,
                    binding,
                    edge_kind,
                    ..
                } => {
                    self.add_owner(*owner);
                    self.add_string(binding);
                    self.add_string(edge_kind);
                    self.raw_owner_references_binding.insert((
                        *owner,
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
        self.owner_kinds_index = OwnerStringIndex::from_facts(&self.owner_kinds);
        self.declared_bindings_index = OwnerStringIndex::from_facts(&self.declared_bindings);
        self.export_names_index = OwnerStringIndex::from_facts(&self.export_names);
        self.relation_supports = RelationSupportCache::from_domains(self);
    }

    fn discard_unneeded_raw_relations(&mut self) {
        self.raw_owner_references_binding.clear();
        self.raw_member_reads.clear();
        self.raw_module_member_uses.clear();
        self.raw_call_arguments.clear();
        self.decorate_calls.clear();
        self.intrinsic_aliases.clear();
    }

    fn discard_full_domain_source_sets(&mut self) {
        self.owners.clear();
        self.strings.clear();
    }

    fn add_derived_facts(&mut self, requirements: &DerivedFactRequirements) {
        let mut owners_with_declarations = BTreeSet::new();
        let mut owners_by_binding: BTreeMap<String, BTreeSet<OwnerId>> = BTreeMap::new();
        for (owner, binding) in &self.declared_bindings {
            owners_with_declarations.insert(*owner);
            owners_by_binding
                .entry(binding.clone())
                .or_default()
                .insert(*owner);
        }

        if requirements.owner_references_binding {
            for (owner, binding, edge_kind) in &self.raw_owner_references_binding {
                if owners_with_declarations.contains(owner) {
                    self.owner_references_binding.insert((
                        *owner,
                        binding.clone(),
                        edge_kind.clone(),
                    ));
                }
            }
        }

        if requirements.references_owner {
            for (owner, binding, _edge_kind) in &self.raw_owner_references_binding {
                if !owners_with_declarations.contains(owner) {
                    continue;
                }
                if let Some(referenced_owners) = owners_by_binding.get(binding) {
                    self.references_owner.extend(
                        referenced_owners
                            .iter()
                            .map(|referenced| (*owner, *referenced)),
                    );
                }
            }
        }

        if requirements.aliases_owner {
            let var_decl_owners = self
                .owner_kinds
                .iter()
                .filter_map(|(owner, kind)| (kind == "var_decl").then_some(*owner))
                .collect::<BTreeSet<_>>();
            for (owner, binding, edge_kind) in &self.raw_owner_references_binding {
                if edge_kind != "eager_use"
                    || !var_decl_owners.contains(owner)
                    || !owners_with_declarations.contains(owner)
                {
                    continue;
                }
                if let Some(aliased_owners) = owners_by_binding.get(binding) {
                    self.aliases_owner
                        .extend(aliased_owners.iter().map(|aliased| (*owner, *aliased)));
                }
            }
        }

        let needs_owner_by_ordinal = requirements.member_reads
            || requirements.member_reads_from_binding
            || requirements.reads_member_of_owner
            || requirements.module_member_uses;
        let owner_by_ordinal = needs_owner_by_ordinal.then(|| {
            self.owner_statement_ordinals
                .iter()
                .map(|(owner, ordinal)| (*ordinal, *owner))
                .collect::<BTreeMap<_, _>>()
        });

        if requirements.member_reads
            || requirements.member_reads_from_binding
            || requirements.reads_member_of_owner
        {
            let owner_by_ordinal = owner_by_ordinal
                .as_ref()
                .expect("owner ordinal index should be built for member reads");
            for (statement_ordinal, object, member) in &self.raw_member_reads {
                let Some(owner) = owner_by_ordinal.get(statement_ordinal) else {
                    continue;
                };
                if !owners_with_declarations.contains(owner) {
                    continue;
                }
                if requirements.member_reads {
                    self.member_reads.insert((*owner, member.clone()));
                }
                if (requirements.member_reads_from_binding || requirements.reads_member_of_owner)
                    && let Some(object) = object
                {
                    self.member_reads_from_binding
                        .insert((*owner, object.clone(), member.clone()));
                }
            }
        }

        if requirements.reads_member_of_owner {
            for (owner, object_binding, member) in &self.member_reads_from_binding {
                if let Some(object_owners) = owners_by_binding.get(object_binding) {
                    self.reads_member_of_owner.extend(
                        object_owners
                            .iter()
                            .map(|object_owner| (*owner, *object_owner, member.clone())),
                    );
                }
            }
        }

        if requirements.module_member_uses {
            let owner_by_ordinal = owner_by_ordinal
                .as_ref()
                .expect("owner ordinal index should be built for module member uses");
            for (statement_ordinal, module, member) in &self.raw_module_member_uses {
                let Some(owner) = owner_by_ordinal.get(statement_ordinal) else {
                    continue;
                };
                if !owners_with_declarations.contains(owner) {
                    continue;
                }
                self.module_member_uses
                    .insert((*owner, module.clone(), member.clone()));
            }
        }

        if requirements.call_arguments
            || requirements.call_arguments_from_binding
            || requirements.call_arguments_from_owner
        {
            for (argument, callee_object, callee_member, arg_index) in &self.raw_call_arguments {
                let Some(argument_owners) = owners_by_binding.get(argument) else {
                    continue;
                };
                for owner in argument_owners {
                    if requirements.call_arguments {
                        self.call_arguments
                            .insert((*owner, callee_member.clone(), *arg_index));
                    }
                    if let Some(callee_object) = callee_object {
                        if requirements.call_arguments_from_binding {
                            self.call_arguments_from_binding.insert((
                                *owner,
                                callee_object.clone(),
                                callee_member.clone(),
                                *arg_index,
                            ));
                        }
                        if requirements.call_arguments_from_owner
                            && let Some(callee_object_owners) = owners_by_binding.get(callee_object)
                        {
                            self.call_arguments_from_owner
                                .extend(callee_object_owners.iter().map(|callee_object_owner| {
                                    (
                                        *owner,
                                        *callee_object_owner,
                                        callee_member.clone(),
                                        *arg_index,
                                    )
                                }));
                        }
                    }
                }
            }
        }

        if requirements.makes_decorate_call_for_binding
            || requirements.makes_decorate_call_for_owner
        {
            for (callee, class_anchor, member) in &self.decorate_calls {
                let Some(callee_owners) = owners_by_binding.get(callee) else {
                    continue;
                };
                for owner in callee_owners {
                    if requirements.makes_decorate_call_for_binding {
                        self.makes_decorate_call_for_binding.insert((
                            *owner,
                            class_anchor.clone(),
                            member.clone(),
                        ));
                    }
                    if requirements.makes_decorate_call_for_owner
                        && let Some(class_owners) = owners_by_binding.get(class_anchor)
                    {
                        self.makes_decorate_call_for_owner.extend(
                            class_owners
                                .iter()
                                .map(|class_owner| (*owner, *class_owner, member.clone())),
                        );
                    }
                }
            }
        }

        if requirements.intrinsic_alias_referenced_by {
            let mut raw_referencers_by_binding: BTreeMap<&str, Vec<OwnerId>> = BTreeMap::new();
            for (referencer, binding, _edge_kind) in &self.raw_owner_references_binding {
                raw_referencers_by_binding
                    .entry(binding.as_str())
                    .or_default()
                    .push(*referencer);
            }
            for (binding, property) in &self.intrinsic_aliases {
                let Some(alias_owners) = owners_by_binding.get(binding) else {
                    continue;
                };
                if let Some(referencers) = raw_referencers_by_binding.get(binding.as_str()) {
                    self.intrinsic_alias_referenced_by
                        .extend(alias_owners.iter().flat_map(|alias_owner| {
                            referencers
                                .iter()
                                .map(|referencer| (*alias_owner, property.clone(), *referencer))
                        }));
                }
            }
        }
    }

    fn add_program_constants(&mut self, program: &SelectorProgram) {
        for target in &program.targets {
            match &target.claim {
                ClaimKind::Binding {
                    export_name: Some(export_name),
                }
                | ClaimKind::BindingGroupMember { export_name, .. } => self.add_string(export_name),
                ClaimKind::Binding { export_name: None } | ClaimKind::AnonymousStatement => {}
            }
        }

        for atom in &program.atoms {
            self.add_atom_constants(atom);
        }
    }

    fn add_atom_constants(&mut self, atom: &SelectorAtom) {
        match atom {
            SelectorAtom::OwnerKind {
                owner,
                statement_kind,
            } => {
                self.add_owner_term(owner);
                self.add_string_term(statement_kind);
            }
            SelectorAtom::OwnerDeclaresBinding { owner, binding } => {
                self.add_owner_term(owner);
                self.add_string_term(binding);
            }
            SelectorAtom::ProjectedAllowedTuples {
                variables: _,
                rows,
                reason: _,
            } => {
                for row in rows {
                    for value in row {
                        self.add_projected_value(value);
                    }
                }
            }
            SelectorAtom::OwnerExportName { owner, export_name } => {
                self.add_owner_term(owner);
                self.add_string_term(export_name);
            }
            SelectorAtom::OwnerReferencesBinding {
                owner,
                binding,
                edge_kind,
            } => {
                self.add_owner_term(owner);
                self.add_string_term(binding);
                if let Some(edge_kind) = edge_kind {
                    self.add_string_term(edge_kind);
                }
            }
            SelectorAtom::OwnerReferencesOwner { owner, referenced }
            | SelectorAtom::OwnerAliasesOwner {
                owner,
                aliased: referenced,
            } => {
                self.add_owner_term(owner);
                self.add_owner_term(referenced);
            }
            SelectorAtom::ReadsMember {
                owner,
                object,
                member,
            } => {
                self.add_owner_term(owner);
                if let Some(object) = object {
                    self.add_string_term(object);
                }
                self.add_string_term(member);
            }
            SelectorAtom::ReadsMemberOfOwner {
                owner,
                object,
                member,
            } => {
                self.add_owner_term(owner);
                self.add_owner_term(object);
                self.add_string_term(member);
            }
            SelectorAtom::ConsumesModuleMember {
                owner,
                module,
                member,
            } => {
                self.add_owner_term(owner);
                self.add_string_term(module);
                self.add_string_term(member);
            }
            SelectorAtom::PassedToCall {
                owner,
                callee_object,
                callee_member,
                ..
            } => {
                self.add_owner_term(owner);
                if let Some(callee_object) = callee_object {
                    self.add_string_term(callee_object);
                }
                self.add_string_term(callee_member);
            }
            SelectorAtom::PassedToCallOfOwner {
                owner,
                callee_object,
                callee_member,
                ..
            } => {
                self.add_owner_term(owner);
                self.add_owner_term(callee_object);
                self.add_string_term(callee_member);
            }
            SelectorAtom::MakesDecorateCall {
                owner,
                class_anchor,
                member,
            } => {
                self.add_owner_term(owner);
                self.add_string_term(class_anchor);
                if let Some(member) = member {
                    self.add_string_term(member);
                }
            }
            SelectorAtom::MakesDecorateCallForOwner {
                owner,
                class_anchor,
                member,
            } => {
                self.add_owner_term(owner);
                self.add_owner_term(class_anchor);
                if let Some(member) = member {
                    self.add_string_term(member);
                }
            }
            SelectorAtom::IntrinsicAlias {
                owner,
                property,
                referenced_by,
            } => {
                self.add_owner_term(owner);
                self.add_string_term(property);
                self.add_owner_term(referenced_by);
            }
        }
    }

    fn add_owner_term(&mut self, term: &OwnerTerm) {
        if let OwnerTerm::Const { owner } = term {
            self.add_owner(*owner);
        }
    }

    fn add_string_term(&mut self, term: &StringTerm) {
        if let StringTerm::Const { value } = term {
            self.add_string(value);
        }
    }

    fn add_projected_value(&mut self, value: &SelectorProjectedValue) {
        match value {
            SelectorProjectedValue::Owner(value) => self.add_owner(*value),
            SelectorProjectedValue::String(value) => self.add_string(value),
        }
    }

    fn add_owner(&mut self, owner: OwnerId) {
        self.owners.insert(owner);
    }

    fn add_string(&mut self, value: &str) {
        self.strings.insert(value.to_string());
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use analysis::{ChunkId, OwnerId, StatementOrdinal};
    use selector_constraint_backend::{
        AllowedTupleConstraintId, BackendValueId,
        CompiledAllDifferentConstraint as AllDifferentConstraint, ConstraintValue,
    };
    use selector_ir::ClaimOrigin;

    #[derive(Debug, Clone, PartialEq, Eq)]
    struct AllowedTupleConstraint {
        id: AllowedTupleConstraintId,
        variables: Vec<ConstraintVariableId>,
        tuples: Vec<Vec<ConstraintValue>>,
    }

    impl PartialEq<&AllowedTupleConstraint> for AllowedTupleConstraint {
        fn eq(&self, other: &&AllowedTupleConstraint) -> bool {
            self == *other
        }
    }

    fn owner(value: usize) -> ConstraintValue {
        ConstraintValue::Owner(OwnerId(value))
    }

    fn string(value: &str) -> ConstraintValue {
        ConstraintValue::String(value.to_string())
    }

    fn owner_fact(owner: usize, ordinal: usize, statement_kind: &str) -> SelectorFact {
        SelectorFact::Owner {
            chunk_id: ChunkId(0),
            owner: OwnerId(owner),
            statement_ordinal: StatementOrdinal(ordinal),
            statement_kind: statement_kind.to_string(),
        }
    }

    fn declared_binding(owner: usize, binding: &str) -> SelectorFact {
        SelectorFact::DeclaredBinding {
            chunk_id: ChunkId(0),
            owner: OwnerId(owner),
            binding: binding.to_string(),
            export_name: None,
        }
    }

    fn owner_reference(owner: usize, binding: &str, edge_kind: &str) -> SelectorFact {
        SelectorFact::OwnerReferencesBinding {
            chunk_id: ChunkId(0),
            owner: OwnerId(owner),
            binding: binding.to_string(),
            edge_kind: edge_kind.to_string(),
        }
    }

    fn member_read(ordinal: usize, object: Option<&str>, member: &str) -> SelectorFact {
        SelectorFact::MemberRead {
            chunk_id: ChunkId(0),
            statement_ordinal: StatementOrdinal(ordinal),
            object: object.map(str::to_string),
            member: member.to_string(),
        }
    }

    fn module_member_use(ordinal: usize, module: &str, member: &str) -> SelectorFact {
        SelectorFact::ModuleMemberUse {
            chunk_id: ChunkId(0),
            statement_ordinal: StatementOrdinal(ordinal),
            module: module.to_string(),
            member: member.to_string(),
        }
    }

    fn call_argument_use(
        argument: &str,
        callee_object: Option<&str>,
        callee_member: &str,
        arg_index: usize,
    ) -> SelectorFact {
        SelectorFact::CallArgumentUse {
            chunk_id: ChunkId(0),
            argument: argument.to_string(),
            callee_object: callee_object.map(str::to_string),
            callee_member: callee_member.to_string(),
            arg_index,
        }
    }

    fn decorate_call(callee: &str, class_anchor: &str, member: Option<&str>) -> SelectorFact {
        SelectorFact::DecorateCallUse {
            chunk_id: ChunkId(0),
            callee: callee.to_string(),
            class_anchor: class_anchor.to_string(),
            member: member.map(str::to_string),
        }
    }

    fn intrinsic_alias(binding: &str, property: &str) -> SelectorFact {
        SelectorFact::IntrinsicAliasUse {
            chunk_id: ChunkId(0),
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
        let owner_var = program.add_variable(VariableDomain::Owner, Some("owner".to_string()));
        program.add_atom(SelectorAtom::OwnerReferencesOwner {
            owner: OwnerTerm::Var { id: owner_var },
            referenced: OwnerTerm::Var { id: owner_var },
        });

        let facts = fact_store(vec![
            owner_fact(10, 0, "var"),
            owner_fact(20, 1, "var"),
            owner_fact(30, 2, "var"),
            declared_binding(10, "a"),
            declared_binding(20, "b"),
            declared_binding(30, "c"),
            owner_reference(10, "a", "eager_use"),
            owner_reference(10, "b", "eager_use"),
            owner_reference(20, "b", "eager_use"),
            owner_reference(30, "a", "eager_use"),
        ]);

        let model = compile_selector_problem(&program, &facts).unwrap();

        assert_eq!(
            decoded_variable_domain(&model, ConstraintVariableId(0)),
            vec![owner(10), owner(20)]
        );
    }

    #[test]
    fn single_row_projected_allowed_tuples_restricts_domains_without_table() {
        let mut program = SelectorProgram::default();
        let owner_var = program.add_variable(VariableDomain::Owner, Some("owner".to_string()));
        let binding_var = program.add_variable(VariableDomain::String, Some("binding".to_string()));
        program.add_atom(SelectorAtom::ProjectedAllowedTuples {
            variables: vec![owner_var, binding_var],
            rows: vec![vec![
                SelectorProjectedValue::Owner(OwnerId(7)),
                SelectorProjectedValue::String("actual".to_string()),
            ]],
            reason: "projected singleton test".to_string(),
        });

        let model = compile_selector_problem(&program, &fact_store(vec![])).unwrap();

        assert_eq!(model.allowed_tuples, vec![]);
        assert_eq!(
            decoded_variable_domain(&model, ConstraintVariableId(0)),
            vec![owner(7)]
        );
        assert_eq!(
            decoded_variable_domain(&model, ConstraintVariableId(1)),
            vec![string("actual")]
        );
    }

    #[test]
    fn multi_row_projected_allowed_tuples_preserves_correlation() {
        let mut program = SelectorProgram::default();
        let owner_var = program.add_variable(VariableDomain::Owner, Some("owner".to_string()));
        let binding_var = program.add_variable(VariableDomain::String, Some("binding".to_string()));
        program.add_atom(SelectorAtom::ProjectedAllowedTuples {
            variables: vec![owner_var, binding_var],
            rows: vec![
                vec![
                    SelectorProjectedValue::Owner(OwnerId(7)),
                    SelectorProjectedValue::String("actual".to_string()),
                ],
                vec![
                    SelectorProjectedValue::Owner(OwnerId(8)),
                    SelectorProjectedValue::String("other".to_string()),
                ],
            ],
            reason: "projected correlation test".to_string(),
        });

        let model = compile_selector_problem(&program, &fact_store(vec![])).unwrap();

        assert_eq!(
            decoded_variable_domain(&model, ConstraintVariableId(0)),
            vec![owner(7), owner(8)]
        );
        assert_eq!(
            decoded_variable_domain(&model, ConstraintVariableId(1)),
            vec![string("actual"), string("other")]
        );
        assert_eq!(
            satisfying_tuples_for(&model, &[ConstraintVariableId(0), ConstraintVariableId(1)]),
            vec![
                vec![owner(7), string("actual")],
                vec![owner(8), string("other")]
            ]
        );
    }

    #[test]
    fn target_injectivity_prunes_fixed_values_from_broad_targets() {
        let mut program = SelectorProgram::default();
        let broad_owner = program.add_variable(VariableDomain::Owner, Some("broad".to_string()));
        let strict_owner = program.add_variable(VariableDomain::Owner, Some("strict".to_string()));
        let broad_target = program.add_target(
            ChunkId(0),
            broad_owner,
            "module",
            ClaimKind::Binding {
                export_name: Some("Broad".to_string()),
            },
            ClaimOrigin::Synthetic,
        );
        let strict_target = program.add_target(
            ChunkId(0),
            strict_owner,
            "module",
            ClaimKind::Binding {
                export_name: Some("Strict".to_string()),
            },
            ClaimOrigin::Synthetic,
        );
        program.add_atom(SelectorAtom::OwnerDeclaresBinding {
            owner: OwnerTerm::Var { id: broad_owner },
            binding: StringTerm::Const {
                value: "shared".to_string(),
            },
        });
        program.add_atom(SelectorAtom::OwnerDeclaresBinding {
            owner: OwnerTerm::Var { id: strict_owner },
            binding: StringTerm::Const {
                value: "specific".to_string(),
            },
        });
        program.require_all_different(vec![broad_target, strict_target]);

        let facts = fact_store(vec![
            owner_fact(10, 0, "var"),
            owner_fact(20, 1, "var"),
            declared_binding(10, "shared"),
            declared_binding(20, "shared"),
            declared_binding(20, "specific"),
        ]);

        let model = compile_selector_problem(&program, &facts).unwrap();

        assert_eq!(model.target_projections.len(), 2);
        assert_eq!(model.target_projections[0].target, broad_target);
        assert_eq!(
            model.target_projections[0].owner_variable,
            ConstraintVariableId(0)
        );
        assert_eq!(
            model.target_projections[0].binding_projection,
            Some(TargetBindingProjection::Const("shared".to_string()))
        );
        assert_eq!(model.target_projections[1].target, strict_target);
        assert_eq!(
            model.target_projections[1].owner_variable,
            ConstraintVariableId(1)
        );
        assert_eq!(
            model.target_projections[1].binding_projection,
            Some(TargetBindingProjection::Const("specific".to_string()))
        );
        assert_eq!(model.all_different, Vec::<AllDifferentConstraint>::new());

        assert_eq!(
            decoded_variable_domain(&model, ConstraintVariableId(0)),
            vec![owner(10)]
        );
        assert_eq!(
            decoded_variable_domain(&model, ConstraintVariableId(1)),
            vec![owner(20)]
        );
    }

    #[test]
    fn lowers_selector_semantic_variable_all_different() {
        let mut program = SelectorProgram::default();
        let owner = program.add_variable(VariableDomain::Owner, Some("owner".to_string()));
        let left = program.add_variable(VariableDomain::String, Some("alpha.left".to_string()));
        let right = program.add_variable(VariableDomain::String, Some("alpha.right".to_string()));
        program.add_target(
            ChunkId(0),
            owner,
            "module",
            ClaimKind::Binding {
                export_name: Some("Widget".to_string()),
            },
            ClaimOrigin::Synthetic,
        );
        program.add_atom(SelectorAtom::OwnerDeclaresBinding {
            owner: OwnerTerm::Var { id: owner },
            binding: StringTerm::Const {
                value: "widget".to_string(),
            },
        });
        program.add_atom(SelectorAtom::ProjectedAllowedTuples {
            variables: vec![left],
            rows: vec![vec![SelectorProjectedValue::String("a".to_string())]],
            reason: "left".to_string(),
        });
        program.add_atom(SelectorAtom::ProjectedAllowedTuples {
            variables: vec![right],
            rows: vec![vec![SelectorProjectedValue::String("b".to_string())]],
            reason: "right".to_string(),
        });
        program.require_variables_all_different(
            vec![left, right],
            "module::source_match.alpha_all.frame",
        );

        let facts = fact_store(vec![
            owner_fact(10, 0, "var"),
            declared_binding(10, "widget"),
        ]);

        let model = compile_selector_problem(&program, &facts).unwrap();

        assert_eq!(model.all_different, Vec::<AllDifferentConstraint>::new());
        assert_eq!(
            decoded_variable_domain(&model, ConstraintVariableId(1)),
            vec![string("a")]
        );
        assert_eq!(
            decoded_variable_domain(&model, ConstraintVariableId(2)),
            vec![string("b")]
        );
    }

    #[test]
    fn target_binding_projection_preserves_binding_variable() {
        let mut program = SelectorProgram::default();
        let owner_var = program.add_variable(VariableDomain::Owner, Some("owner".to_string()));
        let binding_var = program.add_variable(VariableDomain::String, Some("binding".to_string()));
        let target = program.add_target(
            ChunkId(0),
            owner_var,
            "module",
            ClaimKind::Binding { export_name: None },
            ClaimOrigin::Synthetic,
        );
        program.add_atom(SelectorAtom::OwnerDeclaresBinding {
            owner: OwnerTerm::Var { id: owner_var },
            binding: StringTerm::Var { id: binding_var },
        });

        let facts = fact_store(vec![
            owner_fact(7, 0, "var"),
            owner_fact(8, 1, "var"),
            declared_binding(7, "actual"),
            declared_binding(8, "other"),
        ]);

        let model = compile_selector_problem(&program, &facts).unwrap();

        assert_eq!(model.target_projections.len(), 1);
        assert_eq!(model.target_projections[0].target, target);
        assert_eq!(
            model.target_projections[0].owner_variable,
            ConstraintVariableId(0)
        );
        assert_eq!(
            model.target_projections[0].binding_projection,
            Some(TargetBindingProjection::Variable(ConstraintVariableId(1)))
        );
        assert_eq!(
            allowed_tuples_for(&model, &[ConstraintVariableId(0), ConstraintVariableId(1)]),
            &AllowedTupleConstraint {
                id: AllowedTupleConstraintId(0),
                variables: vec![ConstraintVariableId(0), ConstraintVariableId(1)],
                tuples: vec![
                    vec![owner(7), string("actual")],
                    vec![owner(8), string("other")],
                ],
            }
        );
    }

    #[test]
    fn relational_atoms_lower_to_allowed_tuple_constraints() {
        let mut program = SelectorProgram::default();
        let reference_owner =
            program.add_variable(VariableDomain::Owner, Some("reference_owner".to_string()));
        let referenced_owner =
            program.add_variable(VariableDomain::Owner, Some("referenced_owner".to_string()));
        let aliased_owner =
            program.add_variable(VariableDomain::Owner, Some("aliased_owner".to_string()));
        let reader_owner =
            program.add_variable(VariableDomain::Owner, Some("reader_owner".to_string()));
        let object_owner =
            program.add_variable(VariableDomain::Owner, Some("object_owner".to_string()));
        let decorator_owner =
            program.add_variable(VariableDomain::Owner, Some("decorator_owner".to_string()));
        let class_owner =
            program.add_variable(VariableDomain::Owner, Some("class_owner".to_string()));
        let intrinsic_referencer = program.add_variable(
            VariableDomain::Owner,
            Some("intrinsic_referencer".to_string()),
        );
        let referenced_binding = program.add_variable(
            VariableDomain::String,
            Some("referenced_binding".to_string()),
        );
        let read_member =
            program.add_variable(VariableDomain::String, Some("read_member".to_string()));
        let object_read_member = program.add_variable(
            VariableDomain::String,
            Some("object_read_member".to_string()),
        );
        let module_consumer =
            program.add_variable(VariableDomain::Owner, Some("module_consumer".to_string()));

        program.add_atom(SelectorAtom::OwnerReferencesBinding {
            owner: OwnerTerm::Var {
                id: reference_owner,
            },
            binding: StringTerm::Var {
                id: referenced_binding,
            },
            edge_kind: Some(StringTerm::Const {
                value: "read".to_string(),
            }),
        });
        program.add_atom(SelectorAtom::OwnerReferencesOwner {
            owner: OwnerTerm::Const { owner: OwnerId(20) },
            referenced: OwnerTerm::Var {
                id: referenced_owner,
            },
        });
        program.add_atom(SelectorAtom::OwnerAliasesOwner {
            owner: OwnerTerm::Const { owner: OwnerId(30) },
            aliased: OwnerTerm::Var { id: aliased_owner },
        });
        program.add_atom(SelectorAtom::ReadsMember {
            owner: OwnerTerm::Var { id: reader_owner },
            object: None,
            member: StringTerm::Var { id: read_member },
        });
        program.add_atom(SelectorAtom::ReadsMember {
            owner: OwnerTerm::Const { owner: OwnerId(40) },
            object: Some(StringTerm::Const {
                value: "objectBinding".to_string(),
            }),
            member: StringTerm::Var {
                id: object_read_member,
            },
        });
        program.add_atom(SelectorAtom::ReadsMemberOfOwner {
            owner: OwnerTerm::Const { owner: OwnerId(40) },
            object: OwnerTerm::Var { id: object_owner },
            member: StringTerm::Const {
                value: "value".to_string(),
            },
        });
        program.add_atom(SelectorAtom::ConsumesModuleMember {
            owner: OwnerTerm::Var {
                id: module_consumer,
            },
            module: StringTerm::Const {
                value: "./accessors".to_string(),
            },
            member: StringTerm::Const {
                value: "Widget".to_string(),
            },
        });
        program.add_atom(SelectorAtom::MakesDecorateCall {
            owner: OwnerTerm::Var {
                id: decorator_owner,
            },
            class_anchor: StringTerm::Const {
                value: "Class".to_string(),
            },
            member: Some(StringTerm::Const {
                value: "field".to_string(),
            }),
        });
        program.add_atom(SelectorAtom::MakesDecorateCallForOwner {
            owner: OwnerTerm::Const { owner: OwnerId(60) },
            class_anchor: OwnerTerm::Var { id: class_owner },
            member: Some(StringTerm::Const {
                value: "field".to_string(),
            }),
        });
        program.add_atom(SelectorAtom::IntrinsicAlias {
            owner: OwnerTerm::Const { owner: OwnerId(80) },
            property: StringTerm::Const {
                value: "defineProperty".to_string(),
            },
            referenced_by: OwnerTerm::Var {
                id: intrinsic_referencer,
            },
        });

        let facts = fact_store(vec![
            owner_fact(10, 0, "function"),
            declared_binding(10, "target"),
            owner_fact(20, 1, "function"),
            declared_binding(20, "referrer"),
            owner_reference(20, "target", "read"),
            owner_fact(30, 2, "var_decl"),
            declared_binding(30, "aliasStatement"),
            owner_reference(30, "target", "eager_use"),
            owner_fact(40, 3, "function"),
            declared_binding(40, "reader"),
            member_read(3, None, "size"),
            member_read(3, Some("objectBinding"), "value"),
            owner_fact(50, 4, "var_decl"),
            declared_binding(50, "objectBinding"),
            owner_fact(60, 5, "function"),
            declared_binding(60, "decorate"),
            owner_fact(65, 55, "function"),
            declared_binding(65, "moduleConsumer"),
            module_member_use(55, "./accessors", "Widget"),
            owner_fact(66, 56, "function"),
            declared_binding(66, "otherModuleConsumer"),
            module_member_use(56, "./accessors", "Other"),
            owner_fact(70, 6, "class"),
            declared_binding(70, "Class"),
            decorate_call("decorate", "Class", Some("field")),
            owner_fact(80, 7, "var_decl"),
            declared_binding(80, "define"),
            intrinsic_alias("define", "defineProperty"),
            owner_fact(90, 8, "function"),
            declared_binding(90, "aliasUser"),
            owner_reference(90, "define", "read"),
        ]);

        let model = compile_selector_problem(&program, &facts).unwrap();

        assert_eq!(
            satisfying_tuples_for(&model, &[ConstraintVariableId(0), ConstraintVariableId(8)]),
            vec![
                vec![owner(20), string("target")],
                vec![owner(90), string("define")],
            ]
        );
        assert_eq!(
            decoded_variable_domain(&model, ConstraintVariableId(1)),
            vec![owner(10)]
        );
        assert_eq!(
            decoded_variable_domain(&model, ConstraintVariableId(2)),
            vec![owner(10)]
        );
        assert_eq!(
            satisfying_tuples_for(&model, &[ConstraintVariableId(3), ConstraintVariableId(9)]),
            vec![
                vec![owner(40), string("size")],
                vec![owner(40), string("value")],
            ]
        );
        assert_eq!(
            decoded_variable_domain(&model, ConstraintVariableId(10)),
            vec![string("value")]
        );
        assert_eq!(
            decoded_variable_domain(&model, ConstraintVariableId(4)),
            vec![owner(50)]
        );
        assert_eq!(
            decoded_variable_domain(&model, ConstraintVariableId(5)),
            vec![owner(60)]
        );
        assert_eq!(
            decoded_variable_domain(&model, ConstraintVariableId(6)),
            vec![owner(70)]
        );
        assert_eq!(
            decoded_variable_domain(&model, ConstraintVariableId(7)),
            vec![owner(90)]
        );
        assert_eq!(
            decoded_variable_domain(&model, ConstraintVariableId(11)),
            vec![owner(65)]
        );
    }

    #[test]
    fn passed_to_call_atoms_lower_to_allowed_tuple_constraints() {
        let mut program = SelectorProgram::default();
        let bare_argument_owner =
            program.add_variable(VariableDomain::Owner, Some("bare_argument".to_string()));
        let object_argument_owner =
            program.add_variable(VariableDomain::Owner, Some("object_argument".to_string()));
        let object_owner =
            program.add_variable(VariableDomain::Owner, Some("object_owner".to_string()));
        let owner_constrained_argument = program.add_variable(
            VariableDomain::Owner,
            Some("owner_constrained_argument".to_string()),
        );
        let owner_constrained_object = program.add_variable(
            VariableDomain::Owner,
            Some("owner_constrained_object".to_string()),
        );

        program.add_atom(SelectorAtom::PassedToCall {
            owner: OwnerTerm::Var {
                id: bare_argument_owner,
            },
            callee_object: None,
            callee_member: StringTerm::Const {
                value: "register".to_string(),
            },
            arg_index: Some(0),
        });
        program.add_atom(SelectorAtom::PassedToCall {
            owner: OwnerTerm::Var {
                id: object_argument_owner,
            },
            callee_object: Some(StringTerm::Const {
                value: "registry".to_string(),
            }),
            callee_member: StringTerm::Const {
                value: "register".to_string(),
            },
            arg_index: None,
        });
        program.add_atom(SelectorAtom::OwnerDeclaresBinding {
            owner: OwnerTerm::Var { id: object_owner },
            binding: StringTerm::Const {
                value: "registry".to_string(),
            },
        });
        program.add_atom(SelectorAtom::PassedToCallOfOwner {
            owner: OwnerTerm::Var {
                id: owner_constrained_argument,
            },
            callee_object: OwnerTerm::Var {
                id: owner_constrained_object,
            },
            callee_member: StringTerm::Const {
                value: "register".to_string(),
            },
            arg_index: Some(1),
        });

        let facts = fact_store(vec![
            owner_fact(10, 0, "class"),
            declared_binding(10, "WidgetA"),
            call_argument_use("WidgetA", None, "register", 0),
            owner_fact(20, 1, "class"),
            declared_binding(20, "WidgetB"),
            call_argument_use("WidgetB", Some("registry"), "register", 1),
            owner_fact(30, 2, "var_decl"),
            declared_binding(30, "registry"),
            owner_fact(40, 3, "class"),
            declared_binding(40, "Other"),
            call_argument_use("Other", Some("otherRegistry"), "register", 1),
            owner_fact(50, 4, "var_decl"),
            declared_binding(50, "otherRegistry"),
        ]);

        let model = compile_selector_problem(&program, &facts).unwrap();

        assert_eq!(
            decoded_variable_domain(&model, ConstraintVariableId(0)),
            vec![owner(10)]
        );
        assert_eq!(
            decoded_variable_domain(&model, ConstraintVariableId(1)),
            vec![owner(20)]
        );
        assert_eq!(
            decoded_variable_domain(&model, ConstraintVariableId(2)),
            vec![owner(30)]
        );
        assert_eq!(
            allowed_tuples_for(&model, &[ConstraintVariableId(3), ConstraintVariableId(4)]).tuples,
            vec![vec![owner(20), owner(30)], vec![owner(40), owner(50)],]
        );
    }

    #[test]
    fn unsupported_atoms_fail_closed() {
        let mut program = SelectorProgram::default();
        let owner_var = program.add_variable(VariableDomain::Owner, Some("owner".to_string()));
        let member_var = program.add_variable(VariableDomain::String, Some("member".to_string()));
        program.add_atom(SelectorAtom::PassedToCall {
            owner: OwnerTerm::Var { id: owner_var },
            callee_object: None,
            callee_member: StringTerm::Var { id: member_var },
            arg_index: None,
        });

        let facts = fact_store(vec![owner_fact(10, 0, "var")]);

        let err = compile_selector_problem(&program, &facts).unwrap_err();
        assert!(matches!(
            err,
            CompiledSelectorProblemBuildError::UnsupportedAtom { .. }
        ));
    }

    #[test]
    fn unsatisfied_constant_only_atoms_fail_closed() {
        let mut program = SelectorProgram::default();
        program.add_atom(SelectorAtom::OwnerDeclaresBinding {
            owner: OwnerTerm::Const { owner: OwnerId(10) },
            binding: StringTerm::Const {
                value: "missing".to_string(),
            },
        });

        let facts = fact_store(vec![owner_fact(10, 0, "var")]);

        let err = compile_selector_problem(&program, &facts).unwrap_err();
        assert!(matches!(
            err,
            CompiledSelectorProblemBuildError::ConstantOnlyAtomUnsatisfied { .. }
        ));
    }
}
